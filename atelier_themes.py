"""
Atelier PDM - themes, modern controls and custom themes (atelier_themes.py)  2.7.4
==================================================================================

2.7.4: much faster - control images are drawn with a quick shape formula, are solid rather than see-through,
       have large middles (Windows paints a few big pieces, not hundreds of tiny ones), and are cached.
       "Rounded controls" switch in the Theme editor for anyone who prefers the plain controls.

* Reworked palettes. The light themes are dimmed (no pure-white surfaces - nothing brighter than a soft
  grey-white), the dark ones are calmer, and there are two new ones: Graphite (neutral dark) and Soft Light.
* Modern controls: rounded buttons, menu buttons, text boxes and drop-downs, and slim scrollbars without
  arrows, drawn as anti-aliased images in the theme's colours (no more 3-D Windows 95 bevels).
* Menus (right-click menus, the account menu, the ≡ menu) are drawn in the theme's colours with a thin
  border, instead of Windows' native menu with its light bevelled edge.
* Theme editor: account menu (top right) > Theme editor… Pick five colours - background, panels, text,
  accent, header band - and everything else is worked out from them, previewed live as you change them.
  Saved themes appear in the theme list (and the panels' Panel look menu). Export / Import to share them.

Installed by atelier_cam.install(), so it's in place before any window is built. Anything that fails falls
back to the normal look, so a drawing problem can never stop the program working.
"""
import base64
import json
import os
import struct
import zlib

E = None
ENABLED = True
PREVIEW = "\u2063preview"                      # invisible name used while previewing in the editor
_IMAGES = []                                   # keeps Tk images alive
_ELEMENTS = {}                                 # (kind, colours, size) -> element name
_COUNTER = [0]


# =============================================================================
# Colour helpers
# =============================================================================
def _hex(c):
    c = (c or "").strip()
    if c.startswith("#") and len(c) == 4:
        c = "#" + "".join(ch * 2 for ch in c[1:])
    return c.lower()


def rgb(c):
    c = _hex(c).lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def to_hex(r, g, b):
    return "#{:02x}{:02x}{:02x}".format(*(max(0, min(255, int(round(v)))) for v in (r, g, b)))


def mix(a, b, t):
    ra, rb = rgb(a), rgb(b)
    return to_hex(*(x + (y - x) * t for x, y in zip(ra, rb)))


def lum(c):
    r, g, b = (v / 255 for v in rgb(c))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def rel_lum(c):
    """WCAG relative luminance (gamma-corrected)."""
    def ch(v):
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = rgb(c)
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def contrast(a, b):
    la, lb = rel_lum(a), rel_lum(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def is_dark(c):
    return lum(c) < 0.5


def text_on(c):
    return "#10141a" if lum(c) > 0.55 else "#f3f6f9"


def valid(c):
    try:
        rgb(c)
        return _hex(c).startswith("#") and len(_hex(c)) == 7
    except ValueError:
        return False


# =============================================================================
# Palettes
# =============================================================================
KEYS = ("dark", "bg", "header_bg", "toolbar_bg", "panel_bg", "border", "fg", "muted_fg", "entry_bg", "entry_fg",
        "grid_bg", "row_alt", "grid_fg", "heading_bg", "heading_fg", "btn_bg", "btn_fg", "accent", "sel_bg",
        "sel_fg", "mesh", "success", "warning", "danger", "log_bg", "log_fg", "sheet_bg", "sheet_fg",
        "header_fg", "header_muted", "header_accent")


def derive(bg, panel, fg, accent, header, base=None, **over):
    """A full palette from five colours. Everything else is mixed from them so it stays in balance."""
    dark = is_dark(bg)
    p = dict(base or {})
    hf = text_on(header)
    p.update(
        dark=dark, bg=bg, panel_bg=panel, fg=fg, accent=accent, header_bg=header,
        toolbar_bg=mix(bg, panel, 0.55),
        border=mix(panel, fg, 0.13 if dark else 0.17),
        muted_fg=mix(fg, panel, 0.40 if dark else 0.30),
        entry_bg=mix(bg, "#000000", 0.18) if dark else mix(panel, "#ffffff", 0.22),
        entry_fg=fg, grid_bg=panel, grid_fg=fg, row_alt=mix(panel, fg, 0.035),
        heading_bg=panel, heading_fg=mix(fg, panel, 0.36 if dark else 0.28),
        btn_bg=mix(panel, fg, 0.09 if dark else 0.08), btn_fg=fg,
        sel_bg=mix(panel, accent, 0.30 if dark else 0.24), sel_fg=fg if dark else mix(fg, accent, 0.35),
        mesh=mix(accent, panel, 0.12),
        success="#4cc98a" if dark else "#2f8a55", warning="#eab353" if dark else "#a96f12",
        danger="#ef7a7a" if dark else "#b8433a",
        log_bg=mix(bg, "#000000", 0.30) if dark else "#20262f", log_fg="#c3ccd7",
        sheet_bg="#f3f1ea", sheet_fg="#1f2a44",
        header_fg=hf, header_muted=mix(hf, header, 0.42),
        header_accent=accent if abs(lum(accent) - lum(header)) > 0.28 else (
            mix(accent, "#ffffff", 0.4) if is_dark(header) else mix(accent, "#000000", 0.35)),
    )
    p.update(over)
    return p


BUILT_IN = {
    "Engineering Slate": derive("#111827", "#172033", "#e2e8f0", "#4cb5f5", "#0d1422",
                                toolbar_bg="#141d30", entry_bg="#0e1525", border="#26324b", sel_bg="#1c3a5d"),
    "High-Contrast Dark": derive("#0b0b0d", "#151518", "#ffffff", "#3fd0ff", "#050506",
                                 border="#3d3d45", muted_fg="#b9b9c2", heading_fg="#e7e7ec", entry_bg="#050506",
                                 sel_bg="#0d4d68", sel_fg="#ffffff", btn_bg="#26262c",
                                 success="#55e38d", warning="#ffd34d", danger="#ff6f6f"),
    "Graphite": derive("#1b1c1f", "#232428", "#e4e4e7", "#7aa2f7", "#151618"),
    "Classic CAD Cyan": derive("#0c1e23", "#11292f", "#d5e5e7", "#3cc6b6", "#08171b",
                               sel_bg="#14504a", success="#9fd55f"),
    "Nordic Light": derive("#cfd5dd", "#dce0e6", "#1c2430", "#2a63c4", "#2a3340",
                           entry_bg="#e2e5ea", row_alt="#d5dae1", btn_bg="#ccd2da", sel_bg="#c2d1ec",
                           toolbar_bg="#d6dbe2"),
    "Drafting Paper / Cream": derive("#d2cabb", "#ddd6c8", "#28231c", "#2b58a8", "#2b3142",
                                     entry_bg="#e3dcd0", row_alt="#d6cec0", btn_bg="#cdc4b2", sel_bg="#c9d1e1",
                                     toolbar_bg="#d8d0c2", sheet_bg="#ebe4d4"),
    "Soft Light": derive("#c6c9ce", "#d3d6da", "#1d2024", "#2a5ca8", "#33373d",
                         entry_bg="#dadde0", row_alt="#cdd0d4", btn_bg="#c1c5ca", sel_bg="#bccbe4",
                         toolbar_bg="#cdd0d4"),
}


def _settings():
    try:
        return E.load_settings() or {}
    except Exception:                                          # noqa: BLE001
        return {}


def custom_themes():
    out = {}
    for name, pal in (_settings().get("custom_themes") or {}).items():
        if isinstance(pal, dict) and all(k in pal for k in ("bg", "panel_bg", "fg", "accent", "header_bg")):
            out[name] = derive(pal["bg"], pal["panel_bg"], pal["fg"], pal["accent"], pal["header_bg"],
                               base=pal)
    return out


def save_custom(name, pal):
    s = _settings()
    d = dict(s.get("custom_themes") or {})
    d[name] = {k: pal[k] for k in KEYS if k in pal}
    s["custom_themes"] = d
    E.save_settings(s)
    E.THEMES[name] = pal


def delete_custom(name):
    s = _settings()
    d = dict(s.get("custom_themes") or {})
    d.pop(name, None)
    s["custom_themes"] = d
    if s.get("theme") == name:
        s["theme"] = "Engineering Slate"
    E.save_settings(s)
    E.THEMES.pop(name, None)


def apply_palettes(module):
    themes = getattr(module, "THEMES", None)
    if themes is None:
        return
    for name, pal in list(BUILT_IN.items()) + list(custom_themes().items()):
        if name in themes and isinstance(themes[name], dict):
            themes[name].update(pal)
        else:
            themes[name] = dict(pal)


# =============================================================================
# Anti-aliased rounded shapes as PNG images (no extra libraries)
# =============================================================================
def _png(w, h, pixels, alpha=True):
    """pixels: rows of (r, g, b[, a]) tuples -> PNG bytes."""
    raw = b"".join(b"\x00" + bytes(v for px in row for v in px) for row in pixels)

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6 if alpha else 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def _sdf(px, py, cx, cy, hx, hy, r):
    """Signed distance from a point to a rounded rectangle (negative inside)."""
    qx, qy = abs(px - cx) - (hx - r), abs(py - cy) - (hy - r)
    ox, oy = max(qx, 0.0), max(qy, 0.0)
    return (ox * ox + oy * oy) ** 0.5 + min(max(qx, qy), 0.0) - r


_PNG_CACHE = {}


def rounded_png(w, h, r, fill, border=None, bw=1.0, inset_x=0.0, inset_y=0.0, bg=None):
    """Anti-aliased rounded rectangle. With bg the image is solid (corners blended into bg), which Windows can
    draw far faster than a see-through image."""
    key = (w, h, round(r, 2), fill, border, round(bw, 2), round(inset_x, 2), round(inset_y, 2), bg)
    if key in _PNG_CACHE:
        return _PNG_CACHE[key]
    fr, br = rgb(fill), rgb(border or fill)
    cx, cy = w / 2.0, h / 2.0
    hx, hy = w / 2.0 - inset_x, h / 2.0 - inset_y
    r = max(0.0, min(r, hx, hy))
    bgc = rgb(bg) if bg else None

    def pixel(x, y):
        d = _sdf(x + 0.5, y + 0.5, cx, cy, hx, hy, r)
        a = 0.5 - d
        a = 0.0 if a < 0 else (1.0 if a > 1 else a)
        if border is None:
            ci = a
        else:
            c2 = 0.5 - (d + bw)
            ci = 0.0 if c2 < 0 else (1.0 if c2 > 1 else c2)
        if bgc is not None:
            return tuple(int(round(g * (1 - a) + b * (a - ci) + f * ci)) for g, b, f in zip(bgc, br, fr))
        if a <= 0:
            return (0, 0, 0, 0)
        col = tuple((b * (a - ci) + f * ci) / a for b, f in zip(br, fr))
        return (int(round(col[0])), int(round(col[1])), int(round(col[2])), int(round(255 * a)))

    # The shape is symmetrical and its middle is uniform: work out the top-left quarter near the edges only,
    # then mirror and repeat (identical result, a fraction of the work).
    band_x = min((w + 1) // 2, int(r + inset_x + bw) + 3)
    band_y = min((h + 1) // 2, int(r + inset_y + bw) + 3)

    def make_row(y):
        left = [pixel(x, y) for x in range(band_x)]
        mid_n = w - 2 * band_x
        if mid_n > 0:
            return left + [pixel(band_x, y)] * mid_n + left[::-1]
        return left + left[::-1][(2 * band_x - w):]
    top = [make_row(y) for y in range(band_y)]
    mid_n = h - 2 * band_y
    rows = top + ([make_row(band_y)] * mid_n if mid_n > 0 else []) + (top[::-1] if mid_n >= 0 else top[::-1][(2 * band_y - h):])
    png = _png(w, h, rows, alpha=bgc is None)
    _PNG_CACHE[key] = png
    return png


def _photo(root, png):
    import tkinter as tk
    img = tk.PhotoImage(master=root, data=base64.b64encode(png).decode("ascii"))
    _IMAGES.append(img)
    return img


# =============================================================================
# Modern ttk controls
# =============================================================================
BUTTON_STYLES = ("TButton", "Accent.TButton", "Success.TButton", "Danger.TButton", "Small.TButton",
                 "Tool.TButton", "ToolAccent.TButton", "ToolSuccess.TButton", "HeaderTool.TButton")
FLAT_STYLES = ("Tool.TButton", "ToolAccent.TButton", "ToolSuccess.TButton", "HeaderTool.TButton",
               "Tool.TMenubutton", "Account.TMenubutton")
MENUBUTTON_STYLES = ("TMenubutton", "Tool.TMenubutton", "Account.TMenubutton")
ENTRY_STYLES = ("TEntry", "Search.TEntry")
COMBO_STYLES = ("TCombobox", "Header.TCombobox")
ON_HEADER = ("HeaderTool.TButton", "Account.TMenubutton", "Header.TCombobox", "Search.TEntry")
ON_TOOLBAR = ("Tool.TButton", "ToolAccent.TButton", "ToolSuccess.TButton", "Tool.TMenubutton")
_ORIG_LAYOUTS = {}
TIMING = {"last_ms": 0.0}


def controls_enabled():
    return ENABLED and bool(_settings().get("modern_controls", True))


def _colour(root, c):
    if not c:
        return None
    c = str(c)
    if valid(c):
        return _hex(c)
    try:
        r, g, b = root.winfo_rgb(c)
        return to_hex(r / 257, g / 257, b / 257)
    except Exception:                                          # noqa: BLE001
        return None


def _look(s, root, style, opt, state=None):
    try:
        v = s.lookup(style, opt, state) if state else s.lookup(style, opt)
    except Exception:                                          # noqa: BLE001
        v = ""
    return _colour(root, v)


def _element(s, root, specs, size, radius, border_px, bg, pad, inset=(0.0, 0.0), suffix="Button.rnd"):
    """Image element for a set of state looks. Elements are shared by every style that looks the same."""
    key = (tuple(sorted(specs.items())), size, round(radius, 2), border_px, bg, pad, inset, suffix)
    if key in _ELEMENTS:
        return _ELEMENTS[key]
    w, h = size
    imgs = {st: _photo(root, rounded_png(w, h, radius, f, b, border_px, inset[0], inset[1], bg))
            for st, (f, b) in specs.items()}
    _COUNTER[0] += 1
    name = f"Atl{_COUNTER[0]}.{suffix}"
    order = ("disabled", "pressed", "active", "focus", "readonly")
    states = [(st, imgs[st]) for st in order if st in imgs]
    s.element_create(name, "image", imgs["normal"], *states, border=pad, sticky="nsew")
    _ELEMENTS[key] = name
    return name


def _set_layout(s, style, layout):
    try:
        if style not in _ORIG_LAYOUTS:
            _ORIG_LAYOUTS[style] = s.layout(style)
        s.layout(style, layout)
        return True
    except Exception:                                          # noqa: BLE001
        old = _ORIG_LAYOUTS.get(style)
        if old:
            try:
                s.layout(style, old)
            except Exception:                                  # noqa: BLE001
                pass
        return False


def restore_controls(s):
    """Puts back the standard controls (the 'Rounded controls' switch turned off)."""
    for style, layout in list(_ORIG_LAYOUTS.items()):
        try:
            s.layout(style, layout)
        except Exception:                                      # noqa: BLE001
            pass


def modernise(root, s, t, S):
    """Rounded, flat controls in the current theme's colours. Images are solid and have large middles, so Tk
    paints a few big pieces instead of hundreds of tiny see-through ones; identical looks share one image."""
    import time as _t
    if not controls_enabled():
        restore_controls(s)
        return
    t0 = _t.perf_counter()
    r_btn, r_ent = max(4, S(5)), max(4, S(5))
    acc = _colour(root, t.get("accent")) or "#3b82f6"
    fg = _colour(root, t.get("fg")) or "#888888"
    panel = _colour(root, t.get("panel_bg")) or "#202020"
    behind = lambda style: _colour(root, t.get("header_bg" if style in ON_HEADER else
                                              "toolbar_bg" if style in ON_TOOLBAR else "panel_bg")) or panel
    for style in BUTTON_STYLES + MENUBUTTON_STYLES:
        try:
            fill = _look(s, root, style, "background")
            if not fill:
                continue
            flat = style in FLAT_STYLES
            line = None if flat else (_look(s, root, style, "bordercolor") or mix(fill, fg, 0.12))
            spec = {"normal": (fill, line)}
            for st in ("active", "pressed", "disabled"):
                f = _look(s, root, style, "background", [st]) or fill
                spec[st] = (f, None if flat else (_look(s, root, style, "bordercolor", [st]) or line))
            pad = r_btn + 2
            size = (2 * pad + 32, 2 * pad + 2)
            if style in MENUBUTTON_STYLES:
                el = _element(s, root, spec, size, r_btn, 1.0, behind(style), pad, suffix="Menubutton.rnd")
                _set_layout(s, style, [(el, {"sticky": "nsew", "children": [
                    ("Menubutton.padding", {"sticky": "nsew", "children": [
                        ("Menubutton.indicator", {"side": "right", "sticky": ""}),
                        ("Menubutton.label", {"side": "left", "sticky": ""})]})]})])
            else:
                el = _element(s, root, spec, size, r_btn, 1.0, behind(style), pad, suffix="Button.rnd")
                _set_layout(s, style, [(el, {"sticky": "nsew", "children": [
                    ("Button.padding", {"sticky": "nsew", "children": [("Button.label", {"sticky": "nsew"})]})]})])
        except Exception:                                      # noqa: BLE001
            continue
    for style in ENTRY_STYLES + COMBO_STYLES:
        try:
            fill = _look(s, root, style, "fieldbackground")
            if not fill:
                continue
            line = _look(s, root, style, "bordercolor") or mix(fill, fg, 0.18)
            focus = _look(s, root, style, "bordercolor", ["focus"]) or acc
            if focus == line:
                focus = acc
            dis = _look(s, root, style, "fieldbackground", ["disabled"]) or mix(fill, panel, 0.6)
            pad = r_ent + 2
            size = (2 * pad + 32, 2 * pad + 2)
            if style in COMBO_STYLES:
                ro = _look(s, root, style, "fieldbackground", ["readonly"]) or fill
                spec = {"normal": (ro, line), "focus": (ro, focus), "disabled": (dis, line)}
                el = _element(s, root, spec, size, r_ent, 1.0, behind(style), pad, suffix="Combobox.rnd")
                s.configure(style, background=ro, lightcolor=ro, darkcolor=ro, bordercolor=ro, arrowsize=max(9, S(11)))
                s.map(style, background=[("active", ro), ("pressed", ro)], bordercolor=[("focus", ro)],
                      lightcolor=[("focus", ro)])
                _set_layout(s, style, [(el, {"sticky": "nsew", "children": [
                    ("Combobox.downarrow", {"side": "right", "sticky": "ns"}),
                    ("Combobox.padding", {"expand": "1", "sticky": "nsew", "children": [
                        ("Combobox.textarea", {"sticky": "nsew"})]})]})])
            else:
                spec = {"normal": (fill, line), "focus": (fill, focus), "disabled": (dis, line)}
                el = _element(s, root, spec, size, r_ent, 1.0, behind(style), pad, suffix="Entry.rnd")
                _set_layout(s, style, [(el, {"sticky": "nsew", "children": [
                    ("Entry.padding", {"sticky": "nsew", "children": [("Entry.textarea", {"sticky": "nsew"})]})]})])
        except Exception:                                      # noqa: BLE001
            continue
    for orient in ("Vertical", "Horizontal"):
        style = f"{orient}.TScrollbar"
        try:
            trough = panel
            thumb = mix(trough, fg, 0.22)
            hover = mix(trough, fg, 0.36)
            th = max(8, S(10))
            end = int(th / 2) + 1
            vertical = orient == "Vertical"
            size = (th, 2 * end + 40) if vertical else (2 * end + 40, th)
            inset = (th * 0.22, th * 0.1) if vertical else (th * 0.1, th * 0.22)
            spec = {"normal": (thumb, None), "active": (hover, None), "pressed": (hover, None)}
            el = _element(s, root, spec, size, th * 0.28, 0.0, trough, (0, end) if vertical else (end, 0), inset,
                          suffix=f"{orient}.Scrollbar.thumb")
            s.configure(style, troughcolor=trough, bordercolor=trough, lightcolor=trough, darkcolor=trough,
                        background=trough, gripcount=0, arrowsize=th)
            sticky = "ns" if vertical else "ew"
            _set_layout(s, style, [(f"{orient}.Scrollbar.trough", {"sticky": sticky, "children": [
                (el, {"expand": "1", "sticky": "nswe"})]})])
        except Exception:                                      # noqa: BLE001
            continue
    try:
        s.configure("Treeview.Heading", borderwidth=0, relief="flat")
        s.configure("TNotebook", borderwidth=0)
    except Exception:                                          # noqa: BLE001
        pass
    TIMING["last_ms"] = (_t.perf_counter() - t0) * 1000


def _hook_style(module):
    orig = getattr(module, "style_ttk", None)
    if orig is None or getattr(orig, "_themes", False):
        return
    pending = {}

    def style_ttk(root, s, t, S):
        res = orig(root, s, t, S)
        try:
            modernise(root, s, t, S)
        except Exception:                                      # noqa: BLE001
            pass
        key = str(root)
        if key not in pending:                                 # once more after any later colour tweaks (cheap:
            pending[key] = True                                # unchanged looks come straight from the cache)

            def later():
                pending.pop(key, None)
                try:
                    modernise(root, s, t, S)
                except Exception:                              # noqa: BLE001
                    pass
            try:
                root.after_idle(later)
            except Exception:                                  # noqa: BLE001
                pending.pop(key, None)
        return res
    style_ttk._themes = True
    module.style_ttk = style_ttk


# =============================================================================
# Menus drawn in the theme's colours
# =============================================================================
class FlatMenu:
    """Shows a tk.Menu's entries in a borderless themed popup. Commands are run through the menu itself, so
    check / radio variables and callbacks behave exactly as before."""
    current = None

    def __init__(self, menu, x, y, parent=None):
        import tkinter as tk
        self.tk = tk
        self.menu, self.parent, self.child = menu, parent, None
        self.rows, self.active, self._open_job = [], None, None
        self.root_menu = parent.root_menu if parent else self
        pc = str(menu.cget("postcommand") or "")
        if pc:
            menu.tk.eval(pc)                                   # lets menus fill themselves in first
        c = lambda o, d: _colour(menu, menu.cget(o)) or d
        self.bg = c("background", "#202020")
        self.fg = c("foreground", "#eeeeee")
        self.abg = c("activebackground", mix(self.bg, self.fg, 0.15))
        self.afg = c("activeforeground", self.fg)
        self.dfg = c("disabledforeground", mix(self.fg, self.bg, 0.5))
        self.line = mix(self.bg, self.fg, 0.2)
        font = menu.cget("font") or ("Segoe UI", 9)
        self.top = top = tk.Toplevel(parent.root_menu.top if parent else menu)
        top.withdraw()
        top.overrideredirect(True)
        try:
            top.attributes("-topmost", True)
        except tk.TclError:
            pass
        top.configure(bg=self.line)
        inner = tk.Frame(top, bg=self.bg, bd=0, highlightthickness=0)
        inner.pack(fill="both", expand=True, padx=1, pady=1)
        tk.Frame(inner, bg=self.bg, height=4).pack(fill="x")
        last = menu.index("end")
        for i in range(0, (last if last is not None else -1) + 1):
            kind = menu.type(i)
            if kind == "tearoff":
                continue
            if kind == "separator":
                tk.Frame(inner, bg=self.bg, height=4).pack(fill="x")
                tk.Frame(inner, bg=self.line, height=1).pack(fill="x", padx=8)
                tk.Frame(inner, bg=self.bg, height=4).pack(fill="x")
                continue
            label = str(menu.entrycget(i, "label"))
            accel = str(menu.entrycget(i, "accelerator") or "")
            enabled = str(menu.entrycget(i, "state")) != "disabled"
            mark = ""
            if kind in ("checkbutton", "radiobutton"):
                var = str(menu.entrycget(i, "variable") or "")
                if var:
                    try:
                        cur = str(menu.tk.globalgetvar(var))
                    except Exception:                          # noqa: BLE001
                        cur = ""
                    want = str(menu.entrycget(i, "onvalue" if kind == "checkbutton" else "value"))
                    if cur == want or (kind == "checkbutton" and cur in ("1", "True") and want in ("1", "True")):
                        mark = "✓" if kind == "checkbutton" else "•"
            row = tk.Frame(inner, bg=self.bg, cursor="hand2" if enabled else "")
            row.pack(fill="x", padx=4)
            fg = self.fg if enabled else self.dfg
            parts = [tk.Label(row, text=mark, width=2, bg=self.bg, fg=fg, font=font, anchor="e")]
            parts.append(tk.Label(row, text=label, bg=self.bg, fg=fg, font=font, anchor="w", padx=6, pady=4))
            right = "›" if kind == "cascade" else accel
            parts.append(tk.Label(row, text=right, bg=self.bg, fg=mix(fg, self.bg, 0.35), font=font, anchor="e",
                                  padx=10))
            parts[0].pack(side="left")
            parts[2].pack(side="right")
            parts[1].pack(side="left", fill="x", expand=True)
            info = {"i": i, "kind": kind, "row": row, "parts": parts, "enabled": enabled, "fg": fg}
            self.rows.append(info)
            for w in [row] + parts:
                w.bind("<Enter>", lambda e, r=info: self.hover(r))
                w.bind("<ButtonRelease-1>", lambda e, r=info: self.choose(r))
        tk.Frame(inner, bg=self.bg, height=4).pack(fill="x")
        top.update_idletasks()
        w, h = top.winfo_reqwidth(), top.winfo_reqheight()
        sw, sh = top.winfo_screenwidth(), top.winfo_screenheight()
        if parent is not None:
            pr = parent.top
            x = pr.winfo_rootx() + pr.winfo_width() - 2
            if x + w > sw and pr.winfo_rootx() < sw:
                x = pr.winfo_rootx() - w + 2
        elif x + w > sw and x < sw:
            x = max(0, sw - w - 2)
        if y + h > sh and y < sh:
            y = max(0, (y - h) if parent is None else sh - h - 2)
        top.geometry(f"+{int(x)}+{int(y)}")
        top.deiconify()
        top.lift()
        if parent is None:
            self.prev_grab = top.grab_current()
            FlatMenu.current = self
            top.bind("<ButtonPress>", self.click_anywhere)
            for seq, fn in (("<Escape>", lambda e: self.close_all()), ("<Up>", lambda e: self.key(-1)),
                            ("<Down>", lambda e: self.key(1)), ("<Return>", lambda e: self.key_enter()),
                            ("<Right>", lambda e: self.key_right()), ("<Left>", lambda e: self.key_left())):
                top.bind(seq, fn)
            top.bind("<FocusOut>", lambda e: top.after(80, self.check_focus))
            try:
                top.grab_set()
            except tk.TclError:
                pass
            top.focus_force()

    # ---- hover / choose
    def _paint(self, r, on):
        bg = self.abg if on and r["enabled"] else self.bg
        fg = self.afg if on and r["enabled"] else r["fg"]
        r["row"].configure(bg=bg)
        for p in r["parts"]:
            p.configure(bg=bg, fg=fg if p is not r["parts"][2] else mix(fg, bg, 0.35))

    def hover(self, r):
        if self.active is not None and self.active is not r:
            self._paint(self.active, False)
        self.active = r
        self._paint(r, True)
        if self._open_job:
            self.top.after_cancel(self._open_job)
            self._open_job = None
        if self.child and (r["kind"] != "cascade" or self.child.index != r["i"]):
            self.child.close()
            self.child = None
        if r["kind"] == "cascade" and r["enabled"] and not self.child:
            self._open_job = self.top.after(180, lambda: self.open_child(r))

    def open_child(self, r):
        self._open_job = None
        if self.child or not self.top.winfo_exists():
            return
        sub = self.menu.nametowidget(str(self.menu.entrycget(r["i"], "menu")))
        self.child = FlatMenu(sub, 0, r["row"].winfo_rooty() - 5, parent=self)
        self.child.index = r["i"]

    def choose(self, r):
        if not r["enabled"]:
            return
        if r["kind"] == "cascade":
            self.open_child(r)
            return
        menu, i = self.menu, r["i"]
        self.close_all()
        menu.after(1, lambda: menu.invoke(i))

    # ---- keyboard
    def deepest(self):
        m = self
        while m.child:
            m = m.child
        return m

    def key(self, step):
        m = self.deepest()
        rows = [r for r in m.rows if r["enabled"]]
        if not rows:
            return
        i = rows.index(m.active) + step if m.active in rows else (0 if step > 0 else len(rows) - 1)
        m.hover(rows[i % len(rows)])

    def key_enter(self):
        m = self.deepest()
        if m.active:
            m.choose(m.active)

    def key_right(self):
        m = self.deepest()
        if m.active and m.active["kind"] == "cascade":
            m.open_child(m.active)
            if m.child and m.child.rows:
                m.child.key(1)

    def key_left(self):
        m = self.deepest()
        if m.parent:
            m.parent.child = None
            m.close()

    # ---- closing
    def all_tops(self):
        out, m = [], self
        while m:
            out.append(m.top)
            m = m.child
        return out

    def click_anywhere(self, e):
        try:
            w = self.top.winfo_containing(e.x_root, e.y_root)
        except Exception:                                      # noqa: BLE001
            w = None
        tops = [str(t) for t in self.all_tops()]
        if w is None or str(w.winfo_toplevel()) not in tops:
            self.close_all()

    def check_focus(self):
        try:
            f = self.top.focus_get()
        except Exception:                                      # noqa: BLE001
            f = None
        if f is None:
            self.close_all()

    def close(self):
        if self.child:
            self.child.close()
            self.child = None
        try:
            self.top.destroy()
        except Exception:                                      # noqa: BLE001
            pass

    def close_all(self):
        root = self.root_menu
        prev = getattr(root, "prev_grab", None)
        root.close()
        if FlatMenu.current is root:
            FlatMenu.current = None
        if prev is not None:
            try:
                if prev.winfo_exists():
                    prev.grab_set()
            except Exception:                                  # noqa: BLE001
                pass


def show_menu(menu, x, y):
    if FlatMenu.current is not None:
        FlatMenu.current.close_all()
    return FlatMenu(menu, x, y)


def _hook_menus():
    import tkinter as tk
    from tkinter import ttk
    if getattr(tk.Menu, "_atelier_flat", False):
        return
    tk.Menu._atelier_flat = True
    orig_popup = tk.Menu.tk_popup

    def tk_popup(self, x, y, entry=""):
        if not ENABLED:
            return orig_popup(self, x, y, entry)
        try:
            show_menu(self, x, y)
        except Exception:                                      # noqa: BLE001
            if FlatMenu.current is not None:
                try:
                    FlatMenu.current.close_all()
                except Exception:                              # noqa: BLE001
                    FlatMenu.current = None
            orig_popup(self, x, y, entry)
    tk.Menu.tk_popup = tk_popup

    orig_init = ttk.Menubutton.__init__

    def mb_init(self, *a, **k):
        orig_init(self, *a, **k)
        self.bind("<ButtonPress-1>", lambda e, w=self: _menubutton_click(w), add="+")
    ttk.Menubutton.__init__ = mb_init


def _menubutton_click(w):
    if not ENABLED:
        return None
    try:
        if "disabled" in w.state():
            return "break"
        path = str(w.cget("menu") or "")
        if not path:
            return None
        show_menu(w.nametowidget(path), w.winfo_rootx(), w.winfo_rooty() + w.winfo_height() + 2)
        return "break"
    except Exception:                                          # noqa: BLE001
        return None                                            # fall back to the native menu


# =============================================================================
# Theme editor
# =============================================================================
PICKS = (("bg", "Background", "Behind everything"), ("panel_bg", "Panels", "Cards, lists and dialogs"),
         ("fg", "Text", "Main text colour"), ("accent", "Accent", "Highlights, selected items, main buttons"),
         ("header_bg", "Header band", "The strip along the top"))


def theme_editor(app):
    import tkinter as tk
    from tkinter import ttk, colorchooser, filedialog, messagebox
    S = app.S
    builtin = set(BUILT_IN) | {"SolidCraft", "PhysCAD"}
    orig_name = app.theme_name
    start = E.THEMES.get(orig_name) or BUILT_IN["Engineering Slate"]
    vars_ = {k: tk.StringVar(value=_hex(start.get(k, "#808080"))) for k, _l, _h in PICKS}
    name_var = tk.StringVar(value=orig_name if orig_name not in builtin else "My theme")
    base_var = tk.StringVar(value=orig_name)
    state = {"job": None}

    dlg = E.ThemedDialog(app, "Theme editor", "Theme editor",
                         "Pick five colours - everything else is worked out from them and previewed live.",
                         width=520)
    top = ttk.Frame(dlg.body, style="Dialog.TFrame")
    top.pack(fill="x")
    ttk.Label(top, text="Start from", style="CardMuted.TLabel").grid(row=0, column=0, sticky="w")
    names = [n for n in E.THEMES if n != PREVIEW]
    base = ttk.Combobox(top, textvariable=base_var, values=names, state="readonly", width=26)
    base.grid(row=0, column=1, sticky="w", padx=(S(10), 0), pady=S(3))
    ttk.Label(top, text="Theme name", style="CardMuted.TLabel").grid(row=1, column=0, sticky="w")
    ttk.Entry(top, textvariable=name_var, width=28).grid(row=1, column=1, sticky="w", padx=(S(10), 0), pady=S(3))

    grid = ttk.Frame(dlg.body, style="Dialog.TFrame")
    grid.pack(fill="x", pady=(S(12), 0))
    swatches = {}

    def pick(key, label):
        c = colorchooser.askcolor(color=vars_[key].get(), parent=dlg, title=label)
        dlg.grab_set()
        if c and c[1]:
            vars_[key].set(_hex(c[1]))

    for r, (key, label, hint) in enumerate(PICKS):
        ttk.Label(grid, text=label, style="Card.TLabel").grid(row=r, column=0, sticky="w", pady=S(3))
        sw = tk.Label(grid, width=5, bd=0, relief="flat", cursor="hand2", highlightthickness=1)
        sw.grid(row=r, column=1, padx=(S(10), S(8)), pady=S(3))
        sw.bind("<Button-1>", lambda e, k=key, l=label: pick(k, l))
        swatches[key] = sw
        ttk.Entry(grid, textvariable=vars_[key], width=9).grid(row=r, column=2, sticky="w")
        ttk.Button(grid, text="Pick…", style="Small.TButton", command=lambda k=key, l=label: pick(k, l)).grid(
            row=r, column=3, padx=(S(8), 0))
        ttk.Label(grid, text=hint, style="CardMuted.TLabel").grid(row=r, column=4, sticky="w", padx=(S(10), 0))
    rc = tk.BooleanVar(value=bool(_settings().get("modern_controls", True)))

    def toggle_rc():
        st = _settings()
        st["modern_controls"] = bool(rc.get())
        E.save_settings(st)
        schedule()
    ttk.Checkbutton(dlg.body, text="Rounded controls (untick if Atelier feels slow on this PC)", variable=rc,
                    style="Dialog.TCheckbutton", command=toggle_rc).pack(anchor="w", pady=(S(10), 0))
    note = ttk.Label(dlg.body, text="", style="CardMuted.TLabel", wraplength=S(480), justify="left")
    note.pack(anchor="w", pady=(S(10), 0))
    err = ttk.Label(dlg.body, text="", style="CardDanger.TLabel")
    err.pack(anchor="w")

    def palette():
        vals = {k: _hex(v.get()) for k, v in vars_.items()}
        if not all(valid(v) for v in vals.values()):
            return None
        return derive(vals["bg"], vals["panel_bg"], vals["fg"], vals["accent"], vals["header_bg"],
                      base=E.THEMES.get(base_var.get()) or start)

    def preview(*_):
        state["job"] = None
        for k, sw in swatches.items():
            v = _hex(vars_[k].get())
            if valid(v):
                sw.configure(bg=v, highlightbackground=mix(v, "#808080", 0.5))
        pal = palette()
        if not pal:
            err.configure(text="Colours are written like #1f2a44.")
            return
        err.configure(text="")
        ratio = contrast(pal["fg"], pal["panel_bg"])
        note.configure(text=("⚠ Text may be hard to read on the panels - pick a lighter or darker text colour."
                             if ratio < 4.5 else f"Readability {ratio:.1f}:1 (4.5 or more is comfortable)."))
        E.THEMES[PREVIEW] = pal
        app.theme_name = PREVIEW
        try:
            app.apply_theme()
        except Exception:                                      # noqa: BLE001
            pass

    def schedule(*_):
        if state["job"]:
            dlg.after_cancel(state["job"])
        state["job"] = dlg.after(250, preview)

    def load_base(*_):
        b = E.THEMES.get(base_var.get())
        if b:
            for k, v in vars_.items():
                v.set(_hex(b.get(k, v.get())))
            if base_var.get() not in builtin:
                name_var.set(base_var.get())
    base.bind("<<ComboboxSelected>>", load_base)
    for v in vars_.values():
        v.trace_add("write", schedule)

    def validate():
        name = name_var.get().strip()
        pal = palette()
        if not name or name == PREVIEW:
            err.configure(text="Give the theme a name.")
            return E.INVALID
        if name in builtin:
            err.configure(text=f"'{name}' is a built-in theme - choose another name.")
            return E.INVALID
        if not pal:
            err.configure(text="Colours are written like #1f2a44.")
            return E.INVALID
        save_custom(name, pal)
        return name

    def do_export():
        pal = palette()
        if not pal:
            return
        p = filedialog.asksaveasfilename(parent=dlg, title="Export theme", defaultextension=".json",
                                         initialfile=f"{name_var.get().strip() or 'theme'}.atelier-theme.json")
        dlg.grab_set()
        if p:
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"format": "atelier-theme", "name": name_var.get().strip(),
                           "colours": {k: pal[k] for k, _l, _h in PICKS}}, f, indent=2)

    def do_import():
        p = filedialog.askopenfilename(parent=dlg, title="Import theme", filetypes=[("Atelier theme", "*.json")])
        dlg.grab_set()
        if not p:
            return
        try:
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
            cols = data["colours"]
            for k, v in vars_.items():
                if valid(cols.get(k, "")):
                    v.set(_hex(cols[k]))
            name_var.set(str(data.get("name") or "Imported theme"))
        except (OSError, ValueError, KeyError, TypeError):
            err.configure(text="That isn't an Atelier theme file.")

    def do_delete():
        n = base_var.get()
        if n in builtin or n not in E.THEMES:
            err.configure(text="Choose one of your own themes in 'Start from' to delete it.")
            return
        if messagebox.askyesno("Delete theme", f"Delete your theme '{n}'?", parent=dlg):
            delete_custom(n)
            base.configure(values=[x for x in E.THEMES if x != PREVIEW])
            base_var.set("Engineering Slate")
            load_base()
        dlg.grab_set()

    for text, cmd in (("Import…", do_import), ("Export…", do_export), ("Delete", do_delete)):
        ttk.Button(dlg.buttons, text=text, command=cmd).pack(side="left", padx=(0, S(6)))
    dlg.add_buttons("Save theme", "Accent.TButton")
    preview()
    result = dlg.show()
    E.THEMES.pop(PREVIEW, None)
    if result:
        combo = getattr(app, "theme_combo", None)
        if combo is not None:
            combo.configure(values=[x for x in E.THEMES if x != PREVIEW])
        app.theme_var.set(result)
        app.on_theme_change()
    else:
        app.theme_name = orig_name if orig_name in E.THEMES else "Engineering Slate"
        app.apply_theme()


def _hook_main(module):
    cls = getattr(module, "AtelierApp", None)
    orig = getattr(cls, "update_identity", None) if cls else None
    if orig is None or getattr(orig, "_themes", False):
        return

    def update_identity(self, *a, **k):
        res = orig(self, *a, **k)
        try:
            m = self.account_menu
            m.add_separator()
            m.add_command(label="🎨  Theme editor…", command=lambda: theme_editor(self))
        except Exception:                                      # noqa: BLE001
            pass
        return res
    update_identity._themes = True
    cls.update_identity = update_identity


def install(module):
    global E
    E = module
    if getattr(module, "_themes_installed", False):
        return
    module._themes_installed = True
    module.atelier_themes = __import__(__name__)
    apply_palettes(module)
    _hook_style(module)
    _hook_main(module)
    try:
        _hook_menus()
    except Exception:                                          # noqa: BLE001
        pass
