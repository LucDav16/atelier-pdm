"""
Atelier PDM 2.9.0 (atelier_v29.py)
=================================

Windows that fit
    Every dialog grows to show all of its contents when it opens, and again when you switch tabs (e.g.
    Administration › Release workflow) - up to the size of the screen; beyond that it scrolls (2.8.6).

Renaming keeps assemblies working   (account menu › Assemblies & drawings…, on by default)
    When a part is renamed or given its number, every assembly and drawing in the vault that refers to it by
    file name is updated to the new name. The previous version of each one is kept in the history first.
    Files checked out by someone else, and file formats that can't be edited safely, are listed so they can be
    re-linked by hand.

Drawings and their part's revision
    A drawing is linked to the part with the same name in the same folder. Its row shows
    "⚠ Part is now B-01" or "⚠ Part changed" until the drawing is checked in again (or right-click › Drawing is up
    to date). With "Drawings follow their part's revision" ticked, up-issuing a part also up-issues its drawing to
    the same revision. Unticked (the default), drawings keep their own revision - you can revise a drawing without
    touching the part.

Server mode
    With  "db_server"  and  "db_token"  in the vault's atelier_config.json, Atelier uses the Atelier server
    (e.g. on a Raspberry Pi) for the vault database instead of opening the file over the network.

SolidCraft plugin bridge
    Requests written by the Atelier SolidCraft plugin to  <working folder>\\_Atelier\\plugin\\requests  are carried
    out (status, check out, check in, undo check-out, save to vault) and answered in  ...\\responses.
"""
import contextlib
import http.client
import json
import os
import re
import shutil
import sqlite3
import sys
import threading
import time
import traceback
import urllib.parse
import uuid
import zipfile

VERSION = "2.9.0"
E = None
ORIG = {}
_TL = threading.local()
_GUARD = threading.local()
SET_REFS, SET_FOLLOW, SET_FLAG = "refs_update_on_rename", "drawing_follow_rev", "drawing_flag"
TEXT_EXTS_IN_ZIP = (".xml", ".json", ".txt", ".csv")


def log_error(where):
    try:
        base = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "Atelier PDM")
        os.makedirs(base, exist_ok=True)
        with open(os.path.join(base, "atelier_error.log"), "a", encoding="utf-8") as f:
            f.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} · {where} ({VERSION}) =====\n"
                    f"{traceback.format_exc()}\n")
    except Exception:                                           # noqa: BLE001
        pass


def setting(key, default):
    try:
        return E.get_setting(key, default)
    except Exception:                                           # noqa: BLE001
        return default


def on(key, default):
    return str(setting(key, "1" if default else "0")) == "1"


def user():
    return getattr(E, "USER_NAME", "") or ""


# =============================================================================
# 1. Server mode - the vault database on the Atelier server
# =============================================================================
class RRow(tuple):
    """Behaves like sqlite3.Row: row[0], row['name'], keys(), dict(row)."""

    def __new__(cls, cols, vals):
        o = tuple.__new__(cls, vals)
        o._cols = cols
        o._idx = {c.lower(): i for i, c in enumerate(cols)}
        return o

    def __getitem__(self, k):
        if isinstance(k, str):
            try:
                return tuple.__getitem__(self, self._idx[k.lower()])
            except KeyError:
                raise IndexError(f"No item with that key: {k}") from None
        return tuple.__getitem__(self, k)

    def keys(self):
        return list(self._cols)


def _dec(v):
    if isinstance(v, dict) and "__b64" in v:
        import base64
        return base64.b64decode(v["__b64"])
    return v


def _enc(v):
    if isinstance(v, (bytes, bytearray, memoryview)):
        import base64
        return {"__b64": base64.b64encode(bytes(v)).decode()}
    return v


class Remote:
    def __init__(self, url, token):
        u = urllib.parse.urlparse(url if "://" in url else "http://" + url)
        self.host, self.port, self.url, self.token = u.hostname, u.port or 8770, url, token

    def _conn(self):
        c = getattr(_TL, "conn", None)
        if c is None or getattr(_TL, "key", None) != (self.host, self.port):
            c = http.client.HTTPConnection(self.host, self.port, timeout=20)
            _TL.conn, _TL.key = c, (self.host, self.port)
        return c

    def call(self, **req):
        body = json.dumps(req).encode()
        last = None
        for attempt in range(2):
            c = self._conn()
            try:
                c.request("POST", "/api", body, {"Content-Type": "application/json", "X-Atelier-Token": self.token})
                r = c.getresponse()
                data = json.loads(r.read() or b"{}")
                break
            except (OSError, http.client.HTTPException, ValueError) as e:
                last = e
                try:
                    c.close()
                except Exception:                               # noqa: BLE001
                    pass
                _TL.conn = None
        else:
            raise sqlite3.OperationalError(f"Can't reach the Atelier server at {self.url} ({last}). Check the server "
                                           "is switched on and this PC is on the same network.")
        if "error" in data:
            err = data["error"]
            if err.get("type") == "PermissionError":
                raise sqlite3.OperationalError("The Atelier server refused the token in atelier_config.json.")
            raise getattr(sqlite3, err.get("type", ""), sqlite3.OperationalError)(err.get("msg", "server error"))
        return data

    def health(self):
        c = http.client.HTTPConnection(self.host, self.port, timeout=5)
        try:
            c.request("GET", "/health")
            return json.loads(c.getresponse().read())
        finally:
            c.close()


class RCursor:
    def __init__(self, conn):
        self.conn, self.rows, self.pos = conn, [], 0
        self.rowcount, self.lastrowid, self.description = -1, None, None
        self.row_factory = None

    def _load(self, res):
        cols = res.get("cols") or []
        self.rows = [RRow(cols, [_dec(v) for v in r]) for r in res.get("rows") or []]
        self.pos = 0
        self.rowcount, self.lastrowid = res.get("rowcount", -1), res.get("lastrowid")
        self.description = tuple((c, None, None, None, None, None, None) for c in cols) or None
        self.conn.in_transaction = bool(res.get("in_tx"))
        return self

    def execute(self, sql, params=()):
        if isinstance(params, dict):
            raise sqlite3.ProgrammingError("Named parameters aren't supported in server mode.")
        return self._load(self.conn.r.call(op="exec", sid=self.conn.sid, sql=sql,
                                           params=[_enc(p) for p in (params or ())]))

    def executemany(self, sql, seq):
        return self._load(self.conn.r.call(op="exec", sid=self.conn.sid, sql=sql, many=True,
                                           params=[[_enc(p) for p in row] for row in seq]))

    def fetchone(self):
        if self.pos >= len(self.rows):
            return None
        self.pos += 1
        return self.rows[self.pos - 1]

    def fetchall(self):
        out, self.pos = self.rows[self.pos:], len(self.rows)
        return out

    def fetchmany(self, n=1):
        out = self.rows[self.pos:self.pos + n]
        self.pos += len(out)
        return out

    def __iter__(self):
        while True:
            r = self.fetchone()
            if r is None:
                return
            yield r

    def close(self):
        pass


class RConn:
    def __init__(self, remote):
        self.r = remote
        self.sid = remote.call(op="open", user=user())["sid"]
        self.row_factory = None
        self.in_transaction = False
        self.closed = False

    def cursor(self):
        return RCursor(self)

    def execute(self, sql, params=()):
        return RCursor(self).execute(sql, params)

    def executemany(self, sql, seq):
        return RCursor(self).executemany(sql, seq)

    def executescript(self, script):
        for stmt in [s.strip() for s in script.split(";") if s.strip()]:
            self.execute(stmt)

    def commit(self):
        if self.in_transaction:
            self.execute("COMMIT")

    def rollback(self):
        if self.in_transaction:
            self.execute("ROLLBACK")

    def close(self, commit=True):
        if not self.closed:
            self.closed = True
            try:
                self.r.call(op="commit" if commit else "rollback", sid=self.sid)
            except sqlite3.OperationalError:
                if commit:
                    raise


def remote_db_factory(remote):
    @contextlib.contextmanager
    def db():
        conn = RConn(remote)
        try:
            yield conn
        except BaseException:
            try:
                conn.close(commit=False)
            except Exception:                                   # noqa: BLE001
                pass
            raise
        conn.close(commit=True)
    return db


def server_config():
    cfg = getattr(E, "CONFIG", {}) or {}
    url = os.environ.get("ATELIER_DB_SERVER") or cfg.get("db_server") or ""
    token = os.environ.get("ATELIER_DB_TOKEN") or cfg.get("db_token") or ""
    return (url.strip(), token.strip()) if url and token else (None, None)


def install_server_mode():
    url, token = server_config()
    if not url:
        return None
    remote = Remote(url, token)
    ORIG["db"] = E.db
    E.db = remote_db_factory(remote)
    E.REMOTE_DB = url
    E.DB_PATH = f"{url} (Atelier server)"
    P = sys.modules.get("atelier_pro")
    if P is not None:
        msg = "Backups of the database are made by the Atelier server every night (in its data/backups folder)."
        if hasattr(P, "backup_due"):
            P.backup_due = lambda *a, **k: False
        for n in ("run_backup", "restore_database"):
            if hasattr(P, n):
                setattr(P, n, lambda *a, _m=msg, **k: (_ for _ in ()).throw(RuntimeError(_m)))
    return remote


# =============================================================================
# 2. Dialogs grow to fit their contents
# =============================================================================
def needed_size(dlg):
    st = getattr(dlg, "_atl_scroll", None)
    if st and dlg.winfo_ismapped():
        cv, inner = st["cv"], st["inner"]
        h = dlg.winfo_height() - cv.winfo_height() + inner.winfo_reqheight()
        w = dlg.winfo_width() - cv.winfo_width() + inner.winfo_reqwidth() + (18 if st.get("shown") else 0)
        return w, h
    return dlg.winfo_reqwidth(), dlg.winfo_reqheight()


def fit(dlg, force=False):
    try:
        if not dlg.winfo_exists() or not dlg.winfo_ismapped():
            return
        dlg.update_idletasks()
        need_w, need_h = needed_size(dlg)
        last = getattr(dlg, "_atl_need", (0, 0))
        if not force and need_h <= last[1] + 2 and need_w <= last[0] + 2:
            return
        dlg._atl_need = (need_w, need_h)
        sw, sh = dlg.winfo_screenwidth(), dlg.winfo_screenheight()
        cw, ch = dlg.winfo_width(), dlg.winfo_height()
        tw, th = min(max(cw, need_w), sw - 20), min(max(ch, need_h), sh - 70)
        if tw <= cw + 1 and th <= ch + 1:
            return
        x, y = dlg.winfo_x(), dlg.winfo_y()
        x = max(0, min(x, sw - tw - 10))
        y = max(0, min(y, sh - th - 60))
        dlg.geometry(f"{tw}x{th}+{x}+{y}")
    except Exception:                                           # noqa: BLE001
        log_error("fit dialog")


def _watch(dlg):
    try:
        if not dlg.winfo_exists():
            return
        fit(dlg)
        dlg.after(350, lambda: _watch(dlg))
    except Exception:                                           # noqa: BLE001
        pass


def _hook_dialogs():
    cls = getattr(E, "ThemedDialog", None)
    if cls is None or getattr(cls, "_atl_v29", False):
        return
    cls._atl_v29 = True
    o_show = cls.show

    def show(self, *a, **k):
        try:
            self.after(60, lambda: fit(self, force=True))
            self.after(400, lambda: _watch(self))
        except Exception:                                       # noqa: BLE001
            pass
        return o_show(self, *a, **k)
    cls.show = show


# =============================================================================
# 3. Renaming updates references
# =============================================================================
def _ref_rx(old):
    # not preceded by a letter, digit, - _ . or space (so "My Bracket.sc" isn't taken for "Bracket.sc")
    return re.compile(r"(?<![A-Za-z0-9_\-. ])" + re.escape(old) + r"(?![A-Za-z0-9_])")


def rewrite_bytes(data, pairs):
    """(new bytes or None, count, kind). kind: 'text', 'zip' or 'binary'."""
    if data[:4] == b"PK\x03\x04":
        import io
        try:
            zin = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile:
            return None, 0, "binary"
        out, total = io.BytesIO(), 0
        with zipfile.ZipFile(out, "w") as zout:
            for it in zin.infolist():
                raw = zin.read(it)
                if it.filename.lower().endswith(TEXT_EXTS_IN_ZIP):
                    try:
                        txt = raw.decode("utf-8")
                        n = 0
                        for old, new in pairs:
                            txt, k = _ref_rx(old).subn(lambda m, new=new: new, txt)
                            n += k
                        if n:
                            raw, total = txt.encode("utf-8"), total + n
                    except UnicodeDecodeError:
                        pass
                zout.writestr(it, raw, compress_type=it.compress_type)
        return (out.getvalue() if total else None), total, "zip"
    head = data[:65536]
    if b"\x00" in head:
        n = sum(data.count(old.encode("utf-8")) for old, _new in pairs)
        return None, n, "binary"
    try:
        txt = data.decode("utf-8")
    except UnicodeDecodeError:
        n = sum(data.count(old.encode("utf-8")) for old, _new in pairs)
        return None, n, "binary"
    total = 0
    for old, new in pairs:
        txt, k = _ref_rx(old).subn(lambda m, new=new: new, txt)
        total += k
    return (txt.encode("utf-8") if total else None), total, "text"


def _read(path):
    copier = getattr(E, "vault_copy", None)
    if copier is None:
        with open(path, "rb") as f:
            return f.read()
    tmp_dir = os.path.join(E.LOCAL_WORKSPACE, "_tmp")
    os.makedirs(tmp_dir, exist_ok=True)
    tmp = os.path.join(tmp_dir, f"ref_{uuid.uuid4().hex}{os.path.splitext(path)[1]}")
    try:
        copier(path, tmp)
        with open(tmp, "rb") as f:
            return f.read()
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def _write(path, data):
    copier = getattr(E, "vault_copy", None)
    tmp_dir = os.path.join(E.LOCAL_WORKSPACE, "_tmp")
    os.makedirs(tmp_dir, exist_ok=True)
    tmp = os.path.join(tmp_dir, f"ref_{uuid.uuid4().hex}{os.path.splitext(path)[1]}")
    with open(tmp, "wb") as f:
        f.write(data)
    try:
        part = path + ".partial"
        (copier or shutil.copy2)(tmp, part)
        os.replace(part, path)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def _ref_exts():
    return set(E.CAD_MODEL_EXTS) | set(getattr(E, "CAD_DRAWING_EXTS", ())) | {".dxf"}


def update_references(pairs, skip=()):
    """pairs: [(old file name, new file name)]. Returns [(level, message)]."""
    if not pairs:
        return []
    out, done, manual = [], [], []
    skip = {s.lower() for s in skip}
    exts = _ref_exts()
    for name, info in list(E.INDEX.files.items()):
        if name.lower() in skip or info.ext not in exts or info.is_library:
            continue
        try:
            data = _read(info.path)
        except OSError:
            continue
        if not any(old.encode("utf-8") in data for old, _ in pairs) and data[:4] != b"PK\x03\x04":
            continue
        new, n, kind = rewrite_bytes(data, pairs)
        if not n:
            continue
        rec = E.get_record(name) or {}
        lock = rec.get("locked_by") or ""
        if kind == "binary":
            manual.append(f"{name} (its file format can't be edited safely)")
            continue
        if lock and lock != user():
            manual.append(f"{name} (checked out by {lock})")
            continue
        rev = rec.get("revision") or "A-01"
        try:
            E.archive_to_history(info.path, name, rev)
            _write(info.path, new)
            if lock == user():
                sp = E.sandbox_path(name)
                if os.path.isfile(sp):
                    with open(sp, "rb") as f:
                        local = f.read()
                    lnew, _k, _kind = rewrite_bytes(local, pairs)
                    if lnew:
                        with open(sp, "wb") as f:
                            f.write(lnew)
            with E.db() as c:
                c.execute("UPDATE files SET hash=? WHERE filename=?", (E.get_file_hash(info.path), name))
            E.log_audit(name, "References Updated", rev, "; ".join(f"{a} -> {b}" for a, b in pairs))
            done.append(name)
        except Exception as e:                                  # noqa: BLE001
            log_error("update references")
            manual.append(f"{name} ({e})")
    if done:
        out.append(("ok", f"Updated the link in {len(done)} file{'s' if len(done) != 1 else ''}: "
                          + ", ".join(done[:6]) + ("…" if len(done) > 6 else "")))
    if manual:
        out.append(("warn", "Re-link by hand (open and point to the new name): " + "; ".join(manual[:6])
                    + ("…" if len(manual) > 6 else "")))
    return out


def _rename_group(path, new_stem, *a, **k):
    d = os.path.dirname(path)
    old_stem = os.path.splitext(os.path.basename(path))[0]
    before = []
    try:
        before = [n for n in os.listdir(d) if os.path.splitext(n)[0].lower() == old_stem.lower()
                  and os.path.splitext(n)[1].lower() in _ref_exts()]
    except OSError:
        pass
    res = ORIG["rename_group"](path, new_stem, *a, **k)
    try:
        if res and res[0] == "ok" and on(SET_REFS, True) and new_stem != old_stem:
            pairs = [(n, new_stem + os.path.splitext(n)[1]) for n in before]
            try:
                E.INDEX.refresh()
            except Exception:                                   # noqa: BLE001
                pass
            extra = update_references(pairs)
            _move_links(pairs)
            if extra:
                return res[0], res[1] + "  " + "  ".join(m for _l, m in extra), *res[2:]
    except Exception:                                           # noqa: BLE001
        log_error("rename references")
    return res


# =============================================================================
# 4. Drawings and their part's revision
# =============================================================================
_TBL = {"ok": False}


def _table():
    if _TBL["ok"]:
        return
    with E.db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS drawing_links (drawing TEXT PRIMARY KEY, part TEXT, part_rev TEXT,
                     part_hash TEXT, at TEXT)""")
    _TBL["ok"] = True


def is_drawing(name):
    return os.path.splitext(name or "")[1].lower() in (set(getattr(E, "CAD_DRAWING_EXTS", ())) | {".dxf"})


def part_of(drawing):
    info = E.INDEX.files.get(drawing)
    if info is None:
        return None
    stem = os.path.splitext(drawing)[0].lower()
    for n, i in E.INDEX.files.items():
        if i.ext in E.CAD_MODEL_EXTS and os.path.splitext(n)[0].lower() == stem and i.folder == info.folder:
            return n
    return None


def drawings_of(part):
    info = E.INDEX.files.get(part)
    if info is None:
        return []
    stem = os.path.splitext(part)[0].lower()
    return [n for n, i in E.INDEX.files.items() if is_drawing(n) and i.folder == info.folder
            and os.path.splitext(n)[0].lower() == stem]


def record(drawing, part=None):
    part = part or part_of(drawing)
    if not part:
        return
    _table()
    rec = E.INDEX.records.get(part) or E.get_record(part) or {}
    with E.db() as c:
        c.execute("INSERT OR REPLACE INTO drawing_links VALUES (?,?,?,?,?)",
                  (drawing, part, rec.get("revision") or "A-01", rec.get("hash") or "", time.strftime("%Y-%m-%d %H:%M:%S")))


def links():
    try:
        _table()
        with E.db() as c:
            return {r[0]: (r[1], r[2], r[3]) for r in c.execute("SELECT drawing, part, part_rev, part_hash FROM drawing_links")}
    except Exception:                                           # noqa: BLE001
        return {}


def drawing_flag(drawing, table=None):
    part = part_of(drawing)
    if not part:
        return None
    table = links() if table is None else table
    row = table.get(drawing)
    rec = E.INDEX.records.get(part) or {}
    if row is None or row[0] != part:
        record(drawing, part)                                   # first time seen: assume it's in step
        return None
    rev = rec.get("revision") or "A-01"
    if rev != row[1]:
        return f"⚠ Part is now {rev} - update drawing"
    if row[2] and rec.get("hash") and rec.get("hash") != row[2]:
        return "⚠ Part changed - check drawing"
    return None


def _move_links(pairs):
    try:
        _table()
        with E.db() as c:
            for old, new in pairs:
                c.execute("UPDATE drawing_links SET drawing=? WHERE drawing=?", (new, old))
                c.execute("UPDATE drawing_links SET part=? WHERE part=?", (new, old))
    except Exception:                                           # noqa: BLE001
        pass


def check_in(filename, *a, **k):
    res = ORIG["check_in"](filename, *a, **k)
    try:
        if res and res[0] == "ok" and is_drawing(filename):
            E.INDEX.reload_records()
            record(filename)
    except Exception:                                           # noqa: BLE001
        log_error("drawing link")
    return res


def up_issue(filename, new_rev, comment="", *a, **k):
    res = ORIG["up_issue"](filename, new_rev, comment, *a, **k)
    try:
        if (res and res[0] == "ok" and not getattr(_GUARD, "on", False) and on(SET_FOLLOW, False)
                and os.path.splitext(filename)[1].lower() in E.CAD_MODEL_EXTS):
            notes = []
            _GUARD.on = True
            try:
                for d in drawings_of(filename):
                    lock = (E.get_record(d) or {}).get("locked_by")
                    if lock:
                        notes.append(f"{d} is checked out by {lock} - up-issue it when it's checked in")
                        continue
                    r = ORIG["up_issue"](d, new_rev, f"Follows {filename} {new_rev}. {comment}".strip())
                    if r and r[0] == "ok":
                        E.INDEX.reload_records()
                        record(d, filename)
                        notes.append(f"drawing {d} is now {new_rev} too")
                    else:
                        notes.append(f"{d}: {r[1] if r else 'not up-issued'}")
            finally:
                _GUARD.on = False
            if notes:
                text = "; ".join(notes)
                return res[0], res[1] + "  " + text[:1].upper() + text[1:] + ".", *res[2:]
    except Exception:                                           # noqa: BLE001
        log_error("drawing follows part")
    return res


def mark_up_to_date(app, drawing):
    record(drawing)
    E.log_audit(drawing, "Drawing Checked", "", f"Marked up to date with {part_of(drawing)}")
    app.log(f"{drawing} marked as up to date with its part.", "ok")
    app.refresh_grid()


def _decorate_grid(app):
    if not on(SET_FLAG, True):
        return
    cols = [c[0] for c in getattr(app, "GRID_COLS", ())]
    if "status" not in cols:
        return
    si = cols.index("status")
    table = None
    grid = app.grid

    def walk(parent):
        nonlocal table
        for iid in grid.get_children(parent):
            name = (app.row_map.get(iid) or (None, None))[1]
            if name and is_drawing(name):
                if table is None:
                    table = links()
                flag = drawing_flag(name, table)
                if flag:
                    vals = list(grid.item(iid, "values") or ())
                    if len(vals) > si:
                        vals[si] = flag
                        grid.item(iid, values=vals)
            walk(iid)
    walk("")


# =============================================================================
# 5. Settings dialog, menus
# =============================================================================
def settings_dialog(app):
    import tkinter as tk
    from tkinter import ttk
    if not app.ensure_admin():
        return
    S = app.S
    dlg = E.ThemedDialog(app, "Assemblies & drawings", "Assemblies & drawings",
                         "Applies to everyone using this vault.", width=520)
    vs = {}
    for key, default, text, hint in (
            (SET_REFS, True, "Update assemblies and drawings when a part is renamed or numbered",
             "Their links are changed to the new file name; the old version of each is kept in the history."),
            (SET_FOLLOW, False, "Drawings follow their part's revision",
             "Up-issuing a part also up-issues its drawing to the same revision. Leave this off to revise drawings "
             "on their own (e.g. a drawing change with no change to the part)."),
            (SET_FLAG, True, "Flag drawings when their part changes",
             "Shows ⚠ on a drawing until it's checked in again after its part changed, or marked up to date.")):
        vs[key] = tk.BooleanVar(value=on(key, default))
        ttk.Checkbutton(dlg.body, text=text, variable=vs[key], style="Dialog.TCheckbutton").pack(anchor="w",
                                                                                                pady=(S(6), 0))
        ttk.Label(dlg.body, text=hint, style="CardMuted.TLabel", wraplength=S(470), justify="left").pack(
            anchor="w", padx=(S(24), 0))
    err = ttk.Label(dlg.body, text="", style="CardDanger.TLabel")
    err.pack(anchor="w", pady=(S(6), 0))

    def ok():
        guard = E._admin_guard()
        if guard:
            err.configure(text=guard[1])
            return E.INVALID
        for key, v in vs.items():
            E.set_setting(key, "1" if v.get() else "0")
        E.log_audit("[settings]", "Assemblies & Drawings Changed", "",
                    ", ".join(f"{k}={'on' if v.get() else 'off'}" for k, v in vs.items()))
        return True
    dlg.validate = ok
    dlg.add_buttons("Save", "Accent.TButton")
    if dlg.show():
        app.log("Assemblies & drawings settings saved.", "ok")
        app.refresh_grid()


def server_status(app):
    from tkinter import messagebox
    url, token = server_config()
    if not url:
        messagebox.showinfo(E.APP_NAME, "This vault isn't using the Atelier server - the database is the file "
                            f"{E.DB_PATH}.\n\nSee the manual, 'Server (Raspberry Pi)', to set one up.", parent=app.root)
        return
    try:
        h = Remote(url, token).health()
        messagebox.showinfo(E.APP_NAME, f"Atelier server {h.get('version')} at {url}\n\nRunning for "
                            f"{h.get('uptime_s', 0) // 3600} h · {h.get('sessions')} open sessions\nLast database "
                            f"backup: {h.get('last_backup') or 'not yet (made nightly)'}", parent=app.root)
    except Exception as e:                                      # noqa: BLE001
        messagebox.showerror(E.APP_NAME, f"Can't reach the Atelier server at {url}:\n{e}", parent=app.root)


def _extra(app, f):
    items = list(ORIG["extra"](app, f))
    try:
        if f and is_drawing(f) and part_of(f) and drawing_flag(f):
            items.append(("✓  Drawing is up to date with its part", lambda: mark_up_to_date(app, f), True))
    except Exception:                                           # noqa: BLE001
        pass
    return items


def _hook_app():
    cls = getattr(E, "AtelierApp", None)
    if cls is None or getattr(cls, "_atl_v29", False):
        return
    cls._atl_v29 = True
    o_grid = cls.refresh_grid

    def refresh_grid(self, *a, **k):
        res = o_grid(self, *a, **k)
        try:
            _decorate_grid(self)
        except Exception:                                       # noqa: BLE001
            log_error("drawing flags")
        return res
    cls.refresh_grid = refresh_grid
    o_ident = getattr(cls, "update_identity", None)
    if o_ident:
        def update_identity(self, *a, **k):
            res = o_ident(self, *a, **k)
            try:
                if E.is_admin():
                    self.account_menu.add_command(label="🔗  Assemblies & drawings…", command=lambda: settings_dialog(self))
                self.account_menu.add_command(label="🖧  Server status…", command=lambda: server_status(self))
            except Exception:                                   # noqa: BLE001
                pass
            return res
        cls.update_identity = update_identity


# =============================================================================
# 6. SolidCraft plugin bridge
# =============================================================================
def bridge_dir():
    return os.path.join(E.LOCAL_WORKSPACE, "_Atelier", "plugin")


def _vault_name(path_or_name):
    n = re.split(r"[\\/]", str(path_or_name or ""))[-1]
    if n in E.INDEX.files or (E.get_record(n) or {}):
        return n
    return None


def handle_request(req):
    action = (req.get("action") or "").lower()
    target = req.get("file") or ""
    if action == "ping":
        return {"ok": True, "message": f"Atelier PDM {VERSION}", "user": user()}
    if action == "save_to_vault":
        src = target
        folder = req.get("folder") or setting("plugin_default_folder", "")
        if not src or not os.path.isfile(src):
            return {"ok": False, "message": "Save the design first, then try again."}
        if not folder:
            return {"ok": False, "message": "No vault folder given. Use the Atelier panel's Save to Vault, or set a "
                                            "default folder (setting plugin_default_folder)."}
        res = E.add_files_to_vault([src], folder, "Saved from the SolidCraft plugin")
        ok = any(l == "ok" for l, _m in res)
        return {"ok": ok, "message": " ".join(m for _l, m in res)}
    name = _vault_name(target)
    if not name:
        return {"ok": False, "message": f"'{os.path.basename(target)}' isn't in the vault. Use Save to Vault first."}
    if action == "status":
        rec = E.get_record(name) or {}
        st = E.file_state(name)[1]
        fl = sys.modules.get("atelier_flow")
        life = fl.badge(name) if fl is not None else ""
        return {"ok": True, "file": name, "status": st, "revision": rec.get("revision") or "", "state": life,
                "locked_by": rec.get("locked_by") or "", "message": f"{name}  {rec.get('revision') or ''}  {st}  {life}"}
    fn = {"check_out": E.check_out, "check_in": E.check_in, "undo": E.undo_check_out}.get(action)
    if fn is None:
        return {"ok": False, "message": f"Unknown request '{action}'."}
    args = (name,) if action == "undo" else (name, req.get("comment") or "From the SolidCraft plugin")
    res = fn(*args)
    try:
        E.INDEX.refresh()
    except Exception:                                           # noqa: BLE001
        pass
    return {"ok": res[0] == "ok", "message": res[1], "file": name}


def bridge_once():
    base = bridge_dir()
    req_dir, resp_dir = os.path.join(base, "requests"), os.path.join(base, "responses")
    try:
        names = sorted(n for n in os.listdir(req_dir) if n.lower().endswith(".json"))
    except OSError:
        return 0
    done = 0
    for n in names:
        src = os.path.join(req_dir, n)
        claim = src + f".{os.getpid()}.working"
        try:
            os.replace(src, claim)                              # only one Atelier window handles each request
        except OSError:
            continue
        rid = os.path.splitext(n)[0]
        try:
            with open(claim, encoding="utf-8") as f:
                req = json.load(f)
            rid = str(req.get("id") or rid)
            out = handle_request(req)
        except Exception as e:                                  # noqa: BLE001
            log_error("plugin request")
            out = {"ok": False, "message": f"Atelier couldn't do that: {e}"}
        out.update(id=rid, answered=time.strftime("%Y-%m-%d %H:%M:%S"))
        os.makedirs(resp_dir, exist_ok=True)
        tmp = os.path.join(resp_dir, f"{rid}.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=1)
        os.replace(tmp, os.path.join(resp_dir, f"{rid}.json"))
        try:
            os.remove(claim)
        except OSError:
            pass
        done += 1
    return done


def _bridge_loop():
    for d in ("requests", "responses"):
        try:
            os.makedirs(os.path.join(bridge_dir(), d), exist_ok=True)
        except OSError:
            pass
    while True:
        try:
            bridge_once()
        except Exception:                                       # noqa: BLE001
            log_error("plugin bridge")
        time.sleep(1.0)


# =============================================================================
# Install
# =============================================================================
def install(module):
    global E
    E = module
    if getattr(module, "_v29_installed", False):
        return
    module._v29_installed = True
    for n in ("atelier_polish", "atelier_guide", "atelier_flow", "atelier_access", "atelier_states",
              "atelier_layout"):
        mm = sys.modules.get(n)
        if mm is not None:
            mm.VERSION = VERSION
    try:
        install_server_mode()
    except Exception:                                           # noqa: BLE001
        log_error("server mode")
    for name, key, fn in (("_rename_group", "rename_group", _rename_group), ("check_in", "check_in", check_in),
                          ("up_issue", "up_issue", up_issue)):
        if hasattr(module, name):
            ORIG[key] = getattr(module, name)
            setattr(module, name, fn)
    G = sys.modules.get("atelier_guide")
    if G is not None and "extra" in getattr(G, "ORIG", {}):
        ORIG["extra"] = G.ORIG["extra"]
        G.ORIG["extra"] = _extra
    else:
        P = sys.modules.get("atelier_pro")
        if P is not None and hasattr(P, "extra_menu_items"):
            ORIG["extra"] = P.extra_menu_items
            P.extra_menu_items = _extra
    for fn in (_hook_dialogs, _hook_app):
        try:
            fn()
        except Exception:                                       # noqa: BLE001
            log_error("2.9 install")
    if not os.environ.get("ATELIER_NO_BRIDGE"):
        threading.Thread(target=_bridge_loop, daemon=True, name="atelier-plugin-bridge").start()
