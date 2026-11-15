"""
Atelier PDM - SolidCraft companion panel  v0.2
==============================================

v0.10: no flicker on hover - debounced open/close, re-arm after closing, steadier on-top.
v0.9: pop-ups open beside the panel, stay on screen and remember where you put them.
v0.8: finds per-user installs (AppData\\Local\\Programs), remembers the program's location,
      Locate <App>.exe picker; card heading names the right program.
v0.7: PhysCAD support (--app physcad). PhysCAD has no control channel, so the panel follows PhysCAD's window
      title, opens files through PhysCAD itself and asks you to save there. Each CAD app's panel keeps its own
      layout settings.
v0.6: resizable overlay (drag the edges, drag the header to move; remembered per PC), labelled Auto-hide
      switch in the header, version shown in the header, SolidCraft look applied by default.
v0.5: Save to Vault - a design started in SolidCraft (or opened from outside the vault) can be given the next
      part number and saved straight into the vault from the panel, optionally checked in at once.
v0.4: SolidCraft look (colours sampled from SolidCraft, compact type, SolidCraft-blue preview viewport);
      header pin / hide / menu icons drawn by the panel (the v0.3 hide button didn't render on some PCs);
      pin = stay open, unpinned = hide when the mouse leaves, in both Dock and Overlay.
v0.3: hides completely when tucked away - only a 5-pixel edge remains, and the panel slides out when the
      pointer touches it (like an auto-hiding taskbar). The 30-pixel strip is still available in the ☰ menu.
v0.2: auto-hide - the panel tucks away to a slim strip on SolidCraft's right edge and flies out when you
      point at the strip; works with SolidCraft full screen (never resizes a full-screen window).

A slim Atelier PDM panel that sits beside SolidCraft, follows the design you have open and gives you the
everyday vault actions without leaving SolidCraft:

    * check out / save & check in / undo check-out
    * part name and description
    * minor / major revisions
    * export STEP & PDF, history
    * new numbered parts (opened straight into SolidCraft)
    * a vault browser with search, your check-outs and recent files

Requirements
    * atelier_pdm.py v2.5 or later in the same folder. The panel uses its vault engine, sign-in,
      themes and settings, so everything stays in step with the full app.
    * SolidCraft with its control channel on. This is the default
      (Preferences > Assistant > "Let AI agents connect (control channel / MCP)").
      Without it the panel still works, but it can't follow the open design or open files inside
      the running SolidCraft (it falls back to the window title and to launching SolidCraft).

Run
    pythonw atelier_panel.py              docks to SolidCraft whenever it is running
    pythonw atelier_panel.py --launch     also starts SolidCraft, and closes when SolidCraft closes
    pythonw atelier_panel.py --float      ordinary floating window
    pythonw atelier_panel.py --port 7878  use a fixed SolidCraft control port

How it attaches
    SolidCraft has no plug-in API for side panels, so the panel is its own window that tracks
    SolidCraft's main window (Windows): docked beside it (SolidCraft is narrowed to make room and put
    back when the panel closes), laid over its right edge, or floating. It talks to SolidCraft over the
    documented local control channel (127.0.0.1 only, per-launch token) to find the open design, open
    files and save before check-in.
"""

import os
import re
import sys
import json
import time
import stat
import queue
import base64
import shutil
import socket
import sqlite3
import threading
import subprocess

import tkinter as tk
from tkinter import ttk, messagebox

PANEL_VERSION = "0.11"
PANEL_PORT = 64739                       # single-instance guard (the full app uses 64738)

HERE = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

# Everything the panel uses from atelier_pdm.py (checked at start-up so an old copy fails clearly).
REQUIRED = (
    "APP_NAME", "HOST_NAME", "INDEX", "LOCAL_WORKSPACE", "LOCAL_LIB_DIR", "TEMPLATES_DIR", "THEMES",
    "DEFAULT_THEME", "REV_PATTERN", "CAD_MODEL_EXTS", "VAULT_ROOT_LABEL", "INVALID",
    "init_db", "enable_dpi_awareness", "BootContext", "sign_in", "style_ttk", "ThemedDialog", "run_form",
    "load_settings", "save_settings", "file_state", "display_part_name", "parse_pn", "numbering_context",
    "peek_next_number", "check_out", "check_in", "undo_check_out", "up_issue", "calculate_next_rev",
    "export_deliverables", "get_audit", "save_metadata", "create_from_template", "auto_number_enabled",
    "auto_number_candidates", "auto_number_sweep", "cad_app_of", "cad_launcher", "sandbox_path",
    "open_path", "find_preview_stl", "reserve_number", "log_audit", "db", "validate_new_name",
    "is_numbered_name", "clean_part_name", "get_record", "load_mesh", "render_mesh", "render_placeholder", "draw_backdrop",
    "mix", "text_on",
)


def _load_engine():
    try:
        import atelier_pdm as engine
    except Exception as e:                                   # noqa: BLE001 - report anything
        return None, f"Could not load atelier_pdm.py from\n{HERE}\n\n{e}"
    missing = [n for n in REQUIRED if not hasattr(engine, n)]
    if missing:
        return None, ("atelier_pdm.py is too old for the panel (it needs v2.5 or later).\n\nMissing: "
                      + ", ".join(missing))
    return engine, None


A, _ENGINE_ERROR = _load_engine()
if A is not None and getattr(A, "secure_shutil", None) is not None:
    shutil = A.secure_shutil                 # read-only copies from an encrypted vault are decrypted

SC_APP = "solidcraft"
SC_THEME = "SolidCraft"


class AppProfile:
    """What the panel needs to know about one CAD program."""

    def __init__(self, key, label, ext, exe_names, has_control, look, port, settings_prefix):
        self.key, self.label, self.ext = key, label, ext
        self.exe_names, self.has_control = exe_names, has_control
        self.look, self.port, self.settings_prefix = look, port, settings_prefix


APPS = {
    "solidcraft": AppProfile("solidcraft", "SolidCraft", ".sc", ("solidcraft.exe", "solidcraft"), True,
                             "SolidCraft", 64739, ""),
    "physcad": AppProfile("physcad", "PhysCAD", ".physcad", ("physcad.exe", "physcad"), False,
                          "PhysCAD", 64740, "physcad_"),
}
APP = APPS["solidcraft"]                  # chosen in main() from --app

# Sampled from SolidCraft's own window (ribbon, menu bar, icons, active tab, viewport).
SOLIDCRAFT_LOOK = {
    "dark": True, "bg": "#1c1e21", "header_bg": "#25272b", "toolbar_bg": "#1c1e21", "panel_bg": "#25272b",
    "border": "#34373d", "fg": "#d2d4d7", "muted_fg": "#8b8e93", "entry_bg": "#1c1e21", "entry_fg": "#e4e6e9",
    "grid_bg": "#25272b", "row_alt": "#292b30", "grid_fg": "#c8cacd", "heading_bg": "#25272b",
    "heading_fg": "#8b8e93", "btn_bg": "#2e3136", "btn_fg": "#d2d4d7", "accent": "#4aa3d8",
    "sel_bg": "#293c49", "sel_fg": "#eaf5fc", "mesh": "#b7bfca", "success": "#6fbf8e", "warning": "#e0b052",
    "danger": "#e57373", "log_bg": "#1c1e21", "log_fg": "#c3c5c8", "sheet_bg": "#ffffff", "sheet_fg": "#1c1e21",
    "header_fg": "#d2d4d7", "header_muted": "#8b8e93", "header_accent": "#4aa3d8",
}
SOLIDCRAFT_VIEWPORT = {"entry_bg": "#5c70ac", "fg": "#e9edf6", "muted_fg": "#dfe4ee", "mesh": "#b7bfca",
                       "accent": "#a4adb8"}


PHYSCAD_LOOK = dict(SOLIDCRAFT_LOOK)
PHYSCAD_VIEWPORT = {"entry_bg": "#3a3f47", "fg": "#e6e9ee", "muted_fg": "#c9ced6", "mesh": "#c3c9d1",
                    "accent": "#8f98a5"}
VIEWPORTS = {"SolidCraft": SOLIDCRAFT_VIEWPORT, "PhysCAD": PHYSCAD_VIEWPORT}


def _install_look():
    if A is None:
        return
    for name, colours in (("SolidCraft", SOLIDCRAFT_LOOK), ("PhysCAD", PHYSCAD_LOOK)):
        if name not in A.THEMES:
            look = dict(A.THEMES.get(A.DEFAULT_THEME, {}))
            look.update(colours)
            A.THEMES[name] = look


_install_look()


def icon_pin(c, s, col, pinned):
    w = max(1, s / 14)
    if pinned:                                            # upright pin: stays open
        c.create_rectangle(s * .38, s * .18, s * .62, s * .44, outline=col, width=w)
        c.create_line(s * .27, s * .47, s * .73, s * .47, fill=col, width=w * 1.4)
        c.create_line(s * .5, s * .47, s * .5, s * .84, fill=col, width=w)
    else:                                                 # pin on its side: auto-hide
        c.create_rectangle(s * .56, s * .38, s * .82, s * .62, outline=col, width=w)
        c.create_line(s * .53, s * .27, s * .53, s * .73, fill=col, width=w * 1.4)
        c.create_line(s * .53, s * .5, s * .16, s * .5, fill=col, width=w)


def icon_hide(c, s, col):
    w = max(1.2, s / 12)
    for x in (.26, .48):
        c.create_line(s * x, s * .3, s * (x + .18), s * .5, s * x, s * .7, fill=col, width=w,
                      capstyle="round", joinstyle="round")


def icon_menu(c, s, col):
    w = max(1.2, s / 12)
    for y in (.32, .5, .68):
        c.create_line(s * .26, s * y, s * .74, s * y, fill=col, width=w, capstyle="round")


class IconButton(tk.Canvas if hasattr(tk, "Canvas") else object):
    """Small flat icon button drawn on a canvas (no dependence on symbol fonts), with a tooltip."""

    def __init__(self, master, draw, command, tip, size):
        super().__init__(master, width=size, height=size, highlightthickness=0, bd=0, cursor="hand2")
        self.draw, self.command, self.tip, self.size = draw, command, tip, size
        self.bg = self.fg = self.hover_bg = self.active_fg = "#000000"
        self.hover = False
        self.active = False
        self._tipwin = None
        self._tipjob = None
        self.bind("<Enter>", self._enter)
        self.bind("<Leave>", self._leave)
        self.bind("<Button-1>", self._click)

    def set_colors(self, bg, fg, hover_bg, active_fg):
        self.bg, self.fg, self.hover_bg, self.active_fg = bg, fg, hover_bg, active_fg
        self.redraw()

    def redraw(self):
        self.delete("all")
        self.configure(bg=self.hover_bg if self.hover else self.bg)
        self.draw(self, self.size, self.active_fg if (self.active or self.hover) else self.fg)

    def _enter(self, _e):
        self.hover = True
        self.redraw()
        self._tipjob = self.after(450, self._show_tip)

    def _leave(self, _e):
        self.hover = False
        self.redraw()
        self._hide_tip()

    def _click(self, _e):
        self._hide_tip()
        self.command()

    def _show_tip(self):
        text = self.tip() if callable(self.tip) else self.tip
        if not text or not self.winfo_viewable():
            return
        self._tipwin = tw = tk.Toplevel(self)
        tw.overrideredirect(True)
        try:
            tw.attributes("-topmost", True)
        except tk.TclError:
            pass
        tk.Label(tw, text=text, bg=self.bg, fg="#e4e6e9", font=("Segoe UI", 8),
                 padx=7, pady=3, highlightthickness=1, highlightbackground=self.hover_bg).pack()
        tw.update_idletasks()
        x = self.winfo_rootx() + self.size - tw.winfo_reqwidth()
        y = self.winfo_rooty() + self.size + 4
        tw.geometry(f"+{max(0, x)}+{max(0, y)}")

    def _hide_tip(self):
        if self._tipjob:
            try:
                self.after_cancel(self._tipjob)
            except tk.TclError:
                pass
            self._tipjob = None
        if self._tipwin is not None:
            try:
                self._tipwin.destroy()
            except tk.TclError:
                pass
            self._tipwin = None
SC_EXE_NAMES = ("solidcraft.exe", "solidcraft")


# =============================================================================
# SolidCraft control channel
# =============================================================================
class LinkError(Exception):
    """The connection to SolidCraft failed or was lost."""


class CommandError(LinkError):
    """SolidCraft is connected but refused or failed a command."""


def _dig(obj, key):
    """Finds key at the top level of a reply, or one level down in a document-like section."""
    if not isinstance(obj, dict):
        return None
    if key in obj:
        return obj[key]
    for section in ("document", "design", "doc", "file", "info"):
        sub = obj.get(section)
        if isinstance(sub, dict) and key in sub:
            return sub[key]
    return None


def summarise_document(res):
    """{'name', 'path', 'dirty'} from a document.inspect reply."""
    if not isinstance(res, dict):
        return None
    path, name, dirty = _dig(res, "path"), _dig(res, "name"), _dig(res, "dirty")
    path = path if isinstance(path, str) and path.strip() else None
    name = name if isinstance(name, str) and name.strip() else None
    if path and not name:
        name = os.path.splitext(os.path.basename(path))[0]
    return {"path": path, "name": name, "dirty": bool(dirty)}


class SolidCraftLink:
    """Client for SolidCraft's local JSON-lines control channel (see SolidCraft docs: Control protocol)."""

    OPEN_CANDIDATES = ("file.open", "doc.open", "document.open", "design.open")
    SAVE_CANDIDATES = ("file.save", "doc.save", "document.save", "design.save")

    def __init__(self, port=None, control_dir=None):
        self.fixed_port = port
        self._dir = control_dir
        self.sock = None
        self.port = None
        self.buf = b""
        self.next_id = 0
        self.commands = None
        self.last_error = ""
        self.lock = threading.RLock()

    # -- discovery ---------------------------------------------------------------
    def control_dir(self):
        if self._dir:
            return self._dir
        env = os.environ.get("SOLIDCRAFT_CONTROL_DIR")
        if env:
            return env
        home = os.path.expanduser("~")
        if sys.platform == "win32":
            base = os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
            return os.path.join(base, "SolidCraft", "control")
        if sys.platform == "darwin":
            return os.path.join(home, "Library", "Application Support", "SolidCraft", "control")
        base = os.environ.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share")
        return os.path.join(base, "solidcraft", "control")

    def _read(self, name):
        try:
            with open(os.path.join(self.control_dir(), name), encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            return ""

    def candidate_ports(self):
        ports = []

        def add(p):
            try:
                p = int(str(p).strip())
            except (TypeError, ValueError):
                return
            if 0 < p < 65536 and p not in ports:
                ports.append(p)

        add(self.fixed_port)
        add(os.environ.get("SOLIDCRAFT_CONTROL_PORT"))
        add(self._read("current.port"))
        try:
            for n in sorted(os.listdir(self.control_dir())):
                if n.endswith(".token"):
                    add(n[:-6])
        except OSError:
            pass
        return ports

    def token_for(self, port):
        return os.environ.get("SOLIDCRAFT_CONTROL_TOKEN") or self._read(f"{port}.token")

    # -- connection --------------------------------------------------------------
    @property
    def connected(self):
        return self.sock is not None

    def close(self):
        with self.lock:
            if self.sock is not None:
                try:
                    self.sock.close()
                except OSError:
                    pass
            self.sock, self.port, self.buf = None, None, b""

    def connect(self):
        with self.lock:
            if self.sock is not None:
                return True
            problems = []
            for port in self.candidate_ports():
                token = self.token_for(port)
                if not token:
                    continue
                try:
                    s = socket.create_connection(("127.0.0.1", port), timeout=1.5)
                except OSError:
                    problems.append(f"port {port} not answering")
                    continue
                self.sock, self.port, self.buf = s, port, b""
                try:
                    self._request("ui.inspect", {}, timeout=5, token=token)   # authenticates the connection
                except CommandError:
                    pass            # token accepted (a bad token closes the connection), probe command refused
                except LinkError as e:
                    self.close()
                    problems.append(f"port {port}: {e}")
                    continue
                self.commands = None
                self.last_error = ""
                return True
            self.last_error = ("; ".join(problems) if problems else
                               "SolidCraft isn't running, or its control channel is turned off")
            return False

    def _request(self, method, params, timeout, token=None):
        self.next_id += 1
        rid = self.next_id
        msg = {"id": rid, "method": method, "params": params or {}}
        if token:
            msg["token"] = token
        try:
            self.sock.settimeout(timeout)
            self.sock.sendall((json.dumps(msg) + "\n").encode("utf-8"))
            deadline = time.monotonic() + timeout
            while True:
                while b"\n" not in self.buf:
                    if time.monotonic() > deadline:
                        raise TimeoutError("SolidCraft did not answer in time")
                    chunk = self.sock.recv(1 << 16)
                    if not chunk:
                        raise ConnectionError("SolidCraft closed the connection")
                    self.buf += chunk
                line, self.buf = self.buf.split(b"\n", 1)
                if not line.strip():
                    continue
                try:
                    reply = json.loads(line)
                except ValueError:
                    continue
                if reply.get("id") != rid:
                    continue
                if reply.get("ok"):
                    return reply.get("result")
                err = str(reply.get("error") or "unknown error")
                if err.startswith("unauthorized") or "too many connections" in err:
                    self.close()
                    raise LinkError(err)
                raise CommandError(err)
        except OSError as e:
            self.close()
            raise LinkError(str(e) or e.__class__.__name__) from None

    def call(self, method, params=None, timeout=10):
        with self.lock:
            if self.sock is None and not self.connect():
                raise LinkError(self.last_error)
            return self._request(method, params, timeout)

    def execute(self, command, params=None, timeout=60):
        return self.call("engine.execute", {"command": command, "params": params or {}}, timeout)

    # -- higher level --------------------------------------------------------------
    def command_ids(self):
        if self.commands is None:
            res = self.call("engine.commands", {}, timeout=20)
            items = res.get("commands", []) if isinstance(res, dict) else (res or [])
            ids = set()
            for it in items:
                if isinstance(it, dict) and it.get("id"):
                    ids.add(str(it["id"]))
                elif isinstance(it, str):
                    ids.add(it)
            self.commands = ids
        return self.commands

    def _pick(self, preferred, suffix):
        try:
            ids = self.command_ids()
        except CommandError:
            ids = set()
        low = {i.lower(): i for i in ids}
        for p in preferred:
            if p in low:
                return low[p]
        for i in sorted(ids):
            parts = i.lower().split(".")
            if i.lower().endswith(suffix) and parts[0] in ("file", "doc", "document", "design"):
                return i
        return None

    def active_document(self):
        try:
            res = self.call("document.inspect", {"measure": False}, timeout=8)
        except CommandError:
            res = self.execute("document.inspect", {}, timeout=8)
        return summarise_document(res)

    def open_file(self, path):
        cmd = self._pick(self.OPEN_CANDIDATES, ".open")
        if not cmd:
            raise CommandError("this SolidCraft has no open command on its control channel")
        return self.execute(cmd, {"path": os.path.abspath(path)}, timeout=180)

    def save(self):
        cmd = self._pick(self.SAVE_CANDIDATES, ".save") or "file.save"
        return self.execute(cmd, {}, timeout=180)


# =============================================================================
# Matching SolidCraft's open design to a vault file (pure helpers, easy to test)
# =============================================================================
def covers(rect, area, slack=2):
    """True if rect covers area (a full-screen or maximised-without-taskbar window)."""
    return bool(rect and area) and rect[0] <= area[0] + slack and rect[1] <= area[1] + slack and \
        rect[2] >= area[2] - slack and rect[3] >= area[3] - slack


def flyout_geometry(anchor, rail_w, panel_w, rail_shown, expanded):
    """Window geometry string. The rail stays put at the anchor's right edge; the panel opens leftwards."""
    right, y, hgt = anchor
    w = (rail_w if rail_shown else 0) + (panel_w if (expanded or not rail_shown) else 0)
    return f"{w}x{hgt}+{right - w}+{y}"


OVERLAY_DEFAULT = {"right": 6, "top": 90, "bottom": 28, "width": 360}
OVERLAY_MIN_W, OVERLAY_MIN_H = 260, 360


def overlay_rect(ext, ov, scale):
    """(right, top, height, width) of the overlay inside SolidCraft's window rect ext, from unscaled offsets."""
    k = lambda v: int(round(v * scale))
    right = ext[2] - k(ov["right"])
    top = ext[1] + k(ov["top"])
    hgt = (ext[3] - k(ov["bottom"])) - top
    return right, top, hgt, k(ov["width"])


def drag_overlay(ov, edge, dx, dy, ext, scale):
    """New offsets after dragging `edge` ('left', 'top', 'bottom' or 'move') by dx, dy screen pixels."""
    k = 1.0 / scale
    o = dict(ov)
    win_w, win_h = (ext[2] - ext[0]) * k, (ext[3] - ext[1]) * k
    if edge == "left":
        o["width"] = ov["width"] - dx * k
    elif edge == "top":
        o["top"] = ov["top"] + dy * k
    elif edge == "bottom":
        o["bottom"] = ov["bottom"] - dy * k
    elif edge == "move":
        o["right"] = ov["right"] - dx * k
        o["top"] = ov["top"] + dy * k
        o["bottom"] = ov["bottom"] - dy * k
    o["width"] = max(OVERLAY_MIN_W, min(o["width"], win_w - 20))
    o["right"] = max(0, min(o["right"], win_w - o["width"]))
    o["top"] = max(0, o["top"])
    o["bottom"] = max(0, o["bottom"])
    if win_h - o["top"] - o["bottom"] < OVERLAY_MIN_H:          # keep a usable height
        if edge == "bottom":
            o["bottom"] = max(0, win_h - o["top"] - OVERLAY_MIN_H)
        else:
            o["top"] = max(0, win_h - o["bottom"] - OVERLAY_MIN_H)
    return {key: round(v, 1) for key, v in o.items()}


class AppSettings(dict):
    """Shared settings file, but each CAD app's panel keeps its own 'panel_*' preferences."""
    prefix = ""

    def _k(self, k):
        return self.prefix + k if (self.prefix and isinstance(k, str) and k.startswith("panel_")) else k

    def __getitem__(self, k):
        return super().__getitem__(self._k(k))

    def __setitem__(self, k, v):
        super().__setitem__(self._k(k), v)

    def __contains__(self, k):
        return super().__contains__(self._k(k))

    def get(self, k, default=None):
        return super().get(self._k(k), default)

    def pop(self, k, *default):
        return super().pop(self._k(k), *default)

    def own_items(self):
        own = (self.prefix + "panel_") if self.prefix else "panel_"
        return {k: v for k, v in dict.items(self) if k.startswith(own)}


# =============================================================================
# Following a CAD program that has no control channel (PhysCAD)
# =============================================================================
def parse_title(title, label, ext):
    """(design name or None, unsaved?) from a window title such as 'Bracket.physcad* - PhysCAD 0.9.7'."""
    if not title or not title.strip():
        return None, False
    t = title.strip()
    dirty = "*" in t or "●" in t or t.startswith("•")
    parts = [x.strip(" *●•\t") for x in re.split(r"\s+[-–—|]\s+", t)]
    low = label.lower()
    parts = [x for x in parts if x and x.lower() != low and not x.lower().startswith(low + " ")]
    if not parts:
        return None, dirty
    name = parts[0]
    if name.lower().endswith(ext.lower()):
        name = name[:-len(ext)].strip()
    return (name or None), dirty


class TitleLink:
    """Same interface as SolidCraftLink, but read-only: follows the program's window title."""

    def __init__(self, app, exe_names):
        self.app, self.exe_names = app, set(exe_names)
        self.hwnd = None
        self.last_error = ""

    @property
    def connected(self):
        return bool(self.hwnd) and is_window(self.hwnd)

    def connect(self):
        self.hwnd = find_window(self.exe_names)
        if not self.hwnd:
            self.last_error = f"{self.app.label} isn't running"
        return bool(self.hwnd)

    def close(self):
        self.hwnd = None

    def active_document(self):
        if not self.connected and not self.connect():
            raise LinkError(self.last_error)
        title = window_title(self.hwnd)
        name, dirty = parse_title(title, self.app.label, self.app.ext)
        return {"name": name, "path": None, "dirty": dirty, "title": title}

    def _cannot(self, what):
        raise CommandError(f"{self.app.label} can't be told to {what} by other programs")

    def open_file(self, path):
        self._cannot("open files")

    def save(self):
        self._cannot("save")

    def execute(self, command, params=None, timeout=60):
        self._cannot(command)


def suggest_part_name(doc_name, clean):
    """Part name to pre-fill from SolidCraft's design name ('' for Untitled)."""
    name = (doc_name or "").strip()
    if not name or name.lower().startswith("untitled"):
        return ""
    return clean(name)


def match_by_name(candidates, name, prefer=None):
    """candidates: file names. Returns the file whose stem equals name (case-insensitive)."""
    if not name:
        return None
    low = name.strip().lower()
    hits = [n for n in candidates if os.path.splitext(n)[0].lower() == low]
    if not hits:
        return None
    if prefer:
        hits.sort(key=lambda n: (0 if prefer(n) else 1, n.lower()))
    return hits[0]


def match_in_title(candidates, title):
    """The longest file stem that appears in a window title."""
    if not title:
        return None
    low = title.lower()
    best = None
    for n in candidates:
        stem = os.path.splitext(n)[0].lower()
        if len(stem) >= 3 and stem in low and (best is None or len(stem) > len(os.path.splitext(best)[0])):
            best = n
    return best


# =============================================================================
# Window tracking (Windows)
# =============================================================================
WIN = sys.platform == "win32"

if WIN:
    import ctypes
    from ctypes import wintypes

    _u32 = ctypes.WinDLL("user32", use_last_error=True)
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    try:
        _dwm = ctypes.WinDLL("dwmapi")
    except OSError:
        _dwm = None

    _WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    class _MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

    _u32.EnumWindows.argtypes = [_WNDENUMPROC, wintypes.LPARAM]
    _u32.EnumWindows.restype = wintypes.BOOL
    for _fn in ("IsWindowVisible", "IsWindow", "IsIconic", "IsZoomed"):
        getattr(_u32, _fn).argtypes = [wintypes.HWND]
        getattr(_u32, _fn).restype = wintypes.BOOL
    _u32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    _u32.GetWindow.restype = wintypes.HWND
    _u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    _u32.GetWindowThreadProcessId.restype = wintypes.DWORD
    _u32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    _u32.GetWindowRect.restype = wintypes.BOOL
    _u32.GetForegroundWindow.argtypes = []
    _u32.GetForegroundWindow.restype = wintypes.HWND
    _u32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    _u32.GetWindowTextLengthW.restype = ctypes.c_int
    _u32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _u32.GetWindowTextW.restype = ctypes.c_int
    _u32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
    _u32.MonitorFromWindow.restype = wintypes.HMONITOR
    _u32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(_MONITORINFO)]
    _u32.GetMonitorInfoW.restype = wintypes.BOOL
    _u32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                  ctypes.c_int, wintypes.UINT]
    _u32.SetWindowPos.restype = wintypes.BOOL
    _u32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    _u32.ShowWindow.restype = wintypes.BOOL
    _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                ctypes.POINTER(wintypes.DWORD)]
    _k32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.CloseHandle.restype = wintypes.BOOL
    if _dwm is not None:
        _dwm.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
        _dwm.DwmGetWindowAttribute.restype = ctypes.c_long

    _EXE_CACHE = {}
    SW_MAXIMIZE, SW_RESTORE = 3, 9
    _SWP_FLAGS = 0x0004 | 0x0010 | 0x0200          # NOZORDER | NOACTIVATE | NOOWNERZORDER

    def pid_of(hwnd):
        pid = wintypes.DWORD()
        _u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return pid.value

    def _exe_of_pid(pid):
        if pid in _EXE_CACHE:
            return _EXE_CACHE[pid]
        name = ""
        h = _k32.OpenProcess(0x1000, False, pid)            # PROCESS_QUERY_LIMITED_INFORMATION
        if h:
            try:
                size = wintypes.DWORD(1024)
                buf = ctypes.create_unicode_buffer(size.value)
                if _k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                    name = os.path.basename(buf.value).lower()
            finally:
                _k32.CloseHandle(h)
        _EXE_CACHE[pid] = name
        return name

    def _win_rect(hwnd):
        r = wintypes.RECT()
        if _u32.GetWindowRect(hwnd, ctypes.byref(r)):
            return r.left, r.top, r.right, r.bottom
        return None

    def window_rect(hwnd):
        """Visible bounds (without the invisible resize border Windows 10/11 adds)."""
        if _dwm is not None:
            r = wintypes.RECT()
            if _dwm.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(r), ctypes.sizeof(r)) == 0:
                return r.left, r.top, r.right, r.bottom
        return _win_rect(hwnd)

    def set_visible_rect(hwnd, left, top, right, bottom):
        win, vis = _win_rect(hwnd), window_rect(hwnd)
        if not win or not vis:
            return False
        dl, dt = vis[0] - win[0], vis[1] - win[1]
        dr, db = win[2] - vis[2], win[3] - vis[3]
        x, y = left - dl, top - dt
        w, h = (right + dr) - x, (bottom + db) - y
        return bool(_u32.SetWindowPos(hwnd, None, x, y, w, h, _SWP_FLAGS))

    def _monitor_info(hwnd):
        mon = _u32.MonitorFromWindow(hwnd, 2)               # MONITOR_DEFAULTTONEAREST
        mi = _MONITORINFO()
        mi.cbSize = ctypes.sizeof(_MONITORINFO)
        if mon and _u32.GetMonitorInfoW(mon, ctypes.byref(mi)):
            return mi
        return None

    def work_area(hwnd):
        mi = _monitor_info(hwnd)
        if mi:
            r = mi.rcWork
            return r.left, r.top, r.right, r.bottom
        return None

    def monitor_rect(hwnd):
        mi = _monitor_info(hwnd)
        if mi:
            r = mi.rcMonitor
            return r.left, r.top, r.right, r.bottom
        return None

    def window_title(hwnd):
        n = _u32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        _u32.GetWindowTextW(hwnd, buf, n + 1)
        return buf.value

    def is_window(hwnd):
        return bool(hwnd) and bool(_u32.IsWindow(hwnd)) and bool(_u32.IsWindowVisible(hwnd))

    def is_iconic(hwnd):
        return bool(_u32.IsIconic(hwnd))

    def is_zoomed(hwnd):
        return bool(_u32.IsZoomed(hwnd))

    def show_window(hwnd, cmd):
        _u32.ShowWindow(hwnd, cmd)

    def foreground_pid():
        h = _u32.GetForegroundWindow()
        return pid_of(h) if h else 0

    def find_window(exe_names, prefer=None):
        found = []
        me = os.getpid()

        def cb(h, _lparam):
            try:
                if _u32.IsWindowVisible(h) and not _u32.GetWindow(h, 4):        # GW_OWNER: top-level only
                    pid = pid_of(h)
                    if pid != me and _exe_of_pid(pid) in exe_names:
                        r = window_rect(h)
                        if r and (r[2] - r[0]) * (r[3] - r[1]) > 200 * 200:
                            found.append(int(h))
            except OSError:
                pass
            return True

        _u32.EnumWindows(_WNDENUMPROC(cb), 0)
        if not found:
            return None
        if prefer and int(prefer) in found:
            return int(prefer)
        fg = _u32.GetForegroundWindow()
        if fg and int(fg) in found:
            return int(fg)

        def area(h):
            r = window_rect(h)
            return (r[2] - r[0]) * (r[3] - r[1]) if r else 0
        return max(found, key=area)
else:                                                       # docking needs Windows; the panel floats elsewhere
    SW_MAXIMIZE, SW_RESTORE = 3, 9

    def find_window(exe_names, prefer=None):
        return None

    def window_rect(hwnd):
        return None

    def set_visible_rect(hwnd, left, top, right, bottom):
        return False

    def work_area(hwnd):
        return None

    def monitor_rect(hwnd):
        return None

    def window_title(hwnd):
        return ""

    def is_window(hwnd):
        return False

    def is_iconic(hwnd):
        return False

    def is_zoomed(hwnd):
        return False

    def show_window(hwnd, cmd):
        pass

    def pid_of(hwnd):
        return 0

    def foreground_pid():
        return 0


# =============================================================================
# The panel
# =============================================================================
def candidate_exe_paths(app_key, label):
    """Usual install locations for a CAD program on Windows (per-user first, then Program Files)."""
    roots = []
    la = os.environ.get("LOCALAPPDATA")
    if la:
        roots.append(os.path.join(la, "Programs", label))
    for var, default in (("ProgramFiles", r"C:\Program Files"), ("ProgramFiles(x86)", "")):
        base = os.environ.get(var, default)
        if base:
            roots.append(os.path.join(base, label))
    out = []
    for r in roots:
        for n in (f"{label}.exe", f"{app_key}.exe"):
            p = os.path.join(r, n)
            if p.lower() not in (x.lower() for x in out):
                out.append(p)
    return out


def exe_path_of_window(hwnd):
    """Full path of the program that owns a window ('' if unknown)."""
    if sys.platform != "win32" or not hwnd:
        return ""
    try:
        import ctypes
        from ctypes import wintypes
        u32 = ctypes.WinDLL("user32")
        k32 = ctypes.WinDLL("kernel32")
        u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                   ctypes.POINTER(wintypes.DWORD)]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        pid = wintypes.DWORD()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        h = k32.OpenProcess(0x1000, False, pid.value)          # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ""
        try:
            size = wintypes.DWORD(1024)
            buf = ctypes.create_unicode_buffer(size.value)
            if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return buf.value
        finally:
            k32.CloseHandle(h)
    except (OSError, AttributeError, ValueError):
        pass
    return ""


def place_dialog(size, owner, pointer, work, saved=None, gap=12):
    """Top-left (x, y) for a pop-up of `size`, kept fully inside `work` (left, top, right, bottom).
    saved: where the user last put this pop-up. owner: (x, y, w, h) of the panel, or None to centre."""
    w, h = size
    left, top, right, bottom = work
    if saved:
        x, y = saved
    elif owner:
        ox, oy, ow, oh = owner
        x = ox - w - gap                                   # beside the panel, on the CAD side
        if x < left:
            x = ox + ow + gap                              # no room there: the other side
        y = pointer[1] - h // 3                            # level with the mouse
    else:
        x, y = left + (right - left - w) // 2, top + (bottom - top - h) // 3
    x = max(left, min(x, right - w))
    y = max(top, min(y, bottom - h))
    return int(x), int(y)


def work_area_at(x, y):
    """Work area (excluding the taskbar) of the monitor nearest a screen point, or None."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class _PT(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

        class _MI(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                        ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]
        u = ctypes.WinDLL("user32")
        u.MonitorFromPoint.argtypes = [_PT, wintypes.DWORD]
        u.MonitorFromPoint.restype = wintypes.HMONITOR
        u.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(_MI)]
        u.GetMonitorInfoW.restype = wintypes.BOOL
        mon = u.MonitorFromPoint(_PT(int(x), int(y)), 2)    # MONITOR_DEFAULTTONEAREST
        mi = _MI()
        mi.cbSize = ctypes.sizeof(_MI)
        if mon and u.GetMonitorInfoW(mon, ctypes.byref(mi)):
            r = mi.rcWork
            return r.left, r.top, r.right, r.bottom
    except (OSError, AttributeError, ValueError):
        pass
    return None


def _panel_dialog_show(self):
    """Replacement for ThemedDialog.show inside the panel (see place_dialog)."""
    import re as _re
    self.update_idletasks()
    w, h = self.winfo_reqwidth(), self.winfo_reqheight()
    m = _re.match(r"^(\d+)x(\d+)", self.wm_geometry())
    if m and int(m.group(1)) > 1 and int(m.group(2)) > 1:          # size set by the caller (History)
        w, h = int(m.group(1)), int(m.group(2))
    app = getattr(self, "app", None)
    settings = getattr(app, "settings", None)
    key = self.title()
    store = settings.get("panel_dialog_pos") if settings is not None else None
    store = dict(store) if isinstance(store, dict) else {}
    saved = store.get(key)
    saved = (int(saved[0]), int(saved[1])) if isinstance(saved, (list, tuple)) and len(saved) == 2 else None
    o = self.owner
    owner = None
    try:
        if o.winfo_viewable():
            owner = (o.winfo_rootx(), o.winfo_rooty(), o.winfo_width(), o.winfo_height())
    except Exception:                                                # noqa: BLE001
        owner = None
    pointer = self.winfo_pointerxy()
    ref = saved if saved else ((owner[0] + owner[2] // 2, pointer[1]) if owner else pointer)
    work = work_area_at(*ref) or (0, 0, self.winfo_screenwidth(), self.winfo_screenheight())
    x, y = place_dialog((w, h), owner, pointer, work, saved)
    self.geometry(f"+{x}+{y}")
    pos = {}

    def track(e):
        if e.widget is self:
            g = _re.match(r"^\d+x\d+\+(-?\d+)\+(-?\d+)$", self.wm_geometry())
            if g:
                pos["xy"] = [int(g.group(1)), int(g.group(2))]
    self.bind("<Configure>", track, add="+")
    self.deiconify()
    self.lift()
    self.focus_force()
    self.grab_set()
    (self.focus_widget or self).focus_set()
    self.wait_window()
    xy = pos.get("xy")
    if xy and settings is not None and hasattr(app, "save_settings") and store.get(key) != xy:
        store[key] = xy
        try:
            settings["panel_dialog_pos"] = store
            app.save_settings()
        except Exception:                                            # noqa: BLE001
            pass
    return self.result


class Panel:
    WIDTHS = {"Narrow": 320, "Normal": 360, "Wide": 420}
    RAIL = 30
    EDGE = 5                                 # width left showing when hidden completely
    HOVER_OPEN = 0.30                        # seconds pointing at the strip before it opens
    HOVER_OPEN_EDGE = 0.12                   # seconds touching the hidden edge before it opens
    LEAVE_CLOSE = 0.70                       # seconds away from the panel before it tucks away
    STATE_ICON = {"mine": "✎", "new": "✚", "locked": "🔒", "lib": "▣", "dup": "⚠", "available": ""}

    def __init__(self, root, args):
        self.root = root
        self.args = args
        self.settings = AppSettings(A.load_settings())
        self.settings.prefix = APP.settings_prefix
        name = self.settings.get("panel_theme", APP.look)
        self.theme_name = name if name in A.THEMES else APP.look
        self.look_var = tk.StringVar(value=self.theme_name)
        self.scale = max(1.0, root.winfo_fpixels("1i") / 96.0)
        self.style = ttk.Style(root)
        self.style.theme_use("clam")

        cfg = getattr(A, "CONFIG", {}) or {}
        self.exe_names = {n.lower() for n in (cfg.get(f"{APP.key}_exe_names") or APP.exe_names)}
        self.link = SolidCraftLink(port=args.port) if APP.has_control else TitleLink(APP, self.exe_names)
        self.mode_var = tk.StringVar(value="float" if args.float else self.settings.get("panel_mode", "dock"))
        wn = self.settings.get("panel_width", "Normal")
        self.width_var = tk.StringVar(value=wn if wn in self.WIDTHS else "Normal")
        self.poll_seconds = float(self.settings.get("panel_poll_seconds", 15 if WIN else 4))

        self.sc_hwnd = None
        self.sc_seen = False
        self.shrunk = {}                     # hwnd -> (was_maximised, original visible rect)
        self._gone_ticks = 0
        self._tick_n = 0
        self._last_title = None
        self._last_front = False
        self._topmost = None
        self._decorated = None
        self._last_geom = None

        # auto-hide rail
        self.autohide_var = tk.BooleanVar(value=bool(self.settings.get("panel_autohide", True)))
        hs = self.settings.get("panel_hide_style", "edge")
        self.hide_style_var = tk.StringVar(value=hs if hs in ("edge", "strip") else "edge")
        self.rail_shown = False
        self.expanded = True
        self.hold_open = False
        self._fullscreen = False
        self._anchor = None
        self._sc_pid = 0
        self._in_since = None
        self._out_since = None
        self._tip = None
        self._tip_key = None
        self._tip_since = None
        self.reserved = {}                   # hwnd -> width kept free beside SolidCraft
        ov = self.settings.get("panel_overlay") or {}
        self.overlay = {k: float(ov.get(k, v)) for k, v in OVERLAY_DEFAULT.items()}
        self._drag = None
        self._ext = None

        self.doc = None
        self.doc_error = ""
        self.active_file = None
        self.picked = None
        self.opened = {}                     # vault file -> path last opened in SolidCraft
        self.recent = [r for r in self.settings.get("panel_recent", []) if isinstance(r, str)][:15]
        self.open_dirs = set(self.settings.get("panel_open_dirs", []))
        self.cam = {"rx": 25.0, "ry": 45.0, "zoom": 1.0}
        self._drag_xy = (0, 0)
        self._redraw_pending = False
        self._search_job = None
        self._thumb_img = None
        self.last_full_refresh = 0.0

        self.q = queue.Queue()
        self.poke = threading.Event()
        self.running = True

        root.title(f"{A.APP_NAME} · {APP.label}")
        root.protocol("WM_DELETE_WINDOW", self.quit)
        root.minsize(self.S(280), self.S(420))
        self.build_ui()
        self._build_grips()
        self.apply_theme()
        root.bind("<F5>", lambda e: self.refresh_vault())
        root.bind_all("<ButtonPress-1>", self._ensure_focus, add="+")
        root.bind("<FocusIn>", self._on_focus_in, add="+")

        self.refresh_vault()
        self._place_initial()
        threading.Thread(target=self._worker, name=f"{APP.key}-link", daemon=True).start()
        self.poke.set()
        if args.launch:
            self.launch_solidcraft()
        self.root.after(300, self._tick)
        if WIN:
            self.root.after(500, self._hover_tick)
            if not self.settings.get("panel_hint_v04"):
                self.settings["panel_hint_v04"] = True
                self.root.after(2500, lambda: self.say(
                    "Tip: click the pin to choose. Unpinned, the panel hides when the mouse leaves; touch "
                    f"{APP.label}'s right edge to bring it back. » hides it straight away."))

    # ------------------------------------------------------------------ helpers
    def S(self, n):
        return int(round(n * self.scale))

    @property
    def t(self):
        return THEMES_OR_DEFAULT(self.theme_name)

    def current(self):
        return self.picked if self.picked else self.active_file

    def is_active(self, f):
        return bool(f) and f == self.active_file

    def ref_dir(self):
        return os.path.join(A.LOCAL_WORKSPACE, "_Reference")

    def _guard(self, fn, *args):
        try:
            return fn(*args)
        except sqlite3.OperationalError as e:
            return "error", f"Database busy or unavailable ({e}). Try again in a moment."
        except OSError as e:
            return "error", f"File operation failed: {e}"
        except ValueError as e:
            return "error", str(e)

    def say(self, msg, level="info"):
        if not msg:
            return
        t = self.t
        color = {"ok": t["success"], "warn": t["warning"], "error": t["danger"]}.get(level, t["accent"])
        self.status_dot.configure(fg=color)
        self.status_label.configure(text=msg if len(msg) < 220 else msg[:217] + "…")

    def report(self, result, refresh=True):
        level, msg = result[0], result[1]
        self.say(msg, level)
        if level == "error":
            messagebox.showerror(A.APP_NAME, msg, parent=self.root)
        if refresh:
            self.refresh_vault()

    def save_settings(self):
        """Merges this panel's own preferences into the shared file (so other panels' changes aren't lost)."""
        fresh = A.load_settings()
        fresh.update(self.settings.own_items())
        A.save_settings(fresh)

    def _sc_names(self):
        """SolidCraft files the panel knows about: in the vault, or new ones checked out to you."""
        names = {n for n in A.INDEX.files if A.cad_app_of(n) == APP.key}
        for n, r in A.INDEX.records.items():
            if r.get("locked_by") == A.USER_NAME and A.cad_app_of(n) == APP.key:
                names.add(n)
        return names

    # ------------------------------------------------------------------ UI
    def build_ui(self):
        S, root = self.S, self.root
        self._build_rail()
        self.content = ttk.Frame(root, style="TFrame")
        self.content.pack(side="left", fill="both", expand=True)
        root = self.content

        # header
        hdr = ttk.Frame(root, style="Header.TFrame", padding=(S(10), S(8)))
        hdr.pack(side="top", fill="x")
        self.logo = tk.Canvas(hdr, width=S(22), height=S(22), highlightthickness=0, bd=0)
        self.logo.pack(side="left")
        tt = ttk.Frame(hdr, style="Header.TFrame")
        tt.pack(side="left", padx=(S(8), 0))
        self.title_label = ttk.Label(tt, text=f"{A.APP_NAME}", style="HeaderTitle.TLabel")
        self.title_label.pack(anchor="w")
        self._hdr_widgets = (hdr, tt, self.title_label)
        lrow = ttk.Frame(tt, style="Header.TFrame")
        lrow.pack(anchor="w")
        self.link_dot = tk.Label(lrow, text="●", font=("Segoe UI", 8), bd=0)
        self.link_dot.pack(side="left")
        self.link_label = ttk.Label(lrow, text=f"{APP.label} · looking…", style="HeaderSub.TLabel")
        self._hdr_widgets += (lrow,)
        self.link_label.pack(side="left", padx=(S(4), 0))
        icons = ttk.Frame(hdr, style="Header.TFrame")
        icons.pack(side="right")
        sz = S(22)
        self.ib_menu = IconButton(icons, icon_menu, self.show_menu, "Panel options", sz)
        self.ib_hide = IconButton(icons, icon_hide, self.tuck_away, "Hide the panel now", sz)
        self.ib_pin = IconButton(icons, lambda c, sz_, col: icon_pin(c, sz_, col, self.pinned()), self.toggle_pin,
                                 lambda: ("Pinned open. Click to auto-hide when the mouse leaves"
                                          if self.pinned() else "Auto-hides. Click to pin the panel open"), sz)
        for ib in (self.ib_menu, self.ib_hide, self.ib_pin):
            ib.pack(side="right", padx=(S(2), 0))
        self.ah_label = tk.Label(icons, text="Auto-hide", font=("Segoe UI", 7), bd=0, cursor="hand2", padx=S(4))
        self.ah_label.pack(side="right")
        self.ah_label.bind("<Button-1>", lambda e: self.toggle_pin())
        self.menu = tk.Menu(self.root, tearoff=0)

        # status line
        st = ttk.Frame(root, style="Status.TFrame", padding=(S(10), S(4)))
        st.pack(side="bottom", fill="x")
        self.status_dot = tk.Label(st, text="●", font=("Segoe UI", 8), bd=0)
        self.status_dot.pack(side="left", anchor="n")
        self.status_label = ttk.Label(st, text="Ready", style="Status.TLabel", justify="left")
        self.status_label.pack(side="left", fill="x", expand=True, padx=(S(5), 0))
        st.bind("<Configure>", lambda e: self.status_label.configure(wraplength=max(100, e.width - S(30))))

        body = ttk.Frame(root, style="TFrame", padding=S(6))
        body.pack(side="top", fill="both", expand=True)

        # ---- active design card
        card = ttk.Frame(body, style="Card.TFrame", padding=S(10))
        card.pack(fill="x")
        top = ttk.Frame(card, style="Card.TFrame")
        top.pack(fill="x")
        self.card_section = ttk.Label(top, text="OPEN IN SOLIDCRAFT", style="CardSection.TLabel")
        self.card_section.pack(side="left")
        self.follow_btn = ttk.Button(top, text=f"↺ Follow {APP.label}", style="Small.TButton",
                                     command=self.follow_solidcraft)
        self.canvas = tk.Canvas(card, height=S(165), highlightthickness=1, bd=0)
        self.canvas.pack(fill="x", pady=(S(6), S(8)))
        self.canvas.bind("<ButtonPress-1>", self._orbit_start)
        self.canvas.bind("<B1-Motion>", self._orbit_drag)
        self.canvas.bind("<Double-1>", lambda e: self._reset_view())
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.canvas.bind(seq, self._zoom)
        self.canvas.bind("<Configure>", lambda e: self.request_redraw())
        self.f_title = ttk.Label(card, text="", style="FileTitle.TLabel", justify="left")
        self.f_title.pack(anchor="w")
        self.f_pill = tk.Label(card, text="", font=("Segoe UI Semibold", 8), padx=S(8), pady=S(2), bd=0)
        self.f_pill.pack(anchor="w", pady=(S(4), 0))
        self.f_note = ttk.Label(card, text="", style="CardWarn.TLabel", justify="left")
        self.f_note.pack(anchor="w", pady=(S(4), 0))
        self.b_to_vault = ttk.Button(card, text="⤓  Save to Vault…", style="Success.TButton",
                                     command=self.do_save_to_vault)
        kv = ttk.Frame(card, style="Card.TFrame")
        kv.pack(fill="x", pady=(S(8), S(2)))
        self.kv = {}
        for r, (key, label) in enumerate((("pn", "Part number"), ("rev", "Revision"), ("folder", "Folder"))):
            ttk.Label(kv, text=label, style="CardMuted.TLabel").grid(row=r, column=0, sticky="nw", pady=S(1))
            v = ttk.Label(kv, text="—", style="CardValue.TLabel", justify="left")
            v.grid(row=r, column=1, sticky="w", padx=(S(12), 0), pady=S(1))
            self.kv[key] = v
        ttk.Label(card, text="Part name", style="CardMuted.TLabel").pack(anchor="w", pady=(S(6), 0))
        self.part_var = tk.StringVar()
        self.part_entry = ttk.Entry(card, textvariable=self.part_var)
        self.part_entry.pack(fill="x", pady=(S(2), S(5)), ipady=S(1))
        ttk.Label(card, text="Description", style="CardMuted.TLabel").pack(anchor="w")
        self.desc_var = tk.StringVar()
        self.desc_entry = ttk.Entry(card, textvariable=self.desc_var)
        self.desc_entry.pack(fill="x", pady=(S(2), S(6)), ipady=S(1))
        for e in (self.part_entry, self.desc_entry):
            e.bind("<Return>", lambda ev: self.do_save_props())
        self.b_save = ttk.Button(card, text="Save properties", style="Small.TButton", command=self.do_save_props)
        self.b_save.pack(anchor="e")

        def wrap(e):
            w = max(120, e.width - S(24))
            for lbl in (self.f_title, self.f_note):
                lbl.configure(wraplength=w)
            self.kv["folder"].configure(wraplength=max(80, w - S(90)))
        card.bind("<Configure>", wrap)

        # ---- actions
        act = ttk.Frame(body, style="TFrame")
        act.pack(fill="x", pady=(S(8), 0))
        act.columnconfigure(0, weight=1, uniform="a")
        act.columnconfigure(1, weight=1, uniform="a")
        self.b_checkout = ttk.Button(act, text="↓  Check Out", style="Accent.TButton", command=self.do_check_out)
        self.b_checkin = ttk.Button(act, text="↑  Save & Check In", style="Success.TButton", command=self.do_check_in)
        self.b_undo = ttk.Button(act, text="↺  Undo Check-Out", command=self.do_undo)
        self.b_rev = ttk.Button(act, text="Revision  ▾", command=self.rev_menu)
        self.b_export = ttk.Button(act, text="⇪  Export STEP & PDF", command=self.do_export)
        self.b_history = ttk.Button(act, text="☰  History", command=self.do_history)
        self.b_new = ttk.Button(act, text="＋  New Part", command=self.do_new_part)
        grid = ((self.b_checkout, 0, 0), (self.b_checkin, 0, 1), (self.b_undo, 1, 0), (self.b_rev, 1, 1),
                (self.b_export, 2, 0), (self.b_history, 2, 1))
        for b, r, c in grid:
            b.grid(row=r, column=c, sticky="ew", padx=S(2), pady=S(2))
        self.b_new.grid(row=3, column=0, columnspan=2, sticky="ew", padx=S(2), pady=S(2))

        # ---- browser
        br = ttk.Frame(body, style="Card.TFrame", padding=S(8))
        br.pack(fill="both", expand=True, pady=(S(8), 0))
        srow = ttk.Frame(br, style="Card.TFrame")
        srow.pack(fill="x", pady=(0, S(6)))
        ttk.Label(srow, text="⌕", style="CardMuted.TLabel").pack(side="left", padx=(0, S(4)))
        self.search_var = tk.StringVar()
        self.search_entry = ttk.Entry(srow, textvariable=self.search_var)
        self.search_entry.pack(side="left", fill="x", expand=True, ipady=S(1))
        self.search_var.trace_add("write", lambda *_: self._schedule_lists())
        self.search_entry.bind("<Escape>", lambda e: self.search_var.set(""))
        self.tabs = ttk.Notebook(br)
        self.tabs.pack(fill="both", expand=True)
        self.vault_tree = self._make_tree("  Vault  ")
        self.mine_tree = self._make_tree("  My check-outs  ")
        self.recent_tree = self._make_tree("  Recent  ")
        self.vault_tree.bind("<<TreeviewOpen>>", lambda e: self._dir_toggle(True))
        self.vault_tree.bind("<<TreeviewClose>>", lambda e: self._dir_toggle(False))

    def _make_tree(self, title):
        S = self.S
        f = ttk.Frame(self.tabs, style="Dialog.TFrame")
        self.tabs.add(f, text=title)
        tv = ttk.Treeview(f, columns=("rev", "st"), show="tree headings", selectmode="browse")
        tv.heading("#0", text="File", anchor="w")
        tv.heading("rev", text="Rev", anchor="w")
        tv.heading("st", text="", anchor="center")
        tv.column("#0", width=S(200), anchor="w", stretch=True)
        tv.column("rev", width=S(46), anchor="w", stretch=False)
        tv.column("st", width=S(26), anchor="center", stretch=False)
        sb = ttk.Scrollbar(f, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=sb.set)
        tv.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        tv.bind("<<TreeviewSelect>>", lambda e, t=tv: self._on_pick(t))
        tv.bind("<Double-1>", lambda e, t=tv: self._on_double(t, e))
        tv.bind("<Return>", lambda e, t=tv: self._open_selected(t))
        for seq in ("<Button-3>", "<Button-2>"):
            tv.bind(seq, lambda e, t=tv: self._tree_menu(t, e))
        return tv

    def menu_colors(self):
        t = self.t
        return dict(bg=t["panel_bg"], fg=t["fg"], activebackground=t["sel_bg"], activeforeground=t["sel_fg"],
                    disabledforeground=t["muted_fg"], bd=0, relief="flat", font=("Segoe UI", 9))

    def _panel_styles(self, t):
        """Compact, flat styling in the spirit of SolidCraft's ribbon."""
        s, S, mix = self.style, self.S, A.mix
        F = "Segoe UI"
        pb, fg, btn = t["panel_bg"], t["fg"], t["btn_bg"]
        line = mix(pb, fg, 0.10)
        s.configure(".", font=(F, 8))
        for name in ("TLabel", "Card.TLabel", "CardValue.TLabel", "CardMuted.TLabel"):
            s.configure(name, font=(F, 8))
        s.configure("CardSection.TLabel", font=("Segoe UI Semibold", 7))
        s.configure("CardAccent.TLabel", font=("Segoe UI Semibold", 8))
        s.configure("FileTitle.TLabel", font=("Segoe UI Semibold", 9))
        s.configure("HeaderTitle.TLabel", font=("Segoe UI Semibold", 10))
        s.configure("HeaderSub.TLabel", font=(F, 7))
        s.configure("Status.TLabel", font=(F, 7))
        s.configure("CardWarn.TLabel", background=pb, foreground=t["warning"], font=("Segoe UI Semibold", 8))
        s.configure("Card.TFrame", bordercolor=line, lightcolor=line, darkcolor=line)
        s.configure("TButton", font=(F, 8), padding=(S(8), S(4)), borderwidth=1,
                    bordercolor=mix(btn, fg, 0.12), lightcolor=btn, darkcolor=btn)
        s.map("TButton", bordercolor=[("disabled", line)])
        for name, col in (("Accent", t["accent"]), ("Success", t["success"]), ("Danger", t["danger"])):
            bg, hov = mix(pb, col, 0.24), mix(pb, col, 0.38)
            s.configure(f"{name}.TButton", background=bg, foreground=fg, bordercolor=mix(pb, col, 0.62),
                        lightcolor=bg, darkcolor=bg, borderwidth=1, padding=(S(8), S(4)),
                        font=("Segoe UI Semibold", 8))
            s.map(f"{name}.TButton", background=[("disabled", mix(pb, btn, 0.5)), ("pressed", hov), ("active", hov)],
                  foreground=[("disabled", t["muted_fg"])], bordercolor=[("disabled", line)])
        s.configure("Small.TButton", font=(F, 7), padding=(S(6), S(2)))
        s.configure("TEntry", padding=(S(5), S(2)))
        s.configure("Treeview", rowheight=S(22), font=(F, 8))
        s.configure("Treeview.Heading", font=(F, 7), padding=(S(6), S(3)))
        s.configure("TNotebook.Tab", font=(F, 8), padding=(S(10), S(4)))
        s.map("TNotebook.Tab", background=[("selected", mix(pb, fg, 0.08)), ("active", mix(pb, fg, 0.04))],
              foreground=[("selected", fg), ("active", fg)])

    def _view_theme(self, t):
        """The preview uses SolidCraft's blue viewport and grey parts when matching SolidCraft."""
        vp = VIEWPORTS.get(self.theme_name)
        return dict(t, **vp) if vp else t

    def _theme_icons(self):
        t = self.t
        hb, hf = t["header_bg"], t.get("header_fg", t["fg"])
        on = not self.pinned()
        self.ah_label.configure(bg=hb, text="Auto-hide on" if on else "Auto-hide off",
                                fg=t.get("header_accent", t["accent"]) if on else t.get("header_muted", t["muted_fg"]))
        for ib in (self.ib_menu, self.ib_hide, self.ib_pin):
            ib.set_colors(hb, t.get("header_muted", t["muted_fg"]), A.mix(hb, hf, 0.12),
                          t.get("header_accent", t["accent"]))
        self.ib_pin.active = self.pinned()
        self.ib_pin.redraw()

    def apply_theme(self):
        t, s, S = self.t, self.style, self.S
        A.style_ttk(self.root, s, t, S)
        self._panel_styles(t)
        self.canvas.configure(highlightbackground=t["border"], bg=t["entry_bg"])
        self.link_dot.configure(bg=t["header_bg"])
        self.status_dot.configure(bg=t["toolbar_bg"])
        self.menu.configure(**self.menu_colors())
        for tv in (self.vault_tree, self.mine_tree, self.recent_tree):
            tv.tag_configure("mine", foreground=t["success"])
            tv.tag_configure("new", foreground=t["accent"])
            tv.tag_configure("locked", foreground=t["warning"])
            tv.tag_configure("dup", foreground=t["danger"])
            tv.tag_configure("lib", foreground=t["muted_fg"])
            tv.tag_configure("dir", foreground=t["fg"])
            tv.tag_configure("active", font=("Segoe UI Semibold", 8))
        self._draw_logo()
        self._theme_icons()
        self._theme_rail()
        self._update_link_label()
        self.update_card()

    def _draw_logo(self):
        c, t, S = self.logo, self.t, self.S
        c.delete("all")
        c.configure(bg=t["header_bg"])
        s = S(22)
        h = s / 2
        acc = t.get("header_accent", t["accent"])
        mix = A.mix
        c.create_polygon(h, 1, s - 2, s * 0.27, h, s * 0.54, 2, s * 0.27, fill=mix(acc, "#ffffff", 0.35), outline="")
        c.create_polygon(2, s * 0.27, h, s * 0.54, h, s - 1, 2, s * 0.73, fill=acc, outline="")
        c.create_polygon(h, s * 0.54, s - 2, s * 0.27, s - 2, s * 0.73, h, s - 1, fill=mix(acc, "#000000", 0.3),
                         outline="")

    def show_menu(self):
        self.build_menu()
        b = self.ib_menu
        try:
            self.menu.tk_popup(b.winfo_rootx() + b.winfo_width() - self.S(230), b.winfo_rooty() + b.winfo_height())
        finally:
            self.menu.grab_release()

    def set_look(self):
        self.theme_name = self.look_var.get()
        self.settings["panel_theme"] = self.theme_name
        self.save_settings()
        self.apply_theme()
        self.rebuild_lists()

    def build_menu(self):
        m = self.menu
        m.configure(**self.menu_colors())
        m.delete(0, "end")
        m.add_command(label=f"{A.USER_NAME}  ·  {A.HOST_NAME}  ·  panel v{PANEL_VERSION}", state="disabled")
        m.add_separator()
        for label, value in ((f"Dock beside {APP.label}", "dock"), (f"Overlay on {APP.label}", "overlay"),
                             ("Floating window", "float")):
            m.add_radiobutton(label=label, variable=self.mode_var, value=value, command=self.set_mode,
                              state="normal" if (WIN or value == "float") else "disabled")
        wm = tk.Menu(m, tearoff=0, **self.menu_colors())
        for name in self.WIDTHS:
            wm.add_radiobutton(label=name, variable=self.width_var, value=name, command=self.set_width)
        m.add_cascade(label="Panel width", menu=wm)
        m.add_checkbutton(label="Auto-hide when the mouse leaves (unpinned)", variable=self.autohide_var,
                          command=self.on_autohide, state="normal" if WIN else "disabled")
        hm = tk.Menu(m, tearoff=0, **self.menu_colors())
        hm.add_radiobutton(label="Hide completely (touch the edge)", variable=self.hide_style_var, value="edge",
                           command=self.set_hide_style)
        hm.add_radiobutton(label="Show a slim strip with quick buttons", variable=self.hide_style_var,
                           value="strip", command=self.set_hide_style)
        m.add_cascade(label="When tucked away", menu=hm, state="normal" if WIN else "disabled")
        lm = tk.Menu(m, tearoff=0, **self.menu_colors())
        lm.add_radiobutton(label=f"Match {APP.label}", variable=self.look_var, value=APP.look,
                           command=self.set_look)
        lm.add_separator()
        for name in A.THEMES:
            if name != APP.look and name not in VIEWPORTS:
                lm.add_radiobutton(label=name, variable=self.look_var, value=name, command=self.set_look)
        m.add_cascade(label="Panel look", menu=lm)
        m.add_command(label="Reset overlay size", command=self.reset_overlay,
                      state="normal" if self.mode_var.get() == "overlay" else "disabled")
        m.add_command(label="Re-dock now", command=self.redock,
                      state="normal" if (WIN and self.mode_var.get() != "float") else "disabled")
        m.add_separator()
        m.add_command(label="⟳  Refresh vault   F5", command=self.refresh_vault)
        m.add_command(label=f"Launch {APP.label}", command=self.launch_solidcraft)
        m.add_command(label=f"Locate {APP.label}…", command=self.locate_app_exe)
        m.add_command(label="Open full Atelier PDM", command=self.open_full_app)
        m.add_separator()
        m.add_command(label="Close panel", command=self.quit)


    # ------------------------------------------------------------------ overlay sizing
    def _build_grips(self):
        S = self.S
        self.grips = {}
        for edge, cursor in (("left", "sb_h_double_arrow"), ("top", "sb_v_double_arrow"),
                             ("bottom", "sb_v_double_arrow")):
            g = tk.Frame(self.root, bd=0, highlightthickness=0, cursor=cursor)
            g.bind("<ButtonPress-1>", lambda e, ed=edge: self._drag_start(ed, e))
            g.bind("<B1-Motion>", self._drag_move)
            g.bind("<ButtonRelease-1>", self._drag_end)
            self.grips[edge] = g
        for w in self._hdr_widgets:
            w.bind("<ButtonPress-1>", lambda e: self._drag_start("move", e), add="+")
            w.bind("<B1-Motion>", self._drag_move, add="+")
            w.bind("<ButtonRelease-1>", self._drag_end, add="+")

    def _overlay_active(self):
        return WIN and self.mode_var.get() == "overlay" and self._decorated is False and not self._fullscreen

    def _place_grips(self):
        S = self.S
        show = self._overlay_active() and self.expanded
        col = A.mix(self.t["accent"], self.t["header_bg"], 0.55)
        for edge, g in self.grips.items():
            if not show:
                g.place_forget()
                continue
            g.configure(bg=col)
            if edge == "left":
                g.place(x=0, y=0, width=S(4), relheight=1)
            elif edge == "top":
                g.place(x=0, y=0, relwidth=1, height=S(3))
            else:
                g.place(x=0, rely=1.0, y=-S(4), relwidth=1, height=S(4))
            g.lift()

    def _drag_start(self, edge, e):
        if not self._overlay_active() or not self._ext:
            return
        if edge == "move" and e.widget not in self._hdr_widgets:
            return
        self._drag = (edge, e.x_root, e.y_root, dict(self.overlay))
        self.root.configure(cursor="fleur" if edge == "move" else "")

    def _drag_move(self, e):
        if not self._drag or not self._ext:
            return
        edge, x0, y0, start = self._drag
        self.overlay = drag_overlay(start, edge, e.x_root - x0, e.y_root - y0, self._ext, self.scale)
        self._layout_overlay()

    def _drag_end(self, _e):
        if not self._drag:
            return
        self._drag = None
        self.root.configure(cursor="")
        self.settings["panel_overlay"] = dict(self.overlay)
        self.save_settings()
        self.say("Overlay size saved. ≡ › Reset overlay size puts it back.", "ok")

    def reset_overlay(self):
        self.overlay = {k: float(v) for k, v in OVERLAY_DEFAULT.items()}
        self.settings.pop("panel_overlay", None)
        self.save_settings()
        self._layout_overlay()

    def _layout_overlay(self):
        if not self._ext:
            return
        right, y, hgt, _w = overlay_rect(self._ext, self.overlay, self.scale)
        self._anchor = (right, y, max(self.S(OVERLAY_MIN_H), hgt))
        self._last_geom = None
        self._apply_geometry()

    def _panel_w(self):
        if self.mode_var.get() == "overlay" and not self._fullscreen:
            return int(round(self.overlay["width"] * self.scale))
        return self.S(self.WIDTHS[self.width_var.get()])

    # ------------------------------------------------------------------ auto-hide rail
    RAIL_BUTTONS = (("checkout", "↓", "Check out"), ("checkin", "↑", "Save & check in"),
                    ("new", "＋", "New part"), ("search", "⌕", "Search the vault"),
                    ("pin", "📌", "Keep the panel open"))

    def _build_rail(self):
        S = self.S
        self.rail = tk.Frame(self.root, width=S(self.RAIL), bd=0, highlightthickness=0, cursor="hand2")
        self.rail.pack_propagate(False)
        self.rail_inner = tk.Frame(self.rail, bd=0, highlightthickness=0)
        self.rail_inner.pack(fill="both", expand=True)
        self.rail_logo = tk.Canvas(self.rail_inner, width=S(20), height=S(20), highlightthickness=0, bd=0)
        self.rail_logo.pack(pady=(S(10), S(4)))
        self.rail_dot = tk.Label(self.rail_inner, text="●", font=("Segoe UI", 9), bd=0)
        self.rail_dot.pack(pady=(0, S(6)))
        self.rail_btns = {}
        self.rail_enabled = {k: True for k, _, _ in self.RAIL_BUTTONS}
        actions = {"checkout": self.do_check_out, "checkin": self._rail_checkin, "new": self.do_new_part,
                   "search": self.rail_search, "pin": self.toggle_hold}
        for key, text, _tip in self.RAIL_BUTTONS:
            b = tk.Label(self.rail_inner, text=text, font=("Segoe UI Semibold", 11), bd=0, width=2, pady=S(3))
            b.pack(fill="x", pady=S(1))
            b.bind("<Button-1>", lambda e, k=key, fn=actions[key]: self._rail_click(k, fn))
            b.bind("<Enter>", lambda e, k=key: self._rail_hover(k, True))
            b.bind("<Leave>", lambda e, k=key: self._rail_hover(k, False))
            self.rail_btns[key] = b
        self.rail_name = tk.Canvas(self.rail_inner, width=S(self.RAIL), highlightthickness=0, bd=0)
        self.rail_name.pack(fill="both", expand=True, pady=(S(8), S(8)))
        self.rail_name.bind("<Configure>", lambda e: self._draw_rail_name())
        for w in (self.rail, self.rail_inner, self.rail_logo, self.rail_dot, self.rail_name):
            w.bind("<Button-1>", lambda e: self.set_expanded(not self.expanded), add="+")

    def _theme_rail(self):
        t = self.t
        bg = t["header_bg"]
        for w in (self.rail, self.rail_inner, self.rail_logo, self.rail_dot, self.rail_name):
            w.configure(bg=bg)
        c, S = self.rail_logo, self.S
        c.delete("all")
        sz, acc, mix = S(20), t.get("header_accent", t["accent"]), A.mix
        h = sz / 2
        c.create_polygon(h, 1, sz - 2, sz * 0.27, h, sz * 0.54, 2, sz * 0.27, fill=mix(acc, "#ffffff", 0.35),
                         outline="")
        c.create_polygon(2, sz * 0.27, h, sz * 0.54, h, sz - 1, 2, sz * 0.73, fill=acc, outline="")
        c.create_polygon(h, sz * 0.54, sz - 2, sz * 0.27, sz - 2, sz * 0.73, h, sz - 1,
                         fill=mix(acc, "#000000", 0.3), outline="")
        self._paint_rail()
        self._layout_rail()

    def _edge_mode(self):
        return self.hide_style_var.get() == "edge"

    def _rail_width(self):
        """Width of the strip right now: 5 px when hidden, nothing while open (edge style), 30 px (strip style)."""
        if self._edge_mode():
            return 0 if self.expanded else self.S(self.EDGE)
        return self.S(self.RAIL)

    def _layout_rail(self):
        t = self.t
        if not self.rail_shown or (self._edge_mode() and self.expanded):
            if self.rail.winfo_manager():
                self.rail.pack_forget()
            self._hide_tip()
            return
        if not self.rail.winfo_manager():
            if self.content.winfo_manager():
                self.rail.pack(side="right", fill="y", before=self.content)
            else:
                self.rail.pack(side="right", fill="y")
        if self._edge_mode():
            if self.rail_inner.winfo_manager():
                self.rail_inner.pack_forget()
            self.rail.configure(width=self.S(self.EDGE), bg=A.mix(t["accent"], t["header_bg"], 0.45))
            self._hide_tip()
        else:
            if not self.rail_inner.winfo_manager():
                self.rail_inner.pack(fill="both", expand=True)
            self.rail.configure(width=self.S(self.RAIL), bg=t["header_bg"])

    def set_hide_style(self):
        self.settings["panel_hide_style"] = self.hide_style_var.get()
        self.save_settings()
        self._layout_rail()
        self._last_geom = None
        self._apply_geometry()
        self.say(f"Tucked away: hidden completely. Touch {APP.label}'s right edge to open the panel."
                 if self._edge_mode() else "Tucked away: a slim strip with quick buttons stays visible.", "ok")

    def _paint_rail(self, hover=None):
        t = self.t
        bg, fg = t["header_bg"], t.get("header_fg", t["fg"])
        dim = A.mix(t.get("header_muted", t["muted_fg"]), bg, 0.35)
        hov = A.mix(bg, fg, 0.14)
        for key, b in self.rail_btns.items():
            on = self.rail_enabled.get(key, True)
            col = fg if on else dim
            if key == "pin" and self.pinned():
                col = t.get("header_accent", t["accent"])
            b.configure(bg=hov if (hover == key and on) else bg, fg=col)
        self._draw_rail_name()

    def _draw_rail_name(self):
        c, t, S = self.rail_name, self.t, self.S
        c.delete("all")
        f = self.current()
        if f:
            rec = A.INDEX.records.get(f) or {}
            pp = A.parse_pn(os.path.splitext(f)[0])
            pn = rec.get("part_number") or (pp[3] if pp else "")
            text = f"{pn}  {A.display_part_name(f, rec)}".strip()
        else:
            text = A.APP_NAME
        h = max(1, c.winfo_height())
        max_chars = max(4, int(h / S(7)))
        if len(text) > max_chars:
            text = text[:max_chars - 1] + "…"
        c.create_text(S(self.RAIL) // 2, S(2), text=text, angle=270, anchor="w",
                      fill=t.get("header_fg", t["fg"]), font=("Segoe UI Semibold", 9))

    def _rail_state(self, f, key):
        t = self.t
        self.rail_enabled.update(checkout=key in ("available", "lib"),
                                 checkin=key in ("mine", "new") or (not f and self._can_save_to_vault()))
        col = {"mine": t["success"], "new": t["accent"], "locked": t["warning"], "dup": t["danger"],
               "available": A.mix(t["success"], t["header_bg"], 0.3)}.get(key, t.get("header_muted", t["muted_fg"]))
        self.rail_dot.configure(fg=col if f else t["header_bg"])
        self._paint_rail()

    def _rail_click(self, key, fn):
        if not self.rail_enabled.get(key, True):
            return
        self._hide_tip()
        fn()

    def _rail_hover(self, key, entered):
        self._paint_rail(key if entered else None)

    def rail_search(self):
        self.set_expanded(True)
        self.tabs.select(0)
        self.root.focus_force()
        self.search_entry.focus_set()
        self.search_entry.select_range(0, "end")

    def pinned(self):
        return not self.autohide_var.get()

    def _docked_or_overlay(self):
        if self.mode_var.get() == "float" or not WIN:
            self.say(f"Hiding works when the panel is docked or overlaid on {APP.label} (≡ menu).", "warn")
            return False
        return True

    def toggle_pin(self):
        if not self._docked_or_overlay():
            return
        self.autohide_var.set(not self.autohide_var.get())
        self.on_autohide()
        if self.pinned():
            self.set_expanded(True)

    toggle_hold = toggle_pin                              # the strip's pin button

    def tuck_away(self):
        if not self._docked_or_overlay():
            return
        if self.pinned():
            self.autohide_var.set(True)
            self.on_autohide()
        self._last_geom = None
        if self.rail_shown:
            self.set_expanded(False)
        else:
            self._track_window()                          # shows the edge / strip straight away

    def on_autohide(self):
        self.settings["panel_autohide"] = bool(self.autohide_var.get())
        self.save_settings()
        self._last_geom = None
        where = (f"Touch the thin line on {APP.label}'s right edge to bring it back." if self._edge_mode()
                 else "Point at the strip to bring it back.")
        self.say(f"Unpinned: the panel hides when the mouse leaves. {where}" if self.autohide_var.get()
                 else "Pinned: the panel stays open.", "ok")
        self._theme_icons()
        self._paint_rail()

    def _show_rail(self, flag):
        if flag == self.rail_shown:
            return
        self.rail_shown = flag
        self.set_expanded(self.pinned() if flag else True, force=True)
        self._last_geom = None

    def set_expanded(self, flag, force=False):
        if flag == self.expanded and not force:
            return
        if not flag and not self.rail_shown:
            return
        self.expanded = flag
        self._in_since = self._out_since = None
        if flag:
            self.content.pack(side="left", fill="both", expand=True)
            try:
                self.root.lift()
            except tk.TclError:
                pass
        else:
            self.content.pack_forget()
        self._layout_rail()
        self._last_geom = None
        self._apply_geometry()
        self._paint_rail()

    def _apply_geometry(self):
        if not self._anchor or self._decorated is not False:
            return
        geom = flyout_geometry(self._anchor, self._rail_width(), self._panel_w(), self.rail_shown, self.expanded)
        if geom != self._last_geom:
            self._last_geom = geom
            self.root.geometry(geom)
        self._place_grips()

    def _dialog_open(self):
        try:
            for w in self.root.winfo_children():
                if isinstance(w, tk.Toplevel) and w is not self._tip and w.winfo_exists() and w.winfo_viewable():
                    return True
            return self.root.grab_current() is not None
        except (tk.TclError, KeyError):
            return False

    def _typing(self):
        try:
            w = self.root.focus_get()
        except (tk.TclError, KeyError):
            return False
        return w in (self.part_entry, self.desc_entry, self.search_entry) and foreground_pid() == os.getpid()

    def _inside(self, px, py, pad=0):
        """Is the pointer over the panel (plus a margin)? Uses the size the panel last set itself, because
        Windows can report the old size for a moment just after the panel opens or closes."""
        g = re.match(r"^(\d+)x(\d+)\+(-?\d+)\+(-?\d+)$", self._last_geom or "")
        if g and self._decorated is False:
            w, h, x, y = (int(v) for v in g.groups())
        else:
            r = self.root
            x, y, w, h = r.winfo_rootx(), r.winfo_rooty(), r.winfo_width(), r.winfo_height()
        return x - pad <= px < x + w + pad and y - pad <= py < y + h + pad
    def _hover_tick(self):
        if not self.running:
            return
        try:
            self._hover_step()
        except (tk.TclError, KeyError):
            pass
        self.root.after(100, self._hover_tick)

    OPEN_SETTLE = 0.6                        # seconds after opening before it may close again
    OTHER_APP_CLOSE = 0.4                    # seconds another program must stay in front before closing
    LEAVE_MARGIN = 12                        # pixels round the panel that still count as "over it"

    def _hover_step(self):
        st = self.__dict__.setdefault("_hov", {"armed": True, "was_open": False, "opened": 0.0, "other": None})
        if not self.rail_shown or self.root.state() == "withdrawn":
            self._hide_tip()
            st["was_open"] = False
            return
        now = time.monotonic()
        px, py = self.root.winfo_pointerxy()
        w = self.root.winfo_containing(px, py)
        self._tooltip_step(w, now)

        if not self.expanded:
            if st["was_open"]:                   # just closed (any reason): wait for the mouse to move away
                st["was_open"], st["armed"] = False, False
            if self._edge_mode():
                over = self._inside(px, py)
            else:
                over = w in (self.rail, self.rail_inner, self.rail_logo, self.rail_dot, self.rail_name)
            if not over:
                st["armed"], self._in_since = True, None
                return
            if not st["armed"]:
                return
            self._in_since = self._in_since or now
            delay = self.HOVER_OPEN_EDGE if self._edge_mode() else self.HOVER_OPEN
            if now - self._in_since >= delay:
                self.set_expanded(True)
            return

        if not st["was_open"]:                   # just opened (hover, click or button)
            st["was_open"], st["opened"], st["other"] = True, now, None
            self._out_since = None
        if self.pinned() or self._dialog_open() or self._typing():
            self._out_since, st["other"] = None, None
            return
        inside = self._inside(px, py, pad=self.S(self.LEAVE_MARGIN))
        if now - st["opened"] < self.OPEN_SETTLE:
            self._out_since = None
            return
        fg = foreground_pid()
        if fg and fg not in (os.getpid(), self._sc_pid) and not inside:
            st["other"] = st["other"] or now
            if now - st["other"] >= self.OTHER_APP_CLOSE:
                self.set_expanded(False)         # another program really came to the front
            return
        st["other"] = None
        if inside:
            self._out_since = None
            return
        self._out_since = self._out_since or now
        if now - self._out_since >= self.LEAVE_CLOSE:
            self.set_expanded(False)
    def _tooltip_step(self, w, now):
        key = next((k for k, b in self.rail_btns.items() if b is w), None)
        if key != self._tip_key:
            self._hide_tip()
            self._tip_key, self._tip_since = key, now
            return
        if key and self._tip is None and now - self._tip_since >= 0.45:
            tip = dict((k, t) for k, _, t in self.RAIL_BUTTONS)[key]
            if key == "pin" and self.pinned():
                tip = "Unpin: hide when the mouse leaves"
            if not self.rail_enabled.get(key, True):
                tip += "  (not available for this file)"
            t = self.t
            self._tip = tk.Toplevel(self.root)
            self._tip.overrideredirect(True)
            try:
                self._tip.attributes("-topmost", True)
            except tk.TclError:
                pass
            tk.Label(self._tip, text=tip, bg=t["panel_bg"], fg=t["fg"], font=("Segoe UI", 9), padx=8, pady=4,
                     highlightthickness=1, highlightbackground=t["border"]).pack()
            self._tip.update_idletasks()
            x = w.winfo_rootx() - self._tip.winfo_reqwidth() - self.S(6)
            y = w.winfo_rooty() + (w.winfo_height() - self._tip.winfo_reqheight()) // 2
            self._tip.geometry(f"+{max(0, x)}+{max(0, y)}")

    def _hide_tip(self):
        if self._tip is not None:
            try:
                self._tip.destroy()
            except tk.TclError:
                pass
        self._tip = None

    # ------------------------------------------------------------------ card
    def follow_solidcraft(self):
        self.picked = None
        for tv in (self.vault_tree, self.mine_tree, self.recent_tree):
            tv.selection_remove(*tv.selection())
        self.poke.set()
        self.update_card()

    def update_card(self):
        t = self.t
        f = self.current()
        following = self.picked is None
        self.card_section.configure(text=f"OPEN IN {APP.label.upper()}" if following else "SELECTED IN VAULT")
        if following:
            self.follow_btn.pack_forget()
        elif not self.follow_btn.winfo_ismapped():
            self.follow_btn.pack(side="right")
        note = ""
        if not f:
            doc = self.doc
            if not self.link.connected and not self.sc_hwnd:
                title, note = "No design open", f"{APP.label} isn't running. Start it from the ≡ menu."
            elif not self.link.connected and APP.has_control:
                title = "Design not recognised"
                note = (f"Can't read the open design from {APP.label}. Turn on Preferences › Assistant › "
                        f"“Let AI agents connect” in {APP.label}, then restart it.")
            elif not self.link.connected:
                title = "Design not recognised"
                note = "Can't read PhysCAD's window yet. Click into PhysCAD and the panel will catch up."
            elif doc and doc.get("name") and not doc["name"].lower().startswith("untitled"):
                title = doc["name"]
                note = "Not in the vault yet. Save to Vault gives it the next part number."
            else:
                title = "Unsaved new design"
                note = "Save to Vault gives it the next part number and saves it into the vault."
            self.f_title.configure(text=title)
            self.f_pill.configure(text="", bg=t["panel_bg"])
            for v in self.kv.values():
                v.configure(text="—")
            self.part_var.set("")
            self.desc_var.set("")
            self.f_note.configure(text=note)
            self._show_to_vault(self._can_save_to_vault())
            self._set_states(None, None)
            self.request_redraw()
            return
        self._show_to_vault(False)
        rec = A.INDEX.records.get(f) or {}
        info = A.INDEX.files.get(f)
        key, label = A.file_state(f)
        pill = {"mine": t["success"], "new": t["accent"], "locked": t["warning"], "dup": t["danger"],
                "lib": t["muted_fg"], "available": A.mix(t["success"], t["panel_bg"], 0.2)}.get(key, t["muted_fg"])
        self.f_title.configure(text=f)
        self.f_pill.configure(text=label.upper(), bg=pill, fg=A.text_on(pill))
        pp = A.parse_pn(os.path.splitext(f)[0])
        self.kv["pn"].configure(text=rec.get("part_number") or (pp[3] if pp else "—"))
        self.kv["rev"].configure(text=rec.get("revision") or "A-01")
        self.kv["folder"].configure(text=(info.folder if info else (rec.get("folder_path") or "—") + "  (pending)"))
        if self.focus_is_not_in_entries():
            self.part_var.set(A.display_part_name(f, rec))
            self.desc_var.set(rec.get("description") or "")
        opened = self.opened.get(f, "")
        if self.is_active(f) and self.doc and self.doc.get("dirty"):
            note = (f"● Unsaved changes in {APP.label}. Save & Check In saves them first." if APP.has_control else
                    f"● Unsaved changes in {APP.label}. Save there (Ctrl+S) before checking in.")
        elif opened.startswith(self.ref_dir()) and key != "mine":
            note = f"Read-only copy open in {APP.label}. Check out to edit it."
        elif opened.startswith(self.ref_dir()) and key == "mine" and self.is_active(f):
            note = f"{APP.label} still has the read-only copy open. Double-click the file to open your editable copy."
        elif key == "locked":
            note = "Someone else is editing this part. You can open a read-only copy."
        self.f_note.configure(text=note)
        self._set_states(f, key)
        self.request_redraw()

    def focus_is_not_in_entries(self):
        try:
            w = self.root.focus_get()
        except (KeyError, tk.TclError):
            return True
        return w not in (self.part_entry, self.desc_entry)

    def _can_save_to_vault(self):
        """The design open in SolidCraft isn't in the vault and the panel is following SolidCraft."""
        return self.picked is None and not self.active_file and (self.link.connected or bool(self.sc_hwnd))

    def _show_to_vault(self, flag):
        if flag and not self.b_to_vault.winfo_manager():
            self.b_to_vault.pack(fill="x", pady=(self.S(8), 0), after=self.f_note)
        elif not flag and self.b_to_vault.winfo_manager():
            self.b_to_vault.pack_forget()

    def _set_states(self, f, key):
        def st(b, on):
            b.state(["!disabled"] if on else ["disabled"])
        is_model = bool(f) and f in A.INDEX.files and os.path.splitext(f)[1].lower() in A.CAD_MODEL_EXTS
        st(self.b_checkout, key in ("available", "lib"))
        st(self.b_checkin, key in ("mine", "new"))
        st(self.b_undo, key in ("mine", "new"))
        st(self.b_rev, key == "available")
        st(self.b_export, is_model)
        st(self.b_history, bool(f))
        editable = bool(f) and key not in (None, "dup", "missing", "lib")
        st(self.b_save, editable)
        for e in (self.part_entry, self.desc_entry):
            e.state(["!disabled"] if editable else ["disabled"])
        self.b_checkout.configure(text="↓  Sync Library Part" if key == "lib" else "↓  Check Out")
        self._rail_state(f, key)

    # ------------------------------------------------------------------ preview
    def request_redraw(self):
        if not self._redraw_pending:
            self._redraw_pending = True
            self.root.after(16, self._redraw)

    def _redraw(self):
        self._redraw_pending = False
        c, t = self.canvas, self._view_theme(self.t)
        c.delete("all")
        w, h = max(c.winfo_width(), 50), max(c.winfo_height(), 50)
        f = self.current()
        if not f:
            A.render_placeholder(c, w, h, t, self.cam, "No vault design", "Open or create a part")
            return
        stl = A.find_preview_stl(f)
        mesh = A.load_mesh(stl) if stl else None
        if mesh:
            A.render_mesh(c, mesh, w, h, t, self.cam)
            return
        local = A.sandbox_path(f)
        info = A.INDEX.files.get(f)
        src = local if (A.file_state(f)[0] in ("mine", "new") and os.path.exists(local)) else (
            info.path if info else None)
        thumb_fn = getattr(A, "cad_thumbnail", None)
        thumb = thumb_fn(src) if (thumb_fn and src) else None
        if thumb and self._draw_thumb(c, w, h, t, thumb):
            return
        A.render_placeholder(c, w, h, t, self.cam, A.display_part_name(f, A.INDEX.records.get(f)),
                             "Preview appears after check-in")

    def _draw_thumb(self, c, w, h, t, data):
        try:
            img = tk.PhotoImage(data=base64.b64encode(data).decode("ascii"))
        except tk.TclError:
            return False
        A.draw_backdrop(c, w, h, t)
        iw, ih = max(1, img.width()), max(1, img.height())
        aw, ah = max(1, w - 16), max(1, h - 16)
        if iw > aw or ih > ah:
            img = img.subsample(max(-(-iw // aw), -(-ih // ah)))
        else:
            k = min(aw // iw, ah // ih)
            if k > 1:
                img = img.zoom(k)
        self._thumb_img = img
        c.create_image(w // 2, h // 2, image=img)
        return True

    def _orbit_start(self, e):
        self._drag_xy = (e.x, e.y)

    def _orbit_drag(self, e):
        dx, dy = e.x - self._drag_xy[0], e.y - self._drag_xy[1]
        self.cam["ry"] += dx * 0.8
        self.cam["rx"] += dy * 0.8
        self._drag_xy = (e.x, e.y)
        self.request_redraw()

    def _zoom(self, e):
        up = getattr(e, "delta", 0) > 0 or getattr(e, "num", 0) == 4
        self.cam["zoom"] = max(0.2, min(5.0, self.cam["zoom"] * (1.15 if up else 1 / 1.15)))
        self.request_redraw()

    def _reset_view(self):
        self.cam.update(rx=25.0, ry=45.0, zoom=1.0)
        self.request_redraw()

    # ------------------------------------------------------------------ lists
    def _schedule_lists(self):
        if self._search_job:
            self.root.after_cancel(self._search_job)
        self._search_job = self.root.after(160, self.rebuild_lists)

    def _row(self, tv, parent, name):
        rec = A.INDEX.records.get(name) or {}
        key = A.file_state(name)[0]
        tags = [key] + (["active"] if name == self.active_file else [])
        tv.insert(parent, "end", iid="f:" + name, text="  " + name, tags=tags,
                  values=(rec.get("revision") or "A-01", self.STATE_ICON.get(key, "")))

    def rebuild_lists(self):
        self._search_job = None
        q = self.search_var.get().strip().lower()
        names = self._sc_names()

        def matches(n):
            if not q:
                return True
            rec = A.INDEX.records.get(n) or {}
            hay = " ".join((n, rec.get("part_number") or "", rec.get("description") or "",
                            rec.get("part_name") or "")).lower()
            return q in hay

        # vault
        tv = self.vault_tree
        tv.delete(*tv.get_children())
        if q:
            for n in sorted((n for n in names if matches(n)), key=str.lower):
                self._row(tv, "", n)
        else:
            nodes = {}
            icon_fn = getattr(getattr(A, "AtelierApp", None), "_folder_icon", None)

            def ensure(rel):
                if not rel or rel in (A.VAULT_ROOT_LABEL, "."):
                    return ""
                if rel in nodes:
                    return nodes[rel]
                parent = ensure(os.path.dirname(rel))
                leaf = os.path.basename(rel)
                icon = icon_fn(rel, leaf)[0] if icon_fn else "📁"
                nodes[rel] = tv.insert(parent, "end", iid="d:" + rel, text=f"{icon}  {leaf}",
                                       open=rel in self.open_dirs, tags=("dir",))
                return nodes[rel]

            for rel in sorted(A.INDEX.folders, key=str.lower):
                ensure(rel)
            for n in sorted(names, key=str.lower):
                info = A.INDEX.files.get(n)
                folder = info.folder if info else (A.INDEX.records.get(n) or {}).get("folder_path") or ""
                self._row(tv, ensure(folder), n)
        # my check-outs
        tv = self.mine_tree
        tv.delete(*tv.get_children())
        for n in sorted(names, key=str.lower):
            if (A.INDEX.records.get(n) or {}).get("locked_by") == A.USER_NAME and matches(n):
                self._row(tv, "", n)
        # recent
        tv = self.recent_tree
        tv.delete(*tv.get_children())
        for n in self.recent:
            if n in names and matches(n) and not tv.exists("f:" + n):
                self._row(tv, "", n)
        if self.active_file and self.vault_tree.exists("f:" + self.active_file) and not q:
            self.vault_tree.see("f:" + self.active_file)

    def _dir_toggle(self, opened):
        iid = self.vault_tree.focus()
        if iid.startswith("d:"):
            (self.open_dirs.add if opened else self.open_dirs.discard)(iid[2:])

    def _on_pick(self, tv):
        sel = tv.selection()
        if not sel or not sel[0].startswith("f:"):
            return
        name = sel[0][2:]
        self.picked = None if name == self.active_file else name
        self.cam.update(rx=25.0, ry=45.0, zoom=1.0)
        self.update_card()

    def _on_double(self, tv, e):
        iid = tv.identify_row(e.y)
        if iid.startswith("f:"):
            self.open_vault_file(iid[2:])
            return "break"
        return None

    def _open_selected(self, tv):
        sel = tv.selection()
        if sel and sel[0].startswith("f:"):
            self.open_vault_file(sel[0][2:])

    def _tree_menu(self, tv, e):
        iid = tv.identify_row(e.y)
        if not iid.startswith("f:"):
            return
        tv.selection_set(iid)
        tv.focus(iid)
        name = iid[2:]
        key = A.file_state(name)[0]
        m = tk.Menu(self.root, tearoff=0, **self.menu_colors())
        m.add_command(label=f"Open in {APP.label}", command=lambda: self.open_vault_file(name))
        m.add_command(label="Check out & open", command=lambda: self.open_vault_file(name, "edit"),
                      state="normal" if key == "available" else "disabled")
        m.add_command(label="Open read-only copy", command=lambda: self.open_vault_file(name, "reference"),
                      state="normal" if name in A.INDEX.files else "disabled")
        m.add_separator()
        rec = A.INDEX.records.get(name) or {}
        pn = rec.get("part_number") or ""
        m.add_command(label=f"Copy part number  {pn}", command=lambda: self._copy(pn),
                      state="normal" if pn else "disabled")
        m.add_command(label="Show in Explorer", command=lambda: self._show_in_folder(name),
                      state="normal" if name in A.INDEX.files else "disabled")
        m.tk_popup(e.x_root, e.y_root)

    def _copy(self, text):
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.say(f"Copied {text}.", "ok")

    def _show_in_folder(self, name):
        info = A.INDEX.files.get(name)
        if not info:
            return
        if WIN:
            subprocess.Popen(["explorer", "/select,", os.path.normpath(info.path)])
        else:
            A.open_path(os.path.dirname(info.path))

    def _add_recent(self, name):
        self.recent = [name] + [r for r in self.recent if r != name][:14]

    # ------------------------------------------------------------------ vault refresh
    def refresh_vault(self):
        try:
            A.INDEX.refresh()
            if A.auto_number_enabled():
                cands = A.auto_number_candidates()
                open_name = ((self.doc or {}).get("name") or "").lower()
                # never rename the design SolidCraft has open; it would save back under the old name
                if cands and not any(os.path.splitext(c)[0].lower() == open_name for c in cands):
                    for lvl, msg in A.auto_number_sweep():
                        self.say(msg, lvl)
        except (OSError, sqlite3.Error) as e:
            self.say(f"Vault refresh failed: {e}", "error")
        self.last_full_refresh = time.time()
        self.active_file = self._map_active()
        if self.picked and self.picked not in self._sc_names():
            self.picked = None
        self.rebuild_lists()
        self.update_card()

    def _on_focus_in(self, _e=None):
        if time.time() - self.last_full_refresh > 60:
            self.refresh_vault()

    # ------------------------------------------------------------------ SolidCraft link
    def _worker(self):
        while self.running:
            self.poke.wait(timeout=self.poll_seconds)
            self.poke.clear()
            if not self.running:
                break
            if not self.link.connected and not self.link.connect():
                self.q.put(("doc", None, self.link.last_error))
                time.sleep(4)                       # don't hammer while SolidCraft is closed
                continue
            try:
                self.q.put(("doc", self.link.active_document(), ""))
            except LinkError as e:
                self.q.put(("doc", None, str(e)))

    def _map_active(self):
        names = self._sc_names()
        doc = self.doc
        if doc:
            path = doc.get("path")
            if path:
                base = os.path.basename(path)
                if base in names:
                    return base
                hit = match_by_name(names, os.path.splitext(base)[0], self._prefer_mine)
                if hit:
                    return hit
            hit = match_by_name(names, doc.get("name"), self._prefer_mine)
            if hit:
                return hit
            if doc.get("title"):
                return match_in_title(names, doc["title"])
            return None
        if not self.link.connected and self._last_title:
            return match_in_title(names, self._last_title)
        return None

    @staticmethod
    def _prefer_mine(n):
        return A.file_state(n)[0] in ("mine", "new")

    def _handle_doc(self, doc, err):
        before = (self.active_file, (self.doc or {}).get("dirty"), (self.doc or {}).get("name"))
        self.doc, self.doc_error = doc, err
        self.active_file = self._map_active()
        if self.active_file and self.active_file != before[0]:
            self._add_recent(self.active_file)
            self.cam.update(rx=25.0, ry=45.0, zoom=1.0)
            self.rebuild_lists()
        self._update_link_label()
        after = (self.active_file, (doc or {}).get("dirty"), (doc or {}).get("name"))
        if after != before:
            self.update_card()

    def _update_link_label(self):
        t = self.t
        if self.link.connected:
            how = "connected" if APP.has_control else "following window"
            text, col = f"{APP.label} · {how} · v{PANEL_VERSION}", t["success"]
        elif self.sc_hwnd:
            text, col = f"{APP.label} · control channel off", t["warning"]
        else:
            text, col = f"{APP.label} · not running", t["muted_fg"]
        self.link_label.configure(text=text)
        self.link_dot.configure(fg=col)

    def open_in_solidcraft(self, name, path):
        self.opened[name] = path
        self._add_recent(name)
        problem = None
        if APP.has_control and (self.link.connected or self.link.connect()):
            try:
                doc = self.link.active_document()
                if doc and doc.get("dirty"):
                    ans = messagebox.askyesnocancel(
                        "Unsaved changes",
                        f"{APP.label} has unsaved changes in '{doc.get('name') or 'the open design'}'.\n\n"
                        "Yes: save them, then open\nNo: open anyway\nCancel: don't open", parent=self.root)
                    if ans is None:
                        return
                    if ans:
                        self.link.save()
                self.link.open_file(path)
                self.picked = None
                self.poke.set()
                self.say(f"Opened '{name}' in {APP.label}.", "ok")
                return
            except LinkError as e:
                problem = str(e)
        exe = self.app_exe()
        try:
            if exe:
                subprocess.Popen([exe, os.path.abspath(path)])
            else:
                A.open_path(os.path.abspath(path))
        except OSError as e:
            self.say(f"Couldn't open '{name}': {e}", "error")
            messagebox.showerror(A.APP_NAME, f"Couldn't open '{name}' in {APP.label}:\n{e}", parent=self.root)
            return
        self.picked = None
        if APP.has_control:
            self.say(f"Opened '{name}' in a new {APP.label} window" + (f" (control channel: {problem})." if problem
                                                                       else "."), "warn" if problem else "ok")
        else:
            self.say(f"Opening '{name}' in {APP.label}.", "ok")
            self.poke.set()

    def reference_copy(self, name):
        info = A.INDEX.files.get(name)
        os.makedirs(self.ref_dir(), exist_ok=True)
        dst = os.path.join(self.ref_dir(), name)
        if os.path.exists(dst):
            os.chmod(dst, stat.S_IREAD | stat.S_IWRITE)
        shutil.copy2(info.path, dst)
        os.chmod(dst, stat.S_IREAD)
        return dst

    def open_vault_file(self, name, mode=None):
        key = A.file_state(name)[0]
        try:
            if mode == "reference":
                path = self.reference_copy(name)
            elif key in ("mine", "new"):
                path = A.sandbox_path(name)
                if not os.path.exists(path):
                    self.report(("error", f"Your sandbox copy of '{name}' is missing."), refresh=False)
                    return
            elif key == "lib":
                res = self._guard(A.check_out, name, "")
                self.report(res)
                if res[0] == "error":
                    return
                path = os.path.join(A.LOCAL_LIB_DIR, name)
            elif key == "available":
                edit = True if mode == "edit" else messagebox.askyesnocancel(
                    "Open from vault", f"Check out '{name}' so you can edit it?\n\n"
                    "Yes: check out and open\nNo: open a read-only copy", parent=self.root)
                if edit is None:
                    return
                if edit:
                    res = self._guard(A.check_out, name, "")
                    self.report(res)
                    if res[0] == "error":
                        return
                    path = A.sandbox_path(name)
                else:
                    path = self.reference_copy(name)
            elif key == "locked":
                owner = (A.INDEX.records.get(name) or {}).get("locked_by")
                messagebox.showinfo(A.APP_NAME, f"'{name}' is checked out by {owner}.\n\n"
                                    "Opening a read-only copy.", parent=self.root)
                path = self.reference_copy(name)
            else:
                self.report(("error", f"'{name}' can't be opened from the panel ({A.file_state(name)[1]})."),
                            refresh=False)
                return
        except OSError as e:
            self.report(("error", f"Couldn't prepare '{name}': {e}"), refresh=False)
            return
        self.open_in_solidcraft(name, path)

    # ------------------------------------------------------------------ actions
    def do_check_out(self):
        f = self.current()
        if not f:
            return
        res = self._guard(A.check_out, f, "")
        self.report(res)
        if res[0] == "error":
            return
        if self.opened.get(f, "").startswith(self.ref_dir()) and self.is_active(f):
            if messagebox.askyesno("Open editable copy", f"{APP.label} has the read-only copy of '{f}' open.\n\n"
                                   "Open your checked-out, editable copy instead?", parent=self.root):
                self.open_in_solidcraft(f, A.sandbox_path(f))

    def do_check_in(self):
        f = self.current()
        if not f:
            return
        if self.is_active(f):
            if not APP.has_control:
                dirty = bool((self.doc or {}).get("dirty"))
                q = (f"'{f}' has unsaved changes in {APP.label}.\n\nSave it there first (Ctrl+S), then click Yes."
                     if dirty else f"Make sure '{f}' is saved in {APP.label}.\n\nCheck in now?")
                if not messagebox.askyesno("Save first", q, icon="warning" if dirty else "question",
                                           parent=self.root):
                    return
            elif self.link.connected:
                try:
                    doc = self.link.active_document()
                    if doc and doc.get("dirty"):
                        self.link.save()
                        self.say(f"Saved in {APP.label}.", "ok")
                except LinkError as e:
                    if not messagebox.askyesno("Couldn't save", f"{APP.label} couldn't save '{f}':\n{e}\n\n"
                                               "Check in the last saved version anyway?", parent=self.root):
                        return
            elif not messagebox.askyesno("Save first", f"Make sure '{f}' is saved in {APP.label}.\n\n"
                                         "Check in now?", parent=self.root):
                return
        comment = A.run_form(self, "Check In", f"Check in {f}",
                             "Describe what changed. This goes in the audit trail.",
                             [("c", "Comment", False, "")], lambda v: v["c"].strip(),
                             "Check In", ok_style="Success.TButton", width=360)
        if comment is None:
            return
        self.root.config(cursor="watch")
        self.root.update_idletasks()
        try:
            self.report(self._guard(A.check_in, f, comment))
        finally:
            self.root.config(cursor="")

    def do_undo(self):
        f = self.current()
        if f and messagebox.askyesno("Undo Check-Out", f"Undo the check-out of '{f}'?\n\nYour sandbox copy moves "
                                     f"to _Discarded and nothing reaches the vault. Close it in {APP.label} "
                                     "without saving.", icon="warning", parent=self.root):
            self.report(self._guard(A.undo_check_out, f))

    def rev_menu(self):
        f = self.current()
        if not f:
            return
        curr = (A.INDEX.records.get(f) or {}).get("revision") or "A-01"
        m = tk.Menu(self.root, tearoff=0, **self.menu_colors())
        for bump, label in (("minor", "Minor"), ("major", "Major")):
            nxt = A.calculate_next_rev(curr, bump) or "?"
            m.add_command(label=f"{label}   {curr} → {nxt}", command=lambda b=bump: self.do_up_issue(b))
        b = self.b_rev
        m.tk_popup(b.winfo_rootx(), b.winfo_rooty() + b.winfo_height())

    def do_up_issue(self, bump):
        f = self.current()
        if not f:
            return
        curr = (A.INDEX.records.get(f) or {}).get("revision") or "A-01"
        default = A.calculate_next_rev(curr, bump) or ""

        def submit(v):
            nr, c = v["rev"].strip().upper(), v["c"].strip()
            if not A.REV_PATTERN.match(nr):
                return "error", "Use the format letter-number, e.g. A-02 or B-01."
            if nr != default and not c:
                return "error", "You changed the suggested revision, so add a comment."
            return self._guard(A.up_issue, f, nr, c)
        res = A.run_form(self, "Up-Issue", f"{'Major' if bump == 'major' else 'Minor'} up-issue",
                         f"{f}\nCurrent revision: {curr}",
                         [("rev", "New revision", False, default),
                          ("c", "Comment (required if you change the revision)", False, "")],
                         submit, "Up-Issue", width=360)
        if res:
            self.report(res)

    def do_export(self):
        f = self.current()
        if not f:
            return
        self.say(f"Exporting '{f}'…")
        self.root.config(cursor="watch")
        self.root.update_idletasks()
        try:
            results = A.export_deliverables(f)
        except (OSError, sqlite3.Error) as e:
            results = [("error", f"Export failed: {e}")]
        finally:
            self.root.config(cursor="")
        worst = "error" if any(l == "error" for l, _ in results) else (
            "warn" if any(l == "warn" for l, _ in results) else "ok")
        self.say(results[-1][1] if results else "Nothing exported.", worst)
        text = "\n\n".join(m for _, m in results)
        if worst == "error":
            messagebox.showerror("Export STEP & PDF", text, parent=self.root)
        else:
            messagebox.showinfo("Export STEP & PDF",
                                text + f"\n\nSaved to {getattr(A, 'OUTPUT_DIR', 'Manufacturing_Output')}",
                                parent=self.root)

    def do_history(self):
        f = self.current()
        if not f:
            return
        S, t = self.S, self.t
        dlg = A.ThemedDialog(self, f"History · {f}", "History", f, width=560, resizable=True)
        wrap = ttk.Frame(dlg.body, style="Dialog.TFrame")
        wrap.pack(fill="both", expand=True)
        cols = (("time", "Time", 130), ("user", "User", 90), ("action", "Action", 120), ("rev", "Rev", 50),
                ("c", "Comment", 220))
        tv = ttk.Treeview(wrap, columns=[c[0] for c in cols], show="headings", height=12)
        for key, label, wdt in cols:
            tv.heading(key, text=label, anchor="w")
            tv.column(key, width=S(wdt), anchor="w", stretch=key == "c")
        sb = ttk.Scrollbar(wrap, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=sb.set)
        tv.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        tv.tag_configure("odd", background=t["row_alt"])
        try:
            rows = A.get_audit(f)
        except sqlite3.Error as e:
            rows = []
            self.say(f"Couldn't read history: {e}", "error")
        for i, row in enumerate(rows):
            tv.insert("", "end", values=row, tags=("odd",) if i % 2 else ())
        if not rows:
            tv.insert("", "end", values=("—", "—", "No events yet", "—", "—"))
        ttk.Label(dlg.buttons, text="Baselines and restore are in the full app.",
                  style="CardMuted.TLabel").pack(side="left")
        ttk.Button(dlg.buttons, text="Close", command=dlg.cancel).pack(side="right")
        dlg.geometry(f"{S(640)}x{S(460)}")
        dlg.show()

    def do_save_props(self):
        f = self.current()
        if not f or "disabled" in self.b_save.state():
            return
        part = self.part_var.get().strip()
        if not part:
            self.say("Part name can't be blank.", "error")
            return
        rec = A.INDEX.records.get(f) or {}
        pp = A.parse_pn(os.path.splitext(f)[0])
        pn = rec.get("part_number") or (pp[3] if pp else "")
        self.root.focus_set()
        self.report(self._guard(A.save_metadata, f, part, pn, self.desc_var.get().strip()))

    def _rail_checkin(self):
        if not self.current() and self._can_save_to_vault():
            self.do_save_to_vault()
        else:
            self.do_check_in()

    def _register_new(self, filename, folder, pn, part, desc):
        """Records a file saved into the sandbox as a new vault file checked out to you."""
        with A.db() as c:
            c.execute("INSERT INTO files (filename, revision, locked_by, hash, is_library, folder_path, part_number, "
                      "part_name, description) VALUES (?, 'A-01', ?, '', 0, ?, ?, ?, ?)",
                      (filename, A.USER_NAME, folder, pn, part, desc))
        A.log_audit(filename, f"Created in {APP.label}", "A-01", f"Saved from the panel, P/N {pn}")

    def _wait_for_file(self, path, timeout=20.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if os.path.exists(path) and os.path.getsize(path) > 0:
                return True
            self.root.update()
            time.sleep(0.15)
        return os.path.exists(path)

    def do_save_to_vault(self):
        folders = self.numbered_folders()
        if not folders:
            self.report(("error", "There are no numbered Parts or Assemblies folders yet. Create a project "
                                  "with New Project in the full Atelier PDM app."), refresh=False)
            return
        labels = [f[0] for f in folders]
        by_label = {f[0]: f for f in folders}
        last = self.settings.get("panel_last_folder")
        default = next((f[0] for f in folders if f[1] == last), labels[0])
        doc = self.doc or {}
        S = self.S

        dlg = A.ThemedDialog(self, "Save to Vault", "Save to vault",
                             f"Gives the design open in {APP.label} the next part number and saves it into the "
                             "vault, checked out to you.", width=380)
        ttk.Label(dlg.body, text="Folder", style="CardMuted.TLabel").pack(anchor="w")
        fv = tk.StringVar(value=default)
        ttk.Combobox(dlg.body, textvariable=fv, values=labels, state="readonly").pack(fill="x", pady=(S(2), S(8)))
        pn_label = ttk.Label(dlg.body, text="", style="CardAccent.TLabel")
        pn_label.pack(anchor="w", pady=(0, S(8)))
        ttk.Label(dlg.body, text="Part name", style="CardMuted.TLabel").pack(anchor="w")
        nv = tk.StringVar(value=suggest_part_name(doc.get("name"), A.clean_part_name))
        ne = ttk.Entry(dlg.body, textvariable=nv)
        ne.pack(fill="x", pady=(S(2), S(8)), ipady=S(2))
        ttk.Label(dlg.body, text="Description (optional)", style="CardMuted.TLabel").pack(anchor="w")
        dv = tk.StringVar()
        ttk.Entry(dlg.body, textvariable=dv).pack(fill="x", pady=(S(2), S(8)), ipady=S(2))
        civ = tk.BooleanVar(value=bool(self.settings.get("panel_save_checkin", False)))
        ttk.Checkbutton(dlg.body, text="Check in straight away", variable=civ,
                        style="Dialog.TCheckbutton").pack(anchor="w")
        err = ttk.Label(dlg.body, text="", style="CardDanger.TLabel", wraplength=S(340))
        err.pack(anchor="w", pady=(S(6), 0))

        def preview(*_):
            ctx = by_label[fv.get()][2]
            try:
                nxt = A.peek_next_number(ctx["prefix"], ctx["kind"])
            except sqlite3.Error:
                nxt = None
            what = "Assembly" if ctx["kind"] == "A" else "Part"
            pn_label.configure(text=f"{what} number:  {nxt}" if nxt else "This number series is full.")
        fv.trace_add("write", preview)
        preview()

        def validate():
            name = nv.get().strip()
            if name.lower().endswith(APP.ext):
                name = name[:-len(APP.ext)].strip()
            if A.is_numbered_name(name):
                name = A.clean_part_name(name)
            problem = A.validate_new_name(name)
            if problem:
                err.configure(text=problem)
                return A.INVALID
            return by_label[fv.get()][1], by_label[fv.get()][2], name, dv.get().strip(), civ.get()
        dlg.validate = validate
        dlg.add_buttons("Save to Vault", "Success.TButton")
        dlg.focus_widget = ne
        res = dlg.show()
        if not res:
            return
        folder, ctx, part, desc, checkin_now = res
        self.settings["panel_last_folder"] = folder
        self.settings["panel_save_checkin"] = bool(checkin_now)

        try:
            pn = A.reserve_number(ctx["prefix"], ctx["kind"])
        except (ValueError, sqlite3.Error) as e:
            self.report(("error", f"Couldn't reserve a part number: {e}"), refresh=False)
            return
        filename = f"{pn} {part}{APP.ext}"
        path = A.sandbox_path(filename)
        if filename in A.INDEX.files or A.get_record(filename) or os.path.exists(path):
            self.report(("error", f"'{filename}' already exists. Number {pn} has been used up; try again."),
                        refresh=False)
            return
        os.makedirs(os.path.dirname(path), exist_ok=True)

        saved = False
        if APP.has_control and (self.link.connected or self.link.connect()):
            self.say(f"Saving as '{filename}'…")
            self.root.config(cursor="watch")
            self.root.update_idletasks()
            try:
                self.link.execute("file.save_as", {"path": path}, timeout=180)
                saved = self._wait_for_file(path, 10)
            except LinkError as e:
                self.say(f"{APP.label} couldn't save it automatically ({e}).", "warn")
            finally:
                self.root.config(cursor="")
        if not saved:
            saved = self._manual_save(filename, path)
        if not saved:
            A.log_audit(filename, "Number Unused", "", f"{pn} reserved but the design wasn't saved")
            self.say(f"Not saved. Part number {pn} was reserved and won't be reused.", "warn")
            return

        try:
            self._register_new(filename, folder, pn, part, desc)
        except sqlite3.Error as e:
            self.report(("error", f"Saved to {path}, but the vault record failed: {e}"), refresh=False)
            return
        self.opened[filename] = path
        self._add_recent(filename)
        self.picked = None
        self.save_settings()
        self.refresh_vault()
        self.poke.set()
        if checkin_now:
            self.report(self._guard(A.check_in, filename, f"First check-in from {APP.label}"))
        else:
            self.say(f"Saved as '{filename}' and checked out to you. Check in when you're ready.", "ok")

    def _manual_save(self, filename, path):
        """Fallback when SolidCraft can't be asked to save: copy the path and wait for the user to save there."""
        self.root.clipboard_clear()
        self.root.clipboard_append(path)
        S = self.S
        dlg = A.ThemedDialog(self, f"Save in {APP.label}", f"Save it in {APP.label}",
                             f"The panel couldn't save the design for you. In {APP.label} use File › Save As and "
                             "paste this path (it's on your clipboard):", width=420)
        e = ttk.Entry(dlg.body)
        e.insert(0, path)
        e.state(["readonly"])
        e.pack(fill="x", ipady=S(2))
        err = ttk.Label(dlg.body, text="", style="CardDanger.TLabel", wraplength=S(380))
        err.pack(anchor="w", pady=(S(6), 0))

        def validate():
            if os.path.exists(path):
                return True
            err.configure(text=f"The file isn't there yet. Save it in {APP.label} first.")
            return A.INVALID
        dlg.validate = validate
        dlg.add_buttons("I've saved it", "Success.TButton")
        return bool(dlg.show())

    def numbered_folders(self):
        out = []
        for rel in sorted(A.INDEX.folders, key=str.lower):
            ctx = A.numbering_context(rel)
            if ctx and ctx["kind"]:
                out.append((" › ".join(rel.split(os.sep)), rel, ctx))
        return out

    def do_new_part(self):
        try:
            templates = sorted(t for t in os.listdir(A.TEMPLATES_DIR)
                               if os.path.isfile(os.path.join(A.TEMPLATES_DIR, t)) and A.cad_app_of(t) == APP.key)
        except OSError:
            templates = []
        if not templates:
            self.report(("error", f"Add a {APP.label} template (for example Part{APP.ext} and Assembly{APP.ext}) to the "
                                  "_Templates folder first."), refresh=False)
            A.open_path(A.TEMPLATES_DIR)
            return
        folders = self.numbered_folders()
        if not folders:
            self.report(("error", "There are no numbered Parts or Assemblies folders yet. Create a project "
                                  "with New Project in the full Atelier PDM app."), refresh=False)
            return
        labels = [f[0] for f in folders]
        by_label = {f[0]: f for f in folders}
        cur = self.current()
        cur_folder = (A.INDEX.files[cur].folder if cur and cur in A.INDEX.files else None)
        last = self.settings.get("panel_last_folder")
        default = next((f[0] for f in folders if f[1] == cur_folder), None) or \
            next((f[0] for f in folders if f[1] == last), None) or labels[0]

        S = self.S
        dlg = A.ThemedDialog(self, "New Part", "New part",
                             f"Created in your sandbox, checked out to you and opened in {APP.label}. It joins the "
                             "vault when you first check it in.", width=380)
        ttk.Label(dlg.body, text="Folder", style="CardMuted.TLabel").pack(anchor="w")
        fv = tk.StringVar(value=default)
        ttk.Combobox(dlg.body, textvariable=fv, values=labels, state="readonly").pack(fill="x", pady=(S(2), S(8)))
        ttk.Label(dlg.body, text="Template", style="CardMuted.TLabel").pack(anchor="w")
        tv = tk.StringVar()
        ttk.Combobox(dlg.body, textvariable=tv, values=templates, state="readonly").pack(fill="x", pady=(S(2), S(8)))
        pn_label = ttk.Label(dlg.body, text="", style="CardAccent.TLabel")
        pn_label.pack(anchor="w", pady=(0, S(8)))
        ttk.Label(dlg.body, text="Part name", style="CardMuted.TLabel").pack(anchor="w")
        nv = tk.StringVar()
        ne = ttk.Entry(dlg.body, textvariable=nv)
        ne.pack(fill="x", pady=(S(2), 0), ipady=S(2))
        err = ttk.Label(dlg.body, text="", style="CardDanger.TLabel", wraplength=S(340))
        err.pack(anchor="w", pady=(S(6), 0))

        def pick_template(*_):
            kind = by_label[fv.get()][2]["kind"]
            words = ("asm", "assem") if kind == "A" else ("part", "prt")
            if not tv.get() or not any(w in tv.get().lower() for w in words):
                tv.set(next((x for x in templates if any(w in x.lower() for w in words)), templates[0]))

        def preview(*_):
            ctx = by_label[fv.get()][2]
            try:
                nxt = A.peek_next_number(ctx["prefix"], ctx["kind"])
            except sqlite3.Error:
                nxt = None
            what = "Assembly" if ctx["kind"] == "A" else "Part"
            pn_label.configure(text=f"{what} number:  {nxt}" if nxt else "This number series is full.")

        fv.trace_add("write", lambda *_: (pick_template(), preview()))
        pick_template()
        preview()

        def validate():
            folder = by_label[fv.get()][1]
            res = self._guard(A.create_from_template, tv.get(), nv.get().strip(), folder)
            if res[0] == "error":
                err.configure(text=res[1])
                return A.INVALID
            return res + (folder,) if len(res) == 3 else res
        dlg.validate = validate
        dlg.add_buttons("Create & Open", "Success.TButton")
        dlg.focus_widget = ne
        res = dlg.show()
        if not res:
            return
        if len(res) >= 4:
            self.settings["panel_last_folder"] = res[3]
        self.report(res[:2])
        if len(res) >= 3:
            self.open_in_solidcraft(res[2], A.sandbox_path(res[2]))

    # ------------------------------------------------------------------ finding the CAD program
    def app_exe(self):
        """Path to the CAD program, or None. Remembers what it finds (per PC, per program)."""
        saved = self.settings.get("panel_exe")
        if saved and os.path.isfile(saved):
            return saved
        cfg = (getattr(A, "CONFIG", {}) or {}).get("cad_apps") or {}
        exe = (cfg.get(APP.key) or {}).get("exe")
        if exe and os.path.isfile(exe):
            return exe
        try:
            found = A.cad_launcher(APP.key)
        except Exception:                                    # noqa: BLE001
            found = None
        if not found:
            found = next((p for p in candidate_exe_paths(APP.key, APP.label) if os.path.isfile(p)), None)
        if found:
            self._save_exe(found)
        return found

    def _save_exe(self, path):
        path = os.path.normpath(path)
        if self.settings.get("panel_exe") != path:
            self.settings["panel_exe"] = path
            self.save_settings()

    def _remember_exe(self, hwnd):
        if getattr(self, "_exe_remembered", False):
            return
        self._exe_remembered = True
        saved = self.settings.get("panel_exe")
        if saved and os.path.isfile(saved):
            return
        path = exe_path_of_window(hwnd)
        if path and os.path.isfile(path):
            self._save_exe(path)

    def locate_app_exe(self):
        from tkinter import filedialog
        start = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs")
        path = filedialog.askopenfilename(
            parent=self.root, title=f"Locate {APP.label}.exe",
            initialdir=start if os.path.isdir(start) else None,
            filetypes=[(f"{APP.label}", "*.exe"), ("All files", "*.*")])
        if not path:
            return None
        self._save_exe(path)
        self.say(f"{APP.label} found: {os.path.normpath(path)}", "ok")
        return path

    def launch_solidcraft(self):
        exe = self.app_exe()
        if not exe:
            if not messagebox.askyesno(A.APP_NAME, f"{APP.label} wasn't found in the usual places.\n\n"
                                       f"Locate {APP.label}.exe now? It's remembered on this PC.",
                                       parent=self.root):
                return
            exe = self.locate_app_exe()
            if not exe:
                return
        try:
            subprocess.Popen([exe])
            self.say(f"Starting {APP.label}…")
        except OSError as e:
            self.report(("error", f"Couldn't start {APP.label}: {e}"), refresh=False)

    def open_full_app(self):
        script = os.path.join(HERE, "atelier_pdm.py")
        exe = sys.executable
        if WIN:
            pw = os.path.join(os.path.dirname(exe), "pythonw.exe")
            exe = pw if os.path.exists(pw) else exe
        try:
            subprocess.Popen([exe, script], cwd=HERE)
        except OSError as e:
            self.say(f"Couldn't start Atelier PDM: {e}", "error")

    # ------------------------------------------------------------------ docking
    def _set_decorated(self, flag):
        if self._decorated == flag:
            return
        self._decorated = flag
        self._last_geom = None
        try:
            self.root.withdraw()
            self.root.overrideredirect(not flag)
            # docked: allow the slim strip; floating: keep a usable minimum size
            self.root.minsize(*((self.S(280), self.S(420)) if flag else (1, 1)))
            self.root.deiconify()
        except tk.TclError:
            pass

    def _set_topmost(self, flag):
        if self._topmost != flag:
            self._topmost = flag
            try:
                self.root.attributes("-topmost", flag)
            except tk.TclError:
                pass

    def _ensure_focus(self, e):
        if not WIN or self._decorated is not False:
            return
        try:
            if foreground_pid() != os.getpid():
                w = e.widget
                self.root.focus_force()
                if isinstance(w, (ttk.Entry, tk.Entry)):
                    self.root.after_idle(w.focus_set)
        except (tk.TclError, AttributeError):
            pass

    def _place_initial(self):
        geom = self.settings.get("panel_float_geom")
        self._set_decorated(True)
        self.root.geometry(geom or f"{self.S(self.WIDTHS[self.width_var.get()])}x{self.S(820)}")
        self.root.deiconify()

    def set_mode(self):
        self.settings["panel_mode"] = self.mode_var.get()
        self.save_settings()
        if self.mode_var.get() == "overlay":
            self.say("Overlay: drag the panel's edges to size it, or its header to move it.", "ok")
        if self.mode_var.get() == "float":
            self._restore_solidcraft()
            self._show_rail(False)
            self._set_decorated(True)
            self._set_topmost(False)
        self._last_geom = None

    def set_width(self):
        self.settings["panel_width"] = self.width_var.get()
        self.redock()

    def redock(self):
        if self.sc_hwnd:
            self.shrunk.pop(self.sc_hwnd, None)
            self.reserved.pop(self.sc_hwnd, None)
        self._last_geom = None

    def _restore_solidcraft(self):
        for h, (was_max, rect) in list(self.shrunk.items()):
            if is_window(h):
                if was_max:
                    show_window(h, SW_MAXIMIZE)
                elif rect:
                    set_visible_rect(h, *rect)
        self.shrunk.clear()
        self.reserved.clear()

    def _fit_solidcraft(self, h, ext, work, reserve):
        """Keeps `reserve` pixels free on SolidCraft's right. Only resizes it the first time, or when the reserve
        changes (auto-hide on/off, width change). Never touches a maximised or full-screen window afterwards."""
        limit = work[2] - reserve
        if h not in self.shrunk:
            was_max = is_zoomed(h)
            self.shrunk[h] = (was_max, None if was_max else ext)
            self.reserved[h] = reserve
            if was_max:
                show_window(h, SW_RESTORE)
                set_visible_rect(h, work[0], work[1], limit, work[3])
                return window_rect(h) or ext
            if ext[2] > limit:
                set_visible_rect(h, max(work[0], min(ext[0], limit - self.S(900))), ext[1], limit, ext[3])
                return window_rect(h) or ext
            return ext
        prev = self.reserved.get(h)
        if prev != reserve and not is_zoomed(h):
            touching = prev is not None and abs(ext[2] - (work[2] - prev)) <= 3
            if ext[2] > limit or touching:
                set_visible_rect(h, max(work[0], min(ext[0], limit - self.S(300))), ext[1], limit, ext[3])
                ext = window_rect(h) or ext
        self.reserved[h] = reserve
        return ext

    def _track_window(self):
        if not WIN:
            return
        self._tick_n += 1
        h = self.sc_hwnd if (self.sc_hwnd and is_window(self.sc_hwnd)) else None
        if h is None and self._tick_n % 3 == 0:
            h = find_window(self.exe_names)
        if h != self.sc_hwnd and h is not None:
            self._last_geom = None
        if h is None and self.sc_hwnd is not None:
            self.sc_hwnd = None
            self._update_link_label()
        if h is None:
            if self.sc_seen and self.args.launch:
                self._gone_ticks += 1
                if self._gone_ticks > 8:
                    self.quit()
                return
            if self.mode_var.get() != "float":
                self._show_rail(False)
                self._set_decorated(True)
                self._set_topmost(False)
            return
        self._gone_ticks = 0
        if self.sc_hwnd is None:
            self.sc_hwnd = h
            self._update_link_label()
        self.sc_seen = True
        self._remember_exe(h)

        title = window_title(h)
        if title != self._last_title:
            self._last_title = title
            self.poke.set()
            if not self.link.connected:
                self._handle_doc(None, self.doc_error)
        fg = foreground_pid()
        self._sc_pid = pid_of(h)
        front = fg in (self._sc_pid, os.getpid())
        if front and not self._last_front:
            self.poke.set()
        self._last_front = front

        mode = self.mode_var.get()
        if mode == "float":
            return
        if is_iconic(h):
            if self.root.state() != "withdrawn":
                self.root.withdraw()
            return
        if self.root.state() == "withdrawn":
            self.root.deiconify()
            self._last_geom = None
        self._set_decorated(False)
        if front:
            self._front_t = time.monotonic()
        steady = front or time.monotonic() - getattr(self, "_front_t", 0.0) < 0.6   # ignore a moment's glitch
        self._set_topmost(steady or (self.rail_shown and self.expanded and not self.pinned()))

        ext, work, mon = window_rect(h), work_area(h), monitor_rect(h)
        if not ext or not work:
            return
        mon = mon or work
        # Full screen: covers the whole monitor. With a visible taskbar only full-screen windows can do that;
        # with an auto-hiding taskbar a maximised window also does, so tell them apart by the maximised flag.
        fullscreen = covers(ext, mon) and (not covers(work, mon, 0) or not is_zoomed(h))
        if fullscreen != self._fullscreen:
            self._fullscreen = fullscreen
            self._last_geom = None
        use_rail = bool(self.autohide_var.get()) or fullscreen
        self._show_rail(use_rail)
        pw = self.S(self.WIDTHS[self.width_var.get()])
        rail_w = self.S(self.EDGE) if self._edge_mode() else self.S(self.RAIL)   # space kept while tucked away
        if fullscreen:                                   # never resize a full-screen window: lay the strip over it
            right, y, hgt, bottom = mon[2], mon[1], mon[3] - mon[1], mon[3]
        elif mode == "dock":
            reserve = rail_w if use_rail else pw
            if is_zoomed(h) and h in self.shrunk:        # re-maximised by the user: sit over its right edge
                right = work[2]
            else:
                ext = self._fit_solidcraft(h, ext, work, reserve)
                right = min(ext[2] + reserve, work[2])
            y, hgt, bottom = ext[1], ext[3] - ext[1], work[3]
        else:
            if h in self.shrunk:                         # coming from Dock: give SolidCraft its space back
                self._restore_solidcraft()
                ext = window_rect(h) or ext
            self._ext = ext
            if self._drag:                               # the drag handler is in charge
                return
            right, y, hgt, _w = overlay_rect(ext, self.overlay, self.scale)
            bottom = ext[3]
        hgt = max(self.S(OVERLAY_MIN_H) if mode == "overlay" else self.S(380), min(hgt, bottom - y))
        self._anchor = (right, y, hgt)
        self._apply_geometry()

    def _tick(self):
        if not self.running:
            return
        try:
            while True:
                kind, payload, err = self.q.get_nowait()
                if kind == "doc":
                    self._handle_doc(payload, err)
        except queue.Empty:
            pass
        try:
            self._track_window()
        except (tk.TclError, OSError) as e:
            self.say(f"Window tracking: {e}", "warn")
        if self._tick_n % 70 == 0:                           # ~20 s: pick up other people's check-outs
            try:
                before = {k: v.get("locked_by") for k, v in A.INDEX.records.items()}
                A.INDEX.reload_records()
                if before != {k: v.get("locked_by") for k, v in A.INDEX.records.items()}:
                    self.rebuild_lists()
                    self.update_card()
            except sqlite3.Error:
                pass
        if not WIN:
            self._tick_n += 1
        self.root.after(300, self._tick)

    # ------------------------------------------------------------------ exit
    def quit(self):
        if not self.running:
            return
        self.running = False
        self.poke.set()
        try:
            self._restore_solidcraft()
        except OSError:
            pass
        self.settings["panel_recent"] = self.recent[:15]
        self.settings["panel_open_dirs"] = sorted(self.open_dirs)[:200]
        self.settings["panel_mode"] = self.mode_var.get()
        self.settings["panel_width"] = self.width_var.get()
        self.settings["panel_autohide"] = bool(self.autohide_var.get())
        self.settings["panel_hide_style"] = self.hide_style_var.get()
        self._hide_tip()
        if self._decorated:
            try:
                self.settings["panel_float_geom"] = self.root.geometry()
            except tk.TclError:
                pass
        self.save_settings()
        self.link.close()
        self.root.destroy()


def THEMES_OR_DEFAULT(name):
    return A.THEMES.get(name) or A.THEMES[A.DEFAULT_THEME]


# =============================================================================
# Entry point
# =============================================================================
_instance_socket = None


def main(argv=None):
    import argparse
    global _instance_socket
    global APP
    ap = argparse.ArgumentParser(description="Atelier PDM companion panel for SolidCraft and PhysCAD")
    ap.add_argument("--app", choices=sorted(APPS), default="solidcraft", help="CAD program to sit beside")
    ap.add_argument("--launch", action="store_true", help="start the CAD program and close when it closes")
    ap.add_argument("--float", action="store_true", help="floating window instead of docking")
    ap.add_argument("--port", type=int, default=None, help="SolidCraft control port (default: auto)")
    args = ap.parse_args(argv)
    APP = APPS[args.app]

    if A is None:
        r = tk.Tk()
        r.withdraw()
        messagebox.showerror("Atelier PDM panel", _ENGINE_ERROR)
        sys.exit(1)
    try:
        _instance_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        _instance_socket.bind(("127.0.0.1", APP.port))
    except OSError:
        r = tk.Tk()
        r.withdraw()
        messagebox.showinfo("Atelier PDM panel", f"The {APP.label} panel is already running.")
        sys.exit(0)

    A.enable_dpi_awareness()
    root = tk.Tk()
    root.withdraw()
    try:
        A.init_db()
    except (OSError, sqlite3.Error) as e:
        messagebox.showerror(A.APP_NAME, f"Could not open the vault database:\n{getattr(A, 'DB_PATH', '')}\n\n{e}")
        sys.exit(1)
    ctx = A.BootContext(root)
    style = ttk.Style(root)
    style.theme_use("clam")
    A.style_ttk(root, style, ctx.t, ctx.S)
    if not A.sign_in(ctx):
        sys.exit(0)
    A.ThemedDialog.show = _panel_dialog_show      # pop-ups beside the panel, on screen
    Panel(root, args)
    root.mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception:                                         # noqa: BLE001
        import traceback
        traceback.print_exc()
        if sys.stdin and sys.stdin.isatty():
            input("\n[ERROR] Press Enter to close...")
