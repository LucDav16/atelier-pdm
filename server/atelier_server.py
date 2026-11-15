#!/usr/bin/env python3
"""
Atelier PDM server (atelier_server.py)  2.9.0
=============================================

Runs on the Raspberry Pi (or any Linux/Windows PC) that holds the vault. It owns the vault database, so every
Atelier PC talks to one copy of it over the network instead of opening the file across a share - which is what
makes several people working at once safe. The vault's files themselves are shared with Samba as before.

    python3 atelier_server.py --db /srv/atelier/data/atelier_index.db --token <secret> [--port 8770]

or with a config file (what install_atelier_server.sh sets up):

    python3 atelier_server.py --config /etc/atelier/server.json

Also: nightly database backups (kept 14 days, in <data>/backups), and  GET /health  for a quick check.
Needs only Python 3.9+ (standard library).
"""
import argparse
import base64
import hmac
import json
import os
import secrets
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "2.9.0"
IDLE = 120                       # seconds before an abandoned session is rolled back
MAX_BODY = 32 * 1024 * 1024
CFG = {"db": "", "token": "", "backups": "", "keep": 14}
SESSIONS = {}
SLOCK = threading.Lock()
STATS = {"started": time.time(), "requests": 0, "sessions": 0, "errors": 0, "last_backup": ""}


def enc(v):
    if isinstance(v, (bytes, bytearray, memoryview)):
        return {"__b64": base64.b64encode(bytes(v)).decode()}
    return v


def dec(v):
    if isinstance(v, dict) and "__b64" in v:
        return base64.b64decode(v["__b64"])
    return v


class Session:
    def __init__(self, user):
        self.conn = sqlite3.connect(CFG["db"], timeout=15, check_same_thread=False)
        self.lock = threading.Lock()
        self.used = time.time()
        self.user = user

    def close(self, commit=False):
        try:
            if commit:
                self.conn.commit()
            else:
                self.conn.rollback()
        finally:
            self.conn.close()


def reaper():
    while True:
        time.sleep(15)
        now = time.time()
        with SLOCK:
            old = [k for k, s in SESSIONS.items() if now - s.used > IDLE]
            dead = [SESSIONS.pop(k) for k in old]
        for s in dead:
            try:
                s.close(commit=False)
            except sqlite3.Error:
                pass


def backup_now():
    if not CFG["backups"]:
        return None
    os.makedirs(CFG["backups"], exist_ok=True)
    dst = os.path.join(CFG["backups"], f"atelier_index_{time.strftime('%Y%m%d_%H%M%S')}.db")
    src = sqlite3.connect(CFG["db"], timeout=30)
    out = sqlite3.connect(dst)
    try:
        src.backup(out)
    finally:
        out.close()
        src.close()
    files = sorted(f for f in os.listdir(CFG["backups"]) if f.startswith("atelier_index_") and f.endswith(".db"))
    for f in files[:-max(1, int(CFG["keep"]))]:
        try:
            os.remove(os.path.join(CFG["backups"], f))
        except OSError:
            pass
    STATS["last_backup"] = time.strftime("%Y-%m-%d %H:%M:%S")
    return dst


def backup_loop():
    last_day = ""
    while True:
        time.sleep(60)
        day = time.strftime("%Y-%m-%d")
        if day != last_day and time.strftime("%H") >= "02":
            try:
                backup_now()
                last_day = day
            except Exception as e:                                   # noqa: BLE001
                print(f"backup failed: {e}", file=sys.stderr)


def run_op(req):
    op = req.get("op")
    if op == "open":
        s = Session(req.get("user") or "?")
        sid = secrets.token_hex(16)
        with SLOCK:
            SESSIONS[sid] = s
        STATS["sessions"] += 1
        return {"sid": sid}
    if op == "backup":
        return {"file": backup_now()}
    sid = req.get("sid")
    with SLOCK:
        s = SESSIONS.get(sid)
        if s is not None and op in ("commit", "rollback", "close"):
            SESSIONS.pop(sid, None)
    if s is None:
        raise sqlite3.OperationalError("The server session expired - please try again.")
    with s.lock:
        s.used = time.time()
        if op in ("commit", "close"):
            s.close(commit=True)
            return {}
        if op == "rollback":
            s.close(commit=False)
            return {}
        if op == "exec":
            sql = req["sql"]
            if req.get("many"):
                cur = s.conn.executemany(sql, [[dec(p) for p in row] for row in req.get("params") or []])
            else:
                cur = s.conn.execute(sql, [dec(p) for p in (req.get("params") or [])])
            cols = [d[0] for d in cur.description] if cur.description else []
            rows = [[enc(v) for v in r] for r in cur.fetchall()] if cols else []
            return {"cols": cols, "rows": rows, "rowcount": cur.rowcount, "lastrowid": cur.lastrowid,
                    "in_tx": s.conn.in_transaction}
    raise ValueError(f"unknown operation {op!r}")


class Handler(BaseHTTPRequestHandler):
    server_version = f"AtelierServer/{VERSION}"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *a):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.rstrip("/") == "/health":
            ok = os.path.isfile(CFG["db"])
            self._send(200 if ok else 500, {"ok": ok, "version": VERSION, "database": CFG["db"],
                                            "sessions": len(SESSIONS), "uptime_s": int(time.time() - STATS["started"]),
                                            "requests": STATS["requests"], "last_backup": STATS["last_backup"]})
        else:
            self._send(404, {"error": {"type": "NotFound", "msg": "not found"}})

    def do_POST(self):
        STATS["requests"] += 1
        n = int(self.headers.get("Content-Length") or 0)
        if self.path.rstrip("/") != "/api" or n > MAX_BODY:
            self._send(404, {"error": {"type": "NotFound", "msg": "not found"}})
            return
        if not hmac.compare_digest(self.headers.get("X-Atelier-Token", ""), CFG["token"]):
            self._send(403, {"error": {"type": "PermissionError", "msg": "Wrong server token."}})
            return
        try:
            req = json.loads(self.rfile.read(n) or b"{}")
            self._send(200, {"ok": True, **run_op(req)})
        except sqlite3.Error as e:
            STATS["errors"] += 1
            self._send(200, {"error": {"type": type(e).__name__, "msg": str(e)}})
        except Exception as e:                                       # noqa: BLE001
            STATS["errors"] += 1
            self._send(200, {"error": {"type": "OperationalError", "msg": f"{type(e).__name__}: {e}"}})


def make_server(host, port):
    return ThreadingHTTPServer((host, port), Handler)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Atelier PDM database server")
    ap.add_argument("--config")
    ap.add_argument("--db")
    ap.add_argument("--token")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--backups")
    ap.add_argument("--new-token", action="store_true", help="print a new random token and exit")
    a = ap.parse_args(argv)
    if a.new_token:
        print(secrets.token_urlsafe(32))
        return
    if a.config:
        with open(a.config, encoding="utf-8") as f:
            c = json.load(f)
        CFG.update({k: c[k] for k in ("db", "token", "backups", "keep") if k in c})
        a.host, a.port = c.get("host", a.host), int(c.get("port", a.port))
    for k in ("db", "token", "backups"):
        if getattr(a, k):
            CFG[k] = getattr(a, k)
    if not CFG["db"] or not CFG["token"]:
        sys.exit("Give the database path and a token (--db and --token, or --config).")
    if len(CFG["token"]) < 16:
        sys.exit("Use a longer token (atelier_server.py --new-token makes one).")
    if not CFG["backups"]:
        CFG["backups"] = os.path.join(os.path.dirname(os.path.abspath(CFG["db"])), "backups")
    os.makedirs(os.path.dirname(os.path.abspath(CFG["db"])), exist_ok=True)
    sqlite3.connect(CFG["db"]).execute("PRAGMA journal_mode=WAL").fetchall()
    for t in (reaper, backup_loop):
        threading.Thread(target=t, daemon=True).start()
    srv = make_server(a.host, a.port)
    print(f"Atelier server {VERSION} on {a.host}:{a.port} - database {CFG['db']}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
