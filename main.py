import glob
import hashlib
import hmac
import os
import re
import secrets
import shutil
import sqlite3
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from urllib.parse import urlparse

import yt_dlp
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.background import BackgroundTask

app = FastAPI(title="Toca da Capivara")
BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, "toca.db")
TMP = tempfile.mkdtemp(prefix="toca_")
VIDEOS = os.path.join(BASE, "videos")  # cópias guardadas para ver no museu
os.makedirs(VIDEOS, exist_ok=True)
JOBS: dict = {}

if not shutil.which("ffmpeg"):
    print("AVISO: ffmpeg nao encontrado. O YouTube precisa dele para a maioria dos videos (winget install ffmpeg).")
if not shutil.which("deno"):
    print("AVISO: deno nao encontrado. O yt-dlp pode precisar dele para o YouTube (winget install DenoLand.Deno).")


class Interrupted(getattr(getattr(yt_dlp, "utils", None), "DownloadCancelled", Exception)):
    """Lançada de dentro do hook para parar a descarga (pausa ou cancelamento)."""


@app.exception_handler(Exception)
async def on_error(request: Request, exc: Exception):
    """Em vez de 'Internal Server Error' mudo, mostra o erro real (e escreve-o na janela do servidor)."""
    import traceback
    traceback.print_exception(type(exc), exc, exc.__traceback__)
    return JSONResponse({"detail": f"Erro no servidor em {request.url.path}: {type(exc).__name__}: {str(exc)[:200]}"}, status_code=500)


BROWSERS = {"firefox", "edge", "chrome", "brave", "chromium", "opera", "vivaldi"}


def check_browser(browser: str) -> str:
    browser = (browser or "").lower()
    if browser and browser not in BROWSERS:
        raise HTTPException(400, "Navegador não suportado.")
    return browser


IMPERSONATE_SITES = ("tiktok.com", "instagram.com")


def impersonate_opt(url: str):
    """TikTok e Instagram só respondem bem a um 'navegador a sério'. Precisa do pacote curl-cffi."""
    try:
        import curl_cffi  # noqa: F401
        from yt_dlp.networking.impersonate import ImpersonateTarget
        host = urlparse(url or "").hostname or ""
        if any(host == d or host.endswith("." + d) for d in IMPERSONATE_SITES):
            return ImpersonateTarget.from_str("chrome")
    except Exception:
        pass
    return None


def explain(e: Exception) -> str:
    """Mensagem de erro curta e útil: tira o ruído do yt-dlp e acrescenta uma dica."""
    msg = re.sub(r"\x1b\[[0-9;]*m", "", str(e)).strip()
    msg = re.sub(r"^ERROR:\s*", "", msg)
    msg = re.split(r";\s*please report this issue", msg)[0][:220]
    low = msg.lower()
    if "unexpected response from webpage" in low or "unable to extract" in low or "failed to parse" in low:
        msg += (" | Dica: o site mudou ou está a bloquear. Fecha e abre o iniciar.bat (atualiza o yt-dlp). "
                "Se continuar, escolhe os cookies do Firefox em 'Configurações'.")
    elif "403" in msg or "sign in to confirm" in low:
        msg += (" | Dica: atualiza o yt-dlp (o iniciar.bat faz isso) ou abre 'Configurações' "
                "e escolhe os cookies do Firefox, com sessão iniciada no YouTube.")
    elif "login" in low or "rate-limit" in low or "empty media response" in low or "private" in low:
        msg += (" | Dica: este site pede sessão iniciada. Em 'Configurações' escolhe o navegador "
                "onde tens a conta aberta (o Firefox funciona melhor).")
    elif "cookie" in low:
        msg += " | Dica: fecha o navegador escolhido ou usa o Firefox."
    return msg


def base_opts(browser: str = "", url: str = "") -> dict:
    """Cookies (ajudam contra o erro 403 do YouTube): cookies.txt ao lado do main.py,
    senão o navegador escolhido na página, senão a variável ABISMO_BROWSER."""
    o = {"quiet": True, "no_color": True, "noplaylist": True, "playlistend": 1}
    cookies = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies.txt")
    if os.path.exists(cookies):
        o["cookiefile"] = cookies
    elif browser:
        o["cookiesfrombrowser"] = (browser,)
    elif os.environ.get("ABISMO_BROWSER"):
        o["cookiesfrombrowser"] = (os.environ["ABISMO_BROWSER"],)
    imp = impersonate_opt(url)
    if imp is not None:
        o["impersonate"] = imp
    return o


def check_url(url: str) -> str:
    """Aceita texto com um link lá dentro (ex.: 'Vê isto: https://...') e devolve só o link."""
    m = re.search(r"https?://[^\s<>\"']+", url or "")
    if not m:
        raise HTTPException(400, "Cola um link que comece por http:// ou https://")
    return m.group(0).rstrip(".,;:!?)]}")  # tira pontuação do fim, ex.: "(link: https://...)."


def pick(data: dict):
    """Carrosséis do Instagram e playlists: usa só o primeiro item."""
    if data.get("_type") == "playlist" or data.get("entries") is not None:
        entries = [e for e in (data.get("entries") or []) if e]
        if not entries:
            raise RuntimeError("Não encontrei nenhum vídeo neste link.")
        return entries[0], True
    return data, False


def get_job(job_id: str) -> dict:
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Descarga não encontrada.")
    return job


def discard(job_id: str, job: dict):
    """Apaga tudo o que foi descarregado (incluindo partes) e marca como cancelado."""
    for f in glob.glob(os.path.join(TMP, f"{job_id}.*")):
        try:
            os.remove(f)
        except OSError:
            pass
    job["path"] = None
    job["speed"] = job["eta"] = None
    job["state"] = "cancelled"



# ---------------------------------------------------------------- contas e histórico
SESSION_COOKIE = "toca_session"
SESSION_DAYS = 30
USER_RE = re.compile(r"^[\w.\-@+]{3,64}$")
FAILS: dict = {}  # tentativas de login falhadas: nome -> [instantes]


@contextmanager
def db():
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    try:
        # bases antigas: garante a coluna "file" do museu (corre em cada ligação, é instantâneo)
        cols = [r[1] for r in con.execute("PRAGMA table_info(history)")]
        if cols and "file" not in cols:
            try:
                con.execute("ALTER TABLE history ADD COLUMN file TEXT")
                con.commit()
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e).lower():
                    raise
        yield con
        con.commit()
    finally:
        con.close()


SCHEMA_PATH = os.path.join(BASE, "schema.sql")


def init_db():
    """Cria o toca.db e as tabelas descritas em schema.sql (não mexe em dados existentes)."""
    with open(SCHEMA_PATH, encoding="utf-8") as f:
        schema = f.read()
    with db() as con:
        con.executescript(schema)
        cols = [r["name"] for r in con.execute("PRAGMA table_info(history)")]
        if "file" not in cols:  # bases antigas: acrescenta a coluna do ficheiro guardado
            con.execute("ALTER TABLE history ADD COLUMN file TEXT")


init_db()
print("Base de dados em uso:", DB_PATH)


def hash_pw(pw: str, salt: bytes = None) -> str:
    salt = salt or secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return f"{salt.hex()}${h.hex()}"


def check_pw(pw: str, stored: str) -> bool:
    try:
        salt, h = stored.split("$")
        calc = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=2 ** 14, r=8, p=1, dklen=32)
        return hmac.compare_digest(calc.hex(), h)
    except Exception:
        return False


DUMMY_HASH = hash_pw("dummy-password")  # gasta o mesmo tempo quando o utilizador não existe


def _sha(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def start_session(response: Response, user_id: int):
    token = secrets.token_urlsafe(32)
    now = time.time()
    with db() as con:
        con.execute("DELETE FROM sessions WHERE created < ?", (now - SESSION_DAYS * 86400,))
        con.execute("INSERT INTO sessions(token, user_id, created) VALUES(?,?,?)", (_sha(token), user_id, now))
    response.set_cookie(SESSION_COOKIE, token, max_age=SESSION_DAYS * 86400,
                        httponly=True, samesite="lax", path="/")


def user_from(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    with db() as con:
        row = con.execute(
            "SELECT u.id, u.username FROM sessions s JOIN users u ON u.id = s.user_id "
            "WHERE s.token = ? AND s.created > ?",
            (_sha(token), time.time() - SESSION_DAYS * 86400)).fetchone()
    return {"id": row["id"], "username": row["username"]} if row else None


def require_user(request: Request) -> dict:
    u = user_from(request)
    if not u:
        raise HTTPException(401, "Entra na tua conta para usar o museu.")
    return u


class Credentials(BaseModel):
    username: str
    password: str


@app.post("/api/register")
def register(c: Credentials, response: Response):
    name = c.username.strip()
    if not USER_RE.match(name):
        raise HTTPException(400, "O nome de utilizador (ou email) tem de ter 3 a 64 caracteres: letras, números, @, ponto, hífen ou _.")
    if not 6 <= len(c.password) <= 200:
        raise HTTPException(400, "A palavra-passe tem de ter pelo menos 6 caracteres.")
    try:
        with db() as con:
            cur = con.execute("INSERT INTO users(username, key, pw, created) VALUES(?,?,?,?)",
                              (name, name.lower(), hash_pw(c.password), time.time()))
            uid = cur.lastrowid
    except sqlite3.IntegrityError:
        raise HTTPException(409, "Esse nome de utilizador já existe.")
    start_session(response, uid)
    return {"user": {"id": uid, "username": name}}


@app.post("/api/login")
def login(c: Credentials, response: Response):
    key = c.username.strip().lower()
    now = time.time()
    recent = [t for t in FAILS.get(key, []) if now - t < 300]
    FAILS[key] = recent
    if len(recent) >= 8:
        raise HTTPException(429, "Demasiadas tentativas. Espera uns minutos e tenta outra vez.")
    with db() as con:
        row = con.execute("SELECT id, username, pw FROM users WHERE key = ?", (key,)).fetchone()
    ok = check_pw(c.password, row["pw"] if row else DUMMY_HASH)
    if not row or not ok:
        FAILS[key].append(now)
        raise HTTPException(401, "Nome de utilizador ou palavra-passe incorretos.")
    FAILS.pop(key, None)
    start_session(response, row["id"])
    return {"user": {"id": row["id"], "username": row["username"]}}


@app.post("/api/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        with db() as con:
            con.execute("DELETE FROM sessions WHERE token = ?", (_sha(token),))
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@app.get("/api/me")
def me(request: Request):
    return {"user": user_from(request)}


@app.get("/api/history")
def history(request: Request):
    u = require_user(request)
    with db() as con:
        rows = con.execute(
            "SELECT id, title, url, thumbnail, site, uploader, duration, kind, height, ext, created, file "
            "FROM history WHERE user_id = ? ORDER BY created DESC, id DESC LIMIT 500", (u["id"],)).fetchall()
    items = []
    for r in rows:
        d = dict(r)
        f = d.pop("file", None)
        d["playable"] = bool(f) and os.path.exists(os.path.join(VIDEOS, f))
        items.append(d)
    return {"items": items}


def make_compatible(file_name: str, item_hint: str):
    """Em segundo plano: se o vídeo guardado não for H.264/AAC (o que todos os navegadores leem),
    converte-o para MP4 compatível e atualiza a base de dados. Precisa de ffmpeg/ffprobe."""
    import json
    import subprocess
    try:
        ffprobe, ffmpeg = shutil.which("ffprobe"), shutil.which("ffmpeg")
        if not ffprobe or not ffmpeg:
            return
        src = os.path.join(VIDEOS, file_name)
        out = subprocess.run([ffprobe, "-v", "error", "-show_entries", "stream=codec_type,codec_name",
                              "-of", "json", src], capture_output=True, text=True, timeout=60)
        streams = json.loads(out.stdout or "{}").get("streams", [])
        v = next((x["codec_name"] for x in streams if x.get("codec_type") == "video"), None)
        a = next((x["codec_name"] for x in streams if x.get("codec_type") == "audio"), None)
        if v is None:
            return  # só áudio: os navegadores leem mp3/m4a/opus
        if v == "h264" and (a in (None, "aac", "mp3")) and src.lower().endswith(".mp4"):
            return  # já é compatível
        dst_name = uuid.uuid4().hex + ".mp4"
        dst = os.path.join(VIDEOS, dst_name)
        cmd = [ffmpeg, "-y", "-v", "error", "-i", src,
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
               "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", dst]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0 or not os.path.exists(dst):
            print("AVISO: não consegui converter o vídeo para ver no museu:", r.stderr[-300:])
            try:
                os.remove(dst)
            except OSError:
                pass
            return
        with db() as con:
            con.execute("UPDATE history SET file = ?, ext = 'mp4' WHERE file = ?", (dst_name, file_name))
        remove_files([file_name])
    except Exception as e:
        print("AVISO: conversão para o museu falhou:", e)


def remove_files(names):
    for n in names:
        if n:
            try:
                os.remove(os.path.join(VIDEOS, os.path.basename(n)))
            except OSError:
                pass


MIME = {"mp4": "video/mp4", "webm": "video/webm", "mkv": "video/x-matroska", "mov": "video/quicktime",
        "mp3": "audio/mpeg", "m4a": "audio/mp4", "opus": "audio/ogg", "ogg": "audio/ogg", "wav": "audio/wav"}


@app.get("/api/watch/{item_id}")
def watch(item_id: int, request: Request):
    """Envia o vídeo guardado, com suporte a Range (permite avançar/recuar no leitor)."""
    u = require_user(request)
    with db() as con:
        row = con.execute("SELECT file, ext FROM history WHERE id = ? AND user_id = ?", (item_id, u["id"])).fetchone()
    if not row or not row["file"]:
        raise HTTPException(404, "Este vídeo não está guardado no museu.")
    path = os.path.join(VIDEOS, os.path.basename(row["file"]))
    if not os.path.exists(path):
        raise HTTPException(404, "O ficheiro já não existe no servidor.")
    size = os.path.getsize(path)
    mime = MIME.get((row["ext"] or "").lower(), "application/octet-stream")
    start, end, status = 0, size - 1, 200
    m = re.match(r"bytes=(\d*)-(\d*)", request.headers.get("range", ""))
    if m and (m.group(1) or m.group(2)):
        if m.group(1):
            start = int(m.group(1))
            end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
        else:  # "bytes=-N": os últimos N bytes
            start = max(size - int(m.group(2)), 0)
        if start > end or start >= size:
            return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
        status = 206

    def chunks():
        with open(path, "rb") as f:
            f.seek(start)
            left = end - start + 1
            while left > 0:
                data = f.read(min(1024 * 1024, left))
                if not data:
                    break
                left -= len(data)
                yield data

    headers = {"Accept-Ranges": "bytes", "Content-Length": str(end - start + 1), "Cache-Control": "private, max-age=3600"}
    if status == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    return StreamingResponse(chunks(), status_code=status, media_type=mime, headers=headers)


@app.delete("/api/history/{item_id}")
def history_delete(item_id: int, request: Request):
    u = require_user(request)
    with db() as con:
        row = con.execute("SELECT file FROM history WHERE id = ? AND user_id = ?", (item_id, u["id"])).fetchone()
        con.execute("DELETE FROM history WHERE id = ? AND user_id = ?", (item_id, u["id"]))
    if row:
        remove_files([row["file"]])
    return {"ok": True}


@app.delete("/api/history")
def history_clear(request: Request):
    u = require_user(request)
    with db() as con:
        rows = con.execute("SELECT file FROM history WHERE user_id = ?", (u["id"],)).fetchall()
        con.execute("DELETE FROM history WHERE user_id = ?", (u["id"],))
    remove_files([r["file"] for r in rows])
    return {"ok": True}


def record_history(job: dict):
    """Guarda no museu do utilizador (se tiver sessão) o vídeo que acabou de ser entregue."""
    uid, m = job.get("user_id"), job.get("meta")
    if not uid or not m or job.get("recorded"):
        return
    job["recorded"] = True
    saved = None
    try:  # guarda uma cópia para poder ver no museu
        if job.get("path") and os.path.exists(job["path"]):
            saved = uuid.uuid4().hex + os.path.splitext(job["path"])[1]
            shutil.copy2(job["path"], os.path.join(VIDEOS, saved))
            if m.get("kind") == "video":
                threading.Thread(target=make_compatible, args=(saved, ""), daemon=True).start()
    except Exception as e:
        saved = None
        print("AVISO: não consegui guardar a cópia do vídeo:", e)
    try:
        with db() as con:
            con.execute(
                "INSERT INTO history(user_id, title, url, thumbnail, site, uploader, duration, kind, height, ext, created, file) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (uid, m["title"], m["url"], m["thumbnail"], m["site"], m["uploader"], m["duration"],
                 m["kind"], m["height"], m["ext"], time.time(), saved))
    except Exception as e:  # o histórico nunca pode impedir a descarga
        print("AVISO: não consegui guardar no museu:", e)


@app.get("/api/info")
def info(url: str, browser: str = ""):
    url = check_url(url)
    browser = check_browser(browser)
    try:
        with yt_dlp.YoutubeDL(base_opts(browser, url)) as ydl:
            data = ydl.extract_info(url, download=False)
        data, multiple = pick(data)
    except Exception as e:
        raise HTTPException(422, f"Não foi possível ler este link: {explain(e)}")
    # só alturas a partir de 144p (remove 90p, 45p, 27p, etc.)
    heights = sorted({f["height"] for f in data.get("formats", []) if f.get("height") and f["height"] >= 144}, reverse=True)
    return {
        "title": data.get("title"),
        "thumbnail": data.get("thumbnail"),
        "duration": data.get("duration"),
        "uploader": data.get("uploader") or data.get("channel"),
        "site": data.get("extractor_key"),
        "multiple": multiple,
        "heights": heights,
    }


def run_job(job_id: str, url: str, quality: str):
    job = JOBS[job_id]
    has_ffmpeg = shutil.which("ffmpeg") is not None
    # com ffmpeg: vídeo + áudio são 2 ficheiros; sem ffmpeg: ficheiro único
    expected = 1 if (quality == "audio" or not has_ffmpeg) else 2

    def hook(d):
        if job["want"]:  # pausa ou cancelamento pedidos: pára já
            raise Interrupted(job["want"])
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            frac = (d.get("downloaded_bytes") or 0) / total if total else 0
            pct = (job["files_done"] + frac) / expected * 100
            job["percent"] = max(job["percent"], min(pct, 99))  # nunca recua
            job["speed"] = d.get("speed")
            job["eta"] = d.get("eta")
            job["state"] = "downloading"
        elif d["status"] == "finished":
            job["files_done"] += 1
            job["state"] = "processing"

    def pp_hook(d):
        if d["status"] == "started":
            job["state"] = "processing"

    opts = {
        **base_opts(job["browser"], url),
        "outtmpl": os.path.join(TMP, f"{job_id}.%(ext)s"),  # mesmo nome => a retoma continua a parte guardada
        "merge_output_format": "mp4",
        "progress_hooks": [hook],
        "postprocessor_hooks": [pp_hook],
    }
    if quality == "audio":
        if has_ffmpeg:
            opts["format"] = "bestaudio/best"
            opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3"}]
        else:
            opts["format"] = "bestaudio[ext=m4a]/bestaudio/best"  # sem ffmpeg fica em m4a
    elif quality.isdigit():
        h = int(quality)
        if has_ffmpeg:
            opts["format"] = f"bestvideo[height<={h}]+bestaudio/best[height<={h}]/best"
        else:
            opts["format"] = f"best[height<={h}][ext=mp4]/best[height<={h}]/best"
    else:
        opts["format"] = "bestvideo+bestaudio/best" if has_ffmpeg else "best[ext=mp4]/best"

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            data, _ = pick(ydl.extract_info(url, download=True))
        if job["want"] == "stop":  # cancelado durante a fase de junção
            discard(job_id, job)
            return
        path = ((data.get("requested_downloads") or [{}])[0]).get("filepath")
        if not path or not os.path.exists(path):
            files = glob.glob(os.path.join(TMP, f"{job_id}.*"))
            if not files:
                raise RuntimeError("Ficheiro não encontrado após a descarga.")
            path = max(files, key=os.path.getsize)  # o final é o maior
        safe = re.sub(r'[\\/:*?"<>|]+', "", data.get("title") or data.get("id") or "video")[:80].strip() or "video"
        thumb = data.get("thumbnail")
        job["meta"] = {
            "title": (data.get("title") or "Sem título")[:300],
            "url": url,
            "thumbnail": thumb if isinstance(thumb, str) and thumb.startswith(("http://", "https://")) else None,
            "site": data.get("extractor_key"),
            "uploader": data.get("uploader") or data.get("channel"),
            "duration": data.get("duration"),
            "kind": "audio" if quality == "audio" else "video",
            "height": int(quality) if quality.isdigit() else None,
            "ext": os.path.splitext(path)[1].lstrip(".").lower(),
        }
        job["want"] = None
        job["path"] = path
        job["filename"] = safe + os.path.splitext(path)[1]
        job["percent"] = 100
        if job.get("museum_id"):
            attach_to_museum(job_id, job)
        job["state"] = "done"
    except Exception as e:
        want = job["want"]
        if want == "pause":
            job["speed"] = job["eta"] = None
            job["state"] = "paused"  # ficheiros parciais ficam guardados
        elif want == "stop":
            discard(job_id, job)
        else:
            msg = explain(e)
            if "Requested format is not available" in msg and not has_ffmpeg:
                msg = ("Este video so tem video e audio em ficheiros separados e o ffmpeg nao esta instalado. "
                       "Instala-o com: winget install ffmpeg. Depois fecha o terminal, abre outro e arranca a Toca da Capivara outra vez.")
            job["error"] = f"Falha na descarga: {msg}"
            job["state"] = "error"


def attach_to_museum(job_id: str, job: dict):
    """Descarga pedida a partir do museu: guarda o ficheiro em videos/ e liga-o ao registo existente."""
    saved = uuid.uuid4().hex + os.path.splitext(job["path"])[1]
    shutil.copy2(job["path"], os.path.join(VIDEOS, saved))
    with db() as con:
        con.execute("UPDATE history SET file = ?, ext = ? WHERE id = ? AND user_id = ?",
                    (saved, job["meta"]["ext"], job["museum_id"], job["user_id"]))
    for f in glob.glob(os.path.join(TMP, f"{job_id}.*")):
        try:
            os.remove(f)
        except OSError:
            pass
    job["path"] = None
    if job["meta"]["kind"] == "video":
        threading.Thread(target=make_compatible, args=(saved, ""), daemon=True).start()


def launch(job_id: str):
    job = JOBS[job_id]
    threading.Thread(target=run_job, args=(job_id, job["url"], job["quality"]), daemon=True).start()


@app.post("/api/start")
def start(request: Request, url: str, quality: str = "best", browser: str = ""):
    url = check_url(url)
    browser = check_browser(browser)
    job_id = uuid.uuid4().hex
    JOBS[job_id] = {"state": "starting", "percent": 0, "speed": None, "eta": None, "files_done": 0,
                    "error": None, "path": None, "filename": None, "want": None,
                    "url": url, "quality": quality, "browser": browser,
                    "user_id": (user_from(request) or {}).get("id"), "meta": None, "recorded": False}
    launch(job_id)
    return {"job": job_id}


@app.post("/api/history/{item_id}/fetch")
def history_fetch(item_id: int, request: Request, browser: str = ""):
    """Para vídeos antigos do museu: volta a descarregar pelo link e guarda para ver."""
    u = require_user(request)
    browser = check_browser(browser)
    with db() as con:
        row = con.execute("SELECT url, kind, height FROM history WHERE id = ? AND user_id = ?",
                          (item_id, u["id"])).fetchone()
    if not row or not row["url"]:
        raise HTTPException(404, "Este vídeo já não está no museu.")
    quality = "audio" if row["kind"] == "audio" else (str(row["height"]) if row["height"] else "best")
    job_id = uuid.uuid4().hex
    JOBS[job_id] = {"state": "starting", "percent": 0, "speed": None, "eta": None, "files_done": 0,
                    "error": None, "path": None, "filename": None, "want": None,
                    "url": row["url"], "quality": quality, "browser": browser,
                    "user_id": u["id"], "meta": None, "recorded": True, "museum_id": item_id}
    launch(job_id)
    return {"job": job_id}


@app.post("/api/pause/{job_id}")
def pause(job_id: str):
    job = get_job(job_id)
    if job["state"] in ("starting", "downloading"):
        job["want"] = "pause"
        return {"ok": True}
    if job["state"] == "paused":
        return {"ok": True}
    raise HTTPException(409, "Não é possível pausar nesta fase.")


@app.post("/api/resume/{job_id}")
def resume(job_id: str):
    job = get_job(job_id)
    if job["state"] != "paused":
        raise HTTPException(409, "A descarga não está em pausa.")
    job["want"] = None
    job["files_done"] = 0  # o yt-dlp salta os ficheiros já completos e conta-os de novo
    job["state"] = "starting"
    launch(job_id)
    return {"ok": True}


@app.post("/api/cancel/{job_id}")
def cancel(job_id: str):
    job = get_job(job_id)
    if job["state"] in ("paused", "done"):
        discard(job_id, job)
    elif job["state"] in ("starting", "downloading", "processing"):
        job["want"] = "stop"
    return {"ok": True}


@app.get("/api/progress/{job_id}")
def progress(job_id: str):
    job = get_job(job_id)
    return {k: job[k] for k in ("state", "percent", "speed", "eta", "error")}


@app.get("/api/file/{job_id}")
def file(job_id: str):
    job = JOBS.get(job_id)
    if not job or job["state"] != "done":
        raise HTTPException(409, "O ficheiro ainda não está pronto.")

    def cleanup():  # apaga do servidor tudo o que sobrou desta descarga
        try:
            for f in glob.glob(os.path.join(TMP, f"{job_id}.*")):
                try:
                    os.remove(f)
                except OSError:
                    pass
        finally:
            JOBS.pop(job_id, None)

    record_history(job)
    return FileResponse(job["path"], filename=job["filename"], background=BackgroundTask(cleanup))


@app.get("/")
def home():
    return FileResponse(os.path.join(BASE, "index.html"), headers={"Cache-Control": "no-cache"})


@app.get("/style.css")
def style():
    # no-cache: alterações ao CSS aparecem logo ao recarregar a página
    return FileResponse(os.path.join(BASE, "style.css"), media_type="text/css",
                        headers={"Cache-Control": "no-cache"})


app.mount("/img", StaticFiles(directory=os.path.join(BASE, "img")), name="img")