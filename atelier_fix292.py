"""
Atelier PDM 2.9.2 (atelier_fix292.py) - companion panel: no teal frame round the panel, no flashing.
The resize edges (Overlay mode) are painted once in the colour of whatever is behind them; they still resize.
"""
import os
import sys
import time
import traceback

VERSION = "2.9.2"
E = None


def log_error(where):
    try:
        base = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "Atelier PDM")
        os.makedirs(base, exist_ok=True)
        with open(os.path.join(base, "atelier_error.log"), "a", encoding="utf-8") as f:
            f.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} · {where} ({VERSION}) =====\n"
                    f"{traceback.format_exc()}\n")
    except Exception:                                           # noqa: BLE001
        pass


def edge_colours(t, body_bg=None):
    hdr = t.get("header_bg") or t.get("panel_bg") or "#172033"
    body = body_bg or t.get("bg") or t.get("panel_bg") or hdr
    return hdr, body


def place_grips(self):
    import tkinter as tk
    S = self.S
    show = self._overlay_active() and self.expanded
    grips = self.grips
    if "left_hdr" not in grips and "left" in grips:
        g = tk.Frame(self.root, bd=0, highlightthickness=0, cursor="sb_h_double_arrow")
        g.bind("<ButtonPress-1>", lambda e: self._drag_start("left", e))
        g.bind("<B1-Motion>", self._drag_move)
        g.bind("<ButtonRelease-1>", self._drag_end)
        grips["left_hdr"] = g
    try:
        body_bg = self.style.lookup("TFrame", "background") or None
    except Exception:                                           # noqa: BLE001
        body_bg = None
    hdr_col, body_col = edge_colours(self.t, body_bg)
    try:
        hdr_h = max(S(30), self._hdr_widgets[0].winfo_height())
    except Exception:                                           # noqa: BLE001
        hdr_h = S(52)
    for edge, g in grips.items():
        if not show:
            g.place_forget()
            continue
        col = hdr_col if edge in ("top", "left_hdr") else body_col
        if str(g.cget("bg")) != col:
            g.configure(bg=col)
        if edge == "left_hdr":
            g.place(x=0, y=0, width=S(4), height=hdr_h)
        elif edge == "left":
            g.place(x=0, y=hdr_h, width=S(4), relheight=1, height=-hdr_h)
        elif edge == "top":
            g.place(x=0, y=0, relwidth=1, height=S(3))
        else:
            g.place(x=0, rely=1.0, y=-S(4), relwidth=1, height=S(4))
        g.lift()


def patch_panel(mod):
    cls = getattr(mod, "Panel", None)
    if cls is None or getattr(cls, "_atl_292", False) or not hasattr(cls, "_place_grips"):
        return False
    cls._atl_292 = True
    cls._place_grips = place_grips
    return True


def _hook():
    import tkinter as tk
    if getattr(tk.Misc.mainloop, "_atl_292", False):
        return
    orig = tk.Misc.mainloop

    def mainloop(self, n=0):
        pm = sys.modules.get("atelier_panel")
        if pm is not None:
            try:
                patch_panel(pm)
            except Exception:                                   # noqa: BLE001
                log_error("panel edges")
        return orig(self, n)
    mainloop._atl_292 = True
    tk.Misc.mainloop = mainloop


def install(module):
    global E
    E = module
    if getattr(module, "_fix292_installed", False):
        return
    module._fix292_installed = True
    f291 = sys.modules.get("atelier_fix291")
    if f291 is not None:
        f291.quiet_edges = lambda root: 0
    pm = sys.modules.get("atelier_panel")
    if pm is not None:
        patch_panel(pm)
    try:
        _hook()
    except Exception:                                           # noqa: BLE001
        log_error("2.9.2 install")
