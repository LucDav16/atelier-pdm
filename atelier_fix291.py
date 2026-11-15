"""
Atelier PDM 2.9.1 (atelier_fix291.py)
=====================================

* Main window: the Folders and Files panes can no longer be squeezed to nothing (the window opened showing only
  Preview and Properties). Pane widths are checked when the window opens and whenever it's resized, and put back
  to sensible sizes if a pane has collapsed. Account menu › Reset window layout does it on demand.
* Preview placeholder: logo, name and text are spaced from the real font sizes, so the lines no longer overlap
  (main window and panels).
* Change state ▾ button drawn in the theme's colours.
* Companion panel: the coloured frame round the panel (its resize edges in Overlay mode) is invisible - the edges
  still resize the panel, and the cursor still changes over them.
"""
import base64
import os
import sys
import time
import traceback

VERSION = "2.9.1"
E = None
ORIG = {}
RESIZE_CURSORS = {"sb_h_double_arrow", "sb_v_double_arrow", "size_we", "size_ns", "size_nw_se", "size_ne_sw",
                  "sizing", "bottom_side", "top_side", "left_side", "right_side", "bottom_left_corner",
                  "bottom_right_corner", "top_left_corner", "top_right_corner", "double_arrow"}


def log_error(where):
    try:
        base = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "Atelier PDM")
        os.makedirs(base, exist_ok=True)
        with open(os.path.join(base, "atelier_error.log"), "a", encoding="utf-8") as f:
            f.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} · {where} ({VERSION}) =====\n"
                    f"{traceback.format_exc()}\n")
    except Exception:                                           # noqa: BLE001
        pass


# =============================================================================
# 1. Main window panes
# =============================================================================
def pane_targets(total, s0, s1, scale=1.0, force=False):
    """New (sash0, sash1) if a pane is too narrow (or force), else None.
    Panes: Folders [0, s0], Files [s0, s1], Preview/Properties [s1, total]."""
    S = lambda n: int(round(n * scale))
    min_l, min_m, min_r = S(170), S(340), S(300)
    if total < min_l + min_m + min_r:
        return None
    if not force and s0 >= min_l and s1 - s0 >= min_m and total - s1 >= min_r:
        return None
    left = max(min_l, min(S(300), int(total * 0.17)))
    right = max(min_r, min(S(480), int(total * 0.28)))
    if total - left - right < min_m:
        right = max(min_r, total - left - min_m)
    return left, total - right


def guard_panes(app, force=False):
    b = getattr(app, "body", None)
    if b is None:
        return False
    try:
        if not b.winfo_ismapped():
            return False
        total = b.winfo_width()
        s0, s1 = b.sashpos(0), b.sashpos(1)
        scale = getattr(app, "scale", 1.0) or 1.0
        new = pane_targets(total, s0, s1, scale, force)
        if new is None:
            return False
        b.sashpos(0, new[0])
        b.sashpos(1, new[1])
        return True
    except Exception:                                           # noqa: BLE001
        log_error("pane layout")
        return False


def _watch_panes(app):
    b = getattr(app, "body", None)
    if b is None:
        return
    pending = {"job": None}

    def later(_e=None):
        if pending["job"] is None:
            def run():
                pending["job"] = None
                guard_panes(app)
            pending["job"] = app.root.after(120, run)
    b.bind("<Configure>", later, add="+")
    for ms in (300, 900, 2000, 4000):
        app.root.after(ms, lambda: guard_panes(app))


# =============================================================================
# 2. Preview placeholder
# =============================================================================
def block_layout(w, h, logo_sizes, lines, gap=8):
    """Vertical layout of logo + text lines, centred. lines: [(text, line height)] (empty text skipped).
    Returns (logo size or 0, logo centre y, [(text, centre y)]). Drops the logo / shrinks it to fit."""
    lines = [(t, lh) for t, lh in lines if t]
    text_h = sum(lh for _t, lh in lines) + gap * max(0, len(lines) - 1)
    avail = h - 16
    logo = 0
    for s in sorted(logo_sizes, reverse=True):
        if s <= min(w, h) * 0.42 and s + gap * 2 + text_h <= avail:
            logo = s
            break
    total = (logo + gap * 2 if logo else 0) + text_h
    y = max(8, (h - total) / 2)
    logo_y = y + logo / 2 if logo else 0
    y += (logo + gap * 2) if logo else 0
    out = []
    for i, (t, lh) in enumerate(lines):
        out.append((t, y + lh / 2))
        y += lh + gap
    return logo, logo_y, out


_IMG = {}


def _logo(canvas, size):
    key = (str(canvas), size)
    if key in _IMG:
        return _IMG[key]
    try:
        from _logo_pngs import LOGO_PNG
    except ImportError:
        return None
    best = min(LOGO_PNG, key=lambda s: (abs(s - size), -s))
    import tkinter as tk
    img = tk.PhotoImage(master=canvas, data=LOGO_PNG[best])
    _IMG[key] = img
    return img


def render_placeholder(canvas, w, h, t, cam, title, hint, *a, **k):
    try:
        import tkinter.font as tkfont
        E.draw_backdrop(canvas, w, h, t)
        fonts = [("Segoe UI Semibold", 13), ("Segoe UI Semibold", 10), ("Segoe UI", 9)]
        lh = [tkfont.Font(root=canvas, family=f, size=s).metrics("linespace") for f, s in fonts]
        try:
            from _logo_pngs import LOGO_PNG
            sizes = list(LOGO_PNG)
        except ImportError:
            sizes = []
        logo, ly, rows = block_layout(w, h, sizes, [(getattr(E, "APP_NAME", "Atelier PDM"), lh[0]),
                                                     (title, lh[1]), (hint, lh[2])])
        if logo:
            img = _logo(canvas, logo)
            if img is not None:
                canvas.create_image(w / 2, ly, image=img)
                canvas._atl_logo = img
        colours = [t.get("fg", "#e2e8f0"), t.get("fg", "#e2e8f0"), t.get("muted_fg", "#8a97ad")]
        texts = [x for x in ((getattr(E, "APP_NAME", "Atelier PDM"), 0), (title, 1), (hint, 2)) if x[0]]
        for (text, y), (_t, i) in zip(rows, texts):
            canvas.create_text(w / 2, y, text=text, fill=colours[i], font=fonts[i], width=max(60, w - 24),
                               justify="center")
    except Exception:                                           # noqa: BLE001
        log_error("preview placeholder")
        return ORIG["placeholder"](canvas, w, h, t, cam, title, hint, *a, **k)


# =============================================================================
# 3. Change state button
# =============================================================================
def _mix(a, b, f):
    pa, pb = [int(a.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4)], [int(b.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4)]
    return "#%02x%02x%02x" % tuple(int(round(x + (y - x) * f)) for x, y in zip(pa, pb))


def style_state_button(s, t, S):
    bg = t.get("btn_bg") or t.get("panel_bg") or "#1d2840"
    fg = t.get("btn_fg") or t.get("fg") or "#e2e8f0"
    hover = _mix(bg, fg, 0.12)
    s.configure("AtlState.TMenubutton", background=bg, foreground=fg, arrowcolor=t.get("muted_fg", fg),
                borderwidth=0, relief="flat", padding=(S(10), S(4)), font=("Segoe UI", 9))
    s.map("AtlState.TMenubutton", background=[("disabled", t.get("panel_bg", bg)), ("active", hover),
                                              ("pressed", hover)],
          foreground=[("disabled", t.get("muted_fg", fg))])


def _wrap_style():
    fn = getattr(E, "style_ttk", None)
    if fn is None or getattr(fn, "_atl_291", False):
        return

    def style_ttk(root, s, t, S, *a, **k):
        res = fn(root, s, t, S, *a, **k)
        try:
            style_state_button(s, t, S)
        except Exception:                                       # noqa: BLE001
            log_error("state button style")
        return res
    style_ttk._atl_291 = True
    E.style_ttk = style_ttk


# =============================================================================
# 4. Panel: hide the coloured resize edges
# =============================================================================
def quiet_edges(root):
    """Makes resize edges the same colour as what's behind them. Returns how many were changed."""
    n = 0
    try:
        if int(float(root.cget("highlightthickness") or 0)):
            root.configure(highlightthickness=0)
            n += 1
    except Exception:                                           # noqa: BLE001
        pass
    stack = [root]
    while stack:
        w = stack.pop()
        try:
            kids = w.winfo_children()
        except Exception:                                       # noqa: BLE001
            kids = []
        stack.extend(kids)
        if w is root:
            continue
        try:
            cls = w.winfo_class()
            if cls not in ("Frame", "Canvas", "Label", "Toplevel"):
                continue
            if cls == "Toplevel":
                if int(float(w.cget("highlightthickness") or 0)):
                    w.configure(highlightthickness=0)
                    n += 1
                continue
            if str(w.cget("cursor")) not in RESIZE_CURSORS:
                continue
            behind = w.master.cget("bg") if hasattr(w.master, "cget") else None
            if cls == "Frame" and str(w.master.winfo_class()) == "Toplevel" or not behind:
                behind = getattr(_PANEL, "bg", None) or behind
            if behind and str(w.cget("bg")) != str(behind):
                w.configure(bg=behind)
                n += 1
            if int(float(w.cget("highlightthickness") or 0)):
                w.configure(highlightthickness=0)
                n += 1
        except Exception:                                       # noqa: BLE001
            continue
    return n


class _Panel:
    bg = None


_PANEL = _Panel()


def _hook_mainloop():
    import tkinter as tk
    if getattr(tk.Misc.mainloop, "_atl_291", False):
        return
    orig = tk.Misc.mainloop

    def mainloop(self, n=0):
        if "atelier_panel" in sys.modules and isinstance(self, tk.Tk):
            def tick():
                try:
                    try:
                        _PANEL.bg = getattr(sys.modules["atelier_panel"], "PANEL_BG", None)
                    except Exception:                           # noqa: BLE001
                        pass
                    quiet_edges(self)
                    self.after(1000, tick)
                except Exception:                               # noqa: BLE001
                    pass
            self.after(400, tick)
        return orig(self, n)
    mainloop._atl_291 = True
    tk.Misc.mainloop = mainloop


# =============================================================================
# Main window hooks
# =============================================================================
def _hook_app():
    cls = getattr(E, "AtelierApp", None)
    if cls is None or getattr(cls, "_atl_291", False):
        return
    cls._atl_291 = True
    o_build = cls.build_ui

    def build_ui(self, *a, **k):
        res = o_build(self, *a, **k)
        try:
            _watch_panes(self)
        except Exception:                                       # noqa: BLE001
            log_error("pane watch")
        try:
            row = getattr(self, "_flow_row", None)
            if row:
                row[1].configure(style="AtlState.TMenubutton")
        except Exception:                                       # noqa: BLE001
            pass
        return res
    cls.build_ui = build_ui
    o_ident = getattr(cls, "update_identity", None)
    if o_ident:
        def update_identity(self, *a, **k):
            res = o_ident(self, *a, **k)
            try:
                self.account_menu.add_command(label="⊞  Reset window layout",
                                              command=lambda: guard_panes(self, force=True))
            except Exception:                                   # noqa: BLE001
                pass
            return res
        cls.update_identity = update_identity


def install(module):
    global E
    E = module
    if getattr(module, "_fix291_installed", False):
        return
    module._fix291_installed = True
    for n in ("atelier_polish", "atelier_guide", "atelier_flow", "atelier_access", "atelier_states",
              "atelier_layout", "atelier_v29"):
        mm = sys.modules.get(n)
        if mm is not None:
            mm.VERSION = VERSION
    if hasattr(module, "render_placeholder"):
        ORIG["placeholder"] = module.render_placeholder
        module.render_placeholder = render_placeholder
    for fn in (_wrap_style, _hook_app, _hook_mainloop):
        try:
            fn()
        except Exception:                                       # noqa: BLE001
            log_error("2.9.1 install")
