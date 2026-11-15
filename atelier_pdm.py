"""
Atelier PDM  -  v2.6
====================
v2.6: optional vault encryption (Administration > Security, atelier_secure.py); installer.

v2.5: PhysCAD preview - shows the thumbnail stored inside .physcad files; large thumbnails are
      scaled to fit the preview panel.

v2.4: renamed to Atelier PDM (was SolidCraft Atelier). Default sandbox C:\Atelier_Work; an existing
      C:\SolidCraft_Work is used until you move it, so current check-outs are not stranded.

v2.3: multi-CAD support - SolidCraft (.sc), FreeCAD (.FCStd) and PhysCAD (.physcad). Per-app launch,
      mesh / STEP conversion (solidcraft-cli, FreeCADCmd), FreeCAD thumbnails, companion-file exports,
      CAD column, CAD Apps admin tab, configurable apps via atelier_config.json.

v2.2: project part numbering (NN_0001 parts / NN_A001 assemblies), numbered project folders,
      auto-numbering of new files, editable part name with optional file rename,
      reworked theme palettes (dark header band on light themes).
v2.1: user accounts & sign-in, Administration panel (users, check-outs, security,
      activity), salted password hashing, admin-only lock breaking, Export STEP & PDF.

Lightweight check-out / check-in vault and part-numbering system for CAD parts and drawings
(SolidCraft, FreeCAD, PhysCAD and any app added in atelier_config.json).

Part numbering
--------------
* Project folders are named  NN_Name  (e.g. 01_Site Machinery) and live under the project root
  (default 'Projects'; set "project_root" in atelier_config.json, "." = vault root).
* 'New Project' picks the next free NN, creates the folder and its Assemblies / Parts subfolders
  (copying the folder layout of a 00_ template folder if one exists alongside).
* Files in  NN_*/Parts  get NN_0001, NN_0002 ...   Files in  NN_*/Assemblies  get NN_A001 ...
* Numbers are reserved in the database, so two people never get the same number.
* File names become  "<part number> <part name>.<ext>", e.g. "01_0007 Motor Mount.FCStd".
* Folders starting 00_ are templates and are never numbered.

Configuration (optional)
------------------------
Place 'atelier_config.json' in the vault folder:
    {
        "db_path": "\\\\SERVER\\PDM\\atelier_index.db",
        "sandbox_dir": "C:\\Atelier_Work",
        "project_root": "Projects",
        "cad_apps": {
            "freecad": {"cmd": "C:\\Program Files\\FreeCAD 1.0\\bin\\FreeCADCmd.exe"},
            "physcad": {"exe": "C:\\Program Files\\PhysCAD\\PhysCAD.exe"}
        }
    }
Each app entry can override: label, models, drawings, exe (or launch list), cmd (FreeCAD),
and converter - "solidcraft-cli", "freecad", null, or a command list such as
["mycad-cli", "--in", "{in}", "--out", "{out}"].
or set the environment variables ATELIER_VAULT, ATELIER_DB, ATELIER_SANDBOX.
"""

import os
import re
import sys
import json
import math
import struct
import shutil
import socket
import sqlite3
import hashlib
import glob
import zipfile
import base64
import hmac
import time
import getpass
import subprocess
from contextlib import contextmanager
from datetime import datetime

import tkinter as tk
from tkinter import ttk, messagebox, filedialog

APP_NAME = "Atelier PDM"
APP_VERSION = "2.6"
SINGLE_INSTANCE_PORT = 64738

# =============================================================================
# Paths & configuration
# =============================================================================
if getattr(sys, "frozen", False):
    _APP_DIR = os.path.dirname(sys.executable)
else:
    _APP_DIR = os.path.dirname(os.path.abspath(__file__))

VAULT_DIR = os.environ.get("ATELIER_VAULT") or _APP_DIR


def _load_config():
    try:
        with open(os.path.join(VAULT_DIR, "atelier_config.json"), "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


CONFIG = _load_config()

_SANDBOX_NEW = r"C:\Atelier_Work"
_SANDBOX_OLD = r"C:\SolidCraft_Work"          # v2.3 and earlier
LOCAL_WORKSPACE = os.environ.get("ATELIER_SANDBOX") or CONFIG.get("sandbox_dir") or (
    _SANDBOX_OLD if os.path.isdir(_SANDBOX_OLD) and not os.path.isdir(_SANDBOX_NEW) else _SANDBOX_NEW)
LOCAL_LIB_DIR = os.path.join(LOCAL_WORKSPACE, "_Library")
DISCARD_DIR = os.path.join(LOCAL_WORKSPACE, "_Discarded")
SETTINGS_PATH = os.path.join(LOCAL_WORKSPACE, "atelier_settings.json")

# Uses 'atelier_index.db'; falls back to the older misspelt 'atellier_index.db' if only that exists.
_DB_NEW = os.path.join(VAULT_DIR, "atelier_index.db")
_DB_OLD = os.path.join(VAULT_DIR, "atellier_index.db")
DB_PATH = os.environ.get("ATELIER_DB") or CONFIG.get("db_path") or (
    _DB_OLD if os.path.exists(_DB_OLD) and not os.path.exists(_DB_NEW) else _DB_NEW)

HISTORY_DIR = os.path.join(VAULT_DIR, "_History")
DELETED_DIR = os.path.join(HISTORY_DIR, "_Deleted")
STANDARD_LIB_DIR = os.path.join(VAULT_DIR, "_Standard_Library")
PROJECTS_DIR = os.path.join(VAULT_DIR, "Projects")
TEMPLATES_DIR = os.path.join(VAULT_DIR, "_Templates")
OUTPUT_DIR = os.path.join(VAULT_DIR, "Manufacturing_Output")
PROJECT_ROOT_REL = os.path.normpath(CONFIG.get("project_root") or "Projects")

HOST_NAME = socket.gethostname()
USER_NAME = getpass.getuser()      # replaced by the signed-in account when sign-in is required
ADMIN_SESSION = None               # name of the administrator account active this session, if any
MIN_PASSWORD_LEN = 6
USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{1,31}$")
PBKDF2_ITERATIONS = 200_000
_NO_WINDOW = {"creationflags": 0x08000000} if sys.platform == "win32" else {}

# -----------------------------------------------------------------------------
# CAD applications. Override or extend with "cad_apps" in atelier_config.json.
#   models     native part/assembly extensions (these get part numbers)
#   drawings   native drawing extensions (shown as linked assets of the part)
#   launch     program names/paths tried in order; then the Windows file association
#   converter  "solidcraft-cli" | "freecad" | None | ["prog", "--in", "{in}", "--out", "{out}"]
#   exports    what the converter can produce from a model
# -----------------------------------------------------------------------------
_DEFAULT_CAD_APPS = {
    "solidcraft": {"label": "SolidCraft", "models": [".sc"], "drawings": [".scdwg", ".drw"],
                   "launch": ["solidcraft"], "converter": "solidcraft-cli", "exports": ["stl", "step", "pdf"]},
    "freecad": {"label": "FreeCAD", "models": [".fcstd"], "drawings": [],
                "launch": ["FreeCAD", "freecad"], "converter": "freecad", "exports": ["stl", "step"]},
    "physcad": {"label": "PhysCAD", "models": [".physcad"], "drawings": [],
                "launch": ["PhysCAD", "physcad"], "converter": None, "exports": []},
}


def _load_cad_apps():
    apps = {k: dict(v) for k, v in _DEFAULT_CAD_APPS.items()}
    for key, over in (CONFIG.get("cad_apps") or {}).items():
        if not isinstance(over, dict):
            continue
        base = apps.setdefault(key.lower(), {"label": key, "models": [], "drawings": [], "launch": [],
                                             "converter": None, "exports": []})
        base.update(over)
    for a in apps.values():
        a["models"] = [e.lower() if e.startswith(".") else "." + e.lower() for e in a.get("models", [])]
        a["drawings"] = [e.lower() if e.startswith(".") else "." + e.lower() for e in a.get("drawings", [])]
        if a.get("exe"):
            a["launch"] = [a["exe"]] + list(a.get("launch", []))
        if isinstance(a.get("converter"), list) and not a.get("exports"):
            a["exports"] = ["stl", "step"]
    return apps


CAD_APPS = _load_cad_apps()
EXT_TO_APP = {}
for _k, _a in CAD_APPS.items():
    for _e in _a["models"] + _a["drawings"]:
        EXT_TO_APP.setdefault(_e, _k)

NEUTRAL_EXTS = {".step", ".stp", ".iges", ".igs"}
CAD_MODEL_EXTS = {e for a in CAD_APPS.values() for e in a["models"]}
CAD_DRAWING_EXTS = {e for a in CAD_APPS.values() for e in a["drawings"]}
PRIMARY_EXTS = CAD_MODEL_EXTS | NEUTRAL_EXTS | CAD_DRAWING_EXTS | {".dxf"}
VAULT_EXTS = PRIMARY_EXTS | {".stl", ".pdf", ".svg", ".obj"}
DRAWING_EXTS = CAD_DRAWING_EXTS | {".dxf", ".pdf", ".svg"}
MODEL_EXTS = CAD_MODEL_EXTS | NEUTRAL_EXTS
AUTO_NUMBER_EXTS = CAD_MODEL_EXTS              # numbered automatically when found in Parts/Assemblies


def cad_app_of(filename):
    """Key of the CAD app that owns this file type, or None (STEP, PDF, STL ...)."""
    return EXT_TO_APP.get(os.path.splitext(filename or "")[1].lower())


def cad_label(filename):
    k = cad_app_of(filename)
    if k:
        return CAD_APPS[k]["label"]
    ext = os.path.splitext(filename or "")[1].upper().lstrip(".")
    return {"STP": "STEP", "IGS": "IGES"}.get(ext, ext)
DERIVED_EXTS = (".stl", ".png", ".jpg")        # regenerated/derived files that travel with a part
SKIP_DIRS = {"_History", "_Templates", ".git", "__pycache__", "Manufacturing_Output"}
PROTECTED_FOLDERS = {"projects", "_standard_library"}
INVALID_NAME_CHARS = re.compile(r'[\\/:*?"<>|]')
SYNC_MARKERS = ("onedrive", "dropbox", "google drive", "googledrive", "my drive", "icloud", "box sync")
REV_PATTERN = re.compile(r"^[A-Z]-\d{2,}$")

# Numbering
PROJECT_RE = re.compile(r"^(\d{2,3})_(.+)$")
PN_RE = re.compile(r"^(\d{2,3})_(?:(A)(\d{3})|(\d{4}))(?![0-9A-Za-z])")
PART_FOLDER_NAMES = {"parts", "part", "prt", "components"}
ASM_FOLDER_NAMES = {"assemblies", "assembly", "asm", "assy"}
DEFAULT_PROJECT_SUBFOLDERS = ("Assemblies", "Parts")
MAX_SERIAL = {"P": 9999, "A": 999}

VAULT_ROOT_LABEL = "Vault Root"


def now_ts():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def now_stamp():
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def norm(p):
    return os.path.normcase(os.path.normpath(p))


def sandbox_path(filename):
    return os.path.join(LOCAL_WORKSPACE, filename)


def folder_abs(rel):
    if not rel or rel in (VAULT_ROOT_LABEL, "*", "."):
        return VAULT_DIR
    return os.path.join(VAULT_DIR, rel)


def folder_rel(abs_dir):
    return VAULT_ROOT_LABEL if norm(abs_dir) == norm(VAULT_DIR) else os.path.relpath(abs_dir, VAULT_DIR)


def is_under(path, parent):
    p, q = norm(os.path.abspath(path)), norm(os.path.abspath(parent))
    return p == q or p.startswith(q + os.sep)


def is_library_path(path):
    return is_under(path, STANDARD_LIB_DIR)


def db_location_warning():
    low = DB_PATH.lower()
    if any(m in low for m in SYNC_MARKERS):
        return ("The vault database is inside a cloud-sync folder. Each PC may sync its own copy, "
                "so locks may not be seen by other users and the database can become corrupted. "
                "Point 'db_path' in atelier_config.json at a single shared location.")
    return None


def open_path(path):
    if sys.platform == "win32":
        os.startfile(path)  # noqa
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


# =============================================================================
# Database
# =============================================================================
@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _add_columns(c, table, cols):
    existing = [col[1] for col in c.execute(f"PRAGMA table_info({table})").fetchall()]
    for col, dtype in cols.items():
        if col not in existing:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {dtype}")


def init_db():
    for folder in (LOCAL_WORKSPACE, LOCAL_LIB_DIR, HISTORY_DIR, STANDARD_LIB_DIR, PROJECTS_DIR, TEMPLATES_DIR):
        os.makedirs(folder, exist_ok=True)
    os.makedirs(folder_abs(PROJECT_ROOT_REL), exist_ok=True)

    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS files (
                        filename TEXT PRIMARY KEY, revision TEXT DEFAULT 'A-01',
                        locked_by TEXT, hash TEXT, last_updated TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS audit_log (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        filename TEXT, action TEXT, revision TEXT,
                        user TEXT, timestamp TEXT, comment TEXT)""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_audit_filename ON audit_log(filename)")
        c.execute("""CREATE TABLE IF NOT EXISTS users (
                        username TEXT PRIMARY KEY, password_hash TEXT, is_admin INTEGER DEFAULT 0)""")
        c.execute("CREATE TABLE IF NOT EXISTS settings (setting_key TEXT PRIMARY KEY, setting_value TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS number_series (series TEXT PRIMARY KEY, last INTEGER DEFAULT 0)")
        c.execute("""CREATE TABLE IF NOT EXISTS projects (
                        prefix TEXT PRIMARY KEY, name TEXT, folder TEXT, created TEXT, created_by TEXT)""")
        _add_columns(c, "users", {"must_change": "INTEGER DEFAULT 0", "last_login": "TEXT", "created": "TEXT"})
        _add_columns(c, "audit_log", {"host": "TEXT DEFAULT ''"})
        if c.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
            c.execute("INSERT INTO users (username, password_hash, is_admin, must_change, created) "
                      "VALUES ('admin', ?, 1, 1, ?)", (hash_password("admin"), now_ts()))
        c.execute("INSERT OR IGNORE INTO settings (setting_key, setting_value) VALUES ('require_login', '1')")
        c.execute("INSERT OR IGNORE INTO settings (setting_key, setting_value) VALUES ('auto_number', '1')")

        _add_columns(c, "files", {
            "category": "TEXT DEFAULT 'Uncategorized'",
            "part_number": "TEXT DEFAULT ''",
            "description": "TEXT DEFAULT ''",
            "is_library": "INTEGER DEFAULT 0",
            "folder_path": "TEXT DEFAULT 'Projects'",
            "part_name": "TEXT DEFAULT ''",
        })
        for row in c.execute("SELECT filename, revision FROM files").fetchall():
            rev = str(row["revision"])
            if rev.isdigit():
                c.execute("UPDATE files SET revision=? WHERE filename=?", (f"A-{int(rev):02d}", row["filename"]))


def get_record(filename):
    with db() as c:
        r = c.execute("SELECT * FROM files WHERE filename=?", (filename,)).fetchone()
        return dict(r) if r else None


def log_audit(filename, action, revision, comment=""):
    with db() as c:
        c.execute("INSERT INTO audit_log (filename, action, revision, user, timestamp, comment, host) "
                  "VALUES (?,?,?,?,?,?,?)",
                  (filename, action, revision or "", USER_NAME, now_ts(), comment or "", HOST_NAME))


def get_audit(filename):
    with db() as c:
        return [tuple(r) for r in c.execute(
            "SELECT timestamp, user, action, revision, comment FROM audit_log WHERE filename=? ORDER BY id DESC",
            (filename,)).fetchall()]


# =============================================================================
# Part-number helpers (pure)
# =============================================================================
def parse_pn(text):
    """'01_0007 Motor Mount' -> ('01', 'P', 7, '01_0007');  '01_A003 Frame' -> ('01', 'A', 3, '01_A003')."""
    m = PN_RE.match(text or "")
    if not m:
        return None
    if m.group(2):
        return m.group(1), "A", int(m.group(3)), m.group(0)
    return m.group(1), "P", int(m.group(4)), m.group(0)


def format_pn(prefix, kind, n):
    return f"{prefix}_A{n:03d}" if kind == "A" else f"{prefix}_{n:04d}"


def clean_part_name(stem):
    """Strips a leading part number / numeric code and a trailing ' A-01' revision tag."""
    s = re.sub(r"\s+[A-Z]-\d{2}$", "", stem).strip()
    m = PN_RE.match(s)
    if m:
        s = s[m.end():]
    else:
        s = re.sub(r"^[\d_]+(?=[\s\-_])", "", s)
    s = s.strip(" _-")
    return s or stem


def numbering_context(rel_folder):
    """For a vault-relative folder returns {'prefix', 'project', 'kind'} or None.
    kind is 'P' (Parts), 'A' (Assemblies) or None (elsewhere inside the project)."""
    if not rel_folder or rel_folder in ("*", VAULT_ROOT_LABEL, "."):
        return None
    parts = os.path.normpath(rel_folder).split(os.sep)
    for i, p in enumerate(parts):
        m = PROJECT_RE.match(p)
        if m:
            if int(m.group(1)) == 0:
                return None                         # 00_ folders are templates
            kind = None
            for sub in parts[i + 1:]:
                low = sub.lower()
                if low in ASM_FOLDER_NAMES:
                    kind = "A"
                elif low in PART_FOLDER_NAMES:
                    kind = "P"
            return {"prefix": m.group(1), "project": os.sep.join(parts[:i + 1]), "kind": kind}
    return None


def display_part_name(filename, rec=None):
    if rec and rec.get("part_name"):
        return rec["part_name"]
    return clean_part_name(os.path.splitext(filename)[0])


# =============================================================================
# Vault index (cached scan)
# =============================================================================
class FileInfo:
    __slots__ = ("name", "path", "folder", "ext", "mtime", "size", "is_library")

    def __init__(self, name, path, folder, mtime, size):
        self.name = name
        self.path = path
        self.folder = folder
        self.ext = os.path.splitext(name)[1].lower()
        self.mtime = mtime
        self.size = size
        self.is_library = is_library_path(path)


class VaultIndex:
    def __init__(self):
        self.files = {}
        self.folders = set()
        self.duplicates = {}
        self.records = {}

    def refresh(self):
        files, folders, dups = {}, set(), {}
        for root, dirs, names in os.walk(VAULT_DIR):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
            rel = os.path.relpath(root, VAULT_DIR)
            if rel != ".":
                folders.add(rel)
            folder = VAULT_ROOT_LABEL if rel == "." else rel
            for n in names:
                if os.path.splitext(n)[1].lower() not in VAULT_EXTS:
                    continue
                p = os.path.join(root, n)
                if n in files:
                    dups.setdefault(n, [files[n].folder]).append(folder)
                    continue
                try:
                    st = os.stat(p)
                    files[n] = FileInfo(n, p, folder, st.st_mtime, st.st_size)
                except OSError:
                    continue
        self.files, self.folders, self.duplicates = files, folders, dups
        self._sync_records()

    def _sync_records(self):
        with db() as c:
            existing = {r["filename"]: dict(r) for r in c.execute("SELECT * FROM files").fetchall()}
            new_rows, moved, pn_fill = [], [], []
            for name, info in self.files.items():
                r = existing.get(name)
                lib = 1 if info.is_library else 0
                p = parse_pn(os.path.splitext(name)[0])
                pn = p[3] if p else ""
                if r is None:
                    new_rows.append((name, info.folder, lib, pn))
                    continue
                if name not in self.duplicates and (r["folder_path"] != info.folder or (r["is_library"] or 0) != lib):
                    moved.append((info.folder, lib, name))
                if pn and not r.get("part_number"):
                    pn_fill.append((pn, name))
            if new_rows:
                c.executemany("INSERT OR IGNORE INTO files (filename, revision, hash, folder_path, is_library, "
                              "part_number) VALUES (?, 'A-01', '', ?, ?, ?)", new_rows)
            if moved:
                c.executemany("UPDATE files SET folder_path=?, is_library=? WHERE filename=?", moved)
            if pn_fill:
                c.executemany("UPDATE files SET part_number=? WHERE filename=?", pn_fill)
            if new_rows or moved or pn_fill:
                existing = {r["filename"]: dict(r) for r in c.execute("SELECT * FROM files").fetchall()}
        self.records = existing

    def reload_records(self):
        with db() as c:
            self.records = {r["filename"]: dict(r) for r in c.execute("SELECT * FROM files").fetchall()}

    def folder_of(self, name):
        info = self.files.get(name)
        if info:
            return info.folder
        return (self.records.get(name) or {}).get("folder_path") or ""


INDEX = VaultIndex()


def file_state(name):
    """Returns (key, label). Keys: dup, lib, new, missing, mine, locked, available."""
    rec = INDEX.records.get(name) or {}
    info = INDEX.files.get(name)
    lock = rec.get("locked_by")
    if name in INDEX.duplicates:
        return "dup", "Duplicate name"
    if info and info.is_library:
        return "lib", "Library · read-only"
    if not info:
        if lock == USER_NAME:
            return "new", "New · not yet in vault"
        return "missing", "Not in vault"
    if lock == USER_NAME:
        return "mine", "Checked out by you"
    if lock:
        return "locked", f"Locked by {lock}"
    return "available", "Available"


# =============================================================================
# File helpers
# =============================================================================
def get_file_hash(filepath):
    h = hashlib.sha256()
    if not os.path.exists(filepath):
        return ""
    with open(filepath, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def safe_copy(src, dst):
    """Copy via a temporary file then atomically replace the destination."""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    tmp = dst + ".partial"
    shutil.copy2(src, tmp)
    os.replace(tmp, dst)


def unique_path(path):
    """Avoids overwriting when two archives land in the same second."""
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    i = 2
    while os.path.exists(f"{base}~{i}{ext}"):
        i += 1
    return f"{base}~{i}{ext}"


def archive_to_history(src, filename, rev, release=False):
    os.makedirs(HISTORY_DIR, exist_ok=True)
    base, ext = os.path.splitext(filename)
    tag = f"RELEASE_{rev}" if release else rev
    dst = unique_path(os.path.join(HISTORY_DIR, f"{base}_{tag}_{now_stamp()}{ext}"))
    shutil.copy2(src, dst)
    return dst


def backup_sandbox_copy(local):
    """Moves a sandbox file into _Discarded so it is never silently lost."""
    os.makedirs(DISCARD_DIR, exist_ok=True)
    dst = unique_path(os.path.join(DISCARD_DIR, f"{now_stamp()}_{os.path.basename(local)}"))
    shutil.move(local, dst)
    return dst


def _baseline_pattern(filename):
    base, ext = os.path.splitext(filename)
    return re.compile(rf"^{re.escape(base)}_(RELEASE_)?[A-Z]-\d+(_\d{{8}}_\d{{6}}(~\d+)?)?{re.escape(ext)}$",
                      re.IGNORECASE)


def baseline_files(filename):
    pat = _baseline_pattern(filename)
    if not os.path.isdir(HISTORY_DIR):
        return []
    out = []
    for f in os.listdir(HISTORY_DIR):
        p = os.path.join(HISTORY_DIR, f)
        if os.path.isfile(p) and pat.match(f):
            out.append((f, os.path.getmtime(p), os.path.getsize(p)))
    return sorted(out, key=lambda x: x[1], reverse=True)


def get_dependencies(filepath, max_bytes=64 * 1024 * 1024):
    """Best-effort: finds referenced filenames inside a file. Read-only, never rewrites files."""
    if not os.path.exists(filepath) or os.path.getsize(filepath) > max_bytes:
        return []
    deps = set()
    exts = "|".join(sorted({re.escape(e[1:]) for e in VAULT_EXTS}, key=len, reverse=True))
    try:
        if zipfile.is_zipfile(filepath):            # FreeCAD .FCStd (and other zipped formats)
            with zipfile.ZipFile(filepath) as z:
                names = [n for n in z.namelist() if n.lower().endswith(".xml")]
                text = " ".join(z.read(n).decode("utf-8", "ignore") for n in names[:4])
        else:
            with open(filepath, "rb") as f:
                text = f.read().decode("latin-1")
        for m in re.findall(rf"[\w\-. ]+\.(?:{exts})\b", text, re.IGNORECASE):
            m = m.strip()
            if m.lower() != os.path.basename(filepath).lower():
                deps.add(m)
    except (OSError, zipfile.BadZipFile, KeyError):
        pass
    return list(deps)


def validate_new_name(name):
    if not name:
        return "Name cannot be blank."
    if INVALID_NAME_CHARS.search(name):
        return 'Name cannot contain any of  \\ / : * ? " < > |'
    if name.strip(" .") != name:
        return "Name cannot start or end with a space or full stop."
    return None


# =============================================================================
# Filename parser & revision maths
# =============================================================================
def parse_structured_name(raw_filename):
    """'01_0054 BRACKET A-01.sc' -> (raw, 'BRACKET', 'SC', 'A-01')"""
    base, ext = os.path.splitext(raw_filename)
    rev_match = re.search(r"\s+([A-Z]-\d{2})$", base)
    rev_code = rev_match.group(1) if rev_match else "A-01"
    return raw_filename, clean_part_name(base), ext.upper().replace(".", ""), rev_code


def calculate_next_rev(curr_rev, bump_type):
    m = re.match(r"^([A-Z])-(\d+)$", curr_rev or "")
    if not m:
        return "A-02" if bump_type == "minor" else "B-01"
    major, minor = m.group(1), int(m.group(2))
    if bump_type == "major":
        if major == "Z":
            return None
        return f"{chr(ord(major) + 1)}-01"
    return f"{major}-{minor + 1:02d}"


# =============================================================================
# Project & part numbering (database-backed reservations)
# =============================================================================
LEGACY_NUM_RE = re.compile(r"^\d{2,3}_[A-Za-z]?\d+(?![A-Za-z])")


def is_numbered_name(stem):
    """True if the name already starts with a part number (new NN_0001 / NN_A001 or an older NN_xxxxx code)."""
    return bool(PN_RE.match(stem) or LEGACY_NUM_RE.match(stem))


def project_root_abs():
    return folder_abs(PROJECT_ROOT_REL)


def list_projects():
    """NN_ project folders directly inside the project root, sorted by number (00_ templates excluded)."""
    root = project_root_abs()
    out = []
    try:
        names = os.listdir(root)
    except OSError:
        names = []
    for n in names:
        p = os.path.join(root, n)
        m = PROJECT_RE.match(n)
        if m and int(m.group(1)) > 0 and os.path.isdir(p):
            out.append({"prefix": m.group(1), "name": m.group(2), "folder": folder_rel(p)})
    return sorted(out, key=lambda r: int(r["prefix"]))


def _project_template_dir():
    root = project_root_abs()
    try:
        for n in sorted(os.listdir(root)):
            p = os.path.join(root, n)
            m = PROJECT_RE.match(n)
            if m and int(m.group(1)) == 0 and os.path.isdir(p):
                return p
    except OSError:
        pass
    return None


def _used_prefixes(c):
    used = {int(p["prefix"]) for p in list_projects()}
    for (p,) in c.execute("SELECT prefix FROM projects").fetchall():
        if str(p).isdigit():
            used.add(int(p))
    return used


def next_project_prefix():
    with db() as c:
        n = max(_used_prefixes(c) | {0}) + 1
    return f"{n:02d}"


def _copy_folder_layout(src, dst):
    """Copies the sub-folder structure (folders only, no files) of src into dst."""
    made = []
    for r, dirs, _ in os.walk(src):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for d in dirs:
            rel = os.path.relpath(os.path.join(r, d), src)
            os.makedirs(os.path.join(dst, rel), exist_ok=True)
            made.append(rel)
    return made


def create_project(name):
    """Creates NN_<name> with the next free NN. Returns (level, message, folder_rel)."""
    name = (name or "").strip()
    err = validate_new_name(name)
    if err:
        return "error", err, None
    if PROJECT_RE.match(name):
        return "error", "Just type the project name. The number is added automatically.", None
    root = project_root_abs()
    os.makedirs(root, exist_ok=True)
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        n = max(_used_prefixes(c) | {0}) + 1
        if n > 999:
            return "error", "No project numbers are left (999 used).", None
        prefix = f"{n:02d}"
        folder = os.path.join(root, f"{prefix}_{name}")
        if os.path.exists(folder):
            return "error", f"A folder called '{prefix}_{name}' already exists.", None
        os.makedirs(folder)
        c.execute("INSERT INTO projects (prefix, name, folder, created, created_by) VALUES (?,?,?,?,?)",
                  (prefix, name, folder_rel(folder), now_ts(), USER_NAME))
    template = _project_template_dir()
    subs = _copy_folder_layout(template, folder) if template else []
    top = {s.split(os.sep)[0].lower() for s in subs}
    if not (top & PART_FOLDER_NAMES) and not (top & ASM_FOLDER_NAMES):
        for s in DEFAULT_PROJECT_SUBFOLDERS:
            os.makedirs(os.path.join(folder, s), exist_ok=True)
            subs.append(s)
    log_audit("[projects]", "Project Created", "", f"{prefix}_{name}")
    src = f" (layout copied from {os.path.basename(template)})" if template else ""
    return "ok", f"Created project '{prefix}_{name}'{src}.", folder_rel(folder)


def _max_existing(c, prefix, kind):
    cands = set(INDEX.files)
    for fn, pn in c.execute("SELECT filename, part_number FROM files WHERE filename LIKE ? OR part_number LIKE ?",
                            (prefix + "%", prefix + "%")).fetchall():
        cands.add(fn)
        cands.add(pn or "")
    for p in list_projects():
        if p["prefix"] == prefix:
            for _, _, fs in os.walk(folder_abs(p["folder"])):
                cands.update(fs)
    best = 0
    for name in cands:
        pp = parse_pn(name)
        if pp and pp[0] == prefix and pp[1] == kind:
            best = max(best, pp[2])
    return best


def _series_last(c, prefix, kind):
    r = c.execute("SELECT last FROM number_series WHERE series=?", (prefix + kind,)).fetchone()
    return r[0] if r else 0


def peek_next_number(prefix, kind):
    """The number the next new file would get (not reserved)."""
    with db() as c:
        n = max(_series_last(c, prefix, kind), _max_existing(c, prefix, kind)) + 1
    return format_pn(prefix, kind, n) if n <= MAX_SERIAL[kind] else None


def reserve_number(prefix, kind):
    """Atomically reserves and returns the next part number. Raises ValueError when the series is full."""
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        n = max(_series_last(c, prefix, kind), _max_existing(c, prefix, kind)) + 1
        if n > MAX_SERIAL[kind]:
            raise ValueError(f"Number series {prefix}_{'A' if kind == 'A' else ''}… is full.")
        c.execute("INSERT OR REPLACE INTO number_series (series, last) VALUES (?, ?)", (prefix + kind, n))
    return format_pn(prefix, kind, n)


def auto_number_enabled():
    return get_setting("auto_number", "1") == "1"


def _companions(path):
    """Files in the same folder with the same base name (drawings, meshes, PDFs, images)."""
    d = os.path.dirname(path)
    base = os.path.splitext(os.path.basename(path))[0].lower()
    exts = VAULT_EXTS | set(DERIVED_EXTS)
    out = []
    try:
        for n in os.listdir(d):
            b, e = os.path.splitext(n)
            p = os.path.join(d, n)
            if b.lower() == base and e.lower() in exts and norm(p) != norm(path):
                out.append(p)
    except OSError:
        pass
    return out


def _rename_baselines(old, new):
    if not os.path.isdir(HISTORY_DIR):
        return
    pat = _baseline_pattern(old)
    ob, nb = os.path.splitext(old)[0], os.path.splitext(new)[0]
    for f in os.listdir(HISTORY_DIR):
        if pat.match(f):
            try:
                os.rename(os.path.join(HISTORY_DIR, f), unique_path(os.path.join(HISTORY_DIR, nb + f[len(ob):])))
            except OSError:
                pass


def _rename_group(path, new_stem, part_name=None, part_number=None, action="Renamed"):
    """Renames a vault file plus same-named companions, and moves their DB rows, audit trail and baselines."""
    d = os.path.dirname(path)
    old_primary = os.path.basename(path)
    plan = []
    for p in [path] + _companions(path):
        n = os.path.basename(p)
        new_n = new_stem + os.path.splitext(n)[1]
        if new_n.lower() != n.lower():
            if os.path.exists(os.path.join(d, new_n)):
                return "error", f"'{new_n}' already exists in that folder."
            if new_n in INDEX.files or get_record(new_n):
                return "error", f"A file called '{new_n}' is already in the vault."
        lock = (get_record(n) or {}).get("locked_by")
        if lock:
            return "error", f"'{n}' is checked out by {lock}. It must be checked in first."
        plan.append((p, n, new_n))
    for p, n, new_n in plan:
        os.rename(p, os.path.join(d, new_n))
        with db() as c:
            c.execute("UPDATE files SET filename=? WHERE filename=?", (new_n, n))
            c.execute("UPDATE audit_log SET filename=? WHERE filename=?", (new_n, n))
        _rename_baselines(n, new_n)
    new_primary = new_stem + os.path.splitext(old_primary)[1]
    with db() as c:
        if part_name is not None:
            c.execute("UPDATE files SET part_name=? WHERE filename=?", (part_name, new_primary))
        if part_number is not None:
            c.execute("UPDATE files SET part_number=? WHERE filename=?", (part_number, new_primary))
        rev = (c.execute("SELECT revision FROM files WHERE filename=?", (new_primary,)).fetchone() or [""])[0]
    extra = f" (+{len(plan) - 1} linked file{'s' if len(plan) > 2 else ''})" if len(plan) > 1 else ""
    log_audit(new_primary, action, rev, f"{old_primary} -> {new_primary}{extra}")
    return "ok", f"{action}: '{old_primary}' -> '{new_primary}'{extra}."


def auto_number_candidates(only_folder=None):
    """Un-numbered CAD files sitting in a project's Parts or Assemblies folder."""
    out = []
    for name, info in INDEX.files.items():
        if info.ext not in AUTO_NUMBER_EXTS or info.is_library or name in INDEX.duplicates:
            continue
        ctx = numbering_context(info.folder)
        if not ctx or not ctx["kind"]:
            continue
        if only_folder and only_folder != "*" and not is_under(info.path, folder_abs(only_folder)):
            continue
        if is_numbered_name(os.path.splitext(name)[0]):
            continue
        if (INDEX.records.get(name) or {}).get("locked_by"):
            continue
        out.append(info)
    return [i.name for i in sorted(out, key=lambda i: i.mtime)]


def auto_number_file(name):
    info = INDEX.files.get(name)
    if not info:
        return "error", f"'{name}' is not in the vault."
    ctx = numbering_context(info.folder)
    if not ctx or not ctx["kind"]:
        return "error", f"'{name}' is not in a project's Parts or Assemblies folder."
    stem = os.path.splitext(name)[0]
    if is_numbered_name(stem):
        return "info", f"'{name}' already has a part number."
    part = clean_part_name(stem)
    try:
        pn = reserve_number(ctx["prefix"], ctx["kind"])
    except ValueError as e:
        return "error", str(e)
    return _rename_group(info.path, f"{pn} {part}", part_name=part, part_number=pn, action="Auto-numbered")


def auto_number_sweep(only_folder=None):
    results = []
    for name in auto_number_candidates(only_folder):
        try:
            results.append(auto_number_file(name))
        except OSError as e:
            results.append(("error", f"Could not number '{name}': {e}"))
        INDEX.refresh()
    return results


def set_auto_number(flag):
    guard = _admin_guard()
    if guard:
        return guard
    set_setting("auto_number", "1" if flag else "0")
    log_audit("[settings]", "Auto-numbering On" if flag else "Auto-numbering Off", "", f"by {ADMIN_SESSION}")
    return "ok", ("New files in Parts / Assemblies folders are now numbered automatically on refresh."
                  if flag else "Automatic numbering on refresh is off. Use the folder menu to number files.")


# =============================================================================
# Core vault operations  ->  each returns (level, message); level in ok/info/warn/error
# =============================================================================
def check_out(filename, comment=""):
    if filename in INDEX.duplicates:
        return "error", (f"'{filename}' exists in more than one folder ({', '.join(INDEX.duplicates[filename])}). "
                         "Rename or remove the duplicate first.")
    info = INDEX.files.get(filename)
    if not info:
        return "error", f"'{filename}' was not found in the vault."

    if info.is_library:
        safe_copy(info.path, os.path.join(LOCAL_LIB_DIR, filename))
        return "ok", f"Library part '{filename}' synced to your local library (read-only)."

    with db() as c:
        c.execute("INSERT OR IGNORE INTO files (filename, revision, hash, is_library, folder_path) "
                  "VALUES (?, 'A-01', '', 0, ?)", (filename, info.folder))
        cur = c.execute("UPDATE files SET locked_by=?, folder_path=? "
                        "WHERE filename=? AND (locked_by IS NULL OR locked_by='')",
                        (USER_NAME, info.folder, filename))
        row = c.execute("SELECT locked_by, revision FROM files WHERE filename=?", (filename,)).fetchone()
        if cur.rowcount == 0:
            return "error", f"'{filename}' is already checked out by {row['locked_by']}."
        rev = row["revision"] or "A-01"

    notes = []
    local = sandbox_path(filename)
    try:
        if os.path.exists(local) and get_file_hash(local) != get_file_hash(info.path):
            notes.append(f"previous sandbox copy backed up to {os.path.basename(backup_sandbox_copy(local))}")
        safe_copy(info.path, local)
        vault_stl = os.path.splitext(info.path)[0] + ".stl"
        if os.path.exists(vault_stl):
            safe_copy(vault_stl, os.path.splitext(local)[0] + ".stl")
        for dep in get_dependencies(info.path):
            d = INDEX.files.get(dep)
            if d:
                dest = os.path.join(LOCAL_LIB_DIR if d.is_library else LOCAL_WORKSPACE, dep)
                if not os.path.exists(dest):
                    shutil.copy2(d.path, dest)
    except Exception as e:
        with db() as c:
            c.execute("UPDATE files SET locked_by=NULL WHERE filename=? AND locked_by=?", (filename, USER_NAME))
        return "error", f"Check-out of '{filename}' failed and the lock was released: {e}"

    log_audit(filename, "Check-Out", rev, comment)
    msg = f"Checked out '{filename}' (Rev {rev})."
    if notes:
        msg += " Note: " + "; ".join(notes) + "."
    return "ok", msg


def check_in(filename, comment=""):
    local = sandbox_path(filename)
    if not os.path.exists(local):
        return "error", f"'{filename}' is not in your sandbox."
    rec = get_record(filename)
    if not rec or not rec.get("locked_by"):
        return "error", f"'{filename}' is not checked out."
    if rec["locked_by"] != USER_NAME:
        return "error", f"'{filename}' is checked out by {rec['locked_by']}, not you."
    if filename in INDEX.duplicates:
        return "error", f"'{filename}' exists in more than one vault folder. Resolve the duplicate first."

    info = INDEX.files.get(filename)
    first_check_in = info is None
    if first_check_in:
        folder = rec.get("folder_path") or "Projects"
        vault_path = os.path.join(folder_abs(folder), filename)
    else:
        folder = info.folder
        vault_path = info.path
    if is_library_path(vault_path):
        return "error", f"'{filename}' is in the Standard Library, which is read-only."

    rev = rec.get("revision") or "A-01"
    mesh_note = ""
    try:
        if not first_check_in and os.path.exists(vault_path):
            archive_to_history(vault_path, filename, rev)          # archive the OLD vault copy
        new_hash = get_file_hash(local)
        safe_copy(local, vault_path)
        local_stl = os.path.splitext(local)[0] + ".stl"
        vault_stl = os.path.splitext(vault_path)[0] + ".stl"
        if can_make_mesh(filename):
            stl, mesh_msg = generate_stl_companion(local)
            if stl:
                safe_copy(stl, vault_stl)
                mesh_note = " 3D mesh updated."
            else:
                mesh_note = f" (Mesh not updated: {mesh_msg})"
        elif os.path.exists(local_stl) and (not os.path.exists(vault_stl) or
                                            os.path.getmtime(local_stl) > os.path.getmtime(vault_stl) + 1):
            safe_copy(local_stl, vault_stl)               # STL you exported yourself (e.g. from PhysCAD)
            mesh_note = " Your exported STL was saved for the preview."
    except Exception as e:
        return "error", f"Check-in of '{filename}' failed; the file is still checked out to you. {e}"

    with db() as c:
        c.execute("UPDATE files SET locked_by=NULL, hash=?, last_updated=?, folder_path=? "
                  "WHERE filename=? AND locked_by=?", (new_hash, now_ts(), folder, filename, USER_NAME))
    log_audit(filename, "Added to Vault" if first_check_in else "Check-In", rev, comment)
    where = f" into '{folder}'" if first_check_in else ""
    return "ok", f"Checked in '{filename}'{where} (Rev {rev}).{mesh_note}"


def undo_check_out(filename):
    rec = get_record(filename)
    if not rec or rec.get("locked_by") != USER_NAME:
        return "error", f"'{filename}' is not checked out by you."
    local = sandbox_path(filename)
    backup = backup_sandbox_copy(local) if os.path.exists(local) else None
    in_vault = filename in INDEX.files
    with db() as c:
        if in_vault:
            c.execute("UPDATE files SET locked_by=NULL WHERE filename=? AND locked_by=?", (filename, USER_NAME))
        else:
            c.execute("DELETE FROM files WHERE filename=? AND locked_by=?", (filename, USER_NAME))
    log_audit(filename, "Undo Check-Out", rec.get("revision"), "Local changes discarded")
    msg = f"Check-out of '{filename}' undone."
    if backup:
        msg += f" Your sandbox copy was kept in _Discarded as '{os.path.basename(backup)}'."
    return "ok", msg


def break_lock(filename, reason):
    if _admin_guard():
        return "error", "Only an administrator can break another user's lock."
    rec = get_record(filename)
    if not rec or not rec.get("locked_by"):
        return "error", f"'{filename}' is not locked."
    owner = rec["locked_by"]
    with db() as c:
        if filename in INDEX.files:
            c.execute("UPDATE files SET locked_by=NULL WHERE filename=? AND locked_by=?", (filename, owner))
        else:
            c.execute("DELETE FROM files WHERE filename=? AND locked_by=?", (filename, owner))
    log_audit(filename, "Lock Broken", rec.get("revision"),
              f"Was locked by {owner}. Reason: {reason}. Admin: {ADMIN_SESSION}")
    return "warn", f"Lock on '{filename}' (held by {owner}) was broken. Their unsaved changes were not checked in."


def up_issue(filename, new_rev, comment=""):
    info = INDEX.files.get(filename)
    if not info:
        return "error", f"'{filename}' is not in the vault."
    if info.is_library:
        return "error", "Standard Library parts cannot be up-issued."
    rec = get_record(filename) or {}
    if rec.get("locked_by"):
        return "error", f"'{filename}' is checked out by {rec['locked_by']}. Check it in before up-issuing."
    if not REV_PATTERN.match(new_rev):
        return "error", f"'{new_rev}' is not a valid revision (expected e.g. A-02, B-01)."
    archive_to_history(info.path, filename, new_rev, release=True)
    with db() as c:
        cur = c.execute("UPDATE files SET revision=? WHERE filename=? AND (locked_by IS NULL OR locked_by='')",
                        (new_rev, filename))
        if cur.rowcount == 0:
            return "error", f"'{filename}' was checked out by someone else while up-issuing. Try again."
    log_audit(filename, f"Up-Issue ({new_rev})", new_rev, comment)
    return "ok", f"Up-issued '{filename}' to Rev {new_rev}. Release baseline archived."


def restore_baseline(filename, history_name):
    rec = get_record(filename)
    if not rec or rec.get("locked_by") != USER_NAME:
        return "error", "Check the file out before restoring a baseline into your sandbox."
    src = os.path.join(HISTORY_DIR, history_name)
    if not os.path.exists(src):
        return "error", f"Baseline '{history_name}' no longer exists."
    local = sandbox_path(filename)
    note = ""
    if os.path.exists(local):
        note = f" Previous sandbox copy kept in _Discarded as '{os.path.basename(backup_sandbox_copy(local))}'."
    shutil.copy2(src, local)
    log_audit(filename, "Restored Baseline", rec.get("revision"), f"From {history_name}")
    return "ok", f"Restored '{history_name}' to your sandbox. Check in to make it the vault copy.{note}"


def delete_component(filename):
    info = INDEX.files.get(filename)
    if not info:
        return "error", f"'{filename}' is not in the vault."
    rec = get_record(filename) or {}
    if rec.get("locked_by"):
        return "error", f"'{filename}' is checked out by {rec['locked_by']}. It must be checked in first."
    dest_dir = unique_path(os.path.join(DELETED_DIR, f"{now_stamp()}_{os.path.splitext(filename)[0]}"))
    os.makedirs(dest_dir, exist_ok=True)
    shutil.move(info.path, os.path.join(dest_dir, filename))
    base = os.path.splitext(info.path)[0]
    for ext in DERIVED_EXTS:                      # derived files only - drawings/PDFs are left alone
        comp = base + ext
        if os.path.exists(comp) and norm(comp) != norm(info.path):
            shutil.move(comp, os.path.join(dest_dir, os.path.basename(comp)))
    with db() as c:
        c.execute("DELETE FROM files WHERE filename=?", (filename,))
    log_audit(filename, "Deleted", rec.get("revision"), f"Archived to {os.path.relpath(dest_dir, VAULT_DIR)}")
    return "ok", f"Deleted '{filename}'. A copy was archived in _History/_Deleted."


def _is_protected_folder(rel):
    if rel in ("*", "", VAULT_ROOT_LABEL, "."):
        return True
    if rel.lower() in PROTECTED_FOLDERS:
        return True
    return norm(folder_abs(rel)) == norm(project_root_abs())


def create_folder(parent_rel, name):
    err = validate_new_name(name)
    if err:
        return "error", err
    parent = folder_abs(parent_rel) if parent_rel not in ("*", "") else project_root_abs()
    if is_under(parent, HISTORY_DIR):
        return "error", "Folders cannot be created inside _History."
    new_dir = os.path.join(parent, name)
    if os.path.exists(new_dir):
        return "error", f"Folder '{name}' already exists here."
    os.makedirs(new_dir)
    return "ok", f"Created folder '{os.path.relpath(new_dir, VAULT_DIR)}'."


def delete_folder(rel):
    if _is_protected_folder(rel):
        return "error", f"'{rel}' is a protected folder and cannot be deleted."
    abs_dir = folder_abs(rel)
    if not os.path.isdir(abs_dir):
        return "error", f"Folder '{rel}' does not exist."
    inside = [n for n, i in INDEX.files.items() if is_under(i.path, abs_dir)]
    locked = [n for n in inside if (INDEX.records.get(n) or {}).get("locked_by")]
    if locked:
        return "error", f"Cannot delete '{rel}': {len(locked)} file(s) are checked out (e.g. '{locked[0]}')."
    os.makedirs(DELETED_DIR, exist_ok=True)
    dest = unique_path(os.path.join(DELETED_DIR, f"{now_stamp()}_{os.path.basename(abs_dir)}"))
    shutil.move(abs_dir, dest)
    with db() as c:
        for n in inside:
            c.execute("DELETE FROM files WHERE filename=?", (n,))
    for n in inside:
        log_audit(n, "Deleted", "", f"Folder '{rel}' archived")
    return "ok", f"Deleted folder '{rel}' ({len(inside)} file(s)). It was archived in _History/_Deleted."


def move_component(filename, target_rel):
    info = INDEX.files.get(filename)
    if not info:
        return "error", f"'{filename}' is not in the vault."
    rec = INDEX.records.get(filename) or {}
    if rec.get("locked_by") and rec["locked_by"] != USER_NAME:
        return "error", f"'{filename}' is checked out by {rec['locked_by']} and cannot be moved."
    dest_dir = folder_abs(target_rel)
    if is_under(dest_dir, HISTORY_DIR):
        return "error", "Files cannot be moved into _History."
    if norm(dest_dir) == norm(os.path.dirname(info.path)):
        return "info", ""
    new_path = os.path.join(dest_dir, filename)
    if os.path.exists(new_path):
        return "error", f"A file called '{filename}' already exists in the target folder."
    os.makedirs(dest_dir, exist_ok=True)
    shutil.move(info.path, new_path)
    base_old, base_new = os.path.splitext(info.path)[0], os.path.splitext(new_path)[0]
    for ext in DERIVED_EXTS:
        if os.path.exists(base_old + ext) and norm(base_old + ext) != norm(info.path):
            shutil.move(base_old + ext, base_new + ext)
    new_folder = folder_rel(dest_dir)
    with db() as c:
        c.execute("UPDATE files SET folder_path=?, is_library=? WHERE filename=?",
                  (new_folder, 1 if is_library_path(new_path) else 0, filename))
    log_audit(filename, "Moved", rec.get("revision"), f"{info.folder} -> {new_folder}")
    msg = f"Moved '{filename}' to '{new_folder}'."
    ctx = numbering_context(new_folder)
    if ctx and ctx["kind"] and not is_numbered_name(os.path.splitext(filename)[0]) and \
            os.path.splitext(filename)[1].lower() in AUTO_NUMBER_EXTS:
        msg += " It will be given a part number on the next refresh."
    return "ok", msg


def numbering_for_target(target_rel, ext):
    """(prefix, kind) if a new file of this type in target_rel should be auto-numbered, else None."""
    ctx = numbering_context(target_rel)
    if ctx and ctx["kind"] and ext.lower() in AUTO_NUMBER_EXTS:
        return ctx["prefix"], ctx["kind"]
    return None


def create_from_template(template_name, new_name, target_rel):
    ext = os.path.splitext(template_name)[1]
    name = (new_name or "").strip()
    if name.lower().endswith(ext.lower()):
        name = name[:-len(ext)].strip()
    err = validate_new_name(name)
    if err:
        return "error", err
    dest_dir = folder_abs(target_rel)
    if is_library_path(dest_dir) or is_under(dest_dir, HISTORY_DIR):
        return "error", "New parts cannot be created in the Standard Library or _History."
    numbering = numbering_for_target(target_rel, ext)
    pn, part = "", name
    if numbering:
        if is_numbered_name(name):
            part = clean_part_name(name)
        try:
            pn = reserve_number(*numbering)
        except ValueError as e:
            return "error", str(e)
        filename = f"{pn} {part}{ext}"
    else:
        filename = name + ext
    if filename in INDEX.files or get_record(filename):
        return "error", f"'{filename}' already exists in the vault."
    local = sandbox_path(filename)
    if os.path.exists(local):
        return "error", f"'{filename}' already exists in your sandbox."
    shutil.copy2(os.path.join(TEMPLATES_DIR, template_name), local)
    folder = folder_rel(dest_dir)
    with db() as c:
        c.execute("INSERT INTO files (filename, revision, locked_by, hash, is_library, folder_path, part_number, "
                  "part_name) VALUES (?, 'A-01', ?, '', 0, ?, ?, ?)", (filename, USER_NAME, folder, pn, part))
    log_audit(filename, "Created from Template", "A-01", f"From {template_name}" + (f", P/N {pn}" if pn else ""))
    lead = f"Part number {pn} assigned. " if pn else ""
    return "ok", (f"{lead}Created '{filename}' from '{template_name}'. It is checked out to you; "
                  f"check in to add it to '{folder}'."), filename


def add_files_to_vault(paths, target_rel, comment=""):
    results = []
    dest_dir = folder_abs(target_rel)
    if is_under(dest_dir, HISTORY_DIR):
        return [("error", "Files cannot be added to _History.")]
    folder = folder_rel(dest_dir)
    for src in paths:
        orig = os.path.basename(src)
        stem, ext = os.path.splitext(orig)
        if ext.lower() not in VAULT_EXTS:
            results.append(("warn", f"Skipped '{orig}': unsupported file type."))
            continue
        name, pn, part = orig, "", clean_part_name(stem)
        numbering = numbering_for_target(target_rel, ext)
        if numbering and not is_numbered_name(stem):
            try:
                pn = reserve_number(*numbering)
            except ValueError as e:
                results.append(("error", str(e)))
                continue
            name = f"{pn} {part}{ext}"
        elif parse_pn(stem):
            pn = parse_pn(stem)[3]
        if name in INDEX.files or get_record(name):
            results.append(("warn", f"Skipped '{orig}': a file called '{name}' is already in the vault."))
            continue
        dst = os.path.join(dest_dir, name)
        safe_copy(src, dst)
        src_stl = os.path.splitext(src)[0] + ".stl"
        if ext.lower() in CAD_MODEL_EXTS and os.path.exists(src_stl):
            safe_copy(src_stl, os.path.splitext(dst)[0] + ".stl")
        with db() as c:
            c.execute("INSERT INTO files (filename, revision, hash, is_library, folder_path, last_updated, "
                      "part_number, part_name) VALUES (?, 'A-01', ?, ?, ?, ?, ?, ?)",
                      (name, get_file_hash(dst), 1 if is_library_path(dst) else 0, folder, now_ts(), pn, part))
        log_audit(name, "Added to Vault", "A-01", comment or f"Imported from {src}")
        renamed = f" as '{name}'" if name != orig else ""
        results.append(("ok", f"Added '{orig}'{renamed} to '{folder}'."))
    return results


def save_metadata(filename, part_name, part_number, description):
    with db() as c:
        cur = c.execute("UPDATE files SET part_name=?, part_number=?, description=? WHERE filename=?",
                        (part_name, part_number, description, filename))
        if cur.rowcount == 0:
            return "error", f"No record for '{filename}'."
    log_audit(filename, "Properties Updated", "",
              f"Name='{part_name}' PN='{part_number}' Desc='{description}'")
    return "ok", f"Saved properties for '{filename}'."


def rename_component(filename, new_part_name):
    """Renames the file to '<part number> <new name>.ext' (number kept). Returns (level, message, new filename)."""
    name = (new_part_name or "").strip()
    err = validate_new_name(name)
    if err:
        return "error", err, filename
    stem, ext = os.path.splitext(filename)
    pp, lm = parse_pn(stem), LEGACY_NUM_RE.match(stem)
    num = pp[3] if pp else (lm.group(0) if lm else "")
    if num and is_numbered_name(name):
        name = clean_part_name(name)
    new_stem = f"{num} {name}" if num else name
    new_file = new_stem + ext
    if new_file == filename:
        return "info", "File name unchanged.", filename
    rec = get_record(filename) or {}
    info = INDEX.files.get(filename)
    if info is None:
        if rec.get("locked_by") != USER_NAME:
            return "error", f"'{filename}' is not in the vault.", filename
        local, new_local = sandbox_path(filename), sandbox_path(new_file)
        if os.path.exists(new_local) or get_record(new_file):
            return "error", f"'{new_file}' already exists.", filename
        os.rename(local, new_local)
        if os.path.exists(os.path.splitext(local)[0] + ".stl"):
            os.rename(os.path.splitext(local)[0] + ".stl", os.path.splitext(new_local)[0] + ".stl")
        with db() as c:
            c.execute("UPDATE files SET filename=?, part_name=? WHERE filename=?", (new_file, name, filename))
            c.execute("UPDATE audit_log SET filename=? WHERE filename=?", (new_file, filename))
        log_audit(new_file, "Renamed", rec.get("revision"), f"{filename} -> {new_file}")
        return "ok", f"Renamed '{filename}' to '{new_file}'.", new_file
    if rec.get("locked_by"):
        who = "you" if rec["locked_by"] == USER_NAME else rec["locked_by"]
        return "error", f"'{filename}' is checked out by {who}. Check it in before renaming.", filename
    level, msg = _rename_group(info.path, new_stem, part_name=name, action="Renamed")
    return level, msg, (new_file if level == "ok" else filename)


def open_in_cad(filename):
    local, lib = sandbox_path(filename), os.path.join(LOCAL_LIB_DIR, filename)
    target = local if os.path.exists(local) else lib if os.path.exists(lib) else None
    if not target:
        return "error", f"'{filename}' is not in your sandbox. Check it out first."
    app = cad_app_of(filename)
    exe = cad_launcher(app) if app else None
    where = CAD_APPS[app]["label"] if app else "its default program"
    try:
        if exe:
            subprocess.Popen([exe, os.path.abspath(target)])
        else:
            open_path(os.path.abspath(target))        # Windows file association
    except Exception as e:
        return "error", (f"Could not open '{filename}' in {where}: {e}. Install it or set its program path in "
                         "atelier_config.json (see Administration > CAD Apps).")
    key, _ = file_state(filename)
    if key not in ("mine", "new", "lib"):
        return "warn", f"Opened '{filename}' in {where} as a reference copy. Check it out before making changes."
    return "ok", f"Opening '{filename}' in {where}."


# =============================================================================
# STL companion, export & mesh handling
# =============================================================================
_TOOL_CACHE = {}


def _program_globs(names):
    """Usual install locations for FreeCAD on Windows / macOS."""
    roots = [os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramFiles(x86)", ""),
             os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs")]
    out = []
    for n in names:
        for r in roots:
            if r:
                out.append(os.path.join(r, "FreeCAD*", "bin", n + ".exe"))
        out.append(f"/Applications/FreeCAD.app/Contents/Resources/bin/{n}")
    return out


def find_program(names, globs=()):
    key = (tuple(names), tuple(globs))
    if key in _TOOL_CACHE:
        return _TOOL_CACHE[key]
    found = None
    for n in names:
        if not n:
            continue
        if os.path.isabs(n):
            if os.path.exists(n):
                found = n
                break
            continue
        p = shutil.which(n)
        if p:
            found = p
            break
    if not found:
        for g in globs:
            hits = sorted(glob.glob(g), reverse=True)        # newest version folder first
            if hits:
                found = hits[0]
                break
    _TOOL_CACHE[key] = found
    return found


def clear_tool_cache():
    _TOOL_CACHE.clear()


def freecad_cmd():
    a = CAD_APPS.get("freecad", {})
    names = ([a["cmd"]] if a.get("cmd") else []) + ["FreeCADCmd", "freecadcmd"]
    return find_program(names, _program_globs(["FreeCADCmd", "freecadcmd"]))


def cad_launcher(app):
    a = CAD_APPS.get(app)
    if not a:
        return None
    globs = _program_globs(["FreeCAD", "freecad"]) if app == "freecad" else ()
    return find_program(a.get("launch", []), globs)


def converter_status(app):
    """(available, description) for an app's command-line converter."""
    a = CAD_APPS.get(app, {})
    conv = a.get("converter")
    if conv == "solidcraft-cli":
        p = find_program(["solidcraft-cli"])
        return bool(p), p or "solidcraft-cli not found on PATH"
    if conv == "freecad":
        p = freecad_cmd()
        return bool(p), p or "FreeCADCmd not found"
    if isinstance(conv, list) and conv:
        p = find_program([conv[0]])
        return bool(p), p or f"{conv[0]} not found"
    return False, "No command-line export"


def can_convert(filename, out_kind):
    """True if a mesh/STEP/PDF can be produced automatically from this file."""
    ext = os.path.splitext(filename)[1].lower()
    if ext in NEUTRAL_EXTS:
        return out_kind == "stl" and bool(freecad_cmd())
    app = cad_app_of(filename)
    if not app or ext not in CAD_APPS[app]["models"] + CAD_APPS[app]["drawings"]:
        return False
    if out_kind not in CAD_APPS[app].get("exports", []):
        return False
    return converter_status(app)[0]


def can_make_mesh(filename):
    return can_convert(filename, "stl")


def _run(cmd, out, timeout, label):
    started = time.time() - 2
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                             text=True, timeout=timeout, **_NO_WINDOW)
    except FileNotFoundError:
        return False, f"'{cmd[0]}' was not found", None
    except subprocess.TimeoutExpired:
        return False, f"{label} timed out after {timeout} s", None
    except Exception as e:
        return False, str(e), None
    if os.path.exists(out) and os.path.getmtime(out) >= started:
        return True, "ok", res
    return False, (res.stderr or res.stdout or "no output").strip()[-300:], res


def _solidcraft_cli(src, out, timeout=300):
    ok, msg, res = _run(["solidcraft-cli", "run", "--in", src, "--out", out], out, timeout, "solidcraft-cli")
    if not ok and res is not None:
        ok, msg, _ = _run(["solidcraft-cli", "run", src, "--out", out], out, timeout, "solidcraft-cli")
    return ok, msg


_FREECAD_SCRIPT = r"""# -*- coding: utf-8 -*-
import os
SRC = __SRC__
OUT = __OUT__
try:
    import FreeCAD, Part, Mesh
    ext = os.path.splitext(SRC)[1].lower()
    if ext in (".step", ".stp", ".iges", ".igs"):
        shape = Part.Shape()
        shape.read(SRC)
    else:
        doc = FreeCAD.openDocument(SRC)
        try:
            doc.recompute()
        except Exception:
            pass

        def shape_of(o):
            try:
                s = Part.getShape(o)
            except Exception:
                return None
            return s if (not s.isNull() and (s.Solids or s.Shells or s.Faces)) else None

        visible = [o for o in doc.Objects if getattr(o, "Visibility", True)]
        shapes = [s for s in (shape_of(o) for o in visible if not o.InList) if s]
        if not shapes:
            shapes = [s for s in (shape_of(o) for o in visible if o.TypeId == "PartDesign::Body") if s]
        if not shapes:
            shapes = [s for s in (shape_of(o) for o in visible) if s and s.Solids]
        if not shapes:
            raise RuntimeError("no visible solid found in the document")
        shape = shapes[0] if len(shapes) == 1 else Part.makeCompound(shapes)
    oext = os.path.splitext(OUT)[1].lower()
    if oext in (".step", ".stp"):
        shape.exportStep(OUT)
    elif oext in (".iges", ".igs"):
        shape.exportIges(OUT)
    elif oext == ".stl":
        Mesh.Mesh(shape.tessellate(0.1)).write(OUT)
    else:
        raise RuntimeError("unsupported output type " + oext)
    print("ATELIER_OK")
except Exception as e:
    print("ATELIER_ERROR: %s" % e)
"""


def _freecad_convert(src, out, timeout=300):
    cmd = freecad_cmd()
    if not cmd:
        return False, "FreeCADCmd was not found. Install FreeCAD or set its path in atelier_config.json"
    tmp_dir = os.path.join(LOCAL_WORKSPACE, "_tmp")
    os.makedirs(tmp_dir, exist_ok=True)
    script = os.path.join(tmp_dir, f"atelier_convert_{os.getpid()}_{int(time.time() * 1000)}.py")
    with open(script, "w", encoding="utf-8") as f:
        f.write(_FREECAD_SCRIPT.replace("__SRC__", repr(os.path.abspath(src)))
                .replace("__OUT__", repr(os.path.abspath(out))))
    try:
        ok, msg, res = _run([cmd, script], out, timeout, "FreeCADCmd")
    finally:
        try:
            os.remove(script)
        except OSError:
            pass
    if not ok and res is not None:
        m = re.search(r"ATELIER_ERROR: (.*)", (res.stdout or "") + (res.stderr or ""))
        if m:
            msg = m.group(1).strip()
    return ok, msg


def cad_convert(src, out):
    """Converts src (model or drawing) to out (.stl / .step / .pdf). Returns (ok, message)."""
    if os.path.splitext(src)[1].lower() in NEUTRAL_EXTS:
        return _freecad_convert(src, out)
    app = cad_app_of(src)
    if not app:
        return False, "unsupported file type"
    conv = CAD_APPS[app].get("converter")
    if conv == "solidcraft-cli":
        return _solidcraft_cli(src, out)
    if conv == "freecad":
        return _freecad_convert(src, out)
    if isinstance(conv, list) and conv:
        cmd = [c.replace("{in}", src).replace("{out}", out) for c in conv]
        ok, msg, _ = _run(cmd, out, 300, conv[0])
        return ok, msg
    return False, f"{CAD_APPS[app]['label']} has no command-line export"


def run_cli(src, out, timeout=300):
    """Kept for compatibility: converts with whichever tool suits the source file."""
    return cad_convert(src, out)


def generate_stl_companion(path):
    """Returns (stl_path or None, message)."""
    if not os.path.exists(path):
        return None, "file not found"
    if not can_make_mesh(path):
        app = cad_app_of(path)
        if app and not CAD_APPS[app].get("converter"):
            return None, (f"{CAD_APPS[app]['label']} can't make meshes automatically. Export an STL with the same "
                          "name into your sandbox and check in, or put it next to the part, to get a 3D preview.")
        ok, why = converter_status(app) if app else (False, "FreeCADCmd is needed to mesh STEP/IGES files")
        return None, why
    stl_path = os.path.splitext(path)[0] + ".stl"
    ok, msg = cad_convert(path, stl_path)
    return (stl_path, "ok") if ok else (None, msg)


def _companion(base, exts):
    for e in exts:
        for cand in (base + e, base + e.upper()):
            if os.path.exists(cand):
                return cand
    return None


def export_deliverables(filename):
    """STEP + PDF into Manufacturing_Output. Uses the CAD app's converter where there is one, otherwise a
    same-named STEP / PDF saved next to the part. Returns [(level, msg)]."""
    info = INDEX.files.get(filename)
    if not info:
        return [("error", f"'{filename}' is not in the vault.")]
    app = cad_app_of(filename)
    if not app or info.ext not in CAD_MODEL_EXTS:
        labels = ", ".join(a["label"] for a in CAD_APPS.values())
        return [("error", f"Select a part or assembly file ({labels}) to export, not a drawing or mesh.")]
    label = CAD_APPS[app]["label"]
    rec = INDEX.records.get(filename) or {}
    stem = os.path.splitext(filename)[0]
    part = display_part_name(filename, rec)
    pp = parse_pn(stem)
    pn = rec.get("part_number") or (pp[3] if pp else "00_0000")
    rev = rec.get("revision") or parse_structured_name(filename)[3]
    out_stem = re.sub(r"[^\w.\-]+", "_", f"{pn}_{part}_{rev}").strip("_")
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    results, made = [], []
    if rec.get("locked_by") == USER_NAME:
        results.append(("warn", "You have this part checked out. The export uses the vault copy, "
                                "not your unsaved sandbox changes."))
    base = os.path.splitext(info.path)[0]
    model_time = info.mtime

    def use_companion(kind, exts, dest):
        comp = _companion(base, exts)
        if not comp:
            return False
        shutil.copy2(comp, dest)
        made.append(dest)
        stale = os.path.getmtime(comp) + 1 < model_time
        results.append(("warn" if stale else "ok",
                        f"{kind} copied from '{os.path.basename(comp)}'" +
                        (" - it is older than the model, so it may be out of date." if stale else ".")))
        return True

    # STEP
    step = os.path.join(OUTPUT_DIR, out_stem + ".step")
    if can_convert(filename, "step"):
        ok, msg = cad_convert(info.path, step)
        if ok:
            made.append(step)
            results.append(("ok", f"STEP exported with {label}: {os.path.basename(step)}"))
        elif not use_companion("STEP", (".step", ".stp"), step):
            results.append(("error", f"STEP export failed: {msg}"))
    elif not use_companion("STEP", (".step", ".stp"), step):
        results.append(("error", f"{label} can't export STEP automatically. Export '{stem}.step' from {label} "
                                 "next to the part and run this again."))

    # PDF
    pdf = os.path.join(OUTPUT_DIR, out_stem + ".pdf")
    drawing = _companion(base, CAD_APPS[app]["drawings"])
    if drawing and can_convert(drawing, "pdf"):
        ok, msg = cad_convert(drawing, pdf)
        if ok:
            made.append(pdf)
            results.append(("ok", f"PDF exported: {os.path.basename(pdf)}"))
        elif not use_companion("PDF", (".pdf",), pdf):
            results.append(("error", f"PDF export failed: {msg}"))
    elif not use_companion("PDF", (".pdf",), pdf):
        why = ("FreeCAD drawings live inside the .FCStd and can only be printed from FreeCAD itself. "
               if app == "freecad" else "")
        results.append(("warn", f"No PDF made. {why}Save the drawing as '{stem}.pdf' next to the part "
                                "and it will be picked up."))
    if made:
        log_audit(filename, "Export Deliverables", rev, ", ".join(os.path.basename(m) for m in made))
    return results


_THUMB_CACHE = {}


def fcstd_thumbnail(path):
    """PNG bytes of the thumbnail FreeCAD stores inside an .FCStd (if it saved one)."""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    hit = _THUMB_CACHE.get(path)
    if hit and hit[0] == mtime:
        return hit[1]
    data = None
    try:
        with zipfile.ZipFile(path) as z:
            for n in z.namelist():
                if n.lower().startswith("thumbnails/") and n.lower().endswith(".png"):
                    data = z.read(n)
                    break
    except (OSError, zipfile.BadZipFile):
        data = None
    _THUMB_CACHE[path] = (mtime, data)
    return data


def physcad_thumbnail(path, max_bytes=64 * 1024 * 1024):
    """PNG/GIF bytes of the preview image PhysCAD stores inside a .physcad file (if any)."""
    try:
        mtime = os.path.getmtime(path)
        if os.path.getsize(path) > max_bytes:
            return None
    except OSError:
        return None
    hit = _THUMB_CACHE.get(path)
    if hit and hit[0] == mtime:
        return hit[1]
    data = None
    try:
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as z:
                names = z.namelist()
                pngs = [n for n in names if n.lower().endswith(".png")]
                if pngs:
                    data = z.read(pngs[0])
                raw = b" ".join(z.read(n) for n in names
                                if not n.lower().endswith((".png", ".jpg", ".jpeg")))
        else:
            with open(path, "rb") as fh:
                raw = fh.read()
        if data is None:
            m = re.search(rb"data:image\\?/(?:png|gif);base64,([A-Za-z0-9+/=\\\r\n]+)", raw)
            if m:
                b64 = m.group(1).replace(b"\\/", b"/").replace(b"\\n", b"").replace(b"\\r", b"")
                b64 = re.sub(rb"[^A-Za-z0-9+/=]", b"", b64)       # JSON escapes and line breaks
                data = base64.b64decode(b64 + b"=" * (-len(b64) % 4))
                if not (data.startswith(b"\x89PNG") or data.startswith(b"GIF8")):
                    data = None
    except (OSError, zipfile.BadZipFile, ValueError):
        data = None
    _THUMB_CACHE[path] = (mtime, data)
    return data


def cad_thumbnail(path):
    """Thumbnail image bytes embedded in a CAD file, for apps that store one."""
    if not path:
        return None
    ext = os.path.splitext(path)[1].lower()
    if ext == ".fcstd":
        return fcstd_thumbnail(path)
    if ext == ".physcad":
        return physcad_thumbnail(path)
    return None


def cad_status():
    rows = []
    for k, a in CAD_APPS.items():
        exe = cad_launcher(k)
        conv_ok, conv = converter_status(k)
        rows.append({"key": k, "label": a["label"], "types": " ".join(a["models"] + a["drawings"]),
                     "open": exe or "Windows file association",
                     "convert": conv if conv_ok else f"— {conv}",
                     "auto": ", ".join(a.get("exports", [])).upper() if conv_ok and a.get("exports") else
                     "Uses STEP/PDF/STL saved next to the part"})
    return rows


def parse_stl_file(filepath, max_faces=2500):
    try:
        with open(filepath, "rb") as f:
            data = f.read()
        if len(data) < 84:
            return [], []
        count = struct.unpack("<I", data[80:84])[0]
        is_binary = abs(len(data) - (84 + count * 50)) < 100
        if not is_binary and data.lstrip().startswith(b"solid"):
            text = data.decode("utf-8", errors="ignore")
            normals = re.findall(r"facet\s+normal\s+([-\d.eE+]+)\s+([-\d.eE+]+)\s+([-\d.eE+]+)", text)
            verts_raw = re.findall(r"vertex\s+([-\d.eE+]+)\s+([-\d.eE+]+)\s+([-\d.eE+]+)", text)
            vertices = [(float(a), float(b), float(c)) for a, b, c in verts_raw]
            faces = []
            step = max(1, (len(vertices) // 3) // max_faces)
            for i in range(0, len(vertices) - 2, 3):
                fi = i // 3
                if fi % step == 0:
                    n = tuple(map(float, normals[fi])) if fi < len(normals) else (0.0, 0.0, 1.0)
                    faces.append((i, i + 1, i + 2, n))
            return vertices, faces
        vertices, faces = [], []
        step = max(1, count // max_faces)
        offset = 84
        for i in range(count):
            if offset + 50 > len(data):
                break
            if i % step == 0:
                fl = struct.unpack("<12f", data[offset:offset + 48])
                idx = len(vertices)
                vertices.extend([fl[3:6], fl[6:9], fl[9:12]])
                faces.append((idx, idx + 1, idx + 2, fl[0:3]))
            offset += 50
        return vertices, faces
    except Exception:
        return [], []


_MESH_CACHE = {}


def load_mesh(path):
    """Cached by path + modification time. Returns dict or None."""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    hit = _MESH_CACHE.get(path)
    if hit and hit[0] == mtime:
        return hit[1]
    v, f = parse_stl_file(path)
    mesh = None
    if v and f:
        xs, ys, zs = zip(*v)
        mesh = {"v": v, "f": f,
                "c": ((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2, (min(zs) + max(zs)) / 2),
                "d": max(max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs)) or 1.0,
                "n": len(f)}
    _MESH_CACHE[path] = (mtime, mesh)
    return mesh


def find_preview_stl(filename):
    """Prefer your sandbox mesh when you have the file checked out, else the vault mesh."""
    local = sandbox_path(filename)
    info = INDEX.files.get(filename)
    key, _ = file_state(filename)
    local_first = key in ("mine", "new")
    local_c = [local] if local.lower().endswith(".stl") else [os.path.splitext(local)[0] + ".stl"]
    vault_c = []
    if info:
        vault_c = [info.path] if info.ext == ".stl" else [os.path.splitext(info.path)[0] + ".stl"]
    for c in (local_c + vault_c) if local_first else (vault_c + local_c):
        if os.path.exists(c):
            return c
    return None


# =============================================================================
# Accounts & administration
# =============================================================================
def hash_password(password):
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password, stored):
    """Returns (ok, needs_rehash). Accepts the v1 unsalted SHA-256 hashes and upgrades them."""
    if not stored:
        return False, False
    if stored.startswith("pbkdf2_sha256$"):
        try:
            _, iters, salt, digest = stored.split("$")
            dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), int(iters))
        except ValueError:
            return False, False
        return hmac.compare_digest(dk.hex(), digest), int(iters) < PBKDF2_ITERATIONS
    for candidate in (password, password.strip()):
        if hmac.compare_digest(hashlib.sha256(candidate.encode("utf-8")).hexdigest(), stored):
            return True, True
    return False, False


def password_problem(password, username=""):
    if len(password) < MIN_PASSWORD_LEN:
        return f"Passwords must be at least {MIN_PASSWORD_LEN} characters."
    if password.lower() in {"password", "admin1", "123456", "atelier", "qwerty"} or \
            (username and password.lower() == username.lower()):
        return "That password is too easy to guess."
    return None


def is_admin():
    return bool(ADMIN_SESSION)


def _admin_guard():
    """Re-checks the database so a demoted or deleted administrator loses rights immediately."""
    if is_admin():
        with db() as c:
            r = c.execute("SELECT is_admin FROM users WHERE username=?", (ADMIN_SESSION,)).fetchone()
        if r and r["is_admin"]:
            return None
    return "error", "Administrator rights are required."


def get_setting(key, default=None):
    with db() as c:
        r = c.execute("SELECT setting_value FROM settings WHERE setting_key=?", (key,)).fetchone()
    return r[0] if r else default


def set_setting(key, value):
    with db() as c:
        c.execute("INSERT OR REPLACE INTO settings (setting_key, setting_value) VALUES (?, ?)", (key, str(value)))


def login_required():
    return get_setting("require_login", "1") == "1"


def authenticate(username, password):
    """Returns (account dict or None, error message)."""
    if not username or not password:
        return None, "Enter your username and password."
    with db() as c:
        r = c.execute("SELECT username, password_hash, is_admin, must_change FROM users "
                      "WHERE username=? COLLATE NOCASE", (username,)).fetchone()
        ok, rehash = verify_password(password, r["password_hash"]) if r else (False, False)
        if not ok:
            return None, "Incorrect username or password."
        if rehash:
            c.execute("UPDATE users SET password_hash=? WHERE username=?", (hash_password(password), r["username"]))
        c.execute("UPDATE users SET last_login=? WHERE username=?", (now_ts(), r["username"]))
    weak = password_problem(password, r["username"]) is not None
    return {"username": r["username"], "is_admin": bool(r["is_admin"]),
            "must_change": bool(r["must_change"]) or weak}, ""


def user_exists(username):
    with db() as c:
        return c.execute("SELECT 1 FROM users WHERE username=? COLLATE NOCASE", (username,)).fetchone() is not None


def list_users():
    with db() as c:
        return [dict(r) for r in c.execute("SELECT username, is_admin, must_change, last_login, created "
                                           "FROM users ORDER BY username COLLATE NOCASE").fetchall()]


def _admin_count(c):
    return c.execute("SELECT COUNT(*) FROM users WHERE is_admin=1").fetchone()[0]


def set_password(username, password, must_change=False):
    """Internal: caller must already have verified the user or hold admin rights."""
    problem = password_problem(password, username)
    if problem:
        return "error", problem
    with db() as c:
        cur = c.execute("UPDATE users SET password_hash=?, must_change=? WHERE username=?",
                        (hash_password(password), 1 if must_change else 0, username))
        if cur.rowcount == 0:
            return "error", f"User '{username}' does not exist."
    log_audit("[users]", "Password Set", "", username)
    return "ok", f"Password updated for '{username}'."


def change_own_password(username, old, new):
    with db() as c:
        r = c.execute("SELECT password_hash FROM users WHERE username=?", (username,)).fetchone()
    if not r or not verify_password(old, r["password_hash"])[0]:
        return "error", "Your current password is incorrect."
    if old == new:
        return "error", "The new password must be different."
    return set_password(username, new)


def add_user(username, password, admin=False):
    guard = _admin_guard()
    if guard:
        return guard
    username = username.strip()
    if not USERNAME_RE.match(username):
        return "error", "Usernames are 2-32 characters: letters, numbers, dot, dash or underscore."
    problem = password_problem(password, username)
    if problem:
        return "error", problem
    if user_exists(username):
        return "error", f"A user called '{username}' already exists."
    with db() as c:
        c.execute("INSERT INTO users (username, password_hash, is_admin, must_change, created) VALUES (?,?,?,1,?)",
                  (username, hash_password(password), 1 if admin else 0, now_ts()))
    log_audit("[users]", "User Added", "", f"{username}{' (admin)' if admin else ''} by {ADMIN_SESSION}")
    return "ok", f"Added '{username}'. They will choose their own password the first time they sign in."


def admin_reset_password(username, password):
    guard = _admin_guard()
    if guard:
        return guard
    level, msg = set_password(username, password, must_change=True)
    if level == "ok":
        msg = f"Password reset for '{username}'. They must change it at their next sign-in."
    return level, msg


def set_admin(username, flag):
    guard = _admin_guard()
    if guard:
        return guard
    if not flag and username.lower() == USER_NAME.lower():
        return "error", "You cannot remove your own administrator rights."
    with db() as c:
        r = c.execute("SELECT is_admin FROM users WHERE username=?", (username,)).fetchone()
        if not r:
            return "error", f"User '{username}' does not exist."
        if not flag and r["is_admin"] and _admin_count(c) <= 1:
            return "error", "There must always be at least one administrator."
        c.execute("UPDATE users SET is_admin=? WHERE username=?", (1 if flag else 0, username))
    log_audit("[users]", "Admin Granted" if flag else "Admin Removed", "", f"{username} by {ADMIN_SESSION}")
    return "ok", f"'{username}' is now {'an administrator' if flag else 'a standard user'}."


def delete_user(username):
    guard = _admin_guard()
    if guard:
        return guard
    if username.lower() == USER_NAME.lower():
        return "error", "You cannot delete the account you are signed in with."
    with db() as c:
        r = c.execute("SELECT is_admin FROM users WHERE username=?", (username,)).fetchone()
        if not r:
            return "error", f"User '{username}' does not exist."
        if r["is_admin"] and _admin_count(c) <= 1:
            return "error", "You cannot delete the last administrator."
        held = c.execute("SELECT COUNT(*) FROM files WHERE locked_by=?", (username,)).fetchone()[0]
        if held:
            return "error", (f"'{username}' has {held} file(s) checked out. Release them on the "
                             "Check-outs tab first.")
        c.execute("DELETE FROM users WHERE username=?", (username,))
    log_audit("[users]", "User Deleted", "", f"{username} by {ADMIN_SESSION}")
    return "ok", f"Deleted user '{username}'."


def set_require_login(flag):
    guard = _admin_guard()
    if guard:
        return guard
    set_setting("require_login", "1" if flag else "0")
    log_audit("[settings]", "Sign-in Required" if flag else "Sign-in Disabled", "", f"by {ADMIN_SESSION}")
    return "ok", ("Sign-in is now required at startup." if flag else
                  "Sign-in disabled. People will be identified by their Windows username.")


def active_checkouts():
    with db() as c:
        return [dict(r) for r in c.execute(
            "SELECT filename, locked_by, revision, folder_path FROM files "
            "WHERE locked_by IS NOT NULL AND locked_by<>'' ORDER BY locked_by, filename").fetchall()]


def recent_activity(limit=500):
    with db() as c:
        return [tuple(r) for r in c.execute(
            "SELECT timestamp, user, COALESCE(host, ''), action, filename, comment FROM audit_log "
            "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]


# =============================================================================
# Themes
# =============================================================================
# Every theme has three tiers: a header band (title, search, account), a toolbar, and the body.
# Light themes use a dark header band over light content, which gives them structure and contrast.
THEMES = {
    # Deep blue-grey with a sky-blue accent. The default.
    "Engineering Slate": {
        "dark": True, "bg": "#0b1120", "header_bg": "#0f172a", "toolbar_bg": "#0f172a",
        "panel_bg": "#121b2d", "border": "#1f2b42",
        "fg": "#e6ebf2", "muted_fg": "#8a97ad", "entry_bg": "#0b1120", "entry_fg": "#f1f5f9",
        "grid_bg": "#121b2d", "row_alt": "#152035", "grid_fg": "#e2e8f0",
        "heading_bg": "#121b2d", "heading_fg": "#8a97ad", "btn_bg": "#1d2840", "btn_fg": "#e6ebf2",
        "accent": "#38bdf8", "sel_bg": "#173a5e", "sel_fg": "#f0f9ff", "mesh": "#4cc3f9",
        "success": "#34d399", "warning": "#fbbf24", "danger": "#f87171",
        "log_bg": "#080d19", "log_fg": "#a5b4c8", "sheet_bg": "#f8fafc", "sheet_fg": "#0f172a"},
    # Neutral graphite greys with an electric-cyan accent; crisp white text.
    "High-Contrast Dark": {
        "dark": True, "bg": "#0c0c0e", "header_bg": "#141417", "toolbar_bg": "#141417",
        "panel_bg": "#18181b", "border": "#303036",
        "fg": "#fafafa", "muted_fg": "#a8a8b3", "entry_bg": "#0f0f11", "entry_fg": "#ffffff",
        "grid_bg": "#18181b", "row_alt": "#1e1e22", "grid_fg": "#f4f4f5",
        "heading_bg": "#18181b", "heading_fg": "#a8a8b3", "btn_bg": "#27272c", "btn_fg": "#fafafa",
        "accent": "#22d3ee", "sel_bg": "#0e4654", "sel_fg": "#ecfeff", "mesh": "#2fd6f0",
        "success": "#4ade80", "warning": "#facc15", "danger": "#fb7185",
        "log_bg": "#09090b", "log_fg": "#d4d4d8", "sheet_bg": "#ffffff", "sheet_fg": "#09090b"},
    # Warm drafting paper under a navy ink header, with blueprint-blue accents.
    "Drafting Paper / Cream": {
        "dark": False, "bg": "#f2ece1", "header_bg": "#1f2a44", "toolbar_bg": "#faf6ee",
        "header_fg": "#f6f1e7", "header_muted": "#aab3c8", "header_accent": "#9db7ff",
        "panel_bg": "#fffdf8", "border": "#e2d9c6",
        "fg": "#2a251e", "muted_fg": "#7c7262", "entry_bg": "#ffffff", "entry_fg": "#2a251e",
        "grid_bg": "#fffdf8", "row_alt": "#f9f4ea", "grid_fg": "#2a251e",
        "heading_bg": "#f6efe1", "heading_fg": "#6b6150", "btn_bg": "#efe6d4", "btn_fg": "#2a251e",
        "accent": "#2747a8", "sel_bg": "#dfe6f8", "sel_fg": "#1a2f73", "mesh": "#4a6dd0",
        "success": "#2e7d4f", "warning": "#b0640c", "danger": "#b42318",
        "log_bg": "#1f2a44", "log_fg": "#dde3f0", "sheet_bg": "#fffcf2", "sheet_fg": "#1e3a8a"},
    # Clean white work surfaces under a slate header, with a vivid blue accent.
    "Nordic Light": {
        "dark": False, "bg": "#eef1f5", "header_bg": "#1e2733", "toolbar_bg": "#ffffff",
        "header_fg": "#f3f6fa", "header_muted": "#9aa6b5", "header_accent": "#8fb5ff",
        "panel_bg": "#ffffff", "border": "#dde3ea",
        "fg": "#101828", "muted_fg": "#667085", "entry_bg": "#ffffff", "entry_fg": "#101828",
        "grid_bg": "#ffffff", "row_alt": "#f7f9fc", "grid_fg": "#101828",
        "heading_bg": "#f7f9fc", "heading_fg": "#475467", "btn_bg": "#eef2f6", "btn_fg": "#101828",
        "accent": "#1f6feb", "sel_bg": "#e3edff", "sel_fg": "#0b3aa1", "mesh": "#3b82f6",
        "success": "#15803d", "warning": "#c2410c", "danger": "#d92d20",
        "log_bg": "#1e2733", "log_fg": "#d0d7e2", "sheet_bg": "#ffffff", "sheet_fg": "#101828"},
    # Deep petrol-teal CAD workspace with an aqua accent.
    "Classic CAD Cyan": {
        "dark": True, "bg": "#061a1f", "header_bg": "#082228", "toolbar_bg": "#082228",
        "panel_bg": "#0b262d", "border": "#163b44",
        "fg": "#dbe9eb", "muted_fg": "#86a6ac", "entry_bg": "#05151a", "entry_fg": "#eef7f8",
        "grid_bg": "#0b262d", "row_alt": "#0e2c34", "grid_fg": "#dbe9eb",
        "heading_bg": "#0b262d", "heading_fg": "#86a6ac", "btn_bg": "#123640", "btn_fg": "#e6f2f3",
        "accent": "#2dd4bf", "sel_bg": "#0f4a49", "sel_fg": "#e6fffb", "mesh": "#3cd6c2",
        "success": "#a3e635", "warning": "#fbbf24", "danger": "#fb7185",
        "log_bg": "#041216", "log_fg": "#a9c4c8", "sheet_bg": "#fdf6e3", "sheet_fg": "#073642"},
}
DEFAULT_THEME = "Engineering Slate"


def _complete_themes():
    for t in THEMES.values():
        t.setdefault("header_fg", t["fg"])
        t.setdefault("header_muted", t["muted_fg"])
        t.setdefault("header_accent", t["accent"])
        t.setdefault("toolbar_bg", t["header_bg"])


_complete_themes()


def mix(c1, c2, t):
    a, b = c1.lstrip("#"), c2.lstrip("#")
    r = [round(int(a[i:i + 2], 16) * (1 - t) + int(b[i:i + 2], 16) * t) for i in (0, 2, 4)]
    return "#{:02x}{:02x}{:02x}".format(*r)


def text_on(color):
    """Readable text colour (near-black or white) for a given background."""
    c = color.lstrip("#")
    r, g, b = (int(c[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return "#0b0f14" if (0.2126 * r + 0.7152 * g + 0.0722 * b) > 0.55 else "#ffffff"


def is_dark(color):
    return text_on(color) == "#ffffff"


def hover_of(color, theme=None):
    return mix(color, "#ffffff", 0.12) if is_dark(color) else mix(color, "#000000", 0.07)


# =============================================================================
# Preview rendering
# =============================================================================
def _rot(x, y, z, cx_, sx_, cy_, sy_):
    x1 = x * cy_ + z * sy_
    z1 = -x * sy_ + z * cy_
    return x1, y * cx_ - z1 * sx_, y * sx_ + z1 * cx_


def draw_backdrop(canvas, w, h, t, step=24):
    canvas.configure(bg=t["entry_bg"])
    grid = mix(t["entry_bg"], t["fg"], 0.07)
    for i in range(0, w, step):
        canvas.create_line(i, 0, i, h, fill=grid)
    for i in range(0, h, step):
        canvas.create_line(0, i, w, i, fill=grid)


def draw_triad(canvas, x0, y0, length, cam):
    rx, ry = math.radians(cam["rx"]), math.radians(cam["ry"])
    trig = (math.cos(rx), math.sin(rx), math.cos(ry), math.sin(ry))
    for vec, col, lab in (((1, 0, 0), "#ef4444", "X"), ((0, 1, 0), "#22c55e", "Y"), ((0, 0, 1), "#3b82f6", "Z")):
        px, py, _ = _rot(*vec, *trig)
        x1, y1 = x0 + px * length, y0 - py * length
        canvas.create_line(x0, y0, x1, y1, fill=col, width=2)
        canvas.create_text(x1 + px * 7, y1 - py * 7, text=lab, fill=col, font=("Segoe UI", 7, "bold"))


def render_mesh(canvas, mesh, w, h, t, cam):
    draw_backdrop(canvas, w, h, t)
    cx, cy, cz = mesh["c"]
    scale = (min(w, h) * 0.72 / mesh["d"]) * cam["zoom"]
    rx, ry = math.radians(cam["rx"]), math.radians(cam["ry"])
    trig = (math.cos(rx), math.sin(rx), math.cos(ry), math.sin(ry))
    lx, ly, lz = 0.577, -0.577, 0.577
    a = t["mesh"].lstrip("#")
    br, bg_, bb = int(a[0:2], 16), int(a[2:4], 16), int(a[4:6], 16)
    verts = mesh["v"]
    out = []
    for i1, i2, i3, nrm in mesh["f"]:
        pts, zsum = [], 0.0
        for idx in (i1, i2, i3):
            vx, vy, vz = verts[idx]
            x, y, z = _rot((vx - cx) * scale, (vy - cy) * scale, (vz - cz) * scale, *trig)
            pts.extend((w / 2 + x, h / 2 - y))
            zsum += z
        nx, ny, nz = _rot(*nrm, *trig)
        d = max(0.28, min(1.0, nx * lx + ny * ly + nz * lz))
        out.append((zsum, pts, "#{:02x}{:02x}{:02x}".format(int(br * d), int(bg_ * d), int(bb * d))))
    out.sort(key=lambda o: o[0])
    for _, pts, col in out:
        canvas.create_polygon(pts, fill=col, outline=col)
    draw_triad(canvas, 30, h - 30, 18, cam)


def render_placeholder(canvas, w, h, t, cam, title, hint):
    draw_backdrop(canvas, w, h, t)
    cx, cy = w / 2, h / 2 - 14
    s = min(w, h) * 0.2 * cam["zoom"]
    acc = t["accent"]
    top = [cx, cy - s, cx + s * 0.866, cy - s * 0.5, cx, cy, cx - s * 0.866, cy - s * 0.5]
    left = [cx - s * 0.866, cy - s * 0.5, cx, cy, cx, cy + s, cx - s * 0.866, cy + s * 0.5]
    right = [cx, cy, cx + s * 0.866, cy - s * 0.5, cx + s * 0.866, cy + s * 0.5, cx, cy + s]
    edge = mix(acc, t["entry_bg"], 0.2)
    canvas.create_polygon(top, fill=mix(acc, "#ffffff", 0.35), outline=edge)
    canvas.create_polygon(left, fill=acc, outline=edge)
    canvas.create_polygon(right, fill=mix(acc, "#000000", 0.3), outline=edge)
    canvas.create_text(w / 2, h - 34, text=title, fill=t["fg"], font=("Segoe UI Semibold", 9))
    canvas.create_text(w / 2, h - 17, text=hint, fill=t["muted_fg"], font=("Segoe UI", 8))


def render_drawing_sheet(canvas, filename, w, h, t, rec):
    canvas.configure(bg=t["entry_bg"])
    sb, sf, acc = t["sheet_bg"], t["sheet_fg"], t["accent"]
    m = 14
    canvas.create_rectangle(m + 3, m + 3, w - m + 3, h - m + 3, fill=mix(t["entry_bg"], "#000000", 0.25), outline="")
    canvas.create_rectangle(m, m, w - m, h - m, fill=sb, outline=sf, width=1)
    b = m + 8
    canvas.create_rectangle(b, b, w - b, h - b, outline=sf)
    for i in range(1, 6):
        x = b + (w - 2 * b) * (i - 0.5) / 6
        canvas.create_text(x, b - 4, text=str(i), fill=mix(sf, sb, 0.4), font=("Segoe UI", 6))
    _, _, ftype, parsed_rev = parse_structured_name(filename)
    part = display_part_name(filename, rec)
    rev = (rec or {}).get("revision") or parsed_rev
    pn = (rec or {}).get("part_number") or "-"
    desc = (rec or {}).get("description") or ""
    tbw, tbh = min(230, int((w - 2 * b) * 0.62)), 58
    x1, y1, x2, y2 = w - b - tbw, h - b - tbh, w - b, h - b
    canvas.create_rectangle(x1, y1, x2, y2, fill=sb, outline=sf)
    canvas.create_line(x1, y1 + 18, x2, y1 + 18, fill=sf)
    canvas.create_line(x1 + tbw * 0.62, y1 + 18, x1 + tbw * 0.62, y2, fill=sf)
    canvas.create_text(x1 + 6, y1 + 9, text=f"{APP_NAME.upper()}  ·  {ftype} DRAWING", fill=acc,
                       font=("Segoe UI", 7, "bold"), anchor="w")
    canvas.create_text(x2 - 6, y1 + 9, text=f"REV {rev}", fill=sf, font=("Segoe UI", 7, "bold"), anchor="e")
    canvas.create_text(x1 + 6, y1 + 28, text=f"PART  {part[:22]}", fill=sf, font=("Segoe UI", 7, "bold"), anchor="w")
    canvas.create_text(x1 + 6, y1 + 41, text=(desc or filename)[:30], fill=sf, font=("Segoe UI", 6), anchor="w")
    canvas.create_text(x1 + tbw * 0.62 + 6, y1 + 28, text=f"P/N  {pn[:14]}", fill=sf, font=("Segoe UI", 6), anchor="w")
    canvas.create_text(x1 + tbw * 0.62 + 6, y1 + 41, text="SHEET 1", fill=sf, font=("Segoe UI", 6), anchor="w")
    cx, cy = b + (w - 2 * b) / 2, b + (h - 2 * b - tbh) / 2
    canvas.create_text(cx, cy - 8, text="Drawing preview not available", fill=mix(sf, sb, 0.35),
                       font=("Segoe UI Semibold", 9))
    canvas.create_text(cx, cy + 9, text="Double-click the file to open it in its CAD app", fill=mix(sf, sb, 0.5),
                       font=("Segoe UI", 8))


# =============================================================================
# Settings
# =============================================================================
def load_settings():
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_settings(data):
    try:
        os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except OSError:
        pass


# =============================================================================
# ttk styling (shared by the main window and the sign-in screen)
# =============================================================================
def style_ttk(root, s, t, S):
    F = "Segoe UI"
    hover = hover_of(t["btn_bg"])
    disabled_bg = mix(t["btn_bg"], t["panel_bg"], 0.5)
    hb, hf, hm, ha = t["header_bg"], t["header_fg"], t["header_muted"], t["header_accent"]
    tb = t["toolbar_bg"]
    root.configure(bg=t["bg"])
    s.configure(".", background=t["bg"], foreground=t["fg"], bordercolor=t["border"],
                lightcolor=t["border"], darkcolor=t["border"], troughcolor=t["bg"],
                focuscolor=t["accent"], selectbackground=t["sel_bg"], selectforeground=t["sel_fg"],
                fieldbackground=t["entry_bg"], insertcolor=t["fg"], font=(F, 9))
    # frames
    s.configure("TFrame", background=t["bg"])
    s.configure("Header.TFrame", background=hb)
    s.configure("Toolbar.TFrame", background=tb)
    s.configure("Status.TFrame", background=tb)
    s.configure("Card.TFrame", background=t["panel_bg"], borderwidth=1, relief="solid", bordercolor=t["border"])
    s.configure("Dialog.TFrame", background=t["panel_bg"])
    # labels
    s.configure("TLabel", background=t["bg"], foreground=t["fg"])
    s.configure("HeaderTitle.TLabel", background=hb, foreground=hf, font=("Segoe UI Semibold", 13))
    s.configure("HeaderSub.TLabel", background=hb, foreground=hm, font=(F, 8))
    s.configure("HeaderSearch.TLabel", background=hb, foreground=hm, font=(F, 12))
    for name, fg, font in (("Card", t["fg"], (F, 9)), ("CardMuted", t["muted_fg"], (F, 8)),
                           ("CardValue", t["fg"], ("Segoe UI Semibold", 9)),
                           ("CardTitle", t["fg"], ("Segoe UI Semibold", 12)),
                           ("CardSection", t["muted_fg"], ("Segoe UI Semibold", 8)),
                           ("CardAccent", t["accent"], ("Segoe UI Semibold", 9)),
                           ("CardDanger", t["danger"], (F, 8)),
                           ("FileTitle", t["fg"], ("Segoe UI Semibold", 11))):
        s.configure(f"{name}.TLabel", background=t["panel_bg"], foreground=fg, font=font)
    s.configure("Status.TLabel", background=tb, foreground=t["muted_fg"], font=(F, 8))
    s.configure("StatusWarn.TLabel", background=tb, foreground=t["warning"], font=("Segoe UI Semibold", 8))
    # buttons
    s.configure("TButton", background=t["btn_bg"], foreground=t["btn_fg"], borderwidth=0, relief="flat",
                padding=(S(14), S(7)), font=(F, 9), focusthickness=0)
    s.map("TButton", background=[("disabled", disabled_bg), ("pressed", hover), ("active", hover)],
          foreground=[("disabled", t["muted_fg"])])
    for name, color in (("Accent", t["accent"]), ("Success", t["success"]), ("Danger", t["danger"])):
        s.configure(f"{name}.TButton", background=color, foreground=text_on(color), font=("Segoe UI Semibold", 9))
        s.map(f"{name}.TButton", background=[("disabled", disabled_bg), ("pressed", hover_of(color)),
                                             ("active", hover_of(color))],
              foreground=[("disabled", t["muted_fg"])])
    # toolbar buttons
    tool_hover = mix(tb, t["fg"], 0.08)
    dim = mix(t["muted_fg"], tb, 0.45)
    s.configure("Tool.TButton", background=tb, foreground=t["fg"], padding=(S(10), S(6)))
    s.map("Tool.TButton", background=[("disabled", tb), ("pressed", tool_hover), ("active", tool_hover)],
          foreground=[("disabled", dim)])
    for name, color in (("ToolAccent", t["accent"]), ("ToolSuccess", t["success"])):
        s.configure(f"{name}.TButton", background=tb, foreground=color,
                    padding=(S(10), S(6)), font=("Segoe UI Semibold", 9))
        s.map(f"{name}.TButton", background=[("disabled", tb), ("pressed", tool_hover),
                                             ("active", tool_hover)], foreground=[("disabled", dim)])
    s.configure("Tool.TMenubutton", background=tb, foreground=t["fg"], borderwidth=0,
                relief="flat", padding=(S(10), S(6)), arrowcolor=t["muted_fg"])
    s.map("Tool.TMenubutton", background=[("active", tool_hover), ("pressed", tool_hover)])
    # header-band widgets
    head_hover = mix(hb, hf, 0.10)
    s.configure("HeaderTool.TButton", background=hb, foreground=hf, padding=(S(10), S(6)))
    s.map("HeaderTool.TButton", background=[("pressed", head_hover), ("active", head_hover)])
    pill_bg = mix(hb, hf, 0.10)
    s.configure("Account.TMenubutton", background=pill_bg, foreground=hf, borderwidth=0, relief="flat",
                padding=(S(12), S(5)), arrowcolor=hm, font=("Segoe UI Semibold", 9))
    s.map("Account.TMenubutton", background=[("active", mix(hb, hf, 0.18)), ("pressed", mix(hb, hf, 0.18))])
    s.configure("Search.TEntry", fieldbackground=mix(hb, hf, 0.08), foreground=hf, insertcolor=hf,
                bordercolor=mix(hb, hf, 0.18), lightcolor=mix(hb, hf, 0.18), darkcolor=mix(hb, hf, 0.18))
    s.map("Search.TEntry", bordercolor=[("focus", ha)], lightcolor=[("focus", ha)])
    s.configure("Header.TCombobox", fieldbackground=mix(hb, hf, 0.08), foreground=hf, background=mix(hb, hf, 0.12),
                arrowcolor=hf, bordercolor=mix(hb, hf, 0.18), lightcolor=mix(hb, hf, 0.18),
                darkcolor=mix(hb, hf, 0.18), padding=(S(6), S(3)))
    s.map("Header.TCombobox", fieldbackground=[("readonly", mix(hb, hf, 0.08))], foreground=[("readonly", hf)],
          selectbackground=[("readonly", mix(hb, hf, 0.08))], selectforeground=[("readonly", hf)],
          bordercolor=[("focus", ha)])
    # misc buttons
    s.configure("Small.TButton", padding=(S(9), S(4)), font=(F, 8))
    s.configure("Icon.TButton", padding=(S(2), 0), font=(F, 10), background=t["panel_bg"])
    s.map("Icon.TButton", background=[("active", hover)])
    s.configure("Link.TButton", background=tb, foreground=t["muted_fg"], padding=(S(4), 0), font=(F, 8))
    s.map("Link.TButton", background=[("active", tb)], foreground=[("active", t["accent"])])
    # entries, combos, checkboxes
    s.configure("TEntry", fieldbackground=t["entry_bg"], foreground=t["entry_fg"], bordercolor=t["border"],
                lightcolor=t["border"], darkcolor=t["border"], insertcolor=t["fg"], padding=(S(7), S(4)))
    s.map("TEntry", bordercolor=[("focus", t["accent"])], lightcolor=[("focus", t["accent"])],
          fieldbackground=[("readonly", mix(t["panel_bg"], t["fg"], 0.03)), ("disabled", t["panel_bg"])],
          foreground=[("disabled", t["muted_fg"])])
    s.configure("TCombobox", fieldbackground=t["entry_bg"], foreground=t["entry_fg"], background=t["btn_bg"],
                arrowcolor=t["fg"], bordercolor=t["border"], padding=(S(6), S(3)))
    s.map("TCombobox", fieldbackground=[("readonly", t["entry_bg"])], foreground=[("readonly", t["entry_fg"])],
          selectbackground=[("readonly", t["entry_bg"])], selectforeground=[("readonly", t["entry_fg"])],
          bordercolor=[("focus", t["accent"])])
    root.option_add("*TCombobox*Listbox.background", t["panel_bg"])
    root.option_add("*TCombobox*Listbox.foreground", t["fg"])
    root.option_add("*TCombobox*Listbox.selectBackground", t["sel_bg"])
    root.option_add("*TCombobox*Listbox.selectForeground", t["sel_fg"])
    s.configure("Dialog.TCheckbutton", background=t["panel_bg"], foreground=t["fg"], font=(F, 9),
                indicatorbackground=t["entry_bg"], indicatorforeground=text_on(t["accent"]),
                upperbordercolor=t["border"], lowerbordercolor=t["border"], padding=(0, S(3)))
    s.map("Dialog.TCheckbutton", background=[("active", t["panel_bg"])],
          indicatorbackground=[("selected", t["accent"]), ("active", t["entry_bg"])])
    # notebook (Administration tabs)
    s.configure("TNotebook", background=t["panel_bg"], borderwidth=0, bordercolor=t["border"],
                lightcolor=t["panel_bg"], darkcolor=t["panel_bg"], tabmargins=(0, 0, 0, 0))
    s.configure("TNotebook.Tab", background=t["panel_bg"], foreground=t["muted_fg"], borderwidth=0,
                bordercolor=t["panel_bg"], lightcolor=t["panel_bg"], padding=(S(14), S(7)),
                font=("Segoe UI Semibold", 9))
    s.map("TNotebook.Tab", background=[("selected", mix(t["panel_bg"], t["accent"], 0.12)),
                                       ("active", mix(t["panel_bg"], t["fg"], 0.05))],
          foreground=[("selected", t["accent"]), ("active", t["fg"])],
          lightcolor=[("selected", t["accent"])], expand=[("selected", (0, 0, 0, 0))])
    # trees
    s.configure("Treeview", background=t["grid_bg"], fieldbackground=t["grid_bg"], foreground=t["grid_fg"],
                rowheight=S(32), borderwidth=0, font=(F, 9))
    s.map("Treeview", background=[("selected", t["sel_bg"])], foreground=[("selected", t["sel_fg"])])
    s.configure("Treeview.Heading", background=t["heading_bg"], foreground=t["heading_fg"], relief="flat",
                borderwidth=0, padding=(S(8), S(7)), font=("Segoe UI Semibold", 8))
    s.map("Treeview.Heading", background=[("active", hover_of(t["heading_bg"]))])
    s.configure("Folder.Treeview", background=t["panel_bg"], fieldbackground=t["panel_bg"],
                foreground=t["fg"], rowheight=S(30))
    s.map("Folder.Treeview", background=[("selected", t["sel_bg"])], foreground=[("selected", t["sel_fg"])])
    # misc
    s.configure("Vertical.TScrollbar", background=t["btn_bg"], troughcolor=t["panel_bg"],
                bordercolor=t["panel_bg"], arrowcolor=t["muted_fg"], lightcolor=t["btn_bg"],
                darkcolor=t["btn_bg"], gripcount=0, arrowsize=S(12))
    s.map("Vertical.TScrollbar", background=[("active", hover)])
    s.configure("TSeparator", background=t["border"])
    s.configure("Accent.TSeparator", background=t["accent"])
    s.configure("TPanedwindow", background=t["bg"])
    s.configure("Sash", sashthickness=S(10), gripcount=0, background=t["bg"], lightcolor=t["bg"],
                bordercolor=t["bg"])


# =============================================================================
# Themed dialogs
# =============================================================================
INVALID = object()


class ThemedDialog(tk.Toplevel):
    def __init__(self, app, title, heading=None, subheading=None, width=440, resizable=False, parent=None):
        self.owner = parent or app.root
        super().__init__(self.owner)
        self.app = app
        self.withdraw()
        self.title(title)
        self.configure(bg=app.t["panel_bg"])
        if self.owner.winfo_viewable():
            self.transient(self.owner)
        self.resizable(resizable, resizable)
        self.result = None
        self.focus_widget = None
        self.validate = lambda: True
        S = app.S
        tk.Frame(self, height=S(3), bg=app.t["accent"]).pack(fill="x")
        outer = ttk.Frame(self, style="Dialog.TFrame", padding=S(20))
        outer.pack(fill="both", expand=True)
        if heading:
            ttk.Label(outer, text=heading, style="CardTitle.TLabel").pack(anchor="w")
        if subheading:
            ttk.Label(outer, text=subheading, style="CardMuted.TLabel", wraplength=S(width - 40),
                      justify="left").pack(anchor="w", pady=(S(3), 0))
        self.body = ttk.Frame(outer, style="Dialog.TFrame")
        self.body.pack(fill="both", expand=True, pady=(S(14), 0))
        self.buttons = ttk.Frame(outer, style="Dialog.TFrame")
        self.buttons.pack(fill="x", pady=(S(18), 0))
        self.minsize(S(width), 1)
        self.bind("<Escape>", lambda e: self.cancel())
        self.protocol("WM_DELETE_WINDOW", self.cancel)

    def add_buttons(self, ok_text="OK", ok_style="Accent.TButton", cancel_text="Cancel"):
        S = self.app.S
        ttk.Button(self.buttons, text=ok_text, style=ok_style, command=self.ok).pack(side="right")
        if cancel_text:
            ttk.Button(self.buttons, text=cancel_text, command=self.cancel).pack(side="right", padx=(0, S(8)))
        self.bind("<Return>", lambda e: self.ok())

    def ok(self):
        value = self.validate()
        if value is INVALID:
            return
        self.result = value
        self.destroy()

    def cancel(self):
        self.result = None
        self.destroy()

    def show(self):
        self.update_idletasks()
        r = self.owner
        w, h = self.winfo_reqwidth(), self.winfo_reqheight()
        if r.winfo_viewable():
            x = r.winfo_rootx() + (r.winfo_width() - w) // 2
            y = r.winfo_rooty() + (r.winfo_height() - h) // 3
        else:
            x = (self.winfo_screenwidth() - w) // 2
            y = (self.winfo_screenheight() - h) // 3
        self.geometry(f"+{max(0, x)}+{max(0, y)}")
        self.deiconify()
        self.lift()
        self.focus_force()
        self.grab_set()
        (self.focus_widget or self).focus_set()
        self.wait_window()
        return self.result


def run_form(ctx, title, heading, sub, fields, submit, ok_text="OK", cancel_text="Cancel",
             checks=(), ok_style="Accent.TButton", width=400, parent=None, note=None):
    """Generic themed form. fields: [(key, label, secret, initial)]; checks: [(key, label)].
    submit(values) returns a result, or ("error", msg) to keep the form open."""
    S = ctx.S
    dlg = ThemedDialog(ctx, title, heading, sub, width=width, parent=parent)
    if note:
        ttk.Label(dlg.body, text=note, style="CardAccent.TLabel", wraplength=S(width - 40),
                  justify="left").pack(anchor="w", pady=(0, S(10)))
    vars_, entries = {}, {}
    for key, label, secret, initial in fields:
        ttk.Label(dlg.body, text=label, style="CardMuted.TLabel").pack(anchor="w")
        v = tk.StringVar(value=initial or "")
        e = ttk.Entry(dlg.body, textvariable=v, show="•" if secret else "")
        e.pack(fill="x", pady=(S(3), S(10)), ipady=S(3))
        vars_[key], entries[key] = v, e
    for key, label in checks:
        v = tk.BooleanVar(value=False)
        ttk.Checkbutton(dlg.body, text=label, variable=v, style="Dialog.TCheckbutton").pack(anchor="w",
                                                                                           pady=(0, S(6)))
        vars_[key] = v
    err = ttk.Label(dlg.body, text="", style="CardDanger.TLabel", wraplength=S(width - 40), justify="left")
    err.pack(anchor="w")

    def validate():
        values = {k: v.get() for k, v in vars_.items()}
        try:
            res = submit(values)
        except sqlite3.Error as e:
            res = ("error", f"Database error: {e}")
        except OSError as e:
            res = ("error", f"File operation failed: {e}")
        if isinstance(res, tuple) and res and res[0] == "error":
            err.configure(text=res[1])
            secrets = [k for k, _, secret, _ in fields if secret]
            for k in secrets:
                vars_[k].set("")
            (entries[secrets[0]] if secrets else dlg).focus_set()
            return INVALID
        return res
    dlg.validate = validate
    dlg.add_buttons(ok_text, ok_style, cancel_text)
    blank = [entries[k] for k, *_ in fields if not vars_[k].get()]
    dlg.focus_widget = blank[0] if blank else (entries[fields[0][0]] if fields else None)
    return dlg.show()


def run_login(ctx, heading="Sign in", sub=None, initial_user=None, ok_text="Sign in", cancel_text="Exit",
              parent=None):
    def submit(v):
        acct, msg = authenticate(v["user"].strip(), v["pw"])
        return acct if acct else ("error", msg)
    if initial_user is None:
        initial_user = ctx.settings.get("last_user", "")
    return run_form(ctx, f"{APP_NAME} · Sign in", heading, sub or f"{APP_NAME} {APP_VERSION}  ·  {HOST_NAME}",
                    [("user", "Username", False, initial_user), ("pw", "Password", True, "")],
                    submit, ok_text, cancel_text, width=380, parent=parent)


def run_password_change(ctx, username, forced, cancel_text="Cancel", parent=None):
    fields = [] if forced else [("old", "Current password", True, "")]
    fields += [("new", f"New password  (at least {MIN_PASSWORD_LEN} characters)", True, ""),
               ("confirm", "Confirm new password", True, "")]

    def submit(v):
        if v["new"] != v["confirm"]:
            return "error", "The new passwords do not match."
        if forced:
            return set_password(username, v["new"], must_change=False)
        return change_own_password(username, v["old"], v["new"])
    sub = (f"Hi {username}, please choose a new password before continuing." if forced
           else f"Signed in as {username}.")
    return run_form(ctx, "Change password", "Change password", sub, fields, submit,
                    "Change password", cancel_text, width=380, parent=parent)


class BootContext:
    """Minimal stand-in for the app so themed dialogs can run before the main window exists."""
    def __init__(self, root):
        self.root = root
        self.settings = load_settings()
        name = self.settings.get("theme", DEFAULT_THEME)
        self.theme_name = name if name in THEMES else DEFAULT_THEME
        self.scale = max(1.0, root.winfo_fpixels("1i") / 96.0)

    def S(self, n):
        return int(round(n * self.scale))

    @property
    def t(self):
        return THEMES[self.theme_name]


def sign_in(ctx, switching=False):
    """Signs a user in (or picks up the Windows user when sign-in is off). Returns True on success."""
    global USER_NAME, ADMIN_SESSION
    if not login_required():
        if not switching:
            USER_NAME, ADMIN_SESSION = getpass.getuser(), None
        return not switching
    acct = run_login(ctx, "Switch user" if switching else "Sign in",
                     initial_user="" if switching else None,
                     cancel_text="Cancel" if switching else "Exit")
    if not acct:
        return False
    if acct["must_change"] and not run_password_change(ctx, acct["username"], True,
                                                       "Cancel" if switching else "Exit"):
        return False
    if switching:
        log_audit("[session]", "Signed Out", "", "")
    USER_NAME = acct["username"]
    ADMIN_SESSION = USER_NAME if acct["is_admin"] else None
    ctx.settings["last_user"] = USER_NAME
    save_settings(ctx.settings)
    log_audit("[session]", "Signed In", "", "")
    return True


# =============================================================================
# Application
# =============================================================================
class AtelierApp:
    GRID_COLS = (("pn", "Part Number", 110), ("part", "Part Name", 200), ("cad", "CAD", 84),
                 ("name", "File Name", 240),
                 ("rev", "Rev", 56), ("status", "Status", 180), ("folder", "Folder", 160),
                 ("modified", "Modified", 130))

    def __init__(self, root):
        self.root = root
        self.settings = load_settings()
        name = self.settings.get("theme", DEFAULT_THEME)
        self.theme_name = name if name in THEMES else DEFAULT_THEME
        self.scale = max(1.0, root.winfo_fpixels("1i") / 96.0)
        self.selected = None          # file used for actions
        self.preview_file = None      # file shown in the viewer
        self.current_folder = "*"
        self.sort_col, self.sort_rev = "name", False
        self.row_map = {}
        self.cam = {"rx": 25.0, "ry": 45.0, "zoom": 1.0}
        self._drag_xy = (0, 0)
        self._dnd_file, self._dnd_target, self._dnd_active = None, None, False
        self._redraw_pending = False
        self._search_job = None
        self.btn = {}

        self.style = ttk.Style(root)
        self.style.theme_use("clam")
        root.title(f"{APP_NAME}  ·  PDM")
        root.geometry(f"{self.S(1440)}x{self.S(880)}")
        root.minsize(self.S(1100), self.S(640))
        try:
            root.state("zoomed")
        except tk.TclError:
            pass

        self.build_ui()
        self.update_identity()
        self.apply_theme()
        self.bind_shortcuts()
        self.refresh_all()
        self.startup_messages()
        self.root.after(60000, self.auto_refresh_locks)
        self.root.after(150, self.set_initial_sashes)

    # ------------------------------------------------------------------ utils
    def S(self, n):
        return int(round(n * self.scale))

    @property
    def t(self):
        return THEMES[self.theme_name]

    # --------------------------------------------------------------- build UI
    def build_ui(self):
        S, root = self.S, self.root

        # Header band
        header = ttk.Frame(root, style="Header.TFrame", padding=(S(18), S(12)))
        header.pack(side="top", fill="x")
        brand = ttk.Frame(header, style="Header.TFrame")
        brand.pack(side="left")
        self.logo = tk.Canvas(brand, width=S(30), height=S(30), highlightthickness=0, bd=0)
        self.logo.pack(side="left", padx=(0, S(10)))
        titles = ttk.Frame(brand, style="Header.TFrame")
        titles.pack(side="left")
        ttk.Label(titles, text=APP_NAME, style="HeaderTitle.TLabel").pack(anchor="w")
        ttk.Label(titles, text="CAD vault & part numbering", style="HeaderSub.TLabel").pack(anchor="w")

        right = ttk.Frame(header, style="Header.TFrame")
        right.pack(side="right")
        self.account_mb = ttk.Menubutton(right, text="", style="Account.TMenubutton")
        self.account_mb.pack(side="right", padx=(S(12), 0))
        self.account_menu = tk.Menu(self.account_mb, tearoff=0)
        self.account_mb["menu"] = self.account_menu
        self.theme_var = tk.StringVar(value=self.theme_name)
        combo = ttk.Combobox(right, textvariable=self.theme_var, values=list(THEMES), state="readonly", width=22,
                             style="Header.TCombobox")
        combo.pack(side="right", padx=(S(10), 0))
        combo.bind("<<ComboboxSelected>>", self.on_theme_change)
        self.theme_combo = combo
        ttk.Button(right, text="⟳  Refresh", style="HeaderTool.TButton", command=self.refresh_all).pack(side="right")

        search_wrap = ttk.Frame(header, style="Header.TFrame")
        search_wrap.pack(side="left", padx=(S(48), 0))
        ttk.Label(search_wrap, text="⌕", style="HeaderSearch.TLabel").pack(side="left", padx=(0, S(6)))
        self.search_var = tk.StringVar()
        self.search_entry = ttk.Entry(search_wrap, textvariable=self.search_var, width=40, style="Search.TEntry")
        self.search_entry.pack(side="left", ipady=S(2))
        self.search_var.trace_add("write", lambda *_: self.schedule_search())
        ttk.Label(search_wrap, text="Ctrl+F · name, part number, description, CAD", style="HeaderSub.TLabel").pack(
            side="left", padx=(S(10), 0))

        # Toolbar
        bar = ttk.Frame(root, style="Toolbar.TFrame", padding=(S(14), S(6)))
        bar.pack(side="top", fill="x")
        self.toolbar = bar

        def tool(key, text, cmd, style="Tool.TButton"):
            b = ttk.Button(bar, text=text, style=style, command=cmd)
            b.pack(side="left", padx=S(2))
            if key:
                self.btn[key] = b
            return b

        def sep():
            ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=S(8), pady=S(4))

        tool(None, "🗂  New Project", self.action_new_project)
        tool(None, "✚  New Part", self.action_new_from_template)
        tool(None, "⊞  Add Files", self.action_add_files)
        sep()
        tool("checkout", "↓  Check Out", self.action_check_out, "ToolAccent.TButton")
        tool("checkin", "↑  Check In", self.action_check_in, "ToolSuccess.TButton")
        tool("undo", "↺  Undo Check-Out", self.action_undo)
        sep()
        tool("open", "◈  Open in CAD", self.action_open)
        sep()
        tool("minor", "Minor Rev  +01", lambda: self.action_up_issue("minor"))
        tool("major", "Major Rev  A→B", lambda: self.action_up_issue("major"))
        tool("history", "☰  History", self.action_history)
        sep()
        tool("export", "⇪  Export STEP & PDF", self.action_export)

        folders_mb = ttk.Menubutton(bar, text="📂  Folders", style="Tool.TMenubutton")
        folders_mb.pack(side="right")
        self.folders_menu = tk.Menu(folders_mb, tearoff=0)
        for label, path in (("Vault Root", VAULT_DIR), ("Projects", project_root_abs()),
                            ("My Sandbox", LOCAL_WORKSPACE), ("Standard Library (COTS)", STANDARD_LIB_DIR),
                            ("Templates", TEMPLATES_DIR), ("Manufacturing Output", OUTPUT_DIR),
                            ("History & Archive", HISTORY_DIR)):
            self.folders_menu.add_command(label=label, command=lambda p=path: self.open_folder(p))
        folders_mb["menu"] = self.folders_menu
        ttk.Button(bar, text="⚙  Administration", style="Tool.TButton", command=self.action_admin).pack(
            side="right", padx=(0, S(4)))

        self.accent_line = ttk.Separator(root, orient="horizontal", style="TSeparator")
        self.accent_line.pack(side="top", fill="x")

        # Status bar
        status = ttk.Frame(root, style="Status.TFrame", padding=(S(14), S(5)))
        status.pack(side="bottom", fill="x")
        self.status_dot = tk.Label(status, text="●", font=("Segoe UI", 9), bd=0)
        self.status_dot.pack(side="left")
        self.status_label = ttk.Label(status, text="Ready", style="Status.TLabel")
        self.status_label.pack(side="left", padx=(S(6), 0))
        self.console_btn = ttk.Button(status, text="Console ▴", style="Link.TButton", command=self.toggle_console)
        self.console_btn.pack(side="right")
        ttk.Label(status, text=f"DB: {DB_PATH}", style="Status.TLabel").pack(side="right", padx=(0, S(16)))
        self.warn_label = ttk.Label(status, text="", style="StatusWarn.TLabel", cursor="hand2")
        self.warn_label.pack(side="right", padx=(0, S(16)))
        self.warn_label.bind("<Button-1>", lambda e: self.show_db_warning())

        # Console
        self.console = ttk.Frame(root, style="TFrame", padding=(S(12), 0, S(12), S(8)))
        self.log_text = tk.Text(self.console, height=7, font=("Consolas", 9), state="disabled", relief="flat",
                                bd=0, padx=S(10), pady=S(8), wrap="word")
        self.log_text.pack(fill="x")

        # Body
        self.body = ttk.PanedWindow(root, orient="horizontal")
        self.body.pack(side="top", fill="both", expand=True, padx=S(12), pady=S(12))

        # -- Folders card
        fcard = ttk.Frame(self.body, style="Card.TFrame", padding=S(10))
        self.body.add(fcard, weight=1)
        fhead = ttk.Frame(fcard, style="Card.TFrame")
        fhead.pack(fill="x", pady=(0, S(6)))
        ttk.Label(fhead, text="FOLDERS", style="CardSection.TLabel").pack(side="left")
        ttk.Button(fhead, text="＋", width=3, style="Icon.TButton", command=self.action_new_folder).pack(side="right")
        fwrap = ttk.Frame(fcard, style="Card.TFrame")
        fwrap.pack(fill="both", expand=True)
        self.ftree = ttk.Treeview(fwrap, show="tree", style="Folder.Treeview", selectmode="browse")
        fsb = ttk.Scrollbar(fwrap, orient="vertical", command=self.ftree.yview)
        self.ftree.configure(yscrollcommand=fsb.set)
        self.ftree.pack(side="left", fill="both", expand=True)
        fsb.pack(side="right", fill="y")
        self.ftree.bind("<<TreeviewSelect>>", self.on_folder_select)
        for seq in ("<Button-3>", "<Button-2>"):
            self.ftree.bind(seq, self.show_folder_menu)
        self.ftree.bind("<Delete>", lambda e: self.action_delete_folder())

        # -- Components card
        gcard = ttk.Frame(self.body, style="Card.TFrame", padding=S(10))
        self.body.add(gcard, weight=4)
        ghead = ttk.Frame(gcard, style="Card.TFrame")
        ghead.pack(fill="x", pady=(0, S(6)))
        self.grid_title = ttk.Label(ghead, text="COMPONENTS", style="CardSection.TLabel")
        self.grid_title.pack(side="left")
        self.count_label = ttk.Label(ghead, text="", style="CardMuted.TLabel")
        self.count_label.pack(side="right")
        self.next_label = ttk.Label(ghead, text="", style="CardAccent.TLabel")
        self.next_label.pack(side="right", padx=(0, S(16)))
        gwrap = ttk.Frame(gcard, style="Card.TFrame")
        gwrap.pack(fill="both", expand=True)
        cols = [c[0] for c in self.GRID_COLS]
        self.grid = ttk.Treeview(gwrap, columns=cols, show="tree headings", selectmode="browse")
        self.grid.heading("#0", text="")
        self.grid.column("#0", width=S(54), minwidth=S(54), stretch=False, anchor="w")
        for key, label, width in self.GRID_COLS:
            self.grid.heading(key, text=label, anchor="w", command=lambda k=key: self.sort_by(k))
            self.grid.column(key, width=S(width), minwidth=S(50), anchor="w",
                             stretch=key in ("part", "name", "status"))
        gsb = ttk.Scrollbar(gwrap, orient="vertical", command=self.grid.yview)
        self.grid.configure(yscrollcommand=gsb.set)
        self.grid.pack(side="left", fill="both", expand=True)
        gsb.pack(side="right", fill="y")
        self.grid.bind("<<TreeviewSelect>>", self.on_grid_select)
        self.grid.bind("<Double-1>", self.on_grid_double_click)
        self.grid.bind("<Return>", lambda e: self.action_open())
        self.grid.bind("<ButtonPress-1>", self.on_dnd_start, add="+")
        self.grid.bind("<B1-Motion>", self.on_dnd_motion, add="+")
        self.grid.bind("<ButtonRelease-1>", self.on_dnd_end, add="+")
        for seq in ("<Button-3>", "<Button-2>"):
            self.grid.bind(seq, self.show_grid_menu)
        self.grid.bind("<Delete>", lambda e: self.action_delete_component())
        self.grid.bind("<F2>", lambda e: self.focus_part_name())

        # -- Right panel
        rpanel = ttk.Frame(self.body, style="TFrame")
        self.body.add(rpanel, weight=0)

        pcard = ttk.Frame(rpanel, style="Card.TFrame", padding=S(10))
        pcard.pack(fill="x")
        phead = ttk.Frame(pcard, style="Card.TFrame")
        phead.pack(fill="x", pady=(0, S(6)))
        ttk.Label(phead, text="PREVIEW", style="CardSection.TLabel").pack(side="left")
        self.preview_mode = ttk.Label(phead, text="", style="CardMuted.TLabel")
        self.preview_mode.pack(side="right")
        self.canvas = tk.Canvas(pcard, width=S(400), height=S(260), highlightthickness=1, bd=0)
        self.canvas.pack(fill="x")
        self.canvas.bind("<ButtonPress-1>", self.on_orbit_start)
        self.canvas.bind("<B1-Motion>", self.on_orbit_drag)
        self.canvas.bind("<Double-1>", lambda e: self.reset_view())
        self.canvas.bind("<MouseWheel>", self.on_zoom)
        self.canvas.bind("<Button-4>", self.on_zoom)
        self.canvas.bind("<Button-5>", self.on_zoom)
        self.canvas.bind("<Configure>", lambda e: self.request_redraw())
        pfoot = ttk.Frame(pcard, style="Card.TFrame")
        pfoot.pack(fill="x", pady=(S(8), 0))
        ttk.Label(pfoot, text="Drag to orbit · scroll to zoom", style="CardMuted.TLabel").pack(side="left")
        ttk.Button(pfoot, text="⚡ Rebuild mesh", style="Small.TButton", command=self.action_rebuild_mesh).pack(side="right")
        ttk.Button(pfoot, text="Reset view", style="Small.TButton", command=self.reset_view).pack(
            side="right", padx=(0, S(6)))

        icard = ttk.Frame(rpanel, style="Card.TFrame", padding=S(14))
        icard.pack(fill="both", expand=True, pady=(S(10), 0))
        ttk.Label(icard, text="PROPERTIES", style="CardSection.TLabel").pack(anchor="w")
        self.p_title = ttk.Label(icard, text="No component selected", style="FileTitle.TLabel",
                                 wraplength=S(380), justify="left")
        self.p_title.pack(anchor="w", pady=(S(6), S(6)))
        self.p_pill = tk.Label(icard, text="", font=("Segoe UI Semibold", 8), padx=S(8), pady=S(2), bd=0)
        self.p_pill.pack(anchor="w")

        kv = ttk.Frame(icard, style="Card.TFrame")
        kv.pack(fill="x", pady=(S(12), S(4)))
        self.p_vals = {}
        for r, (key, label) in enumerate((("type", "File type"), ("rev", "Revision"),
                                          ("folder", "Folder"), ("updated", "Last check-in"))):
            ttk.Label(kv, text=label, style="CardMuted.TLabel").grid(row=r, column=0, sticky="w", pady=S(2))
            v = ttk.Label(kv, text="—", style="CardValue.TLabel")
            v.grid(row=r, column=1, sticky="w", padx=(S(14), 0), pady=S(2))
            self.p_vals[key] = v

        ttk.Separator(icard, orient="horizontal").pack(fill="x", pady=S(10))
        ttk.Label(icard, text="Part name", style="CardMuted.TLabel").pack(anchor="w")
        self.part_var = tk.StringVar()
        self.part_entry = ttk.Entry(icard, textvariable=self.part_var)
        self.part_entry.pack(fill="x", pady=(S(3), S(8)), ipady=S(2))
        self.part_entry.bind("<Return>", lambda e: self.action_save())
        pnrow = ttk.Frame(icard, style="Card.TFrame")
        pnrow.pack(fill="x")
        ttk.Label(pnrow, text="Part number", style="CardMuted.TLabel").pack(side="left")
        self.pn_hint = ttk.Label(pnrow, text="", style="CardMuted.TLabel")
        self.pn_hint.pack(side="right")
        self.pn_var = tk.StringVar()
        self.pn_entry = ttk.Entry(icard, textvariable=self.pn_var)
        self.pn_entry.pack(fill="x", pady=(S(3), S(8)), ipady=S(2))
        ttk.Label(icard, text="Description", style="CardMuted.TLabel").pack(anchor="w")
        self.desc_var = tk.StringVar()
        self.desc_entry = ttk.Entry(icard, textvariable=self.desc_var)
        self.desc_entry.pack(fill="x", pady=(S(3), S(10)), ipady=S(2))
        self.desc_entry.bind("<Return>", lambda e: self.action_save())
        self.btn["save"] = ttk.Button(icard, text="Save properties", style="Accent.TButton", command=self.action_save)
        self.btn["save"].pack(anchor="e")

    def set_initial_sashes(self):
        try:
            total = self.body.winfo_width()
            self.body.sashpos(0, self.S(270))
            self.body.sashpos(1, max(self.S(760), total - self.S(440)))
        except tk.TclError:
            pass

    def bind_shortcuts(self):
        r = self.root
        r.bind("<F5>", lambda e: self.refresh_all())
        r.bind("<Control-f>", lambda e: (self.search_entry.focus_set(), self.search_entry.select_range(0, "end")))
        r.bind("<Control-n>", lambda e: self.action_new_from_template())
        self.search_entry.bind("<Escape>", lambda e: self.search_var.set(""))

    def focus_part_name(self):
        if "disabled" not in self.part_entry.state():
            self.part_entry.focus_set()
            self.part_entry.select_range(0, "end")

    # ------------------------------------------------------------- theming
    def apply_theme(self):
        t = self.t
        style_ttk(self.root, self.style, t, self.S)
        try:
            pop = self.root.tk.call("ttk::combobox::PopdownWindow", self.theme_combo)
            self.root.tk.call(f"{pop}.f.l", "configure", "-background", t["panel_bg"], "-foreground",
                              t["fg"], "-selectbackground", t["sel_bg"], "-selectforeground", t["sel_fg"])
        except tk.TclError:
            pass
        self.canvas.configure(highlightbackground=t["border"], bg=t["entry_bg"])
        self.log_text.configure(bg=t["log_bg"], fg=t["log_fg"], insertbackground=t["log_fg"])
        for lvl, col in (("ok", t["success"]), ("warn", t["warning"]), ("error", t["danger"]),
                         ("time", mix(t["log_fg"], t["log_bg"], 0.45))):
            self.log_text.tag_configure(lvl, foreground=col if t["dark"] or lvl == "time" else
                                        mix(col, "#ffffff", 0.35))
        self.status_dot.configure(bg=t["toolbar_bg"])
        for menu in (self.folders_menu, self.account_menu):
            menu.configure(**self.menu_colors())
        self.grid.tag_configure("odd", background=t["row_alt"])
        self.grid.tag_configure("mine", foreground=t["success"])
        self.grid.tag_configure("new", foreground=t["accent"])
        self.grid.tag_configure("locked", foreground=t["warning"])
        self.grid.tag_configure("dup", foreground=t["danger"])
        self.grid.tag_configure("lib", foreground=t["muted_fg"])
        self.grid.tag_configure("child", foreground=t["muted_fg"])
        self.ftree.tag_configure("drop", background=t["accent"], foreground=text_on(t["accent"]))
        self.ftree.tag_configure("project", foreground=t["fg"], font=("Segoe UI Semibold", 9))
        self.ftree.tag_configure("template", foreground=t["muted_fg"])
        self.draw_logo()
        self.update_properties()
        self.request_redraw()

    def menu_colors(self):
        t = self.t
        return dict(bg=t["panel_bg"], fg=t["fg"], activebackground=t["sel_bg"], activeforeground=t["sel_fg"],
                    disabledforeground=t["muted_fg"], bd=0, relief="flat", font=("Segoe UI", 9))

    def draw_logo(self):
        c, t, S = self.logo, self.t, self.S
        c.delete("all")
        c.configure(bg=t["header_bg"])
        s = S(30)
        h = s / 2
        acc = t["header_accent"]
        c.create_polygon(h, 1, s - 2, s * 0.27, h, s * 0.54, 2, s * 0.27, fill=mix(acc, "#ffffff", 0.35), outline="")
        c.create_polygon(2, s * 0.27, h, s * 0.54, h, s - 1, 2, s * 0.73, fill=acc, outline="")
        c.create_polygon(h, s * 0.54, s - 2, s * 0.27, s - 2, s * 0.73, h, s - 1, fill=mix(acc, "#000000", 0.3), outline="")

    def on_theme_change(self, _event=None):
        self.theme_name = self.theme_var.get()
        self.settings["theme"] = self.theme_name
        save_settings(self.settings)
        self.apply_theme()
        self.theme_combo.selection_clear()

    # ------------------------------------------------------------ logging
    def log(self, msg, level="info"):
        if not msg:
            return
        self.log_text.configure(state="normal")
        self.log_text.insert("end", datetime.now().strftime("%H:%M:%S  "), "time")
        self.log_text.insert("end", msg + "\n", level if level != "info" else ())
        self.log_text.see("end")
        self.log_text.configure(state="disabled")
        t = self.t
        color = {"ok": t["success"], "warn": t["warning"], "error": t["danger"]}.get(level, t["accent"])
        self.status_dot.configure(fg=color)
        self.status_label.configure(text=msg if len(msg) < 160 else msg[:157] + "...")

    def report(self, result, refresh=True):
        level, msg = result[0], result[1]
        self.log(msg, level)
        if level == "error":
            messagebox.showerror(APP_NAME, msg, parent=self.root)
        if refresh:
            self.refresh_all()

    def toggle_console(self):
        if self.console.winfo_ismapped():
            self.console.pack_forget()
            self.console_btn.configure(text="Console ▴")
            self.settings["console"] = False
        else:
            self.console.pack(side="bottom", fill="x", before=self.body)
            self.console_btn.configure(text="Console ▾")
            self.settings["console"] = True
        save_settings(self.settings)

    def startup_messages(self):
        if self.settings.get("console"):
            self.toggle_console()
        self.log(f"{APP_NAME} {APP_VERSION} ready. Signed in as {USER_NAME}.", "ok")
        warn = db_location_warning()
        if warn:
            self.warn_label.configure(text="⚠ Database on a sync folder")
            self.log(warn, "warn")
        legacy = sorted({r["locked_by"] for r in INDEX.records.values()
                         if r.get("locked_by") and "@" in r["locked_by"]})
        if legacy:
            self.log(f"Some files are still locked under v2.0 machine names ({', '.join(legacy)}). "
                     "An administrator can release them in Administration > Check-outs.", "warn")
        if is_admin() and USER_NAME.lower() == "admin":
            self.log("You are using the built-in 'admin' account. Create a named account for each person "
                     "in Administration > Users so the audit trail shows who did what.", "warn")

    def show_db_warning(self):
        messagebox.showwarning(APP_NAME, (db_location_warning() or "") +
                               "\n\nSQLite also needs reliable file locking. A mapped network share works for a "
                               "small team; a sync folder (OneDrive, Dropbox, Google Drive) does not.",
                               parent=self.root)

    # ------------------------------------------------------------ refresh
    def refresh_all(self):
        try:
            INDEX.refresh()
            if auto_number_enabled() and auto_number_candidates():
                for level, msg in auto_number_sweep():
                    self.log(msg, level)
        except sqlite3.Error as e:
            self.log(f"Database error: {e}", "error")
            return
        except OSError as e:
            self.log(f"File error while refreshing: {e}", "error")
        self.refresh_folders()
        self.refresh_grid()

    def auto_refresh_locks(self):
        """Picks up other users' check-outs without rescanning the vault."""
        try:
            before = {k: v.get("locked_by") for k, v in INDEX.records.items()}
            INDEX.reload_records()
            after = {k: v.get("locked_by") for k, v in INDEX.records.items()}
            if before != after:
                self.refresh_grid()
        except sqlite3.Error:
            pass
        self.root.after(60000, self.auto_refresh_locks)

    @staticmethod
    def _folder_icon(rel, leaf):
        low = leaf.lower()
        m = PROJECT_RE.match(leaf)
        if rel == "_Standard_Library":
            return "📚", ()
        if m:
            return ("📋", ("template",)) if int(m.group(1)) == 0 else ("🗂", ("project",))
        if low in PART_FOLDER_NAMES:
            return "🔩", ()
        if low in ASM_FOLDER_NAMES:
            return "🧩", ()
        return "📁", ()

    def refresh_folders(self):
        tree = self.ftree
        tree.delete(*tree.get_children())
        counts = {}
        for info in INDEX.files.values():
            if info.ext in PRIMARY_EXTS:
                counts[info.folder] = counts.get(info.folder, 0) + 1
        total = sum(counts.values())
        tree.insert("", "end", iid="*", text=f"🗄  Vault   {total}", open=True)
        for rel in sorted(INDEX.folders, key=str.lower):
            parts = rel.split(os.sep)
            for i in range(len(parts)):
                sub = os.sep.join(parts[:i + 1])
                iid = "F:" + sub
                if tree.exists(iid):
                    continue
                parent = "*" if i == 0 else "F:" + os.sep.join(parts[:i])
                n = counts.get(sub, 0)
                icon, tags = self._folder_icon(sub, parts[i])
                tree.insert(parent, "end", iid=iid, text=f"{icon}  {parts[i]}" + (f"   {n}" if n else ""),
                            open=i < 2, tags=tags)
        target = "*" if self.current_folder == "*" else "F:" + self.current_folder
        if not tree.exists(target):
            target, self.current_folder = "*", "*"
        parent = tree.parent(target)
        while parent:
            tree.item(parent, open=True)
            parent = tree.parent(parent)
        tree.selection_set(target)
        tree.see(target)

    def on_folder_select(self, _event=None):
        sel = self.ftree.selection()
        if not sel:
            return
        folder = "*" if sel[0] == "*" else sel[0][2:]
        if folder == self.current_folder:
            return
        self.current_folder = folder
        self.refresh_grid()

    def select_folder(self, rel):
        self.current_folder = rel or "*"
        self.refresh_folders()
        self.refresh_grid()

    def target_folder(self):
        return PROJECT_ROOT_REL if self.current_folder == "*" else self.current_folder

    def schedule_search(self):
        if self._search_job:
            self.root.after_cancel(self._search_job)
        self._search_job = self.root.after(160, self.refresh_grid)

    def _row_values(self, name):
        info = INDEX.files.get(name)
        rec = INDEX.records.get(name) or {}
        key, label = file_state(name)
        icons = {"mine": "✎  ", "new": "✚  ", "locked": "🔒 ", "dup": "⚠  ", "lib": "▣  ", "available": "●  "}
        if info:
            folder, mtime = info.folder, info.mtime
        else:
            folder = rec.get("folder_path") or ""
            local = sandbox_path(name)
            mtime = os.path.getmtime(local) if os.path.exists(local) else 0
        _, _, _, parsed_rev = parse_structured_name(name)
        return {
            "name": name, "key": key,
            "pn": rec.get("part_number") or "",
            "part": display_part_name(name, rec),
            "rev": rec.get("revision") or parsed_rev,
            "status": icons.get(key, "") + label,
            "folder": folder,
            "mtime": mtime,
            "modified": datetime.fromtimestamp(mtime).strftime("%d %b %Y  %H:%M") if mtime else "",
            "desc": rec.get("description") or "",
            "cad": cad_label(name),
        }

    def refresh_grid(self):
        self._search_job = None
        tree = self.grid
        tree.delete(*tree.get_children())
        self.row_map = {}
        q = self.search_var.get().strip().lower()
        folder = self.current_folder

        def in_scope(f):
            return q or folder == "*" or norm(f) == norm(folder)

        primaries, children = [], {}
        for name, info in INDEX.files.items():
            if not in_scope(info.folder):
                continue
            base = os.path.splitext(name)[0].lower()
            if info.ext in PRIMARY_EXTS:
                primaries.append(name)
            else:
                children.setdefault((info.folder, base), []).append(name)
        primary_keys = {(INDEX.files[n].folder, os.path.splitext(n)[0].lower()) for n in primaries}
        for key, lst in children.items():          # orphan assets are shown on their own
            if key not in primary_keys:
                primaries.extend(lst)
        for name, rec in INDEX.records.items():   # your new, not-yet-checked-in files
            if name not in INDEX.files and rec.get("locked_by") == USER_NAME and os.path.exists(sandbox_path(name)):
                if in_scope(rec.get("folder_path") or ""):
                    primaries.append(name)

        rows = [self._row_values(n) for n in primaries]
        if q:
            rows = [r for r in rows if q in r["name"].lower() or q in r["pn"].lower() or q in r["desc"].lower()
                    or q in r["part"].lower() or q in r["cad"].lower()]
        sort_key = {"modified": lambda r: r["mtime"]}.get(self.sort_col, lambda r: str(r[self.sort_col]).lower())
        rows.sort(key=sort_key, reverse=self.sort_rev)

        for key, label, _ in self.GRID_COLS:
            arrow = (" ▾" if self.sort_rev else " ▴") if key == self.sort_col else ""
            tree.heading(key, text=label + arrow)

        select_iid = None
        for i, r in enumerate(rows):
            ext = os.path.splitext(r["name"])[1].lower()
            icon = "▤" if ext in DRAWING_EXTS else "◇" if ext in (".step", ".stp", ".iges", ".igs") else \
                "△" if ext == ".stl" else "◆"
            tags = [r["key"]] + (["odd"] if i % 2 else [])
            iid = tree.insert("", "end", text=f"  {icon}", tags=tags, open=False,
                              values=(r["pn"] or "—", r["part"], r["cad"], r["name"], r["rev"], r["status"], r["folder"],
                                      r["modified"]))
            self.row_map[iid] = (r["name"], r["name"])
            if r["name"] == self.selected:
                select_iid = iid
            info = INDEX.files.get(r["name"])
            if info and info.ext in PRIMARY_EXTS:
                for c in sorted(children.get((info.folder, os.path.splitext(r["name"])[0].lower()), [])):
                    cext = os.path.splitext(c)[1].upper().lstrip(".")
                    local = "In sandbox" if os.path.exists(sandbox_path(c)) else "Linked asset"
                    cid = tree.insert(iid, "end", text="    └", tags=["child"] + (["odd"] if i % 2 else []),
                                      values=("", "", cad_label(c), c, f"[{cext}]", local, info.folder, ""))
                    self.row_map[cid] = (r["name"], c)

        scope = "all folders" if (q or folder == "*") else folder
        self.grid_title.configure(text=f"COMPONENTS  ·  {scope.upper()}")
        self.count_label.configure(text=f"{len(rows)} item{'s' if len(rows) != 1 else ''}"
                                        + (f" matching “{q}”" if q else ""))
        self.next_label.configure(text=self._next_number_text(folder))
        if select_iid:
            tree.selection_set(select_iid)
            tree.see(select_iid)
        else:
            self.selected = self.preview_file = None
            self.update_properties()
            self.request_redraw()

    def _next_number_text(self, folder):
        ctx = numbering_context(folder) if folder != "*" else None
        if not ctx:
            return ""
        try:
            if ctx["kind"]:
                nxt = peek_next_number(ctx["prefix"], ctx["kind"])
                what = "assembly" if ctx["kind"] == "A" else "part"
                return f"Next {what} number: {nxt}" if nxt else "Number series full"
            p, a = peek_next_number(ctx["prefix"], "P"), peek_next_number(ctx["prefix"], "A")
            return f"Next numbers: {p} · {a}"
        except sqlite3.Error:
            return ""

    def sort_by(self, col):
        if self.sort_col == col:
            self.sort_rev = not self.sort_rev
        else:
            self.sort_col, self.sort_rev = col, False
        self.refresh_grid()

    # ---------------------------------------------------------- selection
    def on_grid_select(self, _event=None):
        sel = self.grid.selection()
        if not sel or sel[0] not in self.row_map:
            return
        action_file, preview_file = self.row_map[sel[0]]
        changed = preview_file != self.preview_file
        self.selected, self.preview_file = action_file, preview_file
        if changed:
            self.cam.update(rx=25.0, ry=45.0, zoom=1.0)
        self.update_properties()
        self.request_redraw()

    def on_grid_double_click(self, event):
        if self.grid.identify_region(event.x, event.y) in ("cell", "tree"):
            self.action_open()

    def _pn_locked(self, f):
        """Part numbers that are part of the file name are managed by the numbering system."""
        return bool(f) and is_numbered_name(os.path.splitext(f)[0])

    def update_properties(self):
        t, f = self.t, self.selected
        if not f:
            self.p_title.configure(text="No component selected")
            self.p_pill.configure(text="", bg=t["panel_bg"])
            for v in self.p_vals.values():
                v.configure(text="—")
            for var in (self.part_var, self.pn_var, self.desc_var):
                var.set("")
            self.pn_hint.configure(text="")
            self.update_action_states()
            return
        rec = INDEX.records.get(f) or {}
        info = INDEX.files.get(f)
        key, label = file_state(f)
        _, _, ftype, parsed_rev = parse_structured_name(f)
        pill = {"mine": t["success"], "new": t["accent"], "locked": t["warning"], "dup": t["danger"],
                "lib": t["muted_fg"], "available": mix(t["success"], t["panel_bg"], 0.2)}.get(key, t["muted_fg"])
        self.p_title.configure(text=f)
        self.p_pill.configure(text=label.upper(), bg=pill, fg=text_on(pill))
        kind = "Drawing" if os.path.splitext(f)[1].lower() in DRAWING_EXTS else "Component"
        updated = rec.get("last_updated") or ""
        if "T" in updated:
            updated = updated.replace("T", " ")[:16]
        if os.path.splitext(f)[1].lower() in CAD_MODEL_EXTS:
            kind = "Assembly" if (parse_pn(os.path.splitext(f)[0]) or (0, ""))[1] == "A" else "Part"
        self.p_vals["type"].configure(text=f"{cad_label(f)} {kind.lower()}")
        self.p_vals["rev"].configure(text=rec.get("revision") or parsed_rev)
        self.p_vals["folder"].configure(text=info.folder if info else (rec.get("folder_path") or "—") + " (pending)")
        self.p_vals["updated"].configure(text=updated[:16] or "—")
        self.part_var.set(display_part_name(f, rec))
        pp = parse_pn(os.path.splitext(f)[0])
        self.pn_var.set(rec.get("part_number") or (pp[3] if pp else ""))
        self.desc_var.set(rec.get("description") or "")
        self.pn_hint.configure(text="🔒 set by numbering" if self._pn_locked(f) else "")
        self.update_action_states()

    def update_action_states(self):
        f = self.selected
        key = file_state(f)[0] if f else None
        has_local = bool(f) and (os.path.exists(sandbox_path(f)) or os.path.exists(os.path.join(LOCAL_LIB_DIR, f)))
        can = {
            "checkout": key in ("available", "lib"),
            "checkin": key in ("mine", "new"),
            "undo": key in ("mine", "new"),
            "open": has_local,
            "minor": key == "available",
            "major": key == "available",
            "history": bool(f),
            "export": bool(f) and f in INDEX.files and os.path.splitext(f)[1].lower() in CAD_MODEL_EXTS,
            "save": bool(f) and key not in ("dup", "missing", "lib", None),
        }
        for k, b in self.btn.items():
            b.state(["!disabled"] if can.get(k) else ["disabled"])
        if "checkout" in self.btn:
            self.btn["checkout"].configure(text="↓  Sync Library Part" if key == "lib" else "↓  Check Out")
        on = ["!disabled", "!readonly"] if can["save"] else ["disabled"]
        self.part_entry.state(on)
        self.desc_entry.state(on)
        if not can["save"]:
            self.pn_entry.state(["disabled"])
        elif self._pn_locked(f):
            self.pn_entry.state(["!disabled", "readonly"])
        else:
            self.pn_entry.state(["!disabled", "!readonly"])

    # ------------------------------------------------------------ preview
    def request_redraw(self):
        if not self._redraw_pending:
            self._redraw_pending = True
            self.root.after(16, self._redraw)

    def _redraw(self):
        self._redraw_pending = False
        c, t = self.canvas, self.t
        c.delete("all")
        w, h = max(c.winfo_width(), 50), max(c.winfo_height(), 50)
        f = self.preview_file
        if not f:
            self.preview_mode.configure(text="")
            render_placeholder(c, w, h, t, self.cam, "No selection", "Select a component to preview it")
            return
        ext = os.path.splitext(f)[1].lower()
        if ext in DRAWING_EXTS:
            self.preview_mode.configure(text="2D drawing")
            render_drawing_sheet(c, f, w, h, t, INDEX.records.get(f))
            return
        stl = find_preview_stl(f)
        mesh = load_mesh(stl) if stl else None
        if mesh:
            src = "sandbox" if is_under(stl, LOCAL_WORKSPACE) else "vault"
            self.preview_mode.configure(text=f"3D mesh · {mesh['n']:,} faces · {src}")
            render_mesh(c, mesh, w, h, t, self.cam)
        else:
            self.preview_mode.configure(text="No mesh")
            src = self._preview_source(f)
            thumb = cad_thumbnail(src)
            if thumb and self._draw_thumbnail(c, w, h, t, thumb):
                tip = "  ·  export an STL to orbit" if cad_app_of(f) == "physcad" else ""
                self.preview_mode.configure(text=f"{cad_label(f)} thumbnail{tip}")
                return
            if can_make_mesh(f):
                hint = "Use ⚡ Rebuild mesh to generate one"
            elif cad_app_of(f):
                hint = f"Save '{os.path.splitext(f)[0]}.stl' next to it for a 3D preview"
            else:
                hint = "No STL companion for this file"
            render_placeholder(c, w, h, t, self.cam, display_part_name(f, INDEX.records.get(f)), hint)

    def _preview_source(self, f):
        local = sandbox_path(f)
        if file_state(f)[0] in ("mine", "new") and os.path.exists(local):
            return local
        info = INDEX.files.get(f)
        return info.path if info else (local if os.path.exists(local) else None)

    def _draw_thumbnail(self, c, w, h, t, png):
        try:
            img = tk.PhotoImage(data=base64.b64encode(png).decode("ascii"))
        except tk.TclError:
            return False
        draw_backdrop(c, w, h, t)
        iw, ih = max(1, img.width()), max(1, img.height())
        aw, ah = max(1, w - 20), max(1, h - 20)
        if iw > aw or ih > ah:
            d = max(-(-iw // aw), -(-ih // ah))           # ceiling division: shrink to fit
            img = img.subsample(d)
        else:
            k = min(aw // iw, ah // ih)
            if k > 1:
                img = img.zoom(k)
        self._thumb_img = img                         # keep a reference so Tk doesn't discard it
        c.create_image(w // 2, h // 2, image=img)
        return True

    def on_orbit_start(self, e):
        self._drag_xy = (e.x, e.y)

    def on_orbit_drag(self, e):
        dx, dy = e.x - self._drag_xy[0], e.y - self._drag_xy[1]
        self.cam["ry"] += dx * 0.8
        self.cam["rx"] += dy * 0.8
        self._drag_xy = (e.x, e.y)
        self.request_redraw()

    def on_zoom(self, e):
        up = getattr(e, "delta", 0) > 0 or getattr(e, "num", 0) == 4
        self.cam["zoom"] = max(0.2, min(5.0, self.cam["zoom"] * (1.15 if up else 1 / 1.15)))
        self.request_redraw()

    def reset_view(self):
        self.cam.update(rx=25.0, ry=45.0, zoom=1.0)
        self.request_redraw()

    # ------------------------------------------------------- drag & drop
    def on_dnd_start(self, e):
        self._dnd_file, self._dnd_active = None, False
        if self.grid.identify_region(e.x, e.y) not in ("cell", "tree"):
            return
        iid = self.grid.identify_row(e.y)
        if iid in self.row_map and not self.grid.parent(iid):
            name = self.row_map[iid][0]
            if name in INDEX.files:
                self._dnd_file, self._dnd_start = name, (e.x, e.y)

    def on_dnd_motion(self, e):
        if not self._dnd_file:
            return
        if not self._dnd_active:
            if abs(e.x - self._dnd_start[0]) + abs(e.y - self._dnd_start[1]) < 8:
                return
            self._dnd_active = True
            self.grid.configure(cursor="fleur")
        target = None
        if str(self.root.winfo_containing(e.x_root, e.y_root)) == str(self.ftree):
            target = self.ftree.identify_row(e.y_root - self.ftree.winfo_rooty()) or None
        if target != self._dnd_target:
            self._clear_drop_highlight()
            if target:
                self._dnd_saved_tags = self.ftree.item(target, "tags")
                self.ftree.item(target, tags=("drop",))
            self._dnd_target = target

    def _clear_drop_highlight(self):
        prev = self._dnd_target
        if prev and self.ftree.exists(prev):
            self.ftree.item(prev, tags=getattr(self, "_dnd_saved_tags", ()) or ())

    def on_dnd_end(self, _e):
        name, target, active = self._dnd_file, self._dnd_target, self._dnd_active
        self._dnd_file, self._dnd_target, self._dnd_active = None, None, False
        self.grid.configure(cursor="")
        self._dnd_target = target
        self._clear_drop_highlight()
        self._dnd_target = None
        if not (name and active and target):
            return
        rel = VAULT_ROOT_LABEL if target == "*" else target[2:]
        try:
            res = move_component(name, rel)
        except OSError as ex:
            res = ("error", f"Move failed: {ex}")
        self.report(res)

    # --------------------------------------------------------- menus
    def show_grid_menu(self, e):
        iid = self.grid.identify_row(e.y)
        if not iid:
            return
        self.grid.selection_set(iid)
        self.grid.focus(iid)
        self.on_grid_select()
        f = self.selected
        key = file_state(f)[0] if f else None
        m = tk.Menu(self.root, tearoff=0, **self.menu_colors())

        def item(label, cmd, enabled=True):
            m.add_command(label=label, command=cmd, state="normal" if enabled else "disabled")

        item("↓  Check Out", self.action_check_out, key in ("available", "lib"))
        item("↑  Check In", self.action_check_in, key in ("mine", "new"))
        item("↺  Undo Check-Out", self.action_undo, key in ("mine", "new"))
        m.add_separator()
        item("◈  Open in CAD app", self.action_open, "disabled" not in self.btn["open"].state())
        item("📂  Show in Explorer", self.action_show_in_folder, f in INDEX.files)
        item("✎  Rename Part…  (F2)", self.focus_part_name, key in ("available", "new"))
        item("#  Assign Part Number", lambda: self.action_number_file(f),
             bool(f) and f in INDEX.files and f in auto_number_candidates())
        item("⚡  Rebuild mesh", self.action_rebuild_mesh, bool(f) and can_make_mesh(f))
        item("☰  History & Baselines", self.action_history, bool(f))
        m.add_separator()
        item("⇪  Export STEP & PDF", self.action_export,
             bool(f) and f in INDEX.files and os.path.splitext(f)[1].lower() in CAD_MODEL_EXTS)
        item("🔓  Break Lock…  (admin)", self.action_break_lock, key == "locked")
        item("🗑  Delete Component…", self.action_delete_component, key in ("available", "dup"))
        m.tk_popup(e.x_root, e.y_root)

    def show_folder_menu(self, e):
        iid = self.ftree.identify_row(e.y)
        if iid:
            self.ftree.selection_set(iid)
            self.on_folder_select()
        rel = self.current_folder
        protected = _is_protected_folder(rel)
        ctx = numbering_context(rel) if rel != "*" else None
        pending = len(auto_number_candidates(rel)) if rel != "*" else len(auto_number_candidates())
        m = tk.Menu(self.root, tearoff=0, **self.menu_colors())
        m.add_command(label="🗂  New Project…", command=self.action_new_project)
        m.add_command(label="✚  New Part Here…", command=self.action_new_from_template)
        m.add_command(label="📁  New Subfolder…", command=self.action_new_folder)
        m.add_separator()
        m.add_command(label=f"#  Number Un-numbered Files ({pending})", command=self.action_number_folder,
                      state="normal" if pending else "disabled")
        m.add_separator()
        m.add_command(label="🗑  Delete Folder…", command=self.action_delete_folder,
                      state="disabled" if protected else "normal")
        m.add_command(label="📂  Open in Explorer", command=lambda: self.open_folder(folder_abs(rel)))
        if ctx and not ctx["kind"]:
            m.add_separator()
            m.add_command(label="Tip: select Parts or Assemblies to get automatic numbers", state="disabled")
        m.tk_popup(e.x_root, e.y_root)

    def open_folder(self, path):
        try:
            os.makedirs(path, exist_ok=True)
            open_path(path)
        except Exception as ex:
            self.log(f"Could not open folder: {ex}", "error")

    # ------------------------------------------------------- dialogs
    def ask_text(self, title, heading, prompt, required=False, initial="", ok_text="OK", ok_style="Accent.TButton",
                 parent=None):
        dlg = ThemedDialog(self, title, heading, prompt, parent=parent)
        var = tk.StringVar(value=initial)
        e = ttk.Entry(dlg.body, textvariable=var, width=52)
        e.pack(fill="x", ipady=self.S(2))
        hint = ttk.Label(dlg.body, text="Required" if required else "Optional", style="CardMuted.TLabel")
        hint.pack(anchor="w", pady=(self.S(4), 0))

        def validate():
            v = var.get().strip()
            if required and not v:
                hint.configure(text="This field is required.", style="CardDanger.TLabel")
                return INVALID
            return v
        dlg.validate = validate
        dlg.add_buttons(ok_text, ok_style)
        dlg.focus_widget = e
        return dlg.show()

    def confirm(self, title, msg):
        return messagebox.askyesno(title, msg, icon="warning", parent=self.root)

    # ------------------------------------------------------- actions
    def _guard(self, fn, *args):
        try:
            return fn(*args)
        except sqlite3.OperationalError as e:
            return "error", f"Database busy or unavailable ({e}). Try again in a moment."
        except OSError as e:
            return "error", f"File operation failed: {e}"
        except ValueError as e:
            return "error", str(e)

    def action_new_project(self):
        try:
            nxt = next_project_prefix()
        except sqlite3.Error as e:
            self.log(f"Database error: {e}", "error")
            return
        root_label = "the vault root" if PROJECT_ROOT_REL in (".", "") else f"'{PROJECT_ROOT_REL}'"
        tmpl = _project_template_dir()
        layout = (f"Sub-folders are copied from '{os.path.basename(tmpl)}'." if tmpl
                  else "Assemblies and Parts sub-folders are created.")
        res = run_form(self, "New Project", "New project",
                       f"Created in {root_label}. {layout}",
                       [("name", "Project name", False, "")],
                       lambda v: self._guard(create_project, v["name"]),
                       "Create project", note=f"Project number:  {nxt}_", width=420)
        if res:
            self.log(res[1], res[0])
            folder = res[2] if len(res) > 2 else None
            INDEX.refresh()
            if folder:
                parts = [s for s in DEFAULT_PROJECT_SUBFOLDERS if os.path.isdir(os.path.join(folder_abs(folder), s))]
                pick = os.path.join(folder, "Parts") if "Parts" in parts else folder
                self.select_folder(pick)

    def action_number_file(self, f):
        self.report(self._guard(auto_number_file, f))

    def action_number_folder(self):
        rel = self.current_folder
        names = auto_number_candidates(rel)
        if not names:
            return
        if not self.confirm("Assign Part Numbers",
                            f"Give {len(names)} file(s) the next part numbers and rename them?\n\n"
                            + "\n".join(names[:12]) + ("\n…" if len(names) > 12 else "")):
            return
        try:
            results = auto_number_sweep(rel)
        except (OSError, sqlite3.Error) as e:
            results = [("error", f"Numbering failed: {e}")]
        for lvl, msg in results:
            self.log(msg, lvl)
        self.refresh_all()

    def action_check_out(self):
        f = self.selected
        if not f:
            return
        if file_state(f)[0] == "lib":
            self.report(self._guard(check_out, f, ""))
            return
        c = self.ask_text("Check Out", f"Check out {f}", "Add an optional note for the audit trail.",
                          ok_text="Check Out")
        if c is not None:
            self.report(self._guard(check_out, f, c))

    def action_check_in(self):
        f = self.selected
        if not f:
            return
        c = self.ask_text("Check In", f"Check in {f}", "Describe what changed. This is saved in the audit trail.",
                          ok_text="Check In", ok_style="Success.TButton")
        if c is None:
            return
        self.root.config(cursor="watch")
        self.root.update_idletasks()
        try:
            self.report(self._guard(check_in, f, c))
        finally:
            self.root.config(cursor="")

    def action_undo(self):
        f = self.selected
        if f and self.confirm("Undo Check-Out", f"Undo the check-out of '{f}'?\n\nYour sandbox copy will be moved "
                                                "to _Discarded and the lock released. Nothing is written to the vault."):
            self.report(self._guard(undo_check_out, f))

    def action_break_lock(self):
        f = self.selected
        if not f or not self.ensure_admin():
            return
        owner = (INDEX.records.get(f) or {}).get("locked_by")
        reason = self.ask_text("Break Lock", f"Break lock on {f}",
                               f"Currently checked out by {owner}. Any changes they have not checked in will not "
                               "reach the vault. Give a reason for the audit trail.",
                               required=True, ok_text="Break Lock", ok_style="Danger.TButton")
        if reason:
            self.report(self._guard(break_lock, f, reason))

    def action_up_issue(self, bump):
        f = self.selected
        if not f:
            return
        rec = INDEX.records.get(f) or {}
        curr = rec.get("revision") or "A-01"
        default = calculate_next_rev(curr, bump)
        if default is None:
            self.log("Revision Z is the last major revision. Enter a custom revision instead.", "warn")
            default = ""
        dlg = ThemedDialog(self, "Up-Issue", f"{'Major' if bump == 'major' else 'Minor'} up-issue",
                           f"{f}\nCurrent revision: {curr}")
        S = self.S
        form = ttk.Frame(dlg.body, style="Dialog.TFrame")
        form.pack(fill="x")
        ttk.Label(form, text="New revision", style="CardMuted.TLabel").grid(row=0, column=0, sticky="w")
        rev_var = tk.StringVar(value=default)
        rev_e = ttk.Entry(form, textvariable=rev_var, width=12)
        rev_e.grid(row=1, column=0, sticky="w", pady=(S(3), S(10)), ipady=S(2))
        ttk.Label(form, text="Comment (required if you change the suggested revision)",
                  style="CardMuted.TLabel").grid(row=2, column=0, sticky="w")
        com_var = tk.StringVar()
        com_e = ttk.Entry(form, textvariable=com_var, width=52)
        com_e.grid(row=3, column=0, sticky="ew", pady=(S(3), 0), ipady=S(2))
        err = ttk.Label(form, text="", style="CardDanger.TLabel")
        err.grid(row=4, column=0, sticky="w", pady=(S(6), 0))
        form.columnconfigure(0, weight=1)

        def validate():
            nr, c = rev_var.get().strip().upper(), com_var.get().strip()
            if not REV_PATTERN.match(nr):
                err.configure(text="Use the format letter-number, e.g. A-02 or B-01.")
                return INVALID
            if nr != default and not c:
                err.configure(text="You changed the suggested revision, so add a comment.")
                return INVALID
            return nr, c
        dlg.validate = validate
        dlg.add_buttons("Up-Issue", "Accent.TButton")
        dlg.focus_widget = com_e
        res = dlg.show()
        if res:
            self.report(self._guard(up_issue, f, res[0], res[1]))

    def action_new_from_template(self):
        templates = sorted(f for f in os.listdir(TEMPLATES_DIR) if os.path.isfile(os.path.join(TEMPLATES_DIR, f)))
        if not templates:
            self.log("No templates found. Add template files (e.g. Part.sc, Assembly.sc) to the _Templates folder.",
                     "warn")
            self.open_folder(TEMPLATES_DIR)
            return
        folder = self.target_folder()
        ctx = numbering_context(folder)
        S = self.S
        dlg = ThemedDialog(self, "New Part", "New part",
                           f"Created in your sandbox and checked out to you. "
                           f"It is added to '{folder}' when you first check it in.", width=440)
        ttk.Label(dlg.body, text="Template", style="CardMuted.TLabel").pack(anchor="w")
        tv = tk.StringVar(value=self._default_template(templates, ctx))
        ttk.Combobox(dlg.body, textvariable=tv, values=templates, state="readonly").pack(fill="x", pady=(S(3), S(2)))
        app_label = ttk.Label(dlg.body, text="", style="CardMuted.TLabel")
        app_label.pack(anchor="w", pady=(0, S(10)))
        tv.trace_add("write", lambda *_: app_label.configure(text=f"Opens in {cad_label(tv.get())}"))
        app_label.configure(text=f"Opens in {cad_label(tv.get())}")
        numbered_ext = ctx and ctx["kind"]
        pn_label = ttk.Label(dlg.body, text="", style="CardAccent.TLabel")
        pn_label.pack(anchor="w", pady=(0, S(8)))

        def update_pn_preview(*_):
            ext = os.path.splitext(tv.get())[1]
            nb = numbering_for_target(folder, ext)
            if nb:
                nxt = peek_next_number(*nb)
                pn_label.configure(text=f"Part number:  {nxt}   (assigned when you click Create)")
            elif ctx and not ctx["kind"]:
                pn_label.configure(text="Tip: pick the project's Parts or Assemblies folder to get a part number.")
            else:
                pn_label.configure(text="")
        tv.trace_add("write", update_pn_preview)
        update_pn_preview()
        ttk.Label(dlg.body, text="Part name" if numbered_ext else "File name", style="CardMuted.TLabel").pack(anchor="w")
        nv = tk.StringVar()
        ne = ttk.Entry(dlg.body, textvariable=nv)
        ne.pack(fill="x", pady=(S(3), 0), ipady=S(2))
        err = ttk.Label(dlg.body, text="", style="CardDanger.TLabel", wraplength=S(400))
        err.pack(anchor="w", pady=(S(6), 0))

        def validate():
            res = self._guard(create_from_template, tv.get(), nv.get().strip(), folder)
            if res[0] == "error":
                err.configure(text=res[1])
                return INVALID
            return res
        dlg.validate = validate
        dlg.add_buttons("Create & Check Out", "Success.TButton")
        dlg.focus_widget = ne
        res = dlg.show()
        if res:
            if len(res) > 2:
                self.selected = res[2]
            self.settings["last_template"] = tv.get()
            save_settings(self.settings)
            self.report(res)

    def _default_template(self, templates, ctx):
        """Picks a Part/Assembly template, preferring the CAD app you used last time."""
        kw = {"A": ("asm", "assem"), "P": ("part", "prt")}.get(ctx["kind"] if ctx else None, ())
        pool = [t for t in templates if any(k in t.lower() for k in kw)] or templates
        last = self.settings.get("last_template")
        if last:
            last_app = cad_app_of(last)
            for t in pool:
                if cad_app_of(t) == last_app:
                    return t
            if last in pool:
                return last
        return pool[0]

    def action_add_files(self):
        folder = self.target_folder()
        if is_under(folder_abs(folder), STANDARD_LIB_DIR) and not self.confirm(
                "Add to Standard Library", "You are adding files to the read-only Standard Library. Continue?"):
            return
        ctx = numbering_context(folder)
        title = f"Add files to '{folder}'" + ("  (parts will be numbered)" if ctx and ctx["kind"] else "")
        pats = " ".join("*" + e for e in sorted(VAULT_EXTS))
        types = [("CAD files", pats)] + [(a["label"], " ".join("*" + e for e in a["models"] + a["drawings"]))
                                          for a in CAD_APPS.values() if a["models"]] + [("All files", "*.*")]
        paths = filedialog.askopenfilenames(parent=self.root, title=title, filetypes=types)
        if not paths:
            return
        try:
            results = add_files_to_vault(paths, folder)
        except (OSError, sqlite3.Error, ValueError) as e:
            results = [("error", f"Import failed: {e}")]
        for lvl, msg in results:
            self.log(msg, lvl)
        added = sum(1 for lvl, _ in results if lvl == "ok")
        self.log(f"Imported {added} of {len(paths)} file(s) into '{folder}'.", "ok" if added else "warn")
        self.refresh_all()

    def action_open(self):
        if self.selected:
            self.report(self._guard(open_in_cad, self.selected), refresh=False)

    def action_show_in_folder(self):
        info = INDEX.files.get(self.selected or "")
        if not info:
            return
        if sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", os.path.normpath(info.path)])
        else:
            self.open_folder(os.path.dirname(info.path))

    def action_save(self):
        f = self.selected
        if not f or "disabled" in self.btn["save"].state():
            return
        rec = INDEX.records.get(f) or {}
        new_part = self.part_var.get().strip()
        if not new_part:
            self.log("Part name cannot be blank.", "error")
            return
        pn = self.pn_var.get().strip()
        desc = self.desc_var.get().strip()
        current_in_name = clean_part_name(os.path.splitext(f)[0])
        target = f
        if new_part != current_in_name:
            stem, ext = os.path.splitext(f)
            pp, lm = parse_pn(stem), LEGACY_NUM_RE.match(stem)
            num = pp[3] if pp else (lm.group(0) if lm else "")
            proposed = (f"{num} {new_part}" if num else new_part) + ext
            if self.confirm("Rename File", f"Rename the file to match the new part name?\n\n{f}\n→  {proposed}\n\n"
                                           "Linked drawings, meshes and history are renamed with it.\n"
                                           "Choose No to keep the file name and only change the displayed name."):
                level, msg, target = self._guard_rename(f, new_part)
                self.log(msg, level)
                if level == "error":
                    messagebox.showerror(APP_NAME, msg, parent=self.root)
                    return
                INDEX.refresh()
        if self._pn_locked(target):
            pp = parse_pn(os.path.splitext(target)[0])
            pn = pp[3] if pp else (rec.get("part_number") or pn)
        self.selected = target
        self.report(self._guard(save_metadata, target, new_part, pn, desc))

    def _guard_rename(self, f, new_part):
        try:
            return rename_component(f, new_part)
        except (OSError, sqlite3.Error) as e:
            return "error", f"Rename failed: {e}", f

    def action_rebuild_mesh(self):
        f = self.selected
        if not f:
            return
        key = file_state(f)[0]
        local = sandbox_path(f)
        if key in ("mine", "new") and os.path.exists(local):
            src = local
        elif f in INDEX.files:
            src = INDEX.files[f].path
        else:
            self.log(f"Could not locate '{f}' on disk.", "error")
            return
        self.log(f"Generating mesh for '{f}'…")
        self.root.config(cursor="watch")
        self.root.update_idletasks()
        try:
            stl, msg = generate_stl_companion(src)
        finally:
            self.root.config(cursor="")
        if stl:
            self.log(f"Mesh generated: '{os.path.basename(stl)}'.", "ok")
            self.refresh_all()
        else:
            self.log(f"Mesh generation failed: {msg}", "error")

    def action_delete_component(self):
        f = self.selected
        if not f or f not in INDEX.files:
            return
        if self.confirm("Delete Component", f"Delete '{f}' from the vault?\n\nThe file and its mesh are moved to "
                                            "_History/_Deleted. Drawings and PDFs with the same name are kept.\n"
                                            "Its part number is not reused."):
            self.report(self._guard(delete_component, f))

    def action_new_folder(self):
        parent = self.current_folder
        where = PROJECT_ROOT_REL if parent == "*" else parent
        name = self.ask_text("New Folder", "New subfolder", f"Created inside '{where}'. "
                             "To start a numbered project use New Project instead.", required=True,
                             ok_text="Create")
        if name:
            self.report(self._guard(create_folder, parent, name))

    def action_delete_folder(self):
        rel = self.current_folder
        if _is_protected_folder(rel):
            self.log(f"'{rel if rel != '*' else 'Vault'}' is protected and cannot be deleted.", "warn")
            return
        if self.confirm("Delete Folder", f"Delete folder '{rel}' and everything in it?\n\n"
                                         "It is moved to _History/_Deleted, not erased."):
            self.report(self._guard(delete_folder, rel))

    def action_history(self):
        f = self.selected
        if not f:
            return
        S, t = self.S, self.t
        dlg = ThemedDialog(self, f"History · {f}", "History & baselines", f, width=860, resizable=True)
        ttk.Label(dlg.body, text="AUDIT TRAIL", style="CardSection.TLabel").pack(anchor="w")
        cols = ("Timestamp", "User", "Action", "Rev", "Comment")
        wrap = ttk.Frame(dlg.body, style="Dialog.TFrame")
        wrap.pack(fill="both", expand=True, pady=(S(4), S(12)))
        at = ttk.Treeview(wrap, columns=cols, show="headings", height=10)
        for c, wdt in zip(cols, (140, 170, 150, 60, 300)):
            at.heading(c, text=c, anchor="w")
            at.column(c, width=S(wdt), anchor="w", stretch=c == "Comment")
        sb = ttk.Scrollbar(wrap, orient="vertical", command=at.yview)
        at.configure(yscrollcommand=sb.set)
        at.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        at.tag_configure("odd", background=t["row_alt"])
        try:
            logs = get_audit(f)
        except sqlite3.Error as e:
            logs = []
            self.log(f"Could not read audit log: {e}", "error")
        for i, row in enumerate(logs):
            at.insert("", "end", values=row, tags=("odd",) if i % 2 else ())
        if not logs:
            at.insert("", "end", values=("—", "—", "No events logged yet", "—", "—"))

        ttk.Label(dlg.body, text="ARCHIVED BASELINES", style="CardSection.TLabel").pack(anchor="w")
        bt = ttk.Treeview(dlg.body, columns=("file", "date", "size"), show="headings", height=5, selectmode="browse")
        for c, label, wdt in (("file", "File", 420), ("date", "Archived", 160), ("size", "Size", 100)):
            bt.heading(c, text=label, anchor="w")
            bt.column(c, width=S(wdt), anchor="w")
        bt.pack(fill="x", pady=(S(4), 0))
        for name, mtime, size in baseline_files(f):
            bt.insert("", "end", iid=name, values=(name, datetime.fromtimestamp(mtime).strftime("%d %b %Y  %H:%M"),
                                                   f"{size / 1024:,.0f} KB"))
        mine = file_state(f)[0] == "mine"
        ttk.Label(dlg.buttons, style="CardMuted.TLabel",
                  text="" if mine else "Check the file out to restore a baseline into your sandbox.").pack(side="left")

        def do_restore():
            sel = bt.selection()
            if not sel:
                return
            if self.confirm("Restore Baseline", f"Replace your sandbox copy of '{f}' with '{sel[0]}'?\n\n"
                                                "Your current sandbox copy is kept in _Discarded."):
                dlg.destroy()
                self.report(self._guard(restore_baseline, f, sel[0]))
        rb = ttk.Button(dlg.buttons, text="Restore to Sandbox", style="Accent.TButton", command=do_restore)
        rb.pack(side="right")
        if not mine:
            rb.state(["disabled"])
        ttk.Button(dlg.buttons, text="Close", command=dlg.cancel).pack(side="right", padx=(0, S(8)))
        dlg.geometry(f"{S(900)}x{S(620)}")
        dlg.show()

    # ------------------------------------------------------- accounts & admin
    def _login_required(self):
        try:
            return login_required()
        except sqlite3.Error:
            return True

    def update_identity(self):
        role = "Administrator" if is_admin() else "User"
        self.account_mb.configure(text=f"👤  {USER_NAME}" + ("  ·  Admin" if is_admin() else ""))
        self.root.title(f"{APP_NAME}  ·  {USER_NAME}" + ("  (Admin)" if is_admin() else ""))
        m = self.account_menu
        m.delete(0, "end")
        m.add_command(label=f"{USER_NAME}  ·  {role}  ·  {HOST_NAME}", state="disabled")
        m.add_separator()
        lr = self._login_required()
        has_acct = lr and user_exists(USER_NAME)
        m.add_command(label="Change password…", command=self.action_change_password,
                      state="normal" if has_acct else "disabled")
        m.add_command(label="Switch user…", command=self.action_switch_user, state="normal" if lr else "disabled")
        m.add_separator()
        m.add_command(label="⚙  Administration…", command=self.action_admin)

    def ensure_admin(self, parent=None):
        """True if this session has admin rights; otherwise asks for an administrator account."""
        global ADMIN_SESSION
        if is_admin():
            return True
        acct = run_login(self, "Administrator sign-in",
                         "This needs an administrator account. Your own sign-in is not changed.",
                         initial_user="", ok_text="Continue", cancel_text="Cancel", parent=parent)
        if not acct:
            return False
        if not acct["is_admin"]:
            messagebox.showerror(APP_NAME, f"'{acct['username']}' is not an administrator.", parent=parent or self.root)
            return False
        if acct["must_change"] and not run_password_change(self, acct["username"], True, parent=parent):
            return False
        ADMIN_SESSION = acct["username"]
        log_audit("[session]", "Admin Elevated", "", f"via {ADMIN_SESSION}")
        self.update_identity()
        self.log(f"Administrator rights granted for this session (via {ADMIN_SESSION}).", "ok")
        return True

    def action_switch_user(self):
        if sign_in(self, switching=True):
            self.update_identity()
            self.log(f"Signed in as {USER_NAME}.", "ok")
            self.refresh_all()

    def action_change_password(self, parent=None):
        res = run_password_change(self, USER_NAME, forced=False, parent=parent)
        if res:
            self.log(res[1], res[0])

    def action_export(self):
        f = self.selected
        if not f:
            return
        self.log(f"Exporting deliverables for '{f}'…")
        self.root.config(cursor="watch")
        self.root.update_idletasks()
        try:
            results = export_deliverables(f)
        except (OSError, sqlite3.Error) as e:
            results = [("error", f"Export failed: {e}")]
        finally:
            self.root.config(cursor="")
        for lvl, msg in results:
            self.log(msg, lvl)
        if any(lvl == "ok" for lvl, _ in results):
            self.log(f"Saved to {OUTPUT_DIR}", "ok")
        else:
            messagebox.showerror(APP_NAME, "\n".join(m for l, m in results if l == "error") or "Export failed.",
                                 parent=self.root)

    def action_admin(self):
        if not self.ensure_admin():
            return
        S, t = self.S, self.t
        dlg = ThemedDialog(self, "Administration", "Administration",
                           f"Signed in as {USER_NAME}  ·  admin rights via {ADMIN_SESSION}  ·  {HOST_NAME}",
                           width=940, resizable=True)
        nb = ttk.Notebook(dlg.body)
        nb.pack(fill="both", expand=True)

        def page(title):
            f = ttk.Frame(nb, style="Dialog.TFrame", padding=(0, S(14), 0, 0))
            nb.add(f, text=title)
            return f

        def make_tree(parent, cols, height=12):
            wrap = ttk.Frame(parent, style="Dialog.TFrame")
            wrap.pack(fill="both", expand=True)
            tv = ttk.Treeview(wrap, columns=[c[0] for c in cols], show="headings", height=height,
                              selectmode="browse")
            for key, label, w in cols:
                tv.heading(key, text=label, anchor="w")
                tv.column(key, width=S(w), anchor="w", stretch=key == cols[-1][0])
            sb = ttk.Scrollbar(wrap, orient="vertical", command=tv.yview)
            tv.configure(yscrollcommand=sb.set)
            tv.pack(side="left", fill="both", expand=True)
            sb.pack(side="right", fill="y")
            tv.tag_configure("odd", background=t["row_alt"])
            return tv

        def regrab():
            if dlg.winfo_exists():
                dlg.grab_set()
                dlg.focus_set()

        def run(res, reload):
            if not res:
                return
            level, msg = res[0], res[1]
            self.log(msg, level)
            if level == "error":
                messagebox.showerror(APP_NAME, msg, parent=dlg)
            reload()

        def bar(parent):
            b = ttk.Frame(parent, style="Dialog.TFrame")
            b.pack(fill="x", pady=(S(12), 0))
            return b

        # ---- Users
        up = page("  Users  ")
        users = make_tree(up, (("user", "Username", 200), ("role", "Role", 130),
                               ("status", "Status", 230), ("last", "Last sign-in", 200)))

        def load_users():
            users.delete(*users.get_children())
            for i, u in enumerate(list_users()):
                status = "Must change password" if u["must_change"] else "Active"
                if u["username"].lower() == USER_NAME.lower():
                    status += "  ·  you"
                users.insert("", "end", iid=u["username"], tags=("odd",) if i % 2 else (),
                             values=(u["username"], "Administrator" if u["is_admin"] else "User", status,
                                     u["last_login"] or "Never"))

        def selected_user():
            sel = users.selection()
            return sel[0] if sel else None

        def do_add():
            res = run_form(self, "Add user", "Add user",
                           "Give them a temporary password. They will be asked to change it at first sign-in.",
                           [("user", "Username", False, ""), ("pw", "Temporary password", True, ""),
                            ("confirm", "Confirm password", True, "")],
                           lambda v: ("error", "The passwords do not match.") if v["pw"] != v["confirm"]
                           else add_user(v["user"], v["pw"], v["admin"]),
                           "Add user", checks=[("admin", "Administrator")], parent=dlg)
            regrab()
            run(res, load_users)

        def do_reset():
            u = selected_user()
            if not u:
                return
            res = run_form(self, "Reset password", f"Reset password for {u}",
                           "They will be asked to change it at their next sign-in.",
                           [("pw", "Temporary password", True, ""), ("confirm", "Confirm password", True, "")],
                           lambda v: ("error", "The passwords do not match.") if v["pw"] != v["confirm"]
                           else admin_reset_password(u, v["pw"]), "Reset password", parent=dlg)
            regrab()
            run(res, load_users)

        def do_toggle_admin():
            u = selected_user()
            if u:
                run(set_admin(u, users.set(u, "role") != "Administrator"), lambda: (load_users(),
                                                                                   self.update_identity()))

        def do_delete():
            u = selected_user()
            if u and messagebox.askyesno("Delete user", f"Delete user '{u}'?\n\nTheir audit history is kept.",
                                         icon="warning", parent=dlg):
                run(delete_user(u), load_users)

        ub = bar(up)
        ttk.Button(ub, text="＋  Add user…", style="Accent.TButton", command=do_add).pack(side="left")
        ttk.Button(ub, text="Reset password…", command=do_reset).pack(side="left", padx=(S(8), 0))
        ttk.Button(ub, text="Make / remove admin", command=do_toggle_admin).pack(side="left", padx=(S(8), 0))
        ttk.Button(ub, text="Delete user", style="Danger.TButton", command=do_delete).pack(side="right")
        users.bind("<Double-1>", lambda e: do_reset())

        # ---- Check-outs
        cp = page("  Check-outs  ")
        cos = make_tree(cp, (("file", "File", 320), ("user", "Checked out by", 200), ("rev", "Rev", 70),
                             ("folder", "Folder", 240)))

        def load_cos():
            cos.delete(*cos.get_children())
            for i, r in enumerate(active_checkouts()):
                cos.insert("", "end", iid=r["filename"], tags=("odd",) if i % 2 else (),
                           values=(r["filename"], r["locked_by"], r["revision"] or "", r["folder_path"] or ""))

        def do_break():
            sel = cos.selection()
            if not sel:
                return
            f = sel[0]
            reason = self.ask_text("Break Lock", f"Break lock on {f}",
                                   f"Checked out by {cos.set(f, 'user')}. Any changes they have not checked in "
                                   "will not reach the vault. Give a reason for the audit trail.",
                                   required=True, ok_text="Break Lock", ok_style="Danger.TButton", parent=dlg)
            regrab()
            if reason:
                run(self._guard(break_lock, f, reason), lambda: (load_cos(), self.refresh_all()))

        cb = bar(cp)
        ttk.Label(cb, text="Locks left behind by absent users or crashed PCs can be released here.",
                  style="CardMuted.TLabel").pack(side="left")
        ttk.Button(cb, text="🔓  Break lock…", style="Danger.TButton", command=do_break).pack(side="right")

        # ---- Numbering
        npg = page("  Numbering  ")
        auto = tk.BooleanVar(value=auto_number_enabled())

        def toggle_auto():
            run(set_auto_number(auto.get()), lambda: auto.set(auto_number_enabled()))

        ttk.Checkbutton(npg, text="Automatically number new " + " / ".join(a["label"] for a in CAD_APPS.values() if a["models"])
                        + " files saved into Parts / Assemblies folders",
                        variable=auto, command=toggle_auto, style="Dialog.TCheckbutton").pack(anchor="w")
        ttk.Label(npg, text="Parts get NN_0001, NN_0002 …   Assemblies get NN_A001, NN_A002 …   where NN is the "
                            "project number. Numbering runs when the vault is refreshed (F5). Files that already "
                            "start with a number are never renumbered.",
                  style="CardMuted.TLabel", wraplength=S(840), justify="left").pack(anchor="w", pady=(S(2), S(12)))
        projs = make_tree(npg, (("prefix", "No.", 60), ("name", "Project", 260), ("parts", "Next part", 120),
                                ("asms", "Next assembly", 120), ("folder", "Folder", 300)), height=10)

        def load_projects():
            projs.delete(*projs.get_children())
            for i, p in enumerate(list_projects()):
                projs.insert("", "end", tags=("odd",) if i % 2 else (),
                             values=(p["prefix"], p["name"], peek_next_number(p["prefix"], "P") or "full",
                                     peek_next_number(p["prefix"], "A") or "full", p["folder"]))

        nbar = bar(npg)
        ttk.Label(nbar, text=f"Project root: {PROJECT_ROOT_REL}   ·   next project number: "
                             f"{next_project_prefix()}", style="CardMuted.TLabel").pack(side="left")
        ttk.Button(nbar, text="🗂  New project…", style="Accent.TButton",
                   command=lambda: (self.action_new_project(), regrab(), load_projects())).pack(side="right")

        # ---- CAD Apps
        cad_pg = page("  CAD Apps  ")
        ttk.Label(cad_pg, text="Where each CAD app is found and what the vault can do with it automatically. "
                               "Override paths or add apps under \"cad_apps\" in atelier_config.json.",
                  style="CardMuted.TLabel", wraplength=S(840), justify="left").pack(anchor="w", pady=(0, S(10)))
        cad_tree = make_tree(cad_pg, (("app", "App", 100), ("types", "File types", 140),
                                      ("open", "Opens with", 260), ("convert", "Converter", 230),
                                      ("auto", "Automatic exports", 220)), height=6)

        def load_cad():
            cad_tree.delete(*cad_tree.get_children())
            for i, r in enumerate(cad_status()):
                cad_tree.insert("", "end", tags=("odd",) if i % 2 else (),
                                values=(r["label"], r["types"], r["open"], r["convert"], r["auto"]))

        cbar = bar(cad_pg)
        ttk.Label(cbar, text=f"Config file: {os.path.join(VAULT_DIR, 'atelier_config.json')}",
                  style="CardMuted.TLabel").pack(side="left")
        ttk.Button(cbar, text="⟳  Detect again", command=lambda: (clear_tool_cache(), load_cad())).pack(side="right")

        # ---- Security
        sp = page("  Security  ")
        req = tk.BooleanVar(value=self._login_required())

        def toggle_login():
            run(set_require_login(req.get()), lambda: (req.set(self._login_required()), self.update_identity()))

        ttk.Checkbutton(sp, text=f"Require sign-in when {APP_NAME} starts", variable=req,
                        command=toggle_login, style="Dialog.TCheckbutton").pack(anchor="w")
        ttk.Label(sp, text="When this is off, people are identified by their Windows username, and Administration "
                           "and Break Lock ask for an administrator account.",
                  style="CardMuted.TLabel", wraplength=S(820), justify="left").pack(anchor="w", pady=(S(2), S(18)))
        info = ttk.Frame(sp, style="Dialog.TFrame")
        info.pack(fill="x")
        for r, (k, v) in enumerate((("Database", DB_PATH), ("Vault", VAULT_DIR),
                                    ("Passwords", f"Salted PBKDF2-SHA256, minimum {MIN_PASSWORD_LEN} characters. "
                                                  "Old v1 hashes are upgraded at next sign-in."),
                                    ("This PC", HOST_NAME))):
            ttk.Label(info, text=k, style="CardMuted.TLabel").grid(row=r, column=0, sticky="nw", pady=S(3))
            ttk.Label(info, text=v, style="CardValue.TLabel", wraplength=S(700), justify="left").grid(
                row=r, column=1, sticky="w", padx=(S(16), 0), pady=S(3))
        warn = db_location_warning()
        if warn:
            ttk.Label(sp, text="⚠ " + warn, style="CardDanger.TLabel", wraplength=S(820), justify="left").pack(
                anchor="w", pady=(S(14), 0))
        if user_exists(USER_NAME):
            ttk.Button(sp, text="Change my password…",
                       command=lambda: (self.action_change_password(parent=dlg), regrab())).pack(
                anchor="w", pady=(S(18), 0))

        if _secure is not None:
            _secure.admin_section(self, sp, dlg, S, run, regrab)
        # ---- Activity
        ap = page("  Activity  ")
        act = make_tree(ap, (("time", "Time", 140), ("user", "User", 120), ("host", "PC", 120),
                             ("action", "Action", 150), ("file", "File", 220), ("comment", "Comment", 260)),
                        height=14)

        def load_act():
            act.delete(*act.get_children())
            for i, row in enumerate(recent_activity()):
                act.insert("", "end", values=row, tags=("odd",) if i % 2 else ())

        def reload_all():
            try:
                load_users()
                load_cos()
                load_projects()
                load_cad()
                load_act()
            except sqlite3.Error as e:
                self.log(f"Database error: {e}", "error")

        reload_all()
        nb.bind("<<NotebookTabChanged>>", lambda e: reload_all())
        ttk.Button(dlg.buttons, text="Close", command=dlg.cancel).pack(side="right")
        ttk.Button(dlg.buttons, text="⟳  Reload", command=reload_all).pack(side="right", padx=(0, S(8)))
        dlg.geometry(f"{S(980)}x{S(660)}")
        dlg.show()


# =============================================================================
# Entry point
# =============================================================================
_instance_socket = None


def enable_dpi_awareness():
    if sys.platform == "win32":
        try:
            import ctypes
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(1)
            except Exception:
                ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def main():
    global _instance_socket
    try:
        _instance_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        _instance_socket.bind(("127.0.0.1", SINGLE_INSTANCE_PORT))
    except OSError:
        r = tk.Tk()
        r.withdraw()
        messagebox.showerror("Already running", f"{APP_NAME} is already running on this PC.")
        sys.exit(0)

    enable_dpi_awareness()
    root = tk.Tk()
    root.withdraw()
    try:
        init_db()
    except (OSError, sqlite3.Error) as e:
        messagebox.showerror(APP_NAME, f"Could not open the vault database:\n{DB_PATH}\n\n{e}")
        sys.exit(1)
    ctx = BootContext(root)
    style = ttk.Style(root)
    style.theme_use("clam")
    style_ttk(root, style, ctx.t, ctx.S)
    if not sign_in(ctx):
        sys.exit(0)
    root.deiconify()
    AtelierApp(root)
    root.mainloop()


# =============================================================================
# Vault encryption (atelier_secure.py) - optional; the vault works unencrypted without it
# =============================================================================
try:
    import atelier_secure as _secure
    _secure.install(sys.modules[__name__])
except ImportError:
    _secure = None


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        traceback.print_exc()
        input("\n[ERROR] Press Enter to close...")
