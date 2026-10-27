"""Backend simulado do Lyra (só biblioteca standard, sem yt-dlp nem rede).

Serve o index.html real e imita a API. O comportamento depende do link:
  ...slow...   -> /api/info demora 1,5 s
  ...fail...   -> /api/info responde 422 com {"detail": ...}
  ...multi...  -> devolve multiple=true
  outro        -> responde logo com um vídeo de teste cujo título inclui o link

Endpoints de teste:  GET /__log  (pedidos recebidos)   POST /__reset
"""
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX = os.environ.get("LYRA_INDEX", os.path.join(HERE, "..", "index.html"))

LOG: list = []
LOCK = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # silencioso
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _record(self, path, qs):
        with LOCK:
            LOG.append({"method": self.command, "path": path, "url": qs.get("url", [""])[0]})

    def do_GET(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        if u.path == "/":
            data = open(INDEX, "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif u.path == "/__log":
            with LOCK:
                self._json(200, list(LOG))
        elif u.path == "/api/info":
            self._record(u.path, qs)
            link = qs.get("url", [""])[0]
            if "slow" in link:
                time.sleep(1.5)
            if "fail" in link:
                return self._json(422, {"detail": "Não foi possível ler este link: simulado"})
            self._json(200, {"title": "Vídeo de teste " + link, "thumbnail": None, "duration": 125,
                             "uploader": "Canal Teste", "site": "Mock", "multiple": "multi" in link,
                             "heights": [1080, 720, 360]})
        elif u.path.startswith("/api/progress/"):
            self._json(200, {"state": "downloading", "percent": 10, "speed": 1048576, "eta": 30, "error": None}
                       if not CANCELLED else {"state": "cancelled", "percent": 0, "speed": None, "eta": None, "error": None})
        else:
            self._json(404, {"detail": "não encontrado"})

    def do_POST(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        self._record(u.path, qs)
        global CANCELLED
        if u.path == "/api/start":
            CANCELLED = False
            self._json(200, {"job": "job1"})
        elif u.path.startswith("/api/cancel/"):
            CANCELLED = True
            self._json(200, {"ok": True})
        elif u.path == "/__reset":
            with LOCK:
                LOG.clear()
            CANCELLED = False
            self._json(200, {"ok": True})
        else:
            self._json(404, {"detail": "não encontrado"})


CANCELLED = False


class QuietServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address):  # o browser cancela pedidos (AbortController): é esperado
        pass


def serve(port=0):
    srv = QuietServer(("127.0.0.1", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


if __name__ == "__main__":
    s = serve(8766)
    print("Mock em http://127.0.0.1:8766  (Ctrl+C para sair)")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
