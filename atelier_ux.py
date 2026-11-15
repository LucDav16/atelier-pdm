"""
Atelier PDM - usability (atelier_ux.py)  2.7.6
==============================================

* Logo: the 'Vault' design, drawn sharply at every size (header, panels, program icons).
* Dark title bars: windows use the theme's dark header colour for their title bar (Windows 11; Windows 10 gets
  the dark title bar).
* Help: F1, the toolbar Help button or the account menu open a Help window with the same content as the PDF
  manual - chapters on the left, search, and a button to open the PDF.
* Hover tips: commands that aren't self-explanatory show a short explanation after a moment (account menu ›
  Hover tips to turn them off).
* Drag and drop: drag files (or folders of them) from File Explorer onto the main window or a companion panel.
  Each one opens the Add to vault dialog - folder, the part number it will get, part name, description - like
  saving a new part. Dropping onto a folder in the folder list picks that folder.

Installed by atelier_cam.install(). Every piece falls back quietly if it can't be set up.
"""
import base64
import importlib.abc
import importlib.util
import os
import re
import shutil as _shutil
import sys
import tempfile
import weakref

E = None
WIN = sys.platform == "win32"
_IMGS = {}
_WINDOWS = weakref.WeakSet()
_DROP_REFS = []
STATE = {"header": None, "help": None, "app": None}


def _settings():
    try:
        return E.load_settings() or {}
    except Exception:                                           # noqa: BLE001
        return {}


def _theme():
    try:
        name = _settings().get("theme")
        return E.THEMES.get(name) or E.THEMES.get(E.DEFAULT_THEME) or {}
    except Exception:                                           # noqa: BLE001
        return {}


# =============================================================================
# Logo
# =============================================================================
def logo_image(widget, px):
    """PhotoImage of the Vault logo, closest prepared size to px pixels."""
    from _logo_pngs import LOGO_PNG
    import tkinter as tk
    size = min(LOGO_PNG, key=lambda s: (abs(s - px), -s))
    key = (str(widget.winfo_toplevel()), size)
    if key not in _IMGS:
        _IMGS[key] = tk.PhotoImage(master=widget, data=LOGO_PNG[size])
    return _IMGS[key]


def paint_logo(canvas, px, bg):
    try:
        img = logo_image(canvas, px)
    except Exception:                                           # noqa: BLE001
        return False
    canvas.delete("all")
    canvas.configure(bg=bg, width=px, height=px)
    canvas.create_image(px // 2, px // 2, image=img)
    return True


# =============================================================================
# Dark title bars
# =============================================================================
def _colorref(hexcol):
    c = hexcol.lstrip("#")
    r, g, b = int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
    return r | (g << 8) | (b << 16)


def title_bar(win, header=None, fg=None):
    if not WIN:
        return False
    try:
        import ctypes
        from ctypes import wintypes
        hwnd = int(win.wm_frame(), 16)
        if not hwnd:
            return False
        dwm = ctypes.windll.dwmapi
        dwm.DwmSetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
        on = ctypes.c_int(1)
        if dwm.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(on), 4) != 0:      # dark mode (Windows 10 20H1+)
            dwm.DwmSetWindowAttribute(hwnd, 19, ctypes.byref(on), 4)          # older Windows 10
        t = _theme()
        header = header or STATE["header"] or t.get("header_bg") or "#0f172a"
        fg = fg or t.get("header_fg") or "#e2e8f0"
        cap, txt = ctypes.c_uint(_colorref(header)), ctypes.c_uint(_colorref(fg))
        dwm.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(cap), 4)              # caption colour (Windows 11)
        dwm.DwmSetWindowAttribute(hwnd, 36, ctypes.byref(txt), 4)              # caption text (Windows 11)
        ctypes.windll.user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x0037)         # redraw the frame
        return True
    except Exception:                                           # noqa: BLE001
        return False


def retheme_all(header=None, fg=None):
    if header:
        STATE["header"] = header
    for w in list(_WINDOWS):
        try:
            if w.winfo_exists():
                title_bar(w, header, fg)
        except Exception:                                       # noqa: BLE001
            pass


def _hook_windows():
    import tkinter as tk
    if getattr(tk.Toplevel, "_atl_ux", False):
        return
    for cls in (tk.Tk, tk.Toplevel):
        orig = cls.__init__

        def init(self, *a, _orig=orig, **k):
            _orig(self, *a, **k)
            try:
                _WINDOWS.add(self)
                done = {"x": False}

                def on_map(e, w=self):
                    if e.widget is w and not done["x"]:
                        done["x"] = True
                        title_bar(w)
                self.bind("<Map>", on_map, add="+")
                if isinstance(self, tk.Tk):
                    self.bind_all("<F1>", lambda e: open_help(e.widget if not isinstance(e.widget, str) else None),
                                  add="+")
            except Exception:                                   # noqa: BLE001
                pass
        cls.__init__ = init
    tk.Toplevel._atl_ux = True


# =============================================================================
# Hover tips
# =============================================================================
def tips_on():
    return bool(_settings().get("hover_tips", True))


def tip_for(text):
    from atelier_help_text import TIPS
    t = " ".join(str(text or "").split()).lower()
    if not t:
        return None
    for key, tip in TIPS:
        if key.lower() in t:
            return tip
    return None


class _Tip:
    """text=None: the tip is looked up from the button's current label each time (labels can change)."""

    def __init__(self, widget, text=None):
        self.w, self.text, self.job, self.win = widget, text, None, None
        widget.bind("<Enter>", self.enter, add="+")
        widget.bind("<Leave>", self.leave, add="+")
        widget.bind("<ButtonPress>", self.leave, add="+")

    def enter(self, _e=None):
        self.cancel()
        if tips_on() and self.current():
            self.job = self.w.after(650, self.show)

    def current(self):
        if self.text:
            return self.text
        try:
            return tip_for(self.w.cget("text"))
        except Exception:                                       # noqa: BLE001
            return None

    def cancel(self):
        if self.job:
            try:
                self.w.after_cancel(self.job)
            except Exception:                                   # noqa: BLE001
                pass
            self.job = None

    def leave(self, _e=None):
        self.cancel()
        if self.win is not None:
            try:
                self.win.destroy()
            except Exception:                                   # noqa: BLE001
                pass
            self.win = None

    def show(self):
        import tkinter as tk
        self.job = None
        try:
            if not self.w.winfo_viewable():
                return
            t = _theme()
            bg, fg = t.get("panel_bg", "#1e2632"), t.get("fg", "#e6e9ee")
            line = t.get("accent", "#38bdf8")
            self.win = tw = tk.Toplevel(self.w)
            tw.overrideredirect(True)
            try:
                tw.attributes("-topmost", True)
            except tk.TclError:
                pass
            tw.configure(bg=line)
            text = self.current()
            if not text:
                tw.destroy()
                self.win = None
                return
            tk.Label(tw, text=text, bg=bg, fg=fg, font=("Segoe UI", 9), justify="left", wraplength=300,
                     padx=10, pady=6).pack(padx=(3, 1), pady=1)
            tw.update_idletasks()
            x = self.w.winfo_rootx()
            y = self.w.winfo_rooty() + self.w.winfo_height() + 6
            sw, sh = tw.winfo_screenwidth(), tw.winfo_screenheight()
            x = max(0, min(x, sw - tw.winfo_reqwidth() - 4))
            if y + tw.winfo_reqheight() > sh - 40:
                y = self.w.winfo_rooty() - tw.winfo_reqheight() - 6
            tw.geometry(f"+{x}+{y}")
        except Exception:                                       # noqa: BLE001
            self.win = None


def attach_tip(widget, text=None):
    if getattr(widget, "_atl_tip", None) is None:
        widget._atl_tip = _Tip(widget, text)
    return widget._atl_tip


def _hook_tips():
    """Every button gets a (cheap) tip watcher when it's created; a tip only shows if its label has one."""
    from tkinter import ttk
    for cls in (ttk.Button, ttk.Menubutton):
        if getattr(cls, "_atl_tips", False):
            continue
        orig = cls.__init__

        def init(self, *a, _orig=orig, **k):
            _orig(self, *a, **k)
            try:
                if tip_for(k.get("text", "")) or k.get("textvariable") is None:
                    attach_tip(self)
            except Exception:                                   # noqa: BLE001
                pass
        cls.__init__ = init
        cls._atl_tips = True


def toggle_tips(app=None):
    s = _settings()
    s["hover_tips"] = not s.get("hover_tips", True)
    E.save_settings(s)
    if app is not None and hasattr(app, "log"):
        app.log("Hover tips " + ("on." if s["hover_tips"] else "off."), "ok")


# =============================================================================
# Help window
# =============================================================================
def pdf_path():
    here = os.path.dirname(os.path.abspath(__file__))
    for base in filter(None, (getattr(sys, "_MEIPASS", None), here, os.path.join(here, "_internal"),
                              os.path.dirname(sys.executable))):
        for sub in ("docs", ""):
            p = os.path.join(base, sub, "Atelier PDM Manual.pdf")
            if os.path.isfile(p):
                return p
    return None


def _md(text):
    """[(text, tags)] from **bold** / `code` markup."""
    out = []
    for part in re.split(r"(\*\*[^*]+\*\*|`[^`]+`)", text):
        if not part:
            continue
        if part.startswith("**"):
            out.append((part[2:-2], ("b",)))
        elif part.startswith("`"):
            out.append((part[1:-1], ("code",)))
        else:
            out.append((part, ()))
    return out


def chapter_text(ch):
    """Plain text of a chapter (used for searching)."""
    title, blocks = ch
    bits = [title]
    for kind, val in blocks:
        bits += [" ".join(r) for r in val] if kind == "t" else [val]
    return re.sub(r"[*`]", "", "\n".join(bits))


CONTEXT = [("CamWindow", "CAM & Tools"), ("Panel", "Companion panels")]


def open_help(widget=None, topic=None):
    import tkinter as tk
    from tkinter import ttk
    from atelier_help_text import CHAPTERS
    if topic is None and widget is not None:
        try:
            top = widget.winfo_toplevel()
            name = type(top).__name__
            app = STATE.get("app")
            topic = next((t for k, t in CONTEXT if k in name), None)
            if topic is None and app is not None and type(app).__name__ == "Panel":
                topic = "Companion panels"
        except Exception:                                       # noqa: BLE001
            topic = None
    h = STATE.get("help")
    if h is not None:
        try:
            if h.winfo_exists():
                h.deiconify()
                h.lift()
                h.focus_force()
                if topic:
                    h.show_topic(topic)
                return h
        except Exception:                                       # noqa: BLE001
            pass
    root = widget.winfo_toplevel() if widget is not None else tk._default_root
    t = _theme()
    bg, panel, fg = t.get("bg", "#111827"), t.get("panel_bg", "#172033"), t.get("fg", "#e2e8f0")
    muted, acc = t.get("muted_fg", "#8a97ad"), t.get("accent", "#38bdf8")
    border = t.get("border", "#26324b")
    win = tk.Toplevel(root)
    win.title("Atelier PDM · Help")
    win.configure(bg=bg)
    sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
    w, hh = min(1100, sw - 60), min(760, sh - 100)
    win.geometry(f"{w}x{hh}+{(sw - w) // 2}+{max(0, (sh - hh) // 3)}")
    STATE["help"] = win

    top = tk.Frame(win, bg=t.get("header_bg", bg), padx=14, pady=10)
    top.pack(fill="x")
    lg = tk.Canvas(top, highlightthickness=0, bd=0)
    lg.pack(side="left")
    paint_logo(lg, 30, t.get("header_bg", bg))
    tk.Label(top, text="  Help", bg=t.get("header_bg", bg), fg=t.get("header_fg", fg),
             font=("Segoe UI Semibold", 13)).pack(side="left")
    q = tk.StringVar()
    ent = ttk.Entry(top, textvariable=q, width=34)
    ent.pack(side="right", ipady=2)
    tk.Label(top, text="Search ", bg=t.get("header_bg", bg), fg=t.get("header_muted", muted),
             font=("Segoe UI", 9)).pack(side="right")
    p = pdf_path()
    if p:
        ttk.Button(top, text="Open PDF manual", command=lambda: E.open_path(p)).pack(side="right", padx=(0, 14))

    body = tk.PanedWindow(win, orient="horizontal", bg=bg, sashwidth=6, bd=0)
    body.pack(fill="both", expand=True, padx=10, pady=10)
    left = tk.Frame(body, bg=panel)
    tv = ttk.Treeview(left, show="tree", selectmode="browse")
    tv.pack(fill="both", expand=True, padx=4, pady=4)
    body.add(left, minsize=220, width=260)
    right = tk.Frame(body, bg=panel)
    txt = tk.Text(right, wrap="word", bg=panel, fg=fg, relief="flat", bd=0, padx=22, pady=16,
                  font=("Segoe UI", 10), spacing1=2, spacing3=4, cursor="arrow", insertwidth=0,
                  selectbackground=t.get("sel_bg", border), highlightthickness=0)
    sb = ttk.Scrollbar(right, orient="vertical", command=txt.yview)
    txt.configure(yscrollcommand=sb.set)
    sb.pack(side="right", fill="y")
    txt.pack(fill="both", expand=True)
    body.add(right, minsize=400)
    txt.tag_configure("title", font=("Segoe UI Semibold", 17), foreground=fg, spacing3=10)
    txt.tag_configure("h", font=("Segoe UI Semibold", 12), foreground=acc, spacing1=14, spacing3=4)
    txt.tag_configure("b", font=("Segoe UI Semibold", 10))
    txt.tag_configure("code", font=("Consolas", 9), foreground=acc)
    txt.tag_configure("li", lmargin1=14, lmargin2=30)
    txt.tag_configure("note", lmargin1=12, lmargin2=12, rmargin=12, background=border, spacing1=6, spacing3=6)
    txt.tag_configure("th", font=("Segoe UI Semibold", 9), foreground=muted, lmargin1=8)
    txt.tag_configure("td", lmargin1=8, lmargin2=8)
    txt.tag_configure("rowsep", font=("Segoe UI", 3))
    txt.tag_configure("hit", background=t.get("warning", "#eab353"), foreground="#000000")

    def write(text, tags=()):
        for s, extra in _md(text):
            txt.insert("end", s, tuple(tags) + extra)

    def render(i):
        title, blocks = CHAPTERS[i]
        txt.configure(state="normal")
        txt.delete("1.0", "end")
        txt.insert("end", title + "\n", ("title",))
        n = 0
        for kind, val in blocks:
            if kind != "n":
                n = 0
            if kind == "h":
                txt.insert("end", val + "\n", ("h",))
            elif kind == "p":
                write(val + "\n")
            elif kind == "b":
                write("•  " + val + "\n", ("li",))
            elif kind == "n":
                n += 1
                write(f"{n}.  " + val + "\n", ("li",))
            elif kind in ("note", "warn"):
                write(("Note.  " if kind == "note" else "Important.  ") + val + "\n", ("note",))
            elif kind == "t":
                head, *rows = val
                txt.insert("end", "   ·   ".join(head).upper() + "\n", ("th",))
                for r in rows:
                    write(r[0], ("td", "b"))
                    for cell in r[1:]:
                        write("   —   " + cell, ("td",))
                    txt.insert("end", "\n", ("td",))
                txt.insert("end", "\n", ("rowsep",))
        txt.configure(state="disabled")
        highlight()
        txt.yview_moveto(0)

    def highlight():
        txt.tag_remove("hit", "1.0", "end")
        s = q.get().strip()
        if len(s) < 2:
            return
        start = "1.0"
        first = None
        while True:
            pos = txt.search(s, start, "end", nocase=True)
            if not pos:
                break
            end = f"{pos}+{len(s)}c"
            txt.tag_add("hit", pos, end)
            first = first or pos
            start = end
        if first:
            txt.see(first)

    def fill():
        tv.delete(*tv.get_children())
        s = q.get().strip().lower()
        for i, ch in enumerate(CHAPTERS):
            if not s or s in chapter_text(ch).lower():
                tv.insert("", "end", iid=str(i), text=f"  {i + 1}   {ch[0]}")
        kids = tv.get_children()
        if kids:
            tv.selection_set(kids[0])

    tv.bind("<<TreeviewSelect>>", lambda e: tv.selection() and render(int(tv.selection()[0])))
    q.trace_add("write", lambda *_: fill())

    def show_topic(name):
        q.set("")
        for i, ch in enumerate(CHAPTERS):
            if ch[0] == name and tv.exists(str(i)):
                tv.selection_set(str(i))
                tv.see(str(i))
    win.show_topic = show_topic
    fill()
    if topic:
        show_topic(topic)
    win.bind("<Escape>", lambda e: win.destroy())
    ent.focus_set()
    return win


# =============================================================================
# Drag and drop from File Explorer
# =============================================================================
def enable_drop(win, callback):
    """Accept files dropped from File Explorer onto a Tk window (Windows). callback(paths, x_root, y_root)."""
    if not WIN:
        return False
    try:
        import ctypes
        from ctypes import wintypes
        hwnd = int(win.wm_frame(), 16)
        shell32, user32, comctl = ctypes.windll.shell32, ctypes.windll.user32, ctypes.windll.comctl32
        LRESULT = ctypes.c_ssize_t
        PROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
                                  ctypes.c_size_t, ctypes.c_size_t)
        shell32.DragQueryFileW.argtypes = [wintypes.HANDLE, wintypes.UINT, ctypes.c_wchar_p, wintypes.UINT]
        shell32.DragQueryFileW.restype = wintypes.UINT
        shell32.DragFinish.argtypes = [wintypes.HANDLE]
        comctl.DefSubclassProc.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        comctl.DefSubclassProc.restype = LRESULT
        comctl.SetWindowSubclass.argtypes = [wintypes.HWND, PROC, ctypes.c_size_t, ctypes.c_size_t]

        def proc(h, msg, wp, lp, uid, ref):
            if msg == 0x0233:                                   # WM_DROPFILES
                try:
                    n = shell32.DragQueryFileW(wp, 0xFFFFFFFF, None, 0)
                    files = []
                    for i in range(n):
                        size = shell32.DragQueryFileW(wp, i, None, 0) + 1
                        buf = ctypes.create_unicode_buffer(size)
                        shell32.DragQueryFileW(wp, i, buf, size)
                        files.append(buf.value)
                    pt = wintypes.POINT()
                    user32.GetCursorPos(ctypes.byref(pt))
                    shell32.DragFinish(wp)
                    win.after(20, lambda: callback(files, pt.x, pt.y))
                except Exception:                               # noqa: BLE001
                    pass
                return 0
            return comctl.DefSubclassProc(h, msg, wp, lp)
        cb = PROC(proc)
        _DROP_REFS.append(cb)
        comctl.SetWindowSubclass(hwnd, cb, 0xA7E1, 0)
        shell32.DragAcceptFiles(hwnd, True)
        return True
    except Exception:                                           # noqa: BLE001
        return False


def expand_paths(paths):
    """Files to add: dropped files, plus the files directly inside dropped folders. (accepted, rejected).
    An STL with the same name as a part being added travels with that part instead of being added on its own."""
    exts = {e.lower() for e in E.VAULT_EXTS}
    items = []
    for p in paths:
        if os.path.isdir(p):
            items += [os.path.join(p, n) for n in sorted(os.listdir(p)) if os.path.isfile(os.path.join(p, n))]
        elif os.path.isfile(p):
            items.append(p)
    models = {os.path.splitext(f)[0].lower() for f in items if os.path.splitext(f)[1].lower() in E.CAD_MODEL_EXTS}
    ok, bad = [], []
    for f in dict.fromkeys(items):
        stem, ext = os.path.splitext(f)
        if ext.lower() == ".stl" and stem.lower() in models:
            continue
        (ok if ext.lower() in exts else bad).append(f)
    return ok, bad


def folder_choices():
    out = [E.VAULT_ROOT_LABEL]
    for rel in sorted(E.INDEX.folders, key=str.lower):
        if E.is_under(E.folder_abs(rel), E.HISTORY_DIR):
            continue
        out.append(rel)
    return out


def number_preview(folder, ext, stem):
    pp = E.parse_pn(stem)
    try:
        legacy = E.is_numbered_name(stem)
    except Exception:                                           # noqa: BLE001
        legacy = bool(pp)
    if pp or legacy:
        return f"Keeps its number: {pp[3] if pp else stem.split(' ')[0]}"
    nb = E.numbering_for_target(folder, ext)
    if nb:
        nxt = E.peek_next_number(*nb)
        return f"Part number:  {nxt}" if nxt and not str(nxt).startswith("—") else "Part number:  next in the series"
    if ext.lower() in E.CAD_MODEL_EXTS:
        return "Not numbered in this folder - choose a project's Parts or Assemblies folder to number it."
    return "This file type isn't numbered."


def add_one(src, folder, part, desc):
    """Copies src into the vault folder under the chosen name. Returns (level, message, vault file name)."""
    stem, ext = os.path.splitext(os.path.basename(src))
    pp = E.parse_pn(stem)
    try:
        numbered = E.is_numbered_name(stem)
    except Exception:                                           # noqa: BLE001
        numbered = bool(pp)
    num = pp[3] if pp else (stem.split(" ")[0] if numbered else "")
    new_stem = f"{num} {part}".strip() if num else part
    problem = E.validate_new_name(new_stem)
    if problem:
        return "error", problem, None
    work = E.LOCAL_WORKSPACE if os.path.isdir(E.LOCAL_WORKSPACE) else None
    tmpdir = tempfile.mkdtemp(prefix="drop_", dir=work)
    try:
        tmp = os.path.join(tmpdir, new_stem + ext)
        _shutil.copy2(src, tmp)
        stl = os.path.splitext(src)[0] + ".stl"
        if ext.lower() != ".stl" and os.path.isfile(stl):
            _shutil.copy2(stl, os.path.join(tmpdir, new_stem + ".stl"))
        results = E.add_files_to_vault([tmp], folder, comment=f"Dragged in from {src}")
    finally:
        _shutil.rmtree(tmpdir, ignore_errors=True)
    level, msg = results[0] if results else ("error", "Nothing was added.")
    if level != "ok":
        return level, msg.replace(new_stem + ext, os.path.basename(src)), None
    m = re.search(r" as '(.+?)' to ", msg)
    name = m.group(1) if m else new_stem + ext
    if desc:
        try:
            rec = E.get_record(name) or {}
            E.save_metadata(name, rec.get("part_name") or part, rec.get("part_number") or "", desc)
        except Exception:                                       # noqa: BLE001
            pass
    return "ok", f"Added {os.path.basename(src)} to the vault as {name}.", name


def add_dialog(ctx, path, folder, index=1, total=1):
    """The Add to vault dialog for one dropped file. Returns 'skip', 'cancel', or the vault file name."""
    import tkinter as tk
    from tkinter import ttk
    S = getattr(ctx, "S", lambda n: n)
    stem, ext = os.path.splitext(os.path.basename(path))
    folders = folder_choices()
    if folder not in folders:
        folder = folders[0]
    sub = os.path.basename(path) + (f"   ·   file {index} of {total}" if total > 1 else "")
    dlg = E.ThemedDialog(ctx, "Add to vault", "Add to vault", sub, width=460)
    ttk.Label(dlg.body, text="Folder", style="CardMuted.TLabel").pack(anchor="w")
    fv = tk.StringVar(value=folder)
    ttk.Combobox(dlg.body, textvariable=fv, values=folders, state="readonly").pack(fill="x", pady=(S(2), S(8)))
    pn = ttk.Label(dlg.body, text="", style="CardAccent.TLabel", wraplength=S(420), justify="left")
    pn.pack(anchor="w", pady=(0, S(8)))
    ttk.Label(dlg.body, text="Part name", style="CardMuted.TLabel").pack(anchor="w")
    nv = tk.StringVar(value=E.clean_part_name(stem))
    ne = ttk.Entry(dlg.body, textvariable=nv)
    ne.pack(fill="x", pady=(S(2), S(8)), ipady=S(2))
    ttk.Label(dlg.body, text="Description (optional)", style="CardMuted.TLabel").pack(anchor="w")
    dv = tk.StringVar()
    ttk.Entry(dlg.body, textvariable=dv).pack(fill="x", pady=(S(2), 0), ipady=S(2))
    err = ttk.Label(dlg.body, text="", style="CardDanger.TLabel", wraplength=S(420), justify="left")
    err.pack(anchor="w", pady=(S(6), 0))

    def upd(*_):
        try:
            pn.configure(text=number_preview(fv.get(), ext, stem))
        except Exception:                                       # noqa: BLE001
            pn.configure(text="")
    fv.trace_add("write", upd)
    upd()

    def validate():
        part = nv.get().strip()
        if part.lower().endswith(ext.lower()):
            part = part[:-len(ext)].strip()
        problem = E.validate_new_name(part)
        if problem:
            err.configure(text=problem)
            return E.INVALID
        level, msg, name = add_one(path, fv.get(), part, dv.get().strip())
        if level != "ok":
            err.configure(text=msg)
            return E.INVALID
        STATE["last_drop_folder"] = fv.get()
        return name

    def skip():
        dlg.result = "skip"
        dlg.destroy()
    if total > 1:
        ttk.Button(dlg.buttons, text="Skip", command=skip).pack(side="left")
    dlg.validate = validate
    dlg.add_buttons("Add to vault", "Success.TButton")
    dlg.focus_widget = ne
    res = dlg.show()
    return "cancel" if res is None else res


def handle_drop(ctx, paths, x_root, y_root):
    """Works out the target folder from where the files were dropped, then shows Add to vault for each."""
    files, rejected = expand_paths(paths)
    folder = None
    ft = getattr(ctx, "ftree", None)
    if ft is not None:
        try:
            if ctx.root.winfo_containing(x_root, y_root) is ft:
                iid = ft.identify_row(y_root - ft.winfo_rooty())
                if iid and iid != "*":
                    folder = iid[2:]
        except Exception:                                       # noqa: BLE001
            folder = None
    if folder is None:
        if hasattr(ctx, "target_folder"):
            folder = ctx.target_folder()
        else:
            folder = STATE.get("last_drop_folder") or getattr(ctx, "settings", {}).get("panel_last_folder") or \
                getattr(E, "PROJECT_ROOT_REL", E.VAULT_ROOT_LABEL)
    say = getattr(ctx, "log", None) or getattr(ctx, "say", None) or (lambda *a: None)
    if rejected:
        names = ", ".join(os.path.basename(r) for r in rejected[:4]) + ("…" if len(rejected) > 4 else "")
        say(f"Not added (not a CAD, drawing, STEP, STL or PDF file): {names}", "warn")
    added = []
    for i, f in enumerate(files, 1):
        res = add_dialog(ctx, f, folder, i, len(files))
        if res == "cancel":
            break
        if res != "skip":
            added.append(res)
            folder = STATE.get("last_drop_folder", folder)
    if added:
        try:
            E.INDEX.refresh()
        except Exception:                                       # noqa: BLE001
            pass
        if hasattr(ctx, "selected"):
            ctx.selected = added[-1]
        for name in ("refresh_all", "refresh_vault"):
            fn = getattr(ctx, name, None)
            if fn:
                fn()
                break
        say(f"Added {len(added)} file(s) to the vault: " + ", ".join(added[:3]) + ("…" if len(added) > 3 else ""),
            "ok")
    return added


# =============================================================================
# Hooks
# =============================================================================
def _hook_app(module):
    cls = getattr(module, "AtelierApp", None)
    if cls is None or getattr(cls, "_atl_ux", False):
        return
    cls._atl_ux = True

    orig_logo = getattr(cls, "draw_logo", None)
    if orig_logo:
        def draw_logo(self):
            if not paint_logo(self.logo, self.S(30), self.t["header_bg"]):
                orig_logo(self)
        cls.draw_logo = draw_logo

    orig_apply = getattr(cls, "apply_theme", None)
    if orig_apply:
        def apply_theme(self, *a, **k):
            res = orig_apply(self, *a, **k)
            retheme_all(self.t.get("header_bg"), self.t.get("header_fg"))
            return res
        cls.apply_theme = apply_theme

    orig_build = getattr(cls, "build_ui", None)
    if orig_build:
        def build_ui(self, *a, **k):
            res = orig_build(self, *a, **k)
            STATE["app"] = self
            try:
                from tkinter import ttk
                ttk.Button(self.toolbar, text="❓  Help", style="Tool.TButton",
                           command=lambda: open_help(self.root)).pack(side="right", padx=(0, 4))
            except Exception:                                   # noqa: BLE001
                pass
            self.root.after(400, lambda: enable_drop(self.root, lambda p, x, y: handle_drop(self, p, x, y)))
            return res
        cls.build_ui = build_ui

    orig_ident = getattr(cls, "update_identity", None)
    if orig_ident:
        def update_identity(self, *a, **k):
            res = orig_ident(self, *a, **k)
            try:
                m = self.account_menu
                m.add_separator()
                m.add_command(label="❓  Help   F1", command=lambda: open_help(self.root))
                if pdf_path():
                    m.add_command(label="📄  PDF manual", command=lambda: E.open_path(pdf_path()))
                m.add_command(label=("✓ " if tips_on() else "    ") + "Hover tips", command=lambda: toggle_tips(self))
            except Exception:                                   # noqa: BLE001
                pass
            return res
        cls.update_identity = update_identity


def _patch_panel(mod):
    cls = getattr(mod, "Panel", None)
    if cls is None or getattr(cls, "_atl_ux", False):
        return
    cls._atl_ux = True
    orig_logo = getattr(cls, "_draw_logo", None)
    if orig_logo:
        def _draw_logo(self):
            if not paint_logo(self.logo, self.S(22), self.t["header_bg"]):
                orig_logo(self)
        cls._draw_logo = _draw_logo
    orig_rail = getattr(cls, "_theme_rail", None)
    if orig_rail:
        def _theme_rail(self, *a, **k):
            res = orig_rail(self, *a, **k)
            try:
                paint_logo(self.rail_logo, self.S(20), self.t["header_bg"])
            except Exception:                                   # noqa: BLE001
                pass
            return res
        cls._theme_rail = _theme_rail
    orig_apply = getattr(cls, "apply_theme", None)
    if orig_apply:
        def apply_theme(self, *a, **k):
            res = orig_apply(self, *a, **k)
            retheme_all(self.t.get("header_bg"), self.t.get("header_fg"))
            return res
        cls.apply_theme = apply_theme
    orig_init = cls.__init__

    def __init__(self, *a, **k):
        orig_init(self, *a, **k)
        STATE["app"] = self
        try:
            self.root.after(600, lambda: enable_drop(self.root, lambda p, x, y: handle_drop(self, p, x, y)))
        except Exception:                                       # noqa: BLE001
            pass
    cls.__init__ = __init__
    orig_menu = getattr(cls, "build_menu", None)
    if orig_menu:
        def build_menu(self, *a, **k):
            res = orig_menu(self, *a, **k)
            try:
                self.menu.add_separator()
                self.menu.add_command(label="❓  Help   F1", command=lambda: open_help(self.root, "Companion panels"))
            except Exception:                                   # noqa: BLE001
                pass
            return res
        cls.build_menu = build_menu


class _Wrap(importlib.abc.Loader):
    def __init__(self, inner):
        self.inner = inner

    def create_module(self, spec):
        return self.inner.create_module(spec) if hasattr(self.inner, "create_module") else None

    def exec_module(self, module):
        self.inner.exec_module(module)
        try:
            _patch_panel(module)
        except Exception:                                       # noqa: BLE001
            pass


class _PanelFinder(importlib.abc.MetaPathFinder):
    """Patches atelier_panel as soon as it's imported (it's only imported when a panel is started)."""

    def find_spec(self, name, path, target=None):
        if name != "atelier_panel":
            return None
        try:
            sys.meta_path.remove(self)
        except ValueError:
            pass
        try:
            spec = importlib.util.find_spec(name)
        except Exception:                                       # noqa: BLE001
            spec = None
        if spec is None or spec.loader is None:
            return None
        spec.loader = _Wrap(spec.loader)
        return spec


def install(module):
    global E
    E = module
    if getattr(module, "_ux_installed", False):
        return
    module._ux_installed = True
    module.open_help = open_help
    for fn in (_hook_windows, _hook_tips):
        try:
            fn()
        except Exception:                                       # noqa: BLE001
            pass
    _hook_app(module)
    if "atelier_panel" in sys.modules:
        _patch_panel(sys.modules["atelier_panel"])
    else:
        sys.meta_path.insert(0, _PanelFinder())
