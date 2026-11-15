"""
Atelier PDM 2.9.4 (atelier_fix294.py) - translucent (frosted) panel, like SolidCraft's translucent menus
======================================================================================================

Panel ≡ menu › Translucency: Off, Light, Medium, Strong (saved separately for SolidCraft and PhysCAD).

While the panel is out, its background becomes frosted glass: what's behind it shows through, blurred and tinted
in the theme's colour. The header, the cards, the buttons, the lists and all text stay solid and sharp - only the
background between them is see-through. When it's tucked away, or a dialog is open, it's solid.

How: the panel's background is drawn in a key colour (one shade off the theme's background), Windows is told to
make that colour see-through (Tk's -transparentcolor), and Windows' acrylic blur is switched on behind the window
(SetWindowCompositionAttribute). Windows 10 1803 or later; on Windows 10 the blur can lag slightly while the
window moves. If Windows refuses, the panel simply stays solid.

Note: as with any colour-keyed window, a click on a see-through gap goes to the program behind it.
Also: removes the whole-window opacity setting added by an earlier 2.9.3 (it made text see-through too).
"""
import ctypes
import os
import sys
import time
import traceback

VERSION = "2.9.4"
E = None
ORIG = {}
LEVELS = (("off", "Off"), ("light", "Light"), ("medium", "Medium"), ("strong", "Strong"))
TINT_ALPHA = {"light": 0xD0, "medium": 0xA8, "strong": 0x78}       # how much of the theme colour is laid over
PLAIN_FRAMES = ("TFrame",)                                         # the panel's background frames
ACCENT_BLURBEHIND, ACCENT_ACRYLIC, ACCENT_DISABLED = 3, 4, 0
WCA_ACCENT_POLICY = 19


def log_error(where):
    try:
        base = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "Atelier PDM")
        os.makedirs(base, exist_ok=True)
        with open(os.path.join(base, "atelier_error.log"), "a", encoding="utf-8") as f:
            f.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} · {where} ({VERSION}) =====\n"
                    f"{traceback.format_exc()}\n")
    except Exception:                                           # noqa: BLE001
        pass


# ---------------------------------------------------------------- colours
def _rgb(c):
    c = str(c).lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4)) if len(c) == 6 else (32, 34, 38)


def key_colour(bg):
    """A colour one step from bg that the theme doesn't use: drawn there, Windows makes it see-through."""
    r, g, b = _rgb(bg)
    b2 = b + 1 if b < 255 else b - 1
    return "#%02x%02x%02x" % (r, g, b2)


def tint_abgr(bg, level):
    """Acrylic tint as 0xAABBGGRR."""
    r, g, b = _rgb(bg)
    return (TINT_ALPHA.get(level, 0xC0) << 24) | (b << 16) | (g << 8) | r


# ---------------------------------------------------------------- Windows
class _ACCENT(ctypes.Structure):
    _fields_ = [("AccentState", ctypes.c_int), ("AccentFlags", ctypes.c_int),
                ("GradientColor", ctypes.c_uint), ("AnimationId", ctypes.c_int)]


class _WCAD(ctypes.Structure):
    _fields_ = [("Attribute", ctypes.c_int), ("Data", ctypes.c_void_p), ("SizeOfData", ctypes.c_size_t)]


def _hwnd(root):
    try:
        fr = root.wm_frame()
        h = int(fr, 16) if isinstance(fr, str) else int(fr)
        if h:
            return h
    except Exception:                                           # noqa: BLE001
        pass
    try:
        return ctypes.windll.user32.GetParent(root.winfo_id()) or root.winfo_id()
    except Exception:                                           # noqa: BLE001
        return 0


def set_blur(root, on, tint=0, acrylic=True):
    """Acrylic (or plain blur) behind the window. Returns True if Windows accepted it."""
    if sys.platform != "win32":
        return False
    try:
        fn = ctypes.windll.user32.SetWindowCompositionAttribute
    except (AttributeError, OSError):
        return False
    h = _hwnd(root)
    if not h:
        return False
    acc = _ACCENT(ACCENT_DISABLED if not on else (ACCENT_ACRYLIC if acrylic else ACCENT_BLURBEHIND),
                  2 if on else 0, tint if on else 0, 0)
    data = _WCAD(WCA_ACCENT_POLICY, ctypes.cast(ctypes.pointer(acc), ctypes.c_void_p), ctypes.sizeof(acc))
    try:
        return bool(fn(ctypes.c_void_p(h), ctypes.byref(data)))
    except Exception:                                           # noqa: BLE001
        return False


# ---------------------------------------------------------------- panel
def level(self):
    v = self.settings.get("panel_translucency", "off")
    return v if v in dict(LEVELS) else "off"


def wanted(self):
    if level(self) == "off" or not getattr(self, "expanded", True):
        return False
    try:
        if self._dialog_open():
            return False
    except Exception:                                           # noqa: BLE001
        pass
    if self.root.state() == "withdrawn":
        return False
    return True


def _paint(self, key):
    """Background frames (and the window itself) in key, or back to the theme colour when key is None."""
    bg = self.t.get("bg") or self.t.get("panel_bg")
    col = key or self.__dict__.get("_atl_frame_bg") or self.style.lookup("TFrame", "background") or bg
    if key and "_atl_frame_bg" not in self.__dict__:
        self._atl_frame_bg = self.style.lookup("TFrame", "background") or bg
    for st in PLAIN_FRAMES:
        self.style.configure(st, background=col)
    try:
        self.root.configure(bg=col)
    except Exception:                                           # noqa: BLE001
        pass


def apply(self, force=False):
    want = wanted(self)
    lv = level(self)
    state = (want, lv, self.theme_name) if want else (False,)
    if not force and self.__dict__.get("_atl_glass") == state:
        return
    self._atl_glass = state
    try:
        self.root.attributes("-alpha", 1.0)                     # undo the earlier whole-window opacity
    except Exception:                                           # noqa: BLE001
        pass
    if not want:
        try:
            self.root.attributes("-transparentcolor", "")
        except Exception:                                       # noqa: BLE001
            pass
        set_blur(self.root, False)
        _paint(self, None)
        self.__dict__.pop("_atl_frame_bg", None)
        return
    base = self.__dict__.get("_atl_frame_bg") or self.style.lookup("TFrame", "background") or self.t.get("bg")
    self._atl_frame_bg = base
    key = key_colour(base)
    _paint(self, key)
    try:
        self.root.attributes("-transparentcolor", key)
    except Exception:                                           # noqa: BLE001
        self._atl_glass = (False,)
        _paint(self, None)
        return
    ok = set_blur(self.root, True, tint_abgr(base, lv), acrylic=True) or \
        set_blur(self.root, True, tint_abgr(base, lv), acrylic=False)
    if not ok and sys.platform == "win32":
        # no blur from Windows: clear see-through would be unreadable, so stay solid
        self.root.attributes("-transparentcolor", "")
        _paint(self, None)
        self._atl_glass = (False,)
        if not self.__dict__.get("_atl_warned"):
            self._atl_warned = True
            self.say("Translucency isn't available on this version of Windows - the panel stays solid.", "warn")


def set_level(self, lv):
    self.settings["panel_translucency"] = lv
    self.save_settings()
    apply(self, force=True)
    self.say("Translucency: " + dict(LEVELS)[lv].lower() + "." + ("" if lv == "off" else
             " The background between the cards is frosted glass while the panel is out."), "ok")


def hover_tick(self):
    try:
        apply(self)
    except Exception:                                           # noqa: BLE001
        log_error("translucency")
    return ORIG["hover"](self)


def apply_theme(self, *a, **k):
    self.__dict__.pop("_atl_frame_bg", None)
    try:
        if self.__dict__.get("_atl_glass", (False,))[0]:
            _paint(self, None)
    except Exception:                                           # noqa: BLE001
        pass
    res = ORIG["theme"](self, *a, **k)
    try:
        apply(self, force=True)
    except Exception:                                           # noqa: BLE001
        log_error("translucency theme")
    return res


def build_menu(self):
    f293 = sys.modules.get("atelier_fix293")
    base = (f293.ORIG.get("menu") if f293 is not None and "menu" in getattr(f293, "ORIG", {}) else None) \
        or ORIG["menu"]
    base(self)                                                   # the panel's own menu (no old opacity entry)
    try:
        import tkinter as tk
        m = self.menu
        v = self.__dict__.get("_atl_tv")
        if v is None:
            v = self._atl_tv = tk.StringVar(master=self.root)
        v.set(level(self))
        tm = tk.Menu(m, tearoff=0, **self.menu_colors())
        for key, label in LEVELS:
            tm.add_radiobutton(label=label, variable=v, value=key, command=lambda: set_level(self, v.get()),
                               state="normal" if (sys.platform == "win32" or key == "off") else "disabled")
        try:
            idx = m.index("Panel look") + 1
            m.insert_cascade(idx, label="Translucency", menu=tm)
        except Exception:                                       # noqa: BLE001
            m.add_cascade(label="Translucency", menu=tm)
    except Exception:                                           # noqa: BLE001
        log_error("translucency menu")


def patch_panel(mod):
    cls = getattr(mod, "Panel", None)
    if cls is None or getattr(cls, "_atl_294", False):
        return False
    if not all(hasattr(cls, n) for n in ("build_menu", "_hover_tick", "apply_theme")):
        return False
    cls._atl_294 = True
    f293 = sys.modules.get("atelier_fix293")
    old = getattr(f293, "ORIG", {}) if f293 is not None else {}
    # an earlier 2.9.3 wrapped these for whole-window opacity: go back to the panel's own versions
    ORIG["menu"] = old.get("menu") or cls.build_menu
    ORIG["hover"] = old.get("hover") or cls._hover_tick
    ORIG["theme"] = cls.apply_theme
    cls.build_menu = build_menu
    cls._hover_tick = hover_tick
    cls.apply_theme = apply_theme
    return True


def _hook():
    import tkinter as tk
    if getattr(tk.Misc.mainloop, "_atl_294", False):
        return
    orig = tk.Misc.mainloop

    def mainloop(self, n=0):
        pm = sys.modules.get("atelier_panel")
        if pm is not None:
            try:
                patch_panel(pm)
            except Exception:                                   # noqa: BLE001
                log_error("2.9.4 panel")
        return orig(self, n)
    mainloop._atl_294 = True
    tk.Misc.mainloop = mainloop


def install(module):
    global E
    E = module
    if getattr(module, "_fix294_installed", False):
        return
    module._fix294_installed = True
    for n in ("atelier_polish", "atelier_guide", "atelier_flow", "atelier_access", "atelier_states",
              "atelier_layout", "atelier_v29", "atelier_fix291", "atelier_fix292", "atelier_fix293"):
        mm = sys.modules.get(n)
        if mm is not None:
            mm.VERSION = VERSION
    pm = sys.modules.get("atelier_panel")
    if pm is not None:
        patch_panel(pm)
    try:
        _hook()
    except Exception:                                           # noqa: BLE001
        log_error("2.9.4 install")
