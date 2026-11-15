"""
Atelier PDM - release control, BOM, safety nets, backups, finding, getting started (atelier_pro.py)  2.8.0
=========================================================================================================

1  Where used & bill of materials
     Right-click a part › Where used… lists every assembly that uses it; right-click an assembly › Bill of
     materials… shows its parts (multi-level, with totals) and exports a CSV. Up-issuing a part that's used in
     assemblies warns you first.

2  Release control
     Each file has a state: In work › Awaiting approval › Released (› Obsolete). Request release when a design is
     finished; an administrator approves (a release copy goes into the history) or returns it with a note.
     Released and awaiting-approval files can't be checked out, renamed or deleted - start a new revision
     (Revise) to change a released part. Settings: approval required (on), approver must be someone else (off).

3  Safety nets
     Closing Atelier with files checked out asks what to do. Check in all… checks in everything you've changed
     (unchanged files are simply released). "Notify me when it's free" on a file someone else has checked out;
     you're also told when your release requests are approved or returned, when an administrator releases one
     of your locks, and (administrators) when parts are waiting for approval.

4  Backups & integrity (administrators)
     Automatic backups of the database - or the whole vault - on a schedule, kept for a set number of copies,
     to a folder of your choice (another drive is best). Restore a single file, the database, or the whole vault
     into a new folder. Integrity check compares the database with the files and offers safe fixes.

5  Finding things
     Quick filters above the file list (Checked out by me, Changed this week, Released, Watching…) - they search
     every folder. Gallery shows the files as thumbnails.

6  Getting started
     A short guided tour of the window (offered on first start, and in Help), and a sample project with numbered
     practice parts.

Installed by atelier_cam.install(). Every part falls back quietly rather than stop the program.
"""
import csv
import io
import json
import os
import queue
import re
import shutil
import sqlite3
import struct
import sys
import threading
import time
import zipfile
from collections import Counter

E = None
PRO_VERSION = "2.8.2"

IN_WORK, PENDING, RELEASED, OBSOLETE = "In work", "Awaiting approval", "Released", "Obsolete"
LOCKED_STATES = (PENDING, RELEASED, OBSOLETE)
FILTERS = ["All files", "Checked out by me", "Locked by others", "Changed this week", "In work",
           "Awaiting approval", "Released", "Obsolete", "Watching"]
BACKUP_DIR_NAME = "_Backups"
ORIG = {}
_DEP_CACHE = {}
_UI = {"app": None, "queue": queue.Queue(), "snapshot": None}


def now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def user():
    return getattr(E, "USER_NAME", "") or ""


def _admin_ok():
    g = getattr(E, "_admin_guard", None)
    try:
        return g() is None if g else bool(E.is_admin())
    except Exception:                                           # noqa: BLE001
        return False


def setting(key, default=None):
    try:
        v = E.get_setting(key, None)
    except Exception:                                           # noqa: BLE001
        v = None
    return default if v is None else v


def set_setting(key, value):
    E.set_setting(key, str(value))


def local_settings():
    try:
        return E.load_settings() or {}
    except Exception:                                           # noqa: BLE001
        return {}


def save_local(**kw):
    s = local_settings()
    s.update(kw)
    try:
        E.save_settings(s)
    except Exception:                                           # noqa: BLE001
        pass


_TABLES = {"ok": False}


def _ensure_tables():
    if _TABLES["ok"]:
        return
    with E.db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS lifecycle (
                        filename TEXT PRIMARY KEY, state TEXT, revision TEXT, requested_by TEXT,
                        requested_at TEXT, decided_by TEXT, decided_at TEXT, note TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS watches (
                        username TEXT, filename TEXT, created TEXT, PRIMARY KEY (username, filename))""")
    _TABLES["ok"] = True


# =============================================================================
# 1  Where used & bill of materials
# =============================================================================
def _vault_names():
    """lower-case name -> vault file name, for parts/assemblies/drawings that can be referenced."""
    exts = set(getattr(E, "PRIMARY_EXTS", ())) | set(getattr(E, "CAD_MODEL_EXTS", ()))
    return {n.lower(): n for n, i in E.INDEX.files.items() if i.ext in exts}


def _read_text(path):
    """Text of a file for counting references (decrypted if the vault is encrypted)."""
    secure = sys.modules.get("atelier_secure")

    def read(p):
        if zipfile.is_zipfile(p):
            with zipfile.ZipFile(p) as z:
                names = [n for n in z.namelist() if n.lower().endswith((".xml", ".json", ".txt"))]
                return " ".join(z.read(n).decode("utf-8", "ignore") for n in names[:8])
        with open(p, "rb") as f:
            return f.read(64 * 1024 * 1024).decode("latin-1")
    try:
        if secure is not None and secure.is_enabled() and secure.is_encrypted(path):
            with secure.plain_copy(path) as p:
                return read(p)
        return read(path)
    except Exception:                                           # noqa: BLE001
        return ""


def references(name):
    """Counter {vault file name: times referenced} for the files an assembly (or drawing) uses."""
    info = E.INDEX.files.get(name)
    if not info or info.ext not in E.CAD_MODEL_EXTS | set(getattr(E, "DRAWING_EXTS", ())):
        return Counter()
    try:
        key = (info.path, os.path.getmtime(info.path))
    except OSError:
        return Counter()
    hit = _DEP_CACHE.get(info.path)
    if hit and hit[0] == key:
        return hit[1]
    try:
        deps = [str(d).lower() for d in (E.get_dependencies(info.path) or [])]
    except Exception:                                           # noqa: BLE001
        deps = []
    out = Counter()
    if deps:
        models = {n.lower(): n for n, i in E.INDEX.files.items() if i.ext in E.CAD_MODEL_EXTS and n != name}
        # a reference can come back with other text around it, so look for vault names inside each one
        found = [n for low, n in models.items() if any(low in d for d in deps)]
        if found:
            text = _read_text(info.path).lower()
            for n in found:
                out[n] = max(1, text.count(n.lower())) if text else 1
    _DEP_CACHE[info.path] = (key, out)
    return out


def where_used(name):
    """{assembly: qty} for every vault file that references name."""
    out = {}
    for n, info in list(E.INDEX.files.items()):
        if n == name or info.ext not in E.CAD_MODEL_EXTS:
            continue
        q = references(n).get(name)
        if q:
            out[n] = q
    return out


def bom(name, max_depth=25):
    """Indented bill of materials: [(level, file, qty, path_of_parents)]. Cycles are cut and marked."""
    rows = []

    def walk(n, level, chain, mult):
        for child, q in sorted(references(n).items(), key=lambda kv: kv[0].lower()):
            cyc = child in chain
            rows.append({"level": level, "file": child, "qty": q, "total": q * mult, "cycle": cyc})
            if not cyc and level < max_depth:
                walk(child, level + 1, chain | {child}, q * mult)
    walk(name, 1, {name}, 1)
    return rows


def bom_totals(rows):
    tot = Counter()
    for r in rows:
        tot[r["file"]] += r["total"]
    return tot


def _info_row(name):
    rec = E.INDEX.records.get(name) or {}
    pp = E.parse_pn(os.path.splitext(name)[0])
    return {"pn": rec.get("part_number") or (pp[3] if pp else ""), "part": E.display_part_name(name, rec),
            "desc": rec.get("description") or "", "rev": rec.get("revision") or "A-01",
            "state": state_of(name)["state"]}


def bom_csv(name, rows):
    out = io.StringIO()
    w = csv.writer(out)
    top = _info_row(name)
    w.writerow([f"Bill of materials: {top['pn']} {top['part']} rev {top['rev']}", f"made {now()} by {user()}"])
    w.writerow([])
    w.writerow(["Level", "Part number", "Part name", "Description", "Rev", "State", "Qty", "Total qty", "File"])
    for r in rows:
        i = _info_row(r["file"])
        w.writerow([r["level"], i["pn"], ("  " * (r["level"] - 1)) + i["part"], i["desc"], i["rev"], i["state"],
                    r["qty"], r["total"], r["file"] + (" (circular reference)" if r["cycle"] else "")])
    w.writerow([])
    w.writerow(["Totals"])
    w.writerow(["Part number", "Part name", "Rev", "Total qty", "File"])
    for f, q in sorted(bom_totals(rows).items(), key=lambda kv: _info_row(kv[0])["pn"] or kv[0]):
        i = _info_row(f)
        w.writerow([i["pn"], i["part"], i["rev"], q, f])
    return out.getvalue()


# =============================================================================
# 2  Release control
# =============================================================================
def approval_required():
    return setting("release_approval", "1") == "1"


def different_approver():
    return setting("release_other_approver", "0") == "1"


def state_of(name):
    try:
        _ensure_tables()
        with E.db() as c:
            c.row_factory = sqlite3.Row
            r = c.execute("SELECT * FROM lifecycle WHERE filename=?", (name,)).fetchone()
        if r:
            return dict(r)
    except sqlite3.Error:
        pass
    return {"filename": name, "state": IN_WORK}


def all_states():
    try:
        _ensure_tables()
        with E.db() as c:
            return {r[0]: r[1] for r in c.execute("SELECT filename, state FROM lifecycle").fetchall()}
    except sqlite3.Error:
        return {}


def _set_state(name, state, **fields):
    _ensure_tables()
    rec = E.INDEX.records.get(name) or {}
    row = state_of(name)
    row.update(fields)
    row.update(filename=name, state=state, revision=fields.get("revision", rec.get("revision") or "A-01"))
    with E.db() as c:
        c.execute("INSERT OR REPLACE INTO lifecycle (filename, state, revision, requested_by, requested_at, "
                  "decided_by, decided_at, note) VALUES (?,?,?,?,?,?,?,?)",
                  (name, state, row.get("revision"), row.get("requested_by"), row.get("requested_at"),
                   row.get("decided_by"), row.get("decided_at"), row.get("note")))


def _check_ready(name):
    if name not in E.INDEX.files:
        return f"'{name}' isn't in the vault yet - check it in first."
    if E.INDEX.files[name].is_library:
        return "Standard Library parts aren't released."
    lock = (E.INDEX.records.get(name) or {}).get("locked_by")
    if lock:
        return f"'{name}' is checked out by {lock}. It must be checked in first."
    return None


def request_release(name, note=""):
    problem = _check_ready(name)
    if problem:
        return "error", problem
    st = state_of(name)["state"]
    if st != IN_WORK:
        return "error", f"'{name}' is {st.lower()}."
    rev = (E.INDEX.records.get(name) or {}).get("revision") or "A-01"
    if not approval_required():
        return _release(name, note or "Released (no approval required)", requester=user())
    _set_state(name, PENDING, requested_by=user(), requested_at=now(), decided_by=None, decided_at=None,
               note=note, revision=rev)
    E.log_audit(name, "Release Requested", rev, note)
    watch(name)
    return "ok", f"Release of '{name}' rev {rev} requested. An administrator will approve it."


def _release(name, note, requester=None):
    info = E.INDEX.files[name]
    rev = (E.INDEX.records.get(name) or {}).get("revision") or "A-01"
    try:
        keep_release_copy(info.path, name, rev)
    except Exception as e:                                      # noqa: BLE001
        return "error", f"Couldn't keep a release copy: {e}"
    row = state_of(name)
    _set_state(name, RELEASED, requested_by=requester or row.get("requested_by"),
               requested_at=row.get("requested_at") or now(), decided_by=user(), decided_at=now(), note=note,
               revision=rev)
    E.log_audit(name, "Released", rev, note)
    return "ok", f"'{name}' rev {rev} is released."


def keep_release_copy(path, name, rev):
    """Release copy in the history: via the engine when it can, otherwise <stem>_<rev>_RELEASED_<time><ext>."""
    try:
        return E.archive_to_history(path, name, rev, release=True)
    except TypeError:
        pass
    stem, ext = os.path.splitext(name)
    dst = os.path.join(E.HISTORY_DIR, f"{stem}_{rev}_RELEASED_{time.strftime('%Y%m%d_%H%M%S')}{ext}")
    os.makedirs(E.HISTORY_DIR, exist_ok=True)
    shutil.copy2(path, dst)                                     # raw copy: stays encrypted if the vault is
    return dst


def approve(name, note=""):
    if not _admin_ok():
        return "error", "Only an administrator can approve a release."
    row = state_of(name)
    if row["state"] != PENDING:
        return "error", f"'{name}' isn't awaiting approval."
    if different_approver() and (row.get("requested_by") or "").lower() == user().lower():
        return "error", "Someone other than the person who asked must approve it (Release settings)."
    problem = _check_ready(name)
    if problem:
        return "error", problem
    return _release(name, note or "Approved")


def reject(name, note):
    if not _admin_ok():
        return "error", "Only an administrator can return a release request."
    row = state_of(name)
    if row["state"] != PENDING:
        return "error", f"'{name}' isn't awaiting approval."
    if not (note or "").strip():
        return "error", "Say why it's being returned, so they know what to change."
    _set_state(name, IN_WORK, decided_by=user(), decided_at=now(), note=note)
    E.log_audit(name, "Release Returned", row.get("revision") or "", note)
    return "ok", f"'{name}' returned to In work."


def withdraw(name):
    row = state_of(name)
    if row["state"] != PENDING:
        return "error", f"'{name}' isn't awaiting approval."
    if (row.get("requested_by") or "").lower() != user().lower() and not _admin_ok():
        return "error", "Only the person who asked, or an administrator, can withdraw it."
    _set_state(name, IN_WORK, decided_by=user(), decided_at=now(), note="Request withdrawn")
    E.log_audit(name, "Release Request Withdrawn", row.get("revision") or "", "")
    return "ok", f"Release request for '{name}' withdrawn."


def make_obsolete(name, note):
    if not _admin_ok():
        return "error", "Only an administrator can make a file obsolete."
    problem = _check_ready(name)
    if problem:
        return "error", problem
    users_of = where_used(name)
    _set_state(name, OBSOLETE, decided_by=user(), decided_at=now(), note=note)
    E.log_audit(name, "Made Obsolete", "", note)
    extra = f" It's still used in {len(users_of)} assembl{'y' if len(users_of) == 1 else 'ies'}." if users_of else ""
    return "ok", f"'{name}' is obsolete - it can't be checked out or changed.{extra}"


def reinstate(name):
    if not _admin_ok():
        return "error", "Only an administrator can reinstate a file."
    if state_of(name)["state"] != OBSOLETE:
        return "error", f"'{name}' isn't obsolete."
    _set_state(name, IN_WORK, decided_by=user(), decided_at=now(), note="Reinstated")
    E.log_audit(name, "Reinstated", "", "")
    return "ok", f"'{name}' is back In work."


def pending():
    try:
        _ensure_tables()
        with E.db() as c:
            c.row_factory = sqlite3.Row
            return [dict(r) for r in c.execute("SELECT * FROM lifecycle WHERE state=? ORDER BY requested_at",
                                               (PENDING,)).fetchall()]
    except sqlite3.Error:
        return []


def recent_decisions(limit=50):
    try:
        _ensure_tables()
        with E.db() as c:
            c.row_factory = sqlite3.Row
            return [dict(r) for r in c.execute("SELECT * FROM lifecycle WHERE decided_at IS NOT NULL "
                                               "ORDER BY decided_at DESC LIMIT ?", (limit,)).fetchall()]
    except sqlite3.Error:
        return []


def _blocked(name, action):
    st = state_of(name)["state"]
    info = E.INDEX.files.get(name)
    if st not in LOCKED_STATES or (info and info.is_library):
        return None
    rev = (E.INDEX.records.get(name) or {}).get("revision") or ""
    if st == RELEASED:
        return (f"'{name}' is released (rev {rev}), so it can't be {action}. To change it, start a new revision: "
                "right-click › Revise.")
    if st == PENDING:
        return f"'{name}' is awaiting approval, so it can't be {action}. Withdraw the request first."
    return f"'{name}' is obsolete, so it can't be {action}. An administrator can reinstate it."


def _wrap_engine(m):
    if getattr(m, "_pro_wrapped", False):
        return
    m._pro_wrapped = True
    for n in ("check_out", "up_issue", "delete_component", "rename_component", "_rename_group"):
        if hasattr(m, n):
            ORIG[n] = getattr(m, n)

    if "check_out" in ORIG:
        def check_out(filename, comment=""):
            b = _blocked(filename, "checked out")
            return ("error", b) if b else ORIG["check_out"](filename, comment)
        m.check_out = check_out

    if "up_issue" in ORIG:
        def up_issue(filename, new_rev, comment=""):
            st = state_of(filename)["state"]
            if st in (PENDING, OBSOLETE):
                return "error", _blocked(filename, "up-issued")
            res = ORIG["up_issue"](filename, new_rev, comment)
            if res and res[0] == "ok" and st == RELEASED:
                _set_state(filename, IN_WORK, decided_by=user(), decided_at=now(), note=f"Revised to {new_rev}",
                           revision=new_rev)
                return "ok", res[1] + " It's In work again until the new revision is released."
            return res
        m.up_issue = up_issue

    if "delete_component" in ORIG:
        def delete_component(filename):
            st = state_of(filename)["state"]
            if st in (PENDING, RELEASED):
                return "error", (f"'{filename}' is {st.lower()}, so it can't be deleted. An administrator can "
                                 "make it obsolete instead.")
            res = ORIG["delete_component"](filename)
            if res and res[0] == "ok":
                with E.db() as c:
                    c.execute("DELETE FROM lifecycle WHERE filename=?", (filename,))
            return res
        m.delete_component = delete_component

    if "rename_component" in ORIG:
        def rename_component(filename, new_part_name):
            b = _blocked(filename, "renamed")
            return ("error", b, filename) if b else ORIG["rename_component"](filename, new_part_name)
        m.rename_component = rename_component

    if "_rename_group" in ORIG:
        def _rename_group(path, new_stem, *a, **k):
            old = os.path.basename(path)
            new = new_stem + os.path.splitext(old)[1]
            res = ORIG["_rename_group"](path, new_stem, *a, **k)
            if res and res[0] == "ok" and old != new:
                try:
                    with E.db() as c:
                        c.execute("UPDATE lifecycle SET filename=? WHERE filename=?", (new, old))
                        c.execute("UPDATE watches SET filename=? WHERE filename=?", (new, old))
                except sqlite3.Error:
                    pass
            return res
        m._rename_group = _rename_group


# =============================================================================
# 3  Safety nets: check in all, watches, notifications
# =============================================================================
def my_checkouts():
    return sorted(n for n, r in E.INDEX.records.items()
                  if (r.get("locked_by") or "") == user() and not r.get("is_library"))


def changed_locally(name):
    local = E.sandbox_path(name)
    if not os.path.exists(local):
        return False
    info = E.INDEX.files.get(name)
    if not info:
        return True                                             # new file, not in the vault yet
    try:
        return E.get_file_hash(local) != ((E.INDEX.records.get(name) or {}).get("hash") or E.get_file_hash(info.path))
    except Exception:                                           # noqa: BLE001
        return True


def check_in_all(comment):
    """Checks in every changed file you have checked out; unchanged ones are released. [(level, msg)]."""
    out = []
    for n in my_checkouts():
        try:
            if changed_locally(n):
                out.append(E.check_in(n, comment))
            elif os.path.exists(E.sandbox_path(n)) or n in E.INDEX.files:
                res = E.undo_check_out(n)
                out.append((res[0], f"'{n}' unchanged - lock released." if res[0] == "ok" else res[1]))
        except Exception as e:                                  # noqa: BLE001
            out.append(("error", f"'{n}': {e}"))
    return out


def watch(name):
    try:
        _ensure_tables()
        with E.db() as c:
            c.execute("INSERT OR IGNORE INTO watches (username, filename, created) VALUES (?,?,?)",
                      (user(), name, now()))
    except sqlite3.Error:
        pass


def unwatch(name):
    try:
        _ensure_tables()
        with E.db() as c:
            c.execute("DELETE FROM watches WHERE username=? AND filename=?", (user(), name))
    except sqlite3.Error:
        pass


def watching():
    try:
        _ensure_tables()
        with E.db() as c:
            return {r[0] for r in c.execute("SELECT filename FROM watches WHERE username=?", (user(),)).fetchall()}
    except sqlite3.Error:
        return set()


def snapshot():
    """What notifications are worked out from."""
    try:
        _ensure_tables()
        with E.db() as c:
            locks = {r[0]: r[1] or "" for r in c.execute("SELECT filename, locked_by FROM files").fetchall()}
            life = {r[0]: (r[1], r[2] or "", r[3] or "", r[4] or "") for r in c.execute(
                "SELECT filename, state, requested_by, decided_by, note FROM lifecycle").fetchall()}
    except sqlite3.Error:
        return None
    return {"locks": locks, "life": life, "watch": watching(),
            "pending": sum(1 for v in life.values() if v[0] == PENDING)}


def notifications(old, new):
    """[(text, file or None, level)] for what changed between two snapshots."""
    if not old or not new:
        return []
    out, me = [], user().lower()
    for f in new["watch"]:
        was, is_ = old["locks"].get(f, ""), new["locks"].get(f, "")
        if was and not is_ and was.lower() != me:
            out.append((f"{f} is now available - {was} has checked it in.", f, "ok"))
    for f, (st, req, dec, note) in new["life"].items():
        ost = old["life"].get(f, (None, "", "", ""))[0]
        if st == ost or req.lower() != me or dec.lower() == me:
            continue
        if ost == PENDING and st == RELEASED:
            out.append((f"{f} has been approved and released by {dec}.", f, "ok"))
        elif ost == PENDING and st == IN_WORK:
            out.append((f"{f} was returned by {dec}: {note}", f, "warn"))
    if _admin_ok() and new["pending"] > old["pending"]:
        n = new["pending"]
        out.append((f"{n} file{'s' if n != 1 else ''} awaiting your approval.", None, "info"))
    return out


def lock_lost(old, new, local_has):
    """Files I had checked out whose lock was released by someone else (e.g. Break lock)."""
    me = user().lower()
    return [f for f, was in old["locks"].items()
            if was.lower() == me and f in new["locks"] and not new["locks"][f] and local_has(f)]


# =============================================================================
# 4  Backups & integrity
# =============================================================================
def backup_dir():
    d = setting("backup_folder", "") or ""
    return d if d else os.path.join(E.VAULT_DIR, BACKUP_DIR_NAME)


def backup_mode():
    return setting("backup_mode", "full")


def backup_days():
    try:
        return max(0.0, float(setting("backup_every_days", "1")))
    except ValueError:
        return 1.0


def backup_keep():
    try:
        return max(1, int(float(setting("backup_keep", "14"))))
    except ValueError:
        return 14


def last_backup():
    return setting("last_backup", "") or ""


def backup_due():
    if backup_days() <= 0:
        return False
    lb = last_backup()
    if not lb:
        return True
    try:
        t = time.mktime(time.strptime(lb, "%Y-%m-%d %H:%M:%S"))
    except ValueError:
        return True
    return time.time() - t >= backup_days() * 86400 - 600


def _claim_backup():
    """Only one PC backs up at a time."""
    run = setting("backup_running", "") or ""
    if run:
        try:
            t = time.mktime(time.strptime(run.split("|")[0], "%Y-%m-%d %H:%M:%S"))
            if time.time() - t < 3 * 3600:
                return False
        except ValueError:
            pass
    set_setting("backup_running", f"{now()}|{getattr(E, 'HOST_NAME', '')}")
    return True


def _skip_dirs():
    names = {BACKUP_DIR_NAME, "__pycache__", ".git"}
    return names


def run_backup(mode=None, progress=None, force=False):
    """Makes a backup zip. Returns its path. progress(i, n, text) is called from this thread."""
    mode = mode or backup_mode()
    if not force and not _claim_backup():
        raise RuntimeError("Another PC is making a backup right now.")
    try:
        out_dir = backup_dir()
        os.makedirs(out_dir, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        final = os.path.join(out_dir, f"Atelier backup {stamp} ({'vault' if mode == 'full' else 'database'}).zip")
        part = final + ".partial"
        tmp_db = os.path.join(out_dir, f".db_snapshot_{stamp}.db")
        src = sqlite3.connect(E.DB_PATH, timeout=30)
        dst = sqlite3.connect(tmp_db)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        files = []
        if mode == "full":
            skip_abs = os.path.normcase(os.path.abspath(out_dir))
            db_abs = os.path.normcase(os.path.abspath(E.DB_PATH))
            for root, dirs, names in os.walk(E.VAULT_DIR):
                dirs[:] = [d for d in dirs if d not in _skip_dirs()
                           and os.path.normcase(os.path.abspath(os.path.join(root, d))) != skip_abs]
                for n in names:
                    p = os.path.join(root, n)
                    low = n.lower()
                    if low.endswith((".partial", "-journal", ".enc-tmp", "-wal", "-shm")):
                        continue
                    if os.path.normcase(os.path.abspath(p)) == db_abs:
                        continue
                    files.append(p)
        manifest = {"format": "atelier-backup", "version": 1, "created": now(), "by": user(),
                    "host": getattr(E, "HOST_NAME", ""), "mode": mode, "vault": E.VAULT_DIR,
                    "files": len(files), "app": PRO_VERSION}
        with zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as z:
            z.writestr("atelier_backup.json", json.dumps(manifest, indent=2))
            z.write(tmp_db, "database/atelier_index.db")
            for i, p in enumerate(files):
                rel = os.path.relpath(p, E.VAULT_DIR)
                try:
                    z.write(p, "vault/" + rel.replace(os.sep, "/"))
                except OSError:
                    continue
                if progress and (i % 25 == 0 or i == len(files) - 1):
                    progress(i + 1, len(files), rel)
        os.replace(part, final)
        try:
            os.remove(tmp_db)
        except OSError:
            pass
        prune_backups()
        set_setting("last_backup", now())
        E.log_audit("[backup]", "Backup Made", "", f"{os.path.basename(final)} · {len(files)} files")
        return final
    finally:
        if not force:
            set_setting("backup_running", "")
        for p in [f for f in os.listdir(backup_dir()) if f.endswith(".partial") or f.startswith(".db_snapshot_")] \
                if os.path.isdir(backup_dir()) else []:
            pp = os.path.join(backup_dir(), p)
            if time.time() - os.path.getmtime(pp) > 60:
                try:
                    os.remove(pp)
                except OSError:
                    pass


def list_backups():
    d = backup_dir()
    out = []
    try:
        names = sorted((n for n in os.listdir(d) if n.lower().endswith(".zip") and n.startswith("Atelier backup")),
                       reverse=True)
    except OSError:
        return out
    for n in names:
        p = os.path.join(d, n)
        try:
            with zipfile.ZipFile(p) as z:
                man = json.loads(z.read("atelier_backup.json"))
        except (OSError, KeyError, ValueError, zipfile.BadZipFile):
            continue
        man.update(path=p, name=n, size=os.path.getsize(p))
        out.append(man)
    return out


def prune_backups():
    keep = backup_keep()
    for b in list_backups()[keep:]:
        try:
            os.remove(b["path"])
        except OSError:
            pass


def backup_members(path):
    with zipfile.ZipFile(path) as z:
        return sorted(n[6:] for n in z.namelist() if n.startswith("vault/") and not n.endswith("/"))


def restore_file(path, member, dest_dir=None):
    """Extracts one file from a backup into the working folder (decrypting it if needed). Returns its path."""
    dest_dir = dest_dir or os.path.join(E.LOCAL_WORKSPACE, "Restored", os.path.splitext(os.path.basename(path))[0])
    os.makedirs(dest_dir, exist_ok=True)
    out = os.path.join(dest_dir, os.path.basename(member))
    with zipfile.ZipFile(path) as z, z.open("vault/" + member) as src, open(out, "wb") as dst:
        shutil.copyfileobj(src, dst)
    secure = sys.modules.get("atelier_secure")
    if secure is not None and secure.is_encrypted(out):
        try:
            secure.decrypt_file(out, out, secure._need_key())
        except Exception:                                       # noqa: BLE001
            pass
    E.log_audit(member.replace("/", os.sep).split(os.sep)[-1], "Restored From Backup", "",
                f"{os.path.basename(path)} -> {out}")
    return out


def restore_vault_to(path, folder):
    """Unpacks a whole backup into an empty folder - never over the live vault."""
    if os.path.exists(folder) and os.listdir(folder):
        raise ValueError("Choose an empty folder - restoring never overwrites an existing vault.")
    if os.path.normcase(os.path.abspath(folder)).startswith(os.path.normcase(os.path.abspath(E.VAULT_DIR))):
        raise ValueError("Choose a folder outside the current vault.")
    os.makedirs(folder, exist_ok=True)
    with zipfile.ZipFile(path) as z:
        for n in z.namelist():
            if n.startswith("vault/") and not n.endswith("/"):
                target = os.path.join(folder, *n[6:].split("/"))
                if not os.path.abspath(target).startswith(os.path.abspath(folder)):
                    continue                                    # never write outside the folder
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with z.open(n) as s, open(target, "wb") as d:
                    shutil.copyfileobj(s, d)
        with z.open("database/atelier_index.db") as s, open(os.path.join(folder, "atelier_index.db"), "wb") as d:
            shutil.copyfileobj(s, d)
    E.log_audit("[backup]", "Vault Restored To Folder", "", f"{os.path.basename(path)} -> {folder}")
    return folder


def restore_database(path):
    """Replaces the live database with the backup's (the current one is kept beside the backups first)."""
    if not _admin_ok():
        raise PermissionError("Only an administrator can restore the database.")
    keep = os.path.join(backup_dir(), f"database before restore {time.strftime('%Y%m%d_%H%M%S')}.db")
    os.makedirs(backup_dir(), exist_ok=True)
    src = sqlite3.connect(E.DB_PATH, timeout=30)
    dst = sqlite3.connect(keep)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    with zipfile.ZipFile(path) as z:
        data = z.read("database/atelier_index.db")
    tmp = os.path.join(backup_dir(), ".restore.db")
    with open(tmp, "wb") as f:
        f.write(data)
    src = sqlite3.connect(tmp)
    dst = sqlite3.connect(E.DB_PATH, timeout=30)
    try:
        src.backup(dst)                                         # in place, so open connections stay valid
    finally:
        dst.close()
        src.close()
        os.remove(tmp)
    E.log_audit("[backup]", "Database Restored", "", f"from {os.path.basename(path)}; previous kept as "
                f"{os.path.basename(keep)}")
    return keep


def integrity_check(progress=None):
    """[{kind, file, detail, fix}] - fix is None or a key understood by apply_fix."""
    out = []
    try:
        with E.db() as c:
            res = c.execute("PRAGMA integrity_check").fetchone()[0]
            users_ = {r[0].lower() for r in c.execute("SELECT username FROM users").fetchall()}
            life = {r[0] for r in c.execute("SELECT filename FROM lifecycle").fetchall()}
    except sqlite3.Error as e:
        return [{"kind": "Database", "file": "", "detail": f"Can't read the database: {e}", "fix": None}]
    if res != "ok":
        out.append({"kind": "Database", "file": "", "detail": f"Database check reported: {res}. Restore the "
                    "database from a backup.", "fix": None})
    E.INDEX.refresh()
    recs = dict(E.INDEX.records)
    items = list(recs.items())
    for i, (n, r) in enumerate(items):
        if progress and i % 20 == 0:
            progress(i, len(items), n)
        lock = r.get("locked_by") or ""
        if n not in E.INDEX.files:
            if lock and os.path.exists(E.sandbox_path(n)):
                continue                                        # someone's new file, not checked in yet
            out.append({"kind": "Missing file", "file": n, "detail": "In the database but not in the vault folders.",
                        "fix": "drop_record"})
            continue
        if lock and lock.lower() not in users_ and "@" not in lock:
            out.append({"kind": "Unknown lock", "file": n, "detail": f"Checked out by '{lock}', who isn't a user.",
                        "fix": "release_lock"})
        h = r.get("hash") or ""
        if h and not lock:
            try:
                now_h = E.get_file_hash(E.INDEX.files[n].path)
            except Exception:                                   # noqa: BLE001
                now_h = h
            if now_h and now_h != h:
                out.append({"kind": "Changed outside Atelier", "file": n,
                            "detail": "The vault file differs from the last check-in.", "fix": "accept_file"})
    for n, where in getattr(E.INDEX, "duplicates", {}).items():
        out.append({"kind": "Duplicate name", "file": n, "detail": "In " + ", ".join(where) +
                    ". Rename or remove one copy.", "fix": None})
    for n in sorted(life):
        if n not in E.INDEX.files:
            out.append({"kind": "Stale release record", "file": n, "detail": "Release state for a file that's gone.",
                        "fix": "drop_life"})
    if progress:
        progress(len(items), len(items), "")
    return out


def apply_fix(f):
    n, fix = f["file"], f["fix"]
    with E.db() as c:
        if fix == "drop_record":
            c.execute("DELETE FROM files WHERE filename=?", (n,))
        elif fix == "drop_life":
            c.execute("DELETE FROM lifecycle WHERE filename=?", (n,))
        elif fix == "release_lock":
            if not _admin_ok():
                return "error", "Only an administrator can release locks."
            c.execute("UPDATE files SET locked_by=NULL WHERE filename=?", (n,))
        elif fix == "accept_file":
            h = E.get_file_hash(E.INDEX.files[n].path)
            c.execute("UPDATE files SET hash=? WHERE filename=?", (h, n))
        else:
            return "error", "No automatic fix."
    E.log_audit(n or "[integrity]", "Integrity Fix", "", f"{f['kind']}: {fix}")
    return "ok", f"Fixed: {f['kind']} - {n}"


# =============================================================================
# 5  Filters
# =============================================================================
def matches_filter(name, flt, states=None, watched=None):
    if flt in (None, "", FILTERS[0]):
        return True
    rec = E.INDEX.records.get(name) or {}
    lock = rec.get("locked_by") or ""
    st = (states if states is not None else all_states()).get(name, IN_WORK)
    if flt == "Checked out by me":
        return lock == user()
    if flt == "Locked by others":
        return bool(lock) and lock != user()
    if flt == "Changed this week":
        info = E.INDEX.files.get(name)
        t = info.mtime if info else 0
        return time.time() - t < 7 * 86400
    if flt == "Watching":
        return name in (watched if watched is not None else watching())
    return st == flt


# =============================================================================
# 6  Sample project
# =============================================================================
def _stl(tris):
    out = io.BytesIO()
    out.write(b"Atelier PDM sample part".ljust(80, b" ") + struct.pack("<I", len(tris)))
    for a, b, c in tris:
        u = [b[i] - a[i] for i in range(3)]
        v = [c[i] - a[i] for i in range(3)]
        n = (u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0])
        ln = sum(x * x for x in n) ** 0.5 or 1.0
        out.write(struct.pack("<12fH", *(x / ln for x in n), *a, *b, *c, 0))
    return out.getvalue()


def _box(x0, y0, z0, x1, y1, z1):
    v = [(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0), (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)]
    q = [(0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4), (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)]
    return [(v[a], v[b], v[c]) for a, b, c, d in q] + [(v[a], v[c], v[d]) for a, b, c, d in q]


def _tube(cx, cy, z0, z1, r, ri=0.0, n=64):
    import math
    t = []
    p = lambda a, rr, z: (cx + rr * math.cos(a), cy + rr * math.sin(a), z)
    for i in range(n):
        a0, a1 = 2 * math.pi * i / n, 2 * math.pi * (i + 1) / n
        t += [(p(a0, r, z0), p(a1, r, z0), p(a1, r, z1)), (p(a0, r, z0), p(a1, r, z1), p(a0, r, z1))]
        if ri:
            t += [(p(a0, ri, z0), p(a1, ri, z1), p(a1, ri, z0)), (p(a0, ri, z0), p(a0, ri, z1), p(a1, ri, z1))]
            t += [(p(a0, ri, z1), p(a0, r, z1), p(a1, r, z1)), (p(a0, ri, z1), p(a1, r, z1), p(a1, ri, z1))]
            t += [(p(a0, ri, z0), p(a1, r, z0), p(a0, r, z0)), (p(a0, ri, z0), p(a1, ri, z0), p(a1, r, z0))]
        else:
            t += [((cx, cy, z1), p(a0, r, z1), p(a1, r, z1)), ((cx, cy, z0), p(a1, r, z0), p(a0, r, z0))]
    return t


SAMPLES = [
    ("Base Plate", "100 x 60 x 8 mild steel plate", lambda: _box(0, 0, 0, 100, 60, 8)),
    ("Spacer", "Ø20 / Ø10.5 x 15 spacer", lambda: _tube(0, 0, 0, 15, 10, 5.25)),
    ("Angle Bracket", "40 x 40 x 5 angle, 50 long", lambda: _box(0, 0, 0, 50, 40, 5) + _box(0, 0, 5, 50, 5, 40)),
]


def create_sample_project():
    """A numbered project with three practice parts (STL meshes); one is released. Returns (level, msg, folder)."""
    res = E.create_project("Sample Project")
    if res[0] != "ok" or len(res) < 3 or not res[2]:
        return res[0], res[1], None
    folder = res[2]
    parts_rel = next((os.path.join(folder, s) for s in ("Parts", "Part", "Components")
                      if os.path.isdir(os.path.join(E.folder_abs(folder), s))), folder)
    ctx = E.numbering_context(parts_rel) or {}
    prefix = ctx.get("prefix", "00")
    made = []
    for part, desc, shape in SAMPLES:
        try:
            pn = E.reserve_number(prefix, ctx.get("kind") or "P")
        except Exception:                                       # noqa: BLE001
            pn = ""
        name = f"{pn} {part}.stl".strip()
        with open(os.path.join(E.folder_abs(parts_rel), name), "wb") as f:
            f.write(_stl(shape()))
        made.append((name, part, pn, desc))
    E.INDEX.refresh()
    for name, part, pn, desc in made:
        try:
            E.save_metadata(name, part, pn, desc)
        except Exception:                                       # noqa: BLE001
            pass
    if made:
        _set_state(made[0][0], RELEASED, requested_by=user(), requested_at=now(), decided_by=user(),
                   decided_at=now(), note="Sample - released so you can see how released parts behave")
    E.log_audit("[sample]", "Sample Project Created", "", folder)
    return "ok", (f"Created '{os.path.basename(folder)}' with {len(made)} practice parts. "
                  f"'{made[0][1]}' is released, the others are In work."), parts_rel


# =============================================================================
# User interface
# =============================================================================
STATE_COLOURS = {RELEASED: "success", PENDING: "warning", OBSOLETE: "muted_fg"}


def _tk():
    import tkinter as tk
    from tkinter import ttk, messagebox, filedialog
    return tk, ttk, messagebox, filedialog


def _say(app, msg, level="info"):
    try:
        app.log(msg, level)
    except Exception:                                           # noqa: BLE001
        pass


def _report(app, res, refresh=True):
    tk, ttk, mb, fd = _tk()
    level, msg = res[0], res[1]
    _say(app, msg, level)
    if level == "error":
        mb.showerror(E.APP_NAME, msg, parent=app.root)
    if refresh:
        try:
            app.refresh_all()
        except Exception:                                       # noqa: BLE001
            pass


def _ask(app, title, heading, sub, label, required=False, ok="OK", style="Accent.TButton"):
    def submit(v):
        t = v["x"].strip()
        if required and not t:
            return "error", "Please fill this in."
        return t or " "
    r = E.run_form(app, title, heading, sub, [("x", label, False, "")], submit, ok, ok_style=style, width=440)
    return None if r is None else r.strip()


def _tree(parent, cols, height=12, show="headings"):
    tk, ttk, mb, fd = _tk()
    wrap = ttk.Frame(parent, style="Dialog.TFrame")
    wrap.pack(side="top", fill="both", expand=True)
    tv = ttk.Treeview(wrap, columns=[c[0] for c in cols], show=show, height=height, selectmode="browse")
    for key, label, w, stretch in cols:
        tv.heading(key, text=label, anchor="w")
        tv.column(key, width=w, minwidth=40, anchor="w", stretch=stretch)
    sb = ttk.Scrollbar(wrap, orient="vertical", command=tv.yview)
    tv.configure(yscrollcommand=sb.set)
    sb.pack(side="right", fill="y")
    tv.pack(side="left", fill="both", expand=True)
    return tv


def _select_in_app(app, name):
    app.selected = name
    for iid, v in getattr(app, "row_map", {}).items():
        if v[0] == name and not app.grid.parent(iid):
            app.grid.selection_set(iid)
            app.grid.see(iid)
            return True
    return False


# ---- where used / BOM window ---------------------------------------------------------------------------------
def show_structure(app, name, tab="used"):
    tk, ttk, mb, fd = _tk()
    S = app.S
    dlg = E.ThemedDialog(app, "Where used & BOM", "Where used & bill of materials", name, width=860, resizable=True)
    nb = ttk.Notebook(dlg.body)
    nb.pack(fill="both", expand=True)
    p1 = ttk.Frame(nb, style="Dialog.TFrame", padding=(0, S(8), 0, 0))
    p2 = ttk.Frame(nb, style="Dialog.TFrame", padding=(0, S(8), 0, 0))
    nb.add(p1, text="  Where used  ")
    nb.add(p2, text="  Bill of materials  ")
    used = where_used(name)
    ttk.Label(p1, text=(f"Used in {len(used)} assembl{'y' if len(used) == 1 else 'ies'}." if used else
                        "Not used in any assembly in the vault."), style="CardMuted.TLabel").pack(anchor="w")
    t1 = _tree(p1, (("pn", "Part number", 110, False), ("part", "Assembly", 220, True), ("rev", "Rev", 60, False),
                    ("state", "State", 120, False), ("qty", "Qty", 60, False), ("file", "File", 240, True)), 10)
    for a, q in sorted(used.items()):
        i = _info_row(a)
        t1.insert("", "end", iid=a, values=(i["pn"], i["part"], i["rev"], i["state"], q, a))
    t1.bind("<Double-1>", lambda e: t1.focus() and (_select_in_app(app, t1.focus()), dlg.cancel()))
    rows = bom(name)
    tops = {}
    t2 = _tree(p2, (("pn", "Part number", 110, False), ("rev", "Rev", 60, False), ("state", "State", 110, False),
                    ("qty", "Qty", 50, False), ("total", "Total", 60, False), ("desc", "Description", 200, True)), 12,
               show="tree headings")
    t2.heading("#0", text="Part", anchor="w")
    t2.column("#0", width=S(240), anchor="w", stretch=True)
    stack = [""]
    for k, r in enumerate(rows):
        i = _info_row(r["file"])
        del stack[r["level"]:]
        iid = f"r{k}"
        t2.insert(stack[-1], "end", iid=iid, open=True, text=("⟲ " if r["cycle"] else "") + i["part"],
                  values=(i["pn"], i["rev"], i["state"], r["qty"], r["total"], i["desc"]))
        tops[iid] = r["file"]
        stack.append(iid)
    ttk.Label(p2, text=("No parts found in this file." if not rows else
                        f"{len(bom_totals(rows))} different parts. Quantities are counted from the references saved "
                        "in the file - check them the first time you use a new CAD program."),
              style="CardMuted.TLabel", wraplength=S(800)).pack(anchor="w", pady=(S(6), 0))

    def export():
        p = fd.asksaveasfilename(parent=dlg, title="Export bill of materials", defaultextension=".csv",
                                 initialfile=f"BOM {os.path.splitext(name)[0]}.csv",
                                 filetypes=[("CSV (Excel)", "*.csv")])
        dlg.grab_set()
        if p:
            with open(p, "w", encoding="utf-8-sig", newline="") as f:
                f.write(bom_csv(name, rows))
            _say(app, f"Bill of materials saved: {p}", "ok")
            E.open_path(p)
    ttk.Button(dlg.buttons, text="Export BOM (CSV)…", command=export).pack(side="left")
    ttk.Button(dlg.buttons, text="Close", command=dlg.cancel).pack(side="right")
    nb.select(1 if tab == "bom" else 0)
    dlg.geometry(f"{S(900)}x{min(S(560), app.root.winfo_screenheight() - 90)}")
    dlg.show()


# ---- approvals window ------------------------------------------------------------------------------------------
def show_approvals(app):
    tk, ttk, mb, fd = _tk()
    S = app.S
    admin = _admin_ok()
    dlg = E.ThemedDialog(app, "Approvals", "Release approvals",
                         "Files waiting to be released." + ("" if admin else " Administrators approve them."),
                         width=900, resizable=True)
    tv = _tree(dlg.body, (("file", "File", 280, True), ("rev", "Rev", 60, False), ("by", "Requested by", 120, False),
                          ("when", "When", 130, False), ("note", "Note", 240, True)), 10)
    ttk.Label(dlg.body, text="RECENT DECISIONS", style="CardSection.TLabel").pack(anchor="w", pady=(S(12), S(2)))
    tv2 = _tree(dlg.body, (("file", "File", 280, True), ("state", "Now", 110, False), ("by", "Decided by", 120, False),
                           ("when", "When", 130, False), ("note", "Note", 240, True)), 6)

    def load():
        tv.delete(*tv.get_children())
        for r in pending():
            tv.insert("", "end", iid=r["filename"], values=(r["filename"], r.get("revision") or "",
                                                            r.get("requested_by") or "", (r.get("requested_at") or "")[:16],
                                                            r.get("note") or ""))
        tv2.delete(*tv2.get_children())
        for r in recent_decisions():
            tv2.insert("", "end", values=(r["filename"], r["state"], r.get("decided_by") or "",
                                          (r.get("decided_at") or "")[:16], r.get("note") or ""))
        update_approvals_button(app)

    def sel():
        s = tv.selection()
        return s[0] if s else None

    def do(fn, needs_note=False, title=""):
        n = sel()
        if not n:
            return
        if fn is withdraw:
            _report(app, withdraw(n), refresh=False)
        else:
            note = _ask(app, title, f"{title}: {n}", "", "Note" + ("" if needs_note else " (optional)"), needs_note)
            dlg.grab_set()
            if note is None:
                return
            _report(app, fn(n, note), refresh=False)
        load()
        app.refresh_all()
    if admin:
        ttk.Button(dlg.buttons, text="✔  Approve & release", style="Success.TButton",
                   command=lambda: do(approve, False, "Approve")).pack(side="left")
        ttk.Button(dlg.buttons, text="Return…", command=lambda: do(reject, True, "Return")).pack(side="left", padx=(S(6), 0))
    ttk.Button(dlg.buttons, text="Withdraw", command=lambda: do(withdraw, False, "Withdraw")).pack(side="left",
                                                                                                 padx=(S(6), 0))
    ttk.Button(dlg.buttons, text="Show in list", command=lambda: sel() and (_select_in_app(app, sel()), dlg.cancel())
               ).pack(side="left", padx=(S(6), 0))
    ttk.Button(dlg.buttons, text="Close", command=dlg.cancel).pack(side="right")
    load()
    dlg.geometry(f"{S(940)}x{min(S(600), app.root.winfo_screenheight() - 90)}")
    dlg.show()


def update_approvals_button(app):
    b = getattr(app, "_pro_appr_btn", None)
    if b is None:
        return
    n = len(pending())
    try:
        b.configure(text=f"✔  Approvals ({n})" if n else "✔  Approvals")
    except Exception:                                           # noqa: BLE001
        pass


# ---- release settings ----------------------------------------------------------------------------------------
def release_settings(app):
    if not app.ensure_admin():
        return
    tk, ttk, mb, fd = _tk()
    dlg = E.ThemedDialog(app, "Release settings", "Release settings", "Applies to everyone using this vault.",
                         width=480)
    a = tk.BooleanVar(value=approval_required())
    b = tk.BooleanVar(value=different_approver())
    ttk.Checkbutton(dlg.body, text="Releasing needs an administrator's approval", variable=a,
                    style="Dialog.TCheckbutton").pack(anchor="w")
    ttk.Checkbutton(dlg.body, text="The approver must be someone other than the person who asked", variable=b,
                    style="Dialog.TCheckbutton").pack(anchor="w", pady=(app.S(6), 0))
    ttk.Label(dlg.body, text="Leave the second option off if you're the only administrator.",
              style="CardMuted.TLabel").pack(anchor="w", pady=(app.S(4), 0))

    def ok():
        set_setting("release_approval", "1" if a.get() else "0")
        set_setting("release_other_approver", "1" if b.get() else "0")
        E.log_audit("[settings]", "Release Settings", "", f"approval={a.get()} other={b.get()}")
        return True
    dlg.validate = ok
    dlg.add_buttons("Save", "Accent.TButton")
    dlg.show()


# ---- check in all / closing ------------------------------------------------------------------------------------
def ui_check_in_all(app):
    mine = my_checkouts()
    if not mine:
        _say(app, "You haven't got anything checked out.", "ok")
        return True
    changed = [n for n in mine if changed_locally(n)]
    sub = (f"{len(changed)} changed file(s) will be checked in; {len(mine) - len(changed)} unchanged will just be "
           "released.") if changed else f"None of your {len(mine)} file(s) have changed - their locks will be released."

    def submit(v):
        if changed and not v["c"].strip():
            return "error", "Describe what changed (it goes in each file's history)."
        return v["c"].strip() or "No changes"
    c = E.run_form(app, "Check in all", "Check in all", sub, [("c", "What changed", False, "")], submit,
                   "Check in all", ok_style="Success.TButton", width=460)
    if c is None:
        return False
    app.root.config(cursor="watch")
    app.root.update_idletasks()
    try:
        results = check_in_all(c)
    finally:
        app.root.config(cursor="")
    for lvl, msg in results:
        _say(app, msg, lvl)
    bad = [m for l, m in results if l == "error"]
    if bad:
        _tk()[2].showerror(E.APP_NAME, "\n".join(bad[:8]), parent=app.root)
    app.refresh_all()
    return not bad


def on_close(app):
    tk, ttk, mb, fd = _tk()
    try:
        E.INDEX.reload_records()
    except Exception:                                           # noqa: BLE001
        pass
    mine = my_checkouts()
    if not mine:
        app.root.destroy()
        return
    S = app.S
    dlg = E.ThemedDialog(app, "Files still checked out", "You still have files checked out",
                         "Nobody else can change them until they're checked in.", width=480)
    lb = tk.Listbox(dlg.body, height=min(10, len(mine)), bg=app.t["entry_bg"], fg=app.t["fg"], bd=0,
                    highlightthickness=1, highlightbackground=app.t["border"], font=("Segoe UI", 9))
    for n in mine:
        lb.insert("end", ("● " if changed_locally(n) else "   ") + n)
    lb.pack(fill="x")
    ttk.Label(dlg.body, text="● = changed since you checked it out", style="CardMuted.TLabel").pack(anchor="w",
                                                                                                    pady=(S(4), 0))
    choice = {"v": None}

    def pick(v):
        choice["v"] = v
        dlg.destroy()
    ttk.Button(dlg.buttons, text="Cancel", command=lambda: pick(None)).pack(side="right")
    ttk.Button(dlg.buttons, text="Keep checked out & close", command=lambda: pick("keep")).pack(side="right",
                                                                                              padx=(0, S(6)))
    ttk.Button(dlg.buttons, text="Check in all…", style="Success.TButton", command=lambda: pick("all")).pack(
        side="right", padx=(0, S(6)))
    dlg.show()
    if choice["v"] == "keep" or (choice["v"] == "all" and ui_check_in_all(app)):
        app.root.destroy()


# ---- toasts & polling ------------------------------------------------------------------------------------------
def toast(app, text, level="info", file=None):
    tk, ttk, mb, fd = _tk()
    _say(app, text, level)
    t = app.t
    col = {"ok": t["success"], "warn": t["warning"], "error": t["danger"]}.get(level, t["accent"])
    stack = app.__dict__.setdefault("_pro_toasts", [])
    try:
        w = tk.Toplevel(app.root)
        w.overrideredirect(True)
        w.attributes("-topmost", True)
        w.configure(bg=col)
        inner = tk.Frame(w, bg=t["panel_bg"])
        inner.pack(padx=(4, 1), pady=1)
        tk.Label(inner, text=text, bg=t["panel_bg"], fg=t["fg"], font=("Segoe UI", 9), wraplength=320, justify="left",
                 padx=12, pady=9).pack(side="left")
        w.update_idletasks()
        r = app.root
        x = r.winfo_rootx() + r.winfo_width() - w.winfo_reqwidth() - 16
        y = r.winfo_rooty() + r.winfo_height() - 60 - sum(s.winfo_reqheight() + 8 for s in stack if s.winfo_exists())
        w.geometry(f"+{max(0, x)}+{max(0, y - w.winfo_reqheight())}")
        stack.append(w)

        def close(_e=None):
            if w in stack:
                stack.remove(w)
            try:
                w.destroy()
            except Exception:                                   # noqa: BLE001
                pass

        def click(_e=None):
            close()
            if file:
                _select_in_app(app, file)
            elif "approval" in text:
                show_approvals(app)
        for wd in (w, inner) + tuple(inner.winfo_children()):
            wd.bind("<Button-1>", click)
        w.after(9000, close)
    except Exception:                                           # noqa: BLE001
        pass


def poll(app):
    try:
        new = snapshot()
        old = _UI["snapshot"]
        if old and new:
            for text, f, lvl in notifications(old, new):
                toast(app, text, lvl, f)
                if f and lvl == "ok" and "available" in text:
                    unwatch(f)
            for f in lock_lost(old, new, lambda n: os.path.exists(E.sandbox_path(n))):
                toast(app, f"Your lock on {f} was released by an administrator. Your copy is still in your "
                           "working folder.", "warn", f)
            if old["pending"] != new["pending"]:
                update_approvals_button(app)
        _UI["snapshot"] = new or old
    except Exception:                                           # noqa: BLE001
        pass
    try:
        while True:
            fn = _UI["queue"].get_nowait()
            fn()
    except queue.Empty:
        pass
    except Exception:                                           # noqa: BLE001
        pass
    app.root.after(15000, lambda: poll(app))


def _bg(app, work, done):
    """Runs work() in a thread; done(result, error) back on the window's thread."""
    def run():
        try:
            r, err = work(), None
        except Exception as e:                                  # noqa: BLE001
            r, err = None, e
        _UI["queue"].put(lambda: done(r, err))
        _UI["jobs"] -= 1
    _UI["jobs"] = _UI.get("jobs", 0) + 1
    threading.Thread(target=run, daemon=True).start()
    _drain(app)


def _drain(app):
    try:
        while True:
            _UI["queue"].get_nowait()()
    except queue.Empty:
        pass
    except Exception:                                           # noqa: BLE001
        pass
    if _UI.get("jobs", 0) > 0 or not _UI["queue"].empty():
        app.root.after(200, lambda: _drain(app))


def auto_backup(app):
    try:
        if not backup_due():
            return
    except Exception:                                           # noqa: BLE001
        return
    prog = {"t": ""}
    _say(app, "Making the scheduled backup in the background…")
    _bg(app, lambda: run_backup(progress=lambda i, n, t: prog.update(t=f"{i}/{n}")),
        lambda r, err: _say(app, f"Backup made: {os.path.basename(r)}" if r else f"Backup skipped: {err}",
                            "ok" if r else "warn"))


# ---- backups & integrity window --------------------------------------------------------------------------------
def show_maintenance(app):
    if not app.ensure_admin():
        return
    tk, ttk, mb, fd = _tk()
    S = app.S
    dlg = E.ThemedDialog(app, "Backups & integrity", "Backups & integrity", f"Vault: {E.VAULT_DIR}", width=900,
                         resizable=True)
    nb = ttk.Notebook(dlg.body)
    nb.pack(fill="both", expand=True)
    pb = ttk.Frame(nb, style="Dialog.TFrame", padding=(0, S(10), 0, 0))
    pi = ttk.Frame(nb, style="Dialog.TFrame", padding=(0, S(10), 0, 0))
    nb.add(pb, text="  Backups  ")
    nb.add(pi, text="  Integrity check  ")

    # -- settings
    form = ttk.Frame(pb, style="Dialog.TFrame")
    form.pack(fill="x")
    fv = tk.StringVar(value=setting("backup_folder", "") or "")
    dv = tk.StringVar(value=str(setting("backup_every_days", "1")))
    kv = tk.StringVar(value=str(setting("backup_keep", "14")))
    mv = tk.StringVar(value="Whole vault" if backup_mode() == "full" else "Database only")
    ttk.Label(form, text="Backup folder", style="CardMuted.TLabel").grid(row=0, column=0, sticky="w")
    ttk.Entry(form, textvariable=fv, width=60).grid(row=0, column=1, sticky="ew", padx=(S(8), S(6)))

    def browse():
        p = fd.askdirectory(parent=dlg, title="Backup folder (another drive is best)")
        dlg.grab_set()
        if p:
            fv.set(os.path.normpath(p))
    ttk.Button(form, text="Browse…", command=browse).grid(row=0, column=2)
    ttk.Label(form, text=f"Empty = {os.path.join(E.VAULT_DIR, BACKUP_DIR_NAME)}. A folder on another drive or PC "
                         "protects you if the vault's drive fails.", style="CardMuted.TLabel",
              wraplength=S(700)).grid(row=1, column=1, columnspan=2, sticky="w")
    for r, (lab, var, vals) in enumerate((("What to back up", mv, ["Whole vault", "Database only"]),
                                          ("Every (days, 0 = never)", dv, None), ("Keep", kv, None)), start=2):
        ttk.Label(form, text=lab, style="CardMuted.TLabel").grid(row=r, column=0, sticky="w", pady=S(3))
        w = ttk.Combobox(form, textvariable=var, values=vals, state="readonly", width=16) if vals else \
            ttk.Entry(form, textvariable=var, width=8)
        w.grid(row=r, column=1, sticky="w", padx=(S(8), 0), pady=S(3))
    form.columnconfigure(1, weight=1)
    info = ttk.Label(pb, text="", style="CardMuted.TLabel")
    info.pack(anchor="w", pady=(S(6), S(4)))
    bt = _tree(pb, (("when", "Made", 140, False), ("what", "Contents", 110, False), ("files", "Files", 70, False),
                    ("size", "Size", 90, False), ("by", "By", 160, False), ("name", "File", 260, True)), 7)

    def load():
        bt.delete(*bt.get_children())
        for b in list_backups():
            bt.insert("", "end", iid=b["path"], values=(b["created"][:16], "Whole vault" if b["mode"] == "full" else
                                                        "Database", b.get("files", 0), f"{b['size'] / 1e6:.1f} MB",
                                                        f"{b.get('by', '')} · {b.get('host', '')}", b["name"]))
        info.configure(text=f"Last backup: {last_backup() or 'never'}   ·   next: "
                            f"{'when due' if backup_days() > 0 else 'off'}")

    def save():
        try:
            float(dv.get())
            int(float(kv.get()))
        except ValueError:
            mb.showerror(E.APP_NAME, "Days and Keep must be numbers.", parent=dlg)
            return
        set_setting("backup_folder", fv.get().strip())
        set_setting("backup_every_days", dv.get().strip())
        set_setting("backup_keep", kv.get().strip())
        set_setting("backup_mode", "full" if mv.get() == "Whole vault" else "database")
        E.log_audit("[settings]", "Backup Settings", "", f"{fv.get()} every {dv.get()} keep {kv.get()} {mv.get()}")
        load()
        _say(app, "Backup settings saved.", "ok")

    status = ttk.Label(pb, text="", style="CardAccent.TLabel")

    def backup_now():
        save()
        prog = {"t": "Starting…"}
        status.configure(text="Making a backup…")

        def tick():
            if prog.get("done"):
                return
            status.configure(text=f"Making a backup… {prog['t']}")
            dlg.after(400, tick)

        def done(r, err):
            prog["done"] = True
            status.configure(text=f"Made {os.path.basename(r)}" if r else f"Backup failed: {err}")
            load()
        tick()
        _bg(app, lambda: run_backup(progress=lambda i, n, t: prog.update(t=f"{i} of {n}")), done)

    def sel():
        s = bt.selection()
        return s[0] if s else None

    def restore_one():
        p = sel()
        if not p:
            return
        members = backup_members(p)
        if not members:
            mb.showinfo(E.APP_NAME, "This is a database-only backup - it has no files to restore.", parent=dlg)
            return

        def submit(v):
            q = v["q"].strip().lower()
            hits = [m for m in members if q and q in m.lower()]
            if not hits:
                return "error", "No file in this backup matches that."
            if len(hits) > 1 and not any(os.path.basename(h).lower() == q for h in hits):
                return "error", f"{len(hits)} files match - type more of the name. First: " + \
                    ", ".join(os.path.basename(h) for h in hits[:4])
            return next((h for h in hits if os.path.basename(h).lower() == q), hits[0])
        m = E.run_form(app, "Restore a file", "Restore a file", "Type the file name (or part of it). It's put in your "
                       "working folder - the vault isn't changed.", [("q", "File name", False, "")], submit, "Restore",
                       width=480, parent=dlg)
        dlg.grab_set()
        if m:
            out = restore_file(p, m)
            _say(app, f"Restored {os.path.basename(m)} to {out}", "ok")
            E.open_path(os.path.dirname(out))

    def restore_db():
        p = sel()
        if p and mb.askyesno("Restore database", "Replace the vault's database with the one in this backup?\n\n"
                             "Everyone else should close Atelier first. The current database is kept beside the "
                             "backups.", icon="warning", parent=dlg):
            try:
                keep = restore_database(p)
                _say(app, f"Database restored. Previous one kept as {os.path.basename(keep)}.", "ok")
                app.refresh_all()
            except Exception as e:                              # noqa: BLE001
                mb.showerror(E.APP_NAME, str(e), parent=dlg)

    def restore_all():
        p = sel()
        if not p:
            return
        folder = fd.askdirectory(parent=dlg, title="Empty folder to restore the vault into")
        dlg.grab_set()
        if folder:
            try:
                out = restore_vault_to(p, folder)
                mb.showinfo(E.APP_NAME, f"Restored into:\n{out}\n\nTo use it, run Atelier PDM setup and choose "
                            "'Use an existing vault'.", parent=dlg)
            except Exception as e:                              # noqa: BLE001
                mb.showerror(E.APP_NAME, str(e), parent=dlg)

    row = ttk.Frame(pb, style="Dialog.TFrame")
    row.pack(fill="x", pady=(S(8), 0))
    for text, cmd, style in (("Save settings", save, "TButton"), ("Back up now", backup_now, "Accent.TButton"),
                             ("Restore a file…", restore_one, "TButton"), ("Restore database…", restore_db, "TButton"),
                             ("Restore whole vault to a folder…", restore_all, "TButton"),
                             ("Open folder", lambda: E.open_path(backup_dir()) if os.path.isdir(backup_dir()) else None,
                              "TButton")):
        ttk.Button(row, text=text, style=style, command=cmd).pack(side="left", padx=(0, S(6)))
    status.pack(anchor="w", pady=(S(6), 0))

    # -- integrity
    ttk.Label(pi, text="Compares the database with the files in the vault. Nothing is changed unless you choose "
                       "Fix.", style="CardMuted.TLabel").pack(anchor="w")
    it = _tree(pi, (("kind", "Problem", 170, False), ("file", "File", 260, True), ("detail", "Details", 320, True),
                    ("fix", "Fix", 90, False)), 12)
    found = {}
    istat = ttk.Label(pi, text="", style="CardAccent.TLabel")

    def run_check():
        istat.configure(text="Checking…")
        prog = {"t": ""}

        def done(r, err):
            it.delete(*it.get_children())
            found.clear()
            if err:
                istat.configure(text=f"Check failed: {err}")
                return
            for k, f in enumerate(r):
                found[str(k)] = f
                it.insert("", "end", iid=str(k), values=(f["kind"], f["file"], f["detail"],
                                                         "Available" if f["fix"] else "—"))
            istat.configure(text="No problems found." if not r else f"{len(r)} item(s) found.")
            E.log_audit("[integrity]", "Integrity Check", "", f"{len(r)} item(s)")
        _bg(app, lambda: integrity_check(lambda i, n, t: prog.update(t=t)), done)

    def fix():
        s = it.selection()
        if not s or not found.get(s[0], {}).get("fix"):
            return
        _report(app, apply_fix(found[s[0]]), refresh=False)
        it.delete(s[0])

    r2 = ttk.Frame(pi, style="Dialog.TFrame")
    r2.pack(fill="x", pady=(S(8), 0))
    ttk.Button(r2, text="Run check", style="Accent.TButton", command=run_check).pack(side="left")
    ttk.Button(r2, text="Fix selected", command=fix).pack(side="left", padx=(S(6), 0))
    istat.pack(anchor="w", pady=(S(6), 0))
    ttk.Button(dlg.buttons, text="Close", command=dlg.cancel).pack(side="right")
    load()
    dlg.geometry(f"{S(940)}x{min(S(640), app.root.winfo_screenheight() - 90)}")
    dlg.show()


# ---- gallery -----------------------------------------------------------------------------------------------------
def show_gallery(app):
    tk, ttk, mb, fd = _tk()
    S, t = app.S, app.t
    names = [app.row_map[i][0] for i in app.grid.get_children("") if i in app.row_map]
    old = getattr(app, "_pro_gallery", None)
    if old is not None:
        try:
            old.destroy()
        except Exception:                                       # noqa: BLE001
            pass
    win = tk.Toplevel(app.root)
    app._pro_gallery = win
    win.title(f"{E.APP_NAME} · Gallery")
    win.configure(bg=t["bg"])
    sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
    win.geometry(f"{min(S(1000), sw - 60)}x{min(S(700), sh - 100)}")
    top = ttk.Frame(win, style="Toolbar.TFrame", padding=(S(12), S(6)))
    top.pack(fill="x")
    ttk.Label(top, text=f"{len(names)} file(s) - the files shown in the main list. Click to select, double-click to "
                        "open.", style="Status.TLabel").pack(side="left")
    size = tk.StringVar(value=local_settings().get("gallery_size", "Medium"))
    ttk.Combobox(top, textvariable=size, values=["Small", "Medium", "Large"], state="readonly", width=8).pack(
        side="right")
    canvas = tk.Canvas(win, bg=t["bg"], highlightthickness=0)
    sb = ttk.Scrollbar(win, orient="vertical", command=canvas.yview)
    canvas.configure(yscrollcommand=sb.set)
    sb.pack(side="right", fill="y")
    canvas.pack(fill="both", expand=True)
    holder = tk.Frame(canvas, bg=t["bg"])
    canvas.create_window((0, 0), window=holder, anchor="nw")
    holder.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
    canvas.bind_all("<MouseWheel>", lambda e: canvas.winfo_exists() and canvas.yview_scroll(-int(e.delta / 120), "units"),
                    add="+")
    viewer = sys.modules.get("atelier_viewer")
    cards, todo = {}, []
    cam = {"rx": 25.0, "ry": 40.0, "zoom": 1.0}
    imgs = []

    def draw(name, c, px):
        h = int(px * 0.78)
        try:
            stl = E.find_preview_stl(name)
            mesh = E.load_mesh(stl) if stl else None
            if mesh and viewer is not None and "tris" in mesh:
                viewer.render(c, mesh, px, h, t, cam, fast=True, tag="g")
                return
            if mesh:
                E.render_mesh(c, mesh, px, h, t, cam)
                return
            th = getattr(E, "cad_thumbnail", None)
            info = E.INDEX.files.get(name)
            data = th(info.path) if (th and info) else None
            if data:
                import base64
                img = tk.PhotoImage(master=c, data=base64.b64encode(data).decode("ascii"))
                k = max(1, -(-max(img.width(), img.height()) // px))
                img = img.subsample(k) if k > 1 else img
                imgs.append(img)
                c.create_rectangle(0, 0, px, h, fill=t["entry_bg"], outline="")
                c.create_image(px // 2, h // 2, image=img)
                return
        except Exception:                                       # noqa: BLE001
            pass
        E.render_placeholder(c, px, h, t, cam, "", os.path.splitext(name)[1].upper().lstrip("."))

    def build():
        for w in holder.winfo_children():
            w.destroy()
        cards.clear()
        del todo[:]
        px = {"Small": S(120), "Medium": S(170), "Large": S(240)}[size.get()]
        save_local(gallery_size=size.get())
        cols = max(1, (canvas.winfo_width() or S(900)) // (px + S(16)))
        states = all_states()
        for k, n in enumerate(names):
            rec = E.INDEX.records.get(n) or {}
            card = tk.Frame(holder, bg=t["panel_bg"], highlightthickness=2, highlightbackground=t["panel_bg"],
                            cursor="hand2")
            card.grid(row=k // cols, column=k % cols, padx=S(6), pady=S(6), sticky="n")
            c = tk.Canvas(card, width=px, height=int(px * 0.78), highlightthickness=0, bd=0, bg=t["entry_bg"])
            c.pack()
            i = _info_row(n)
            st = states.get(n, IN_WORK)
            key, label = E.file_state(n)
            line2 = label if st == IN_WORK else f"{st} · {label}" if key != "available" else st
            tk.Label(card, text=(i["pn"] + "  " if i["pn"] else "") + i["part"], bg=t["panel_bg"], fg=t["fg"],
                     font=("Segoe UI Semibold", 9), wraplength=px, justify="left", anchor="w").pack(fill="x", padx=6,
                                                                                                pady=(4, 0))
            col = t.get(STATE_COLOURS.get(st, ""), t["muted_fg"]) if st != IN_WORK else t["muted_fg"]
            tk.Label(card, text=f"{rec.get('revision') or 'A-01'}  ·  {line2}", bg=t["panel_bg"], fg=col,
                     font=("Segoe UI", 8), anchor="w").pack(fill="x", padx=6, pady=(0, 5))
            cards[n] = card

            def click(_e, name=n):
                for nn, cc in cards.items():
                    cc.configure(highlightbackground=t["accent"] if nn == name else t["panel_bg"])
                _select_in_app(app, name)

            def dbl(_e, name=n):
                _select_in_app(app, name)
                try:
                    app.action_open()
                except Exception:                               # noqa: BLE001
                    pass
            for w in (card, c) + tuple(card.winfo_children()):
                w.bind("<Button-1>", click)
                w.bind("<Double-1>", dbl)
            todo.append((n, c, px))
        step()

    def step():
        if not win.winfo_exists() or not todo:
            return
        n, c, px = todo.pop(0)
        draw(n, c, px)
        win.after(12, step)
    size.trace_add("write", lambda *_: build())
    win.after(120, build)
    win.bind("<Escape>", lambda e: win.destroy())


# ---- tour --------------------------------------------------------------------------------------------------------
def _find_button(container, text):
    try:
        for w in container.winfo_children():
            try:
                if text.lower() in str(w.cget("text")).lower():
                    return w
            except Exception:                                   # noqa: BLE001
                pass
            hit = _find_button(w, text)
            if hit:
                return hit
    except Exception:                                           # noqa: BLE001
        pass
    return None


def tour_steps(app):
    tb = getattr(app, "toolbar", None)
    return [
        (lambda: getattr(app, "search_entry", None), "Search",
         "Type part of a name, part number or description. Results come from every folder. Ctrl+F jumps here."),
        (lambda: _find_button(tb, "New Project"), "Projects",
         "A project is a numbered folder (01_, 02_ …) with Parts and Assemblies folders. Parts saved in it are "
         "numbered automatically."),
        (lambda: _find_button(tb, "New Part"), "New parts",
         "Creates a part from a template, gives it the next number and checks it out to you. You can also drag files "
         "in from File Explorer."),
        (lambda: getattr(app, "ftree", None), "Folders", "Your vault's folders. Right-click for more options."),
        (lambda: getattr(app, "grid", None), "Files",
         "Everything in the selected folder. Right-click a file for Where used, Bill of materials, Request release "
         "and more. The filter above the list finds things across all folders."),
        (lambda: _find_button(tb, "Check Out"), "Check out",
         "Locks a file to you so nobody else changes it, and copies it to your working folder."),
        (lambda: _find_button(tb, "Check In"), "Check in",
         "Puts your changes back in the vault and keeps the old version in the history."),
        (lambda: getattr(app, "canvas", None), "Preview", "Drag to turn the part, scroll to zoom, double-click to reset."),
        (lambda: getattr(app, "part_entry", None), "Properties",
         "The selected file's name, number and description. Edit them and Save properties."),
        (lambda: getattr(app, "_pro_appr_btn", None), "Releasing",
         "When a design is finished, right-click › Request release. An administrator approves it here. Released "
         "parts are protected - start a new revision to change one."),
        (lambda: getattr(app, "account_mb", None), "Your menu",
         "Password, themes, Check in all, help, and (administrators) backups and settings. F1 opens Help anywhere."),
    ]


def start_tour(app, offer_sample=True):
    tk, ttk, mb, fd = _tk()
    steps = [s for s in tour_steps(app)]
    state = {"i": 0, "win": None}
    t = app.t

    def close():
        if state["win"] is not None:
            try:
                state["win"].destroy()
            except Exception:                                   # noqa: BLE001
                pass
            state["win"] = None

    def finish():
        close()
        save_local(tour_done=True)
        if offer_sample and mb.askyesno("Sample project", "Would you like a sample project with a few practice parts "
                                        "to try checking out, checking in and releasing?", parent=app.root):
            ui_sample(app)

    def show(i):
        close()
        while i < len(steps):
            w = steps[i][0]()
            try:
                if w is not None and w.winfo_viewable():
                    break
            except Exception:                                   # noqa: BLE001
                pass
            i += 1
        if i >= len(steps):
            finish()
            return
        state["i"] = i
        get, title, text = steps[i]
        w = get()
        win = tk.Toplevel(app.root)
        state["win"] = win
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg=t["accent"])
        body = tk.Frame(win, bg=t["panel_bg"], padx=14, pady=10)
        body.pack(padx=2, pady=2)
        tk.Label(body, text=f"{title}   ·   {i + 1} of {len(steps)}", bg=t["panel_bg"], fg=t["accent"],
                 font=("Segoe UI Semibold", 10)).pack(anchor="w")
        tk.Label(body, text=text, bg=t["panel_bg"], fg=t["fg"], font=("Segoe UI", 9), wraplength=320,
                 justify="left").pack(anchor="w", pady=(4, 8))
        row = tk.Frame(body, bg=t["panel_bg"])
        row.pack(fill="x")
        ttk.Button(row, text="Skip tour", command=lambda: (close(), save_local(tour_done=True))).pack(side="left")
        ttk.Button(row, text="Next ›" if i < len(steps) - 1 else "Finish", style="Accent.TButton",
                   command=lambda: show(i + 1)).pack(side="right")
        if i:
            ttk.Button(row, text="‹ Back", command=lambda: show(i - 1)).pack(side="right", padx=(0, 6))
        win.update_idletasks()
        x, y = w.winfo_rootx(), w.winfo_rooty() + w.winfo_height() + 8
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        if y + win.winfo_reqheight() > sh - 50:
            y = max(0, w.winfo_rooty() - win.winfo_reqheight() - 8)
        x = max(0, min(x, sw - win.winfo_reqwidth() - 8))
        win.geometry(f"+{x}+{y}")
        win.bind("<Escape>", lambda e: close())
        win.focus_force()
    show(0)


def offer_tour(app):
    if local_settings().get("tour_done"):
        return
    tk, ttk, mb, fd = _tk()
    r = mb.askyesnocancel("Welcome to Atelier PDM", "New to Atelier? Take a one-minute tour of the window.\n\n"
                          "Yes: take the tour     No: not now     Cancel: don't ask again", parent=app.root)
    if r is None:
        save_local(tour_done=True)
    elif r:
        start_tour(app)


def ui_sample(app):
    try:
        res = create_sample_project()
    except Exception as e:                                      # noqa: BLE001
        res = ("error", f"Couldn't create the sample project: {e}", None)
    _report(app, res[:2], refresh=False)
    if res[0] == "ok" and res[2]:
        try:
            app.select_folder(res[2])
        except Exception:                                       # noqa: BLE001
            app.refresh_all()


# ---- grid menu additions ----------------------------------------------------------------------------------------
def extra_menu_items(app, f):
    """[(label, command, enabled) or None] added to the file list's right-click menu."""
    if not f or f not in E.INDEX.files:
        return []
    info = E.INDEX.files[f]
    st = state_of(f)["state"]
    rec = E.INDEX.records.get(f) or {}
    lock = rec.get("locked_by") or ""
    admin = E.is_admin()
    model = info.ext in E.CAD_MODEL_EXTS
    items = [None, ("⧉  Where used…", lambda: show_structure(app, f, "used"), model),
             ("☷  Bill of materials…", lambda: show_structure(app, f, "bom"), model), None]
    if st == IN_WORK and not info.is_library:
        items.append(("✔  Request release…", lambda: ui_request(app, f), not lock))
    if st == PENDING:
        if admin:
            items.append(("✔  Approve & release…", lambda: ui_decide(app, f, True), True))
            items.append(("↩  Return…", lambda: ui_decide(app, f, False), True))
        items.append(("Withdraw release request", lambda: _report(app, withdraw(f)), True))
    if st == RELEASED:
        items.append(("✎  Revise (minor)…", lambda: app.action_up_issue("minor"), not lock))
        items.append(("✎  Revise (major)…", lambda: app.action_up_issue("major"), not lock))
    if admin and st in (IN_WORK, RELEASED):
        items.append(("⊘  Make obsolete…", lambda: ui_obsolete(app, f), not lock))
    if admin and st == OBSOLETE:
        items.append(("Reinstate", lambda: _report(app, reinstate(f)), True))
    if lock and lock != user():
        if f in watching():
            items.append(("🔔  Stop notifying me", lambda: (unwatch(f), _say(app, "Stopped watching.", "ok")), True))
        else:
            items.append(("🔔  Notify me when it's free", lambda: (watch(f), _say(app, f"You'll be told when {lock} "
                                                                                       "checks it in.", "ok")), True))
    return items


def ui_request(app, f):
    note = _ask(app, "Request release", f"Request release: {f}",
                "An administrator will approve it." if approval_required() else "It will be released straight away.",
                "Note for the approver (optional)", ok="Request release", style="Success.TButton")
    if note is not None:
        _report(app, request_release(f, note))
        update_approvals_button(app)


def ui_decide(app, f, ok_):
    note = _ask(app, "Approve" if ok_ else "Return", ("Approve & release: " if ok_ else "Return: ") + f, "",
                "Note" + ("" if not ok_ else " (optional)"), required=not ok_,
                ok="Approve & release" if ok_ else "Return", style="Success.TButton" if ok_ else "Accent.TButton")
    if note is not None:
        _report(app, approve(f, note) if ok_ else reject(f, note))
        update_approvals_button(app)


def ui_obsolete(app, f):
    used = where_used(f)
    sub = (f"Still used in {len(used)} assembl{'y' if len(used) == 1 else 'ies'}: " + ", ".join(list(used)[:4]) +
           ("…" if len(used) > 4 else "")) if used else "It won't be able to be checked out or changed."
    note = _ask(app, "Make obsolete", f"Make obsolete: {f}", sub, "Reason", required=True, ok="Make obsolete",
                style="Danger.TButton")
    if note is not None:
        _report(app, make_obsolete(f, note))


class _MenuHook:
    """While the original right-click menu is built, add our items just before it's shown."""

    def __init__(self, items):
        self.items = items

    def __enter__(self):
        tk = _tk()[0]
        self.orig = tk.Menu.tk_popup
        items = self.items
        orig = self.orig

        def popup(menu, x, y, entry=""):
            tk.Menu.tk_popup = orig
            last_sep = False
            for it in items:
                if it is None:
                    if not last_sep:
                        menu.add_separator()
                        last_sep = True
                    continue
                label, cmd, enabled = it
                menu.add_command(label=label, command=cmd, state="normal" if enabled else "disabled")
                last_sep = False
            return orig(menu, x, y, entry)
        tk.Menu.tk_popup = popup
        return self

    def __exit__(self, *exc):
        _tk()[0].Menu.tk_popup = self.orig
        return False


# =============================================================================
# Hooks into the main window
# =============================================================================
def _hook_app(m):
    cls = getattr(m, "AtelierApp", None)
    if cls is None or getattr(cls, "_pro", False):
        return
    cls._pro = True

    o_build = cls.build_ui

    def build_ui(self, *a, **k):
        res = o_build(self, *a, **k)
        _UI["app"] = self
        tk, ttk, mb, fd = _tk()
        try:
            self._pro_appr_btn = ttk.Button(self.toolbar, text="✔  Approvals", style="Tool.TButton",
                                            command=lambda: show_approvals(self))
            self._pro_appr_btn.pack(side="right", padx=(0, 4))
        except Exception:                                       # noqa: BLE001
            pass
        try:                                                    # filter sits with the search box in the header
            wrap = self.search_entry.master
            self._pro_filter = tk.StringVar(value=local_settings().get("list_filter", FILTERS[0]))
            cb = ttk.Combobox(wrap, textvariable=self._pro_filter, values=FILTERS, state="readonly", width=18,
                              style="Header.TCombobox")
            cb.pack(side="left", padx=(self.S(8), 0), after=self.search_entry)
            cb.bind("<<ComboboxSelected>>", lambda e: (save_local(list_filter=self._pro_filter.get()),
                                                       cb.selection_clear(), self.refresh_grid()))
            self._pro_filter_cb = cb
        except Exception:                                       # noqa: BLE001
            self._pro_filter = None
        try:                                                    # Gallery is a view, so it lives on the toolbar
            self._pro_gallery_btn = ttk.Button(self.toolbar, text="▦  Gallery", style="Tool.TButton",
                                               command=lambda: show_gallery(self))
            self._pro_gallery_btn.pack(side="right", padx=(0, 4))
        except Exception:                                       # noqa: BLE001
            pass
        try:
            self.root.protocol("WM_DELETE_WINDOW", lambda: on_close(self))
        except Exception:                                       # noqa: BLE001
            pass
        self.root.after(2500, lambda: poll(self))
        self.root.after(20000, lambda: auto_backup(self))
        self.root.after(1500, lambda: offer_tour(self))
        self.root.after(1200, lambda: update_approvals_button(self))
        return res
    cls.build_ui = build_ui

    o_grid = cls.refresh_grid

    def refresh_grid(self, *a, **k):
        var = getattr(self, "_pro_filter", None)
        flt = var.get() if var is not None else FILTERS[0]
        if flt == FILTERS[0]:
            return o_grid(self, *a, **k)
        saved = self.current_folder
        self.current_folder = "*"
        try:
            res = o_grid(self, *a, **k)
        finally:
            self.current_folder = saved
        states, watched = all_states(), watching()
        n = 0
        for iid in list(self.grid.get_children("")):
            name = (self.row_map.get(iid) or (None,))[0]
            if name and matches_filter(name, flt, states, watched):
                n += 1
                continue
            for kid in self.grid.get_children(iid):
                self.row_map.pop(kid, None)
            self.row_map.pop(iid, None)
            self.grid.delete(iid)
        try:
            self.grid_title.configure(text=f"COMPONENTS  ·  {flt.upper()}  ·  ALL FOLDERS")
            self.count_label.configure(text=f"{n} item{'s' if n != 1 else ''}")
        except Exception:                                       # noqa: BLE001
            pass
        return res
    cls.refresh_grid = refresh_grid

    o_row = getattr(cls, "_row_values", None)
    if o_row:
        def _row_values(self, name):
            r = o_row(self, name)
            st = state_of(name)["state"] if name in E.INDEX.files else IN_WORK
            if st != IN_WORK:
                r["status"] = f"{r['status']}  ·  {st}"
            return r
        cls._row_values = _row_values

    o_props = getattr(cls, "update_properties", None)
    if o_props:
        def update_properties(self, *a, **k):
            res = o_props(self, *a, **k)
            try:
                f = self.selected
                if f and f in E.INDEX.files:
                    st = state_of(f)["state"]
                    if st != IN_WORK:
                        self.p_pill.configure(text=f"{self.p_pill.cget('text')}  ·  {st.upper()}")
            except Exception:                                   # noqa: BLE001
                pass
            return res
        cls.update_properties = update_properties

    o_states = getattr(cls, "update_action_states", None)
    if o_states:
        def update_action_states(self, *a, **k):
            res = o_states(self, *a, **k)
            try:
                f = self.selected
                if f and state_of(f)["state"] in LOCKED_STATES and "checkout" in self.btn:
                    self.btn["checkout"].state(["disabled"])
            except Exception:                                   # noqa: BLE001
                pass
            return res
        cls.update_action_states = update_action_states

    o_menu = getattr(cls, "show_grid_menu", None)
    if o_menu:
        def show_grid_menu(self, event):
            try:
                iid = self.grid.identify_row(event.y)
                f = (self.row_map.get(iid) or (None,))[0] if iid and not self.grid.parent(iid) else None
                items = extra_menu_items(self, f) if f else []
            except Exception:                                   # noqa: BLE001
                items = []
            if not items:
                return o_menu(self, event)
            with _MenuHook(items):
                return o_menu(self, event)
        cls.show_grid_menu = show_grid_menu

    o_up = getattr(cls, "action_up_issue", None)
    if o_up:
        def action_up_issue(self, bump):
            f = self.selected
            if f:
                try:
                    used = where_used(f)
                except Exception:                               # noqa: BLE001
                    used = {}
                if used:
                    tk, ttk, mb, fd = _tk()
                    lst = "\n".join("• " + n for n in list(used)[:10]) + ("\n…" if len(used) > 10 else "")
                    if not mb.askyesno("Used in assemblies", f"'{f}' is used in:\n\n{lst}\n\nThose assemblies will "
                                       "use the new revision. Carry on?", icon="warning", parent=self.root):
                        return None
            return o_up(self, bump)
        cls.action_up_issue = action_up_issue

    o_ident = getattr(cls, "update_identity", None)
    if o_ident:
        def update_identity(self, *a, **k):
            res = o_ident(self, *a, **k)
            try:
                mm = self.account_menu
                mm.add_separator()
                mm.add_command(label="⇧  Check in all…", command=lambda: ui_check_in_all(self))
                mm.add_command(label="▦  Gallery", command=lambda: show_gallery(self))
                mm.add_command(label="✔  Approvals…", command=lambda: show_approvals(self))
                mm.add_command(label="🧭  Take the tour", command=lambda: start_tour(self, offer_sample=False))
                mm.add_command(label="📦  Create sample project", command=lambda: ui_sample(self))
                if E.is_admin():
                    mm.add_command(label="🛡  Backups & integrity…", command=lambda: show_maintenance(self))
                    mm.add_command(label="⚙  Release settings…", command=lambda: release_settings(self))
            except Exception:                                   # noqa: BLE001
                pass
            return res
        cls.update_identity = update_identity


# =============================================================================
# Help
# =============================================================================
HELP_CHAPTER = ("Release, BOM and safety nets", [
    ("h", "Release states"),
    ("t", [["State", "Meaning"],
           ["In work", "Normal: can be checked out and changed."],
           ["Awaiting approval", "Someone has asked for it to be released. It can't be checked out or changed."],
           ["Released", "Approved for use. A release copy is in the history. It can't be checked out, renamed or "
                        "deleted - start a new revision to change it."],
           ["Obsolete", "No longer used. Can't be checked out or changed. An administrator can reinstate it."]]),
    ("n", "When a design is finished and checked in: right-click › **Request release…**."),
    ("n", "An administrator opens **Approvals** (toolbar) and chooses **Approve & release** or **Return…** with a note. "
          "You're notified either way."),
    ("n", "To change a released part: right-click › **Revise (minor)** or **Revise (major)**. It gets the next "
          "revision and is In work again."),
    ("p", "Administrators: account menu › **Release settings…** to turn approval off (anyone can release) or require "
          "a second person to approve."),
    ("h", "Where used and bill of materials"),
    ("b", "Right-click a part › **Where used…** lists the assemblies that use it."),
    ("b", "Right-click an assembly › **Bill of materials…** shows its parts and sub-assemblies with quantities and "
          "totals; **Export BOM (CSV)** opens it in Excel."),
    ("b", "Up-issuing a part that's used in assemblies warns you first."),
    ("note", "Quantities are counted from the references saved in the assembly file. Check them the first time you "
             "use a new CAD program."),
    ("h", "Safety nets"),
    ("b", "Closing Atelier with files checked out asks whether to **Check in all**, keep them, or cancel."),
    ("b", "**Check in all…** (account menu) checks in everything you've changed; unchanged files are just released."),
    ("b", "Right-click a file someone else has checked out › **Notify me when it's free**. A message appears in the "
          "corner when it's checked in. You're also told when your release requests are decided and if an "
          "administrator releases one of your locks."),
    ("h", "Finding things"),
    ("b", "The filter above the file list: Checked out by me, Locked by others, Changed this week, a release state, or "
          "Watching. Filters search every folder; choose All files to go back."),
    ("b", "**▦ Gallery** shows the files in the list as thumbnails. Click to select, double-click to open."),
    ("h", "Backups and integrity (administrators)"),
    ("p", "Account menu › **Backups & integrity…**."),
    ("b", "Choose a backup folder - another drive or PC is best - whether to back up the whole vault or just the "
          "database, how often, and how many to keep. Backups run automatically in the background."),
    ("b", "**Restore a file** puts one file from a backup in your working folder. **Restore database** replaces the "
          "database (everyone else should close Atelier first; the current one is kept). **Restore whole vault** "
          "unpacks a backup into a new, empty folder - never over the live vault."),
    ("b", "**Integrity check** compares the database with the files and lists problems such as files changed outside "
          "Atelier or locks held by people who aren't users, with a safe fix where there is one."),
    ("h", "Getting started"),
    ("b", "Account menu › **Take the tour** walks round the window."),
    ("b", "Account menu › **Create sample project** makes a numbered project with three practice parts (one already "
          "released) to try things on safely. Delete the project folder when you've finished."),
])

TIPS = [
    ("Approvals", "Files waiting to be released. Administrators approve them or return them with a note."),
    ("Gallery", "The files in the list as thumbnails."),
    ("Request release", "Asks an administrator to release this file. It's frozen until they decide."),
    ("Revise", "Starts a new revision of a released part, so it can be changed."),
    ("Where used", "Every assembly in the vault that uses this part."),
    ("Bill of materials", "The parts and sub-assemblies in this assembly, with quantities. Exports to Excel."),
    ("Make obsolete", "Stops the file being used or changed. An administrator can reinstate it."),
    ("Back up now", "Makes a backup now, in the background."),
    ("Integrity", "Compares the database with the files in the vault."),
]


def _add_help():
    try:
        import atelier_help_text as H
    except ImportError:
        return
    if not any(c[0] == HELP_CHAPTER[0] for c in H.CHAPTERS):
        idx = next((i + 1 for i, c in enumerate(H.CHAPTERS) if c[0] == "Working with files"), len(H.CHAPTERS))
        H.CHAPTERS.insert(idx, HELP_CHAPTER)
    have = {k for k, _ in H.TIPS}
    H.TIPS[:0] = [t for t in TIPS if t[0] not in have]


def install(module):
    global E
    E = module
    if getattr(module, "_pro_installed", False):
        return
    module._pro_installed = True
    module.atelier_pro = sys.modules[__name__]
    try:
        module.SKIP_DIRS.add(BACKUP_DIR_NAME)
    except AttributeError:
        pass
    try:
        _ensure_tables()
    except Exception:                                           # noqa: BLE001
        pass
    _wrap_engine(module)
    _hook_app(module)
    _add_help()
