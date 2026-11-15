"""
Atelier PDM 2.9.5 (atelier_fix295.py) - SolidCraft and PhysCAD panels: more room for the vault
==============================================================================================

* Smaller action buttons: Check out, Check in, Undo, Revision, Export, History and New sit in two compact rows
  (hover for the full name), so the vault browser below gets the space.
* Vault browser works like File Explorer: it shows one folder at a time. Click a folder to open it;
  ‹ goes back, ↑ goes up a level, and the path above the list shows where you are (click a part of it to jump
  there). Backspace goes up, Alt+Left goes back. Typing in the search box still searches every folder.
  The folder you were in is remembered.
"""
import os
import sys
import time
import traceback

VERSION = "2.9.5"
E = None
ORIG = {}
SHORT = {  # attribute -> (short label, tooltip)
    "b_checkout": ("↓ Check out", "Check out - lock it to you so you can edit it"),
    "b_checkin": ("↑ Check in", "Save & check in - saves in the CAD program, then puts it back in the vault"),
    "b_undo": ("↺ Undo", "Undo check-out - release your lock without changing the vault"),
    "b_rev": ("Rev ▾", "Revision - minor or major up-issue"),
    "b_export": ("⇪ Export", "Export STEP & PDF"),
    "b_history": ("☰ History", "History of this file"),
    "b_new": ("＋ New", "New part from a template"),
}
ORDER = (("b_checkout", 0, 0), ("b_checkin", 0, 1), ("b_undo", 0, 2), ("b_rev", 0, 3),
         ("b_export", 1, 0), ("b_history", 1, 1), ("b_new", 1, 2))


def log_error(where):
    try:
        base = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "Atelier PDM")
        os.makedirs(base, exist_ok=True)
        with open(os.path.join(base, "atelier_error.log"), "a", encoding="utf-8") as f:
            f.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} · {where} ({VERSION}) =====\n"
                    f"{traceback.format_exc()}\n")
    except Exception:                                           # noqa: BLE001
        pass


def _A():
    return getattr(sys.modules.get("atelier_panel"), "A", None) or E


# =============================================================================
# 1. Compact action buttons
# =============================================================================
def compact_actions(self):
    S = self.S
    act = self.b_checkout.master
    for name, _r, _c in ORDER:
        b = getattr(self, name, None)
        if b is not None:
            b.grid_forget()
    for c in range(4):
        act.columnconfigure(c, weight=1, uniform="a")
    for name, r, c in ORDER:
        b = getattr(self, name, None)
        if b is None:
            continue
        style = str(b.cget("style") or "TButton")
        base = style.split(".", 1)[1] if "." in style and style.startswith("Act") else style
        b.configure(text=SHORT[name][0], style="Act" + ("." + base if base != "TButton" else ".TButton"))
        b.grid(row=r, column=c, sticky="ew", padx=S(1), pady=S(1))
        _tip(self, b, SHORT[name][1])


def _tip(self, widget, text):
    tip = getattr(sys.modules.get("atelier_ux"), "attach_tip", None)
    if tip:
        try:
            tip(widget, text)
        except Exception:                                       # noqa: BLE001
            pass


def style_actions(self):
    s, S = self.style, self.S
    for base in ("TButton", "Accent.TButton", "Success.TButton"):
        name = "Act." + base
        try:
            for opt in ("background", "foreground", "bordercolor", "lightcolor", "darkcolor"):
                v = s.lookup(base, opt)
                if v:
                    s.configure(name, **{opt: v})
            m = s.map(base)
            if m:
                s.map(name, **m)
        except Exception:                                       # noqa: BLE001
            pass
        s.configure(name, padding=(S(2), S(3)), font=("Segoe UI", 8))
    try:
        s.layout("Act.TButton", s.layout("TButton"))
        s.layout("Act.Accent.TButton", s.layout("Accent.TButton"))
        s.layout("Act.Success.TButton", s.layout("Success.TButton"))
    except Exception:                                           # noqa: BLE001
        pass


def set_states(self, f, key):
    res = ORIG["states"](self, f, key)
    try:
        self.b_checkout.configure(text="↓ Sync lib" if key == "lib" else SHORT["b_checkout"][0])
    except Exception:                                           # noqa: BLE001
        pass
    return res


# =============================================================================
# 2. Explorer-style vault browser
# =============================================================================
def norm(rel):
    A = _A()
    rel = (rel or "").strip()
    if rel in ("", ".", getattr(A, "VAULT_ROOT_LABEL", "Vault Root")):
        return ""
    return os.path.normpath(rel)


def parent_of(rel):
    rel = norm(rel)
    p = os.path.dirname(rel)
    return norm(p)


def children(rel, folders, files_by_folder):
    """(sub-folders, files) directly in rel."""
    rel = norm(rel)
    subs = sorted({norm(f) for f in folders if parent_of(f) == rel and norm(f) != rel and norm(f)},
                  key=lambda x: os.path.basename(x).lower())
    return subs, sorted(files_by_folder.get(rel, []), key=str.lower)


def file_folder(name):
    A = _A()
    info = A.INDEX.files.get(name)
    return norm(info.folder if info else (A.INDEX.records.get(name) or {}).get("folder_path") or "")


def go(self, rel, push=True):
    rel = norm(rel)
    A = _A()
    if rel and rel not in {norm(f) for f in A.INDEX.folders}:
        rel = ""
    cur = norm(getattr(self, "_cwd", ""))
    if push and rel != cur:
        self._back = (getattr(self, "_back", []) + [cur])[-50:]
    self._cwd = rel
    self.settings["panel_cwd"] = rel
    try:
        self.save_settings()
    except Exception:                                           # noqa: BLE001
        pass
    self.rebuild_lists()


def go_back(self):
    back = getattr(self, "_back", [])
    if back:
        go(self, back.pop(), push=False)


def go_up(self):
    cur = norm(getattr(self, "_cwd", ""))
    if cur:
        go(self, parent_of(cur))


def fill_vault(self):
    """Replaces the tree in the Vault tab with the contents of the current folder."""
    A = _A()
    tv = self.vault_tree
    tv.delete(*tv.get_children())
    names = self._sc_names()
    by_folder = {}
    for n in names:
        by_folder.setdefault(file_folder(n), []).append(n)
    cwd = norm(getattr(self, "_cwd", ""))
    subs, files = children(cwd, A.INDEX.folders, by_folder)
    icon_fn = getattr(getattr(A, "AtelierApp", None), "_folder_icon", None)
    for rel in subs:
        leaf = os.path.basename(rel)
        try:
            icon = icon_fn(rel, leaf)[0] if icon_fn else "📁"
        except Exception:                                       # noqa: BLE001
            icon = "📁"
        inside = sum(1 for k, v in by_folder.items() for _ in v if k == rel or k.startswith(rel + os.sep))
        tv.insert("", "end", iid="d:" + rel, text=f"{icon}  {leaf}", tags=("dir",),
                  values=(str(inside) if inside else "", "›"))
    for n in files:
        self._row(tv, "", n)
    if not subs and not files:
        tv.insert("", "end", iid="x:empty", text="   (empty folder)", tags=("dir",))
    update_nav(self)


def update_nav(self):
    nav = getattr(self, "_nav", None)
    if not nav:
        return
    back_b, up_b, crumbs = nav
    q = self.search_var.get().strip()
    cwd = norm(getattr(self, "_cwd", ""))
    back_b.state(["!disabled"] if (getattr(self, "_back", []) and not q) else ["disabled"])
    up_b.state(["!disabled"] if (cwd and not q) else ["disabled"])
    for w in crumbs.winfo_children():
        w.destroy()
    import tkinter as tk
    t, S = self.t, self.S
    bg = self.style.lookup("Card.TFrame", "background") or t.get("panel_bg")
    if q:
        tk.Label(crumbs, text="Search results - all folders", bg=bg, fg=t["muted_fg"],
                 font=("Segoe UI", 8)).pack(side="left")
        return
    parts = [("Vault", "")]
    acc = ""
    for p in (cwd.split(os.sep) if cwd else []):
        acc = os.path.join(acc, p) if acc else p
        parts.append((p, acc))
    shown = parts if len(parts) <= 3 else [parts[0], ("…", None)] + parts[-2:]
    for i, (label, rel) in enumerate(shown):
        last = i == len(shown) - 1
        lb = tk.Label(crumbs, text=label, bg=bg, fg=t["fg"] if last else t["accent"],
                      font=("Segoe UI Semibold" if last else "Segoe UI", 8), cursor="" if (last or rel is None)
                      else "hand2")
        lb.pack(side="left")
        if not last and rel is not None:
            lb.bind("<Button-1>", lambda e, r=rel: go(self, r))
        if not last:
            tk.Label(crumbs, text=" › ", bg=bg, fg=t["muted_fg"], font=("Segoe UI", 8)).pack(side="left")


def rebuild_lists(self, *a, **k):
    res = ORIG["rebuild"](self, *a, **k)
    try:
        if not self.search_var.get().strip():
            fill_vault(self)
        else:
            update_nav(self)
    except Exception:                                           # noqa: BLE001
        log_error("vault browser")
    return res


def _row_click(self, e):
    tv = self.vault_tree
    iid = tv.identify_row(e.y)
    if iid.startswith("d:"):
        go(self, iid[2:])
        return "break"
    return None


def _on_double(self, tv, e):
    if tv is getattr(self, "vault_tree", None) and tv.identify_row(e.y).startswith("d:"):
        return "break"
    return ORIG["double"](self, tv, e)


def build_nav(self):
    import tkinter as tk
    from tkinter import ttk
    S = self.S
    tv = self.vault_tree
    host = tv.master
    bar = ttk.Frame(host, style="Card.TFrame")
    bar.pack(side="top", fill="x", pady=(S(2), S(3)), before=tv)
    back_b = ttk.Button(bar, text="‹", width=2, style="Act.TButton", command=lambda: go_back(self))
    up_b = ttk.Button(bar, text="↑", width=2, style="Act.TButton", command=lambda: go_up(self))
    back_b.pack(side="left")
    up_b.pack(side="left", padx=(S(2), S(6)))
    crumbs = tk.Frame(bar, bd=0, highlightthickness=0,
                      bg=self.style.lookup("Card.TFrame", "background") or self.t.get("panel_bg"))
    crumbs.pack(side="left", fill="x", expand=True)
    _tip(self, back_b, "Back (Alt+Left)")
    _tip(self, up_b, "Up a level (Backspace)")
    self._nav = (back_b, up_b, crumbs)
    tv.heading("#0", text="Name", anchor="w")
    tv.heading("rev", text="Rev", anchor="w")
    tv.bind("<ButtonRelease-1>", lambda e: _row_click(self, e), add="+")
    tv.bind("<BackSpace>", lambda e: (go_up(self), "break")[1])
    tv.bind("<Alt-Left>", lambda e: (go_back(self), "break")[1])
    tv.bind("<Return>", lambda e: (_enter(self), "break")[1])


def _enter(self):
    sel = self.vault_tree.selection()
    if sel and sel[0].startswith("d:"):
        go(self, sel[0][2:])
    elif sel and sel[0].startswith("f:"):
        self.open_vault_file(sel[0][2:])


def start_folder(self):
    A = _A()
    saved = norm(self.settings.get("panel_cwd", ""))
    if saved and saved in {norm(f) for f in A.INDEX.folders}:
        return saved
    f = self.current() if hasattr(self, "current") else None
    if f:
        return file_folder(f)
    return ""


def setup_instance(self):
    """Compact buttons and the folder bar on an already-built panel. Safe to call more than once."""
    if self.__dict__.get("_atl_295_ui"):
        return
    self._atl_295_ui = True
    try:
        style_actions(self)
        compact_actions(self)
    except Exception:                                           # noqa: BLE001
        log_error("compact actions")
    try:
        self._back = []
        self._cwd = start_folder(self)
        build_nav(self)
        self.rebuild_lists()
    except Exception:                                           # noqa: BLE001
        log_error("vault navigation")


def build_ui(self, *a, **k):
    res = ORIG["build"](self, *a, **k)
    setup_instance(self)
    return res


def panel_styles(self, t, *a, **k):
    res = ORIG["styles"](self, t, *a, **k)
    try:
        style_actions(self)
    except Exception:                                           # noqa: BLE001
        pass
    return res


# =============================================================================
# Install
# =============================================================================
def patch_panel(mod):
    cls = getattr(mod, "Panel", None)
    need = ("build_ui", "rebuild_lists", "_set_states", "_on_double", "_panel_styles")
    if cls is None or getattr(cls, "_atl_295", False) or not all(hasattr(cls, n) for n in need):
        return False
    cls._atl_295 = True
    ORIG.update(build=cls.build_ui, rebuild=cls.rebuild_lists, states=cls._set_states, double=cls._on_double,
                styles=cls._panel_styles)
    cls.build_ui = build_ui
    cls.rebuild_lists = rebuild_lists
    cls._set_states = set_states
    cls._on_double = _on_double
    cls._panel_styles = panel_styles
    return True


def _hook():
    """Patch the panel class and set up any panel already built, just before its window starts."""
    import tkinter as tk
    if getattr(tk.Misc.mainloop, "_atl_295", False):
        return
    orig = tk.Misc.mainloop

    def mainloop(self, n=0):
        pm = sys.modules.get("atelier_panel")
        if pm is not None:
            try:
                patch_panel(pm)
                import gc
                for obj in gc.get_objects():
                    if isinstance(obj, pm.Panel) and getattr(obj, "root", None) is not None:
                        setup_instance(obj)
            except Exception:                                   # noqa: BLE001
                log_error("2.9.5 panel")
        return orig(self, n)
    mainloop._atl_295 = True
    tk.Misc.mainloop = mainloop


def install(module):
    global E
    E = module
    if getattr(module, "_fix295_installed", False):
        return
    module._fix295_installed = True
    for n in ("atelier_polish", "atelier_guide", "atelier_flow", "atelier_access", "atelier_states",
              "atelier_layout", "atelier_v29", "atelier_fix291", "atelier_fix292", "atelier_fix293",
              "atelier_fix294"):
        mm = sys.modules.get(n)
        if mm is not None:
            mm.VERSION = VERSION
    pm = sys.modules.get("atelier_panel")
    if pm is not None:
        patch_panel(pm)
    try:
        _hook()
    except Exception:                                           # noqa: BLE001
        log_error("2.9.5 install")
