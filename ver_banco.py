"""Mostra o que está na base de dados (toca.db). Uso:  python ver_banco.py
Nunca mostra palavras-passe nem tokens de sessão."""
import os
import sqlite3
import time

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "toca.db")
if not os.path.exists(DB):
    raise SystemExit("Ainda não existe toca.db. Arranca primeiro o servidor (iniciar.bat).")

fmt = lambda t: time.strftime("%d/%m/%Y %H:%M", time.localtime(t)) if t else "-"
con = sqlite3.connect(DB)
con.row_factory = sqlite3.Row

print(f"Base de dados: {DB}\n")
for t in ("users", "sessions", "history"):
    print(f"{t}: {con.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]} registos")

print("\n== Contas ==")
for u in con.execute("SELECT u.id, u.username, u.created, COUNT(h.id) n FROM users u "
                     "LEFT JOIN history h ON h.user_id = u.id GROUP BY u.id ORDER BY u.id"):
    print(f"  #{u['id']}  {u['username']:<24} criada em {fmt(u['created'])}  ·  {u['n']} vídeos no museu")

print("\n== Museu dos Vídeos (últimos 30) ==")
for h in con.execute("SELECT h.*, u.username FROM history h JOIN users u ON u.id = h.user_id "
                     "ORDER BY h.created DESC LIMIT 30"):
    label = "MP3/áudio" if h["kind"] == "audio" else f"{h['height'] or '?'}p"
    print(f"  {fmt(h['created'])}  [{h['username']}]  {(h['title'] or '')[:60]}  ({h['site']}, {label}, {h['ext']})")
