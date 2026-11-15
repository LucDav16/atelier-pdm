"""
Atelier PDM 2.9.3 (atelier_fix293.py) - SolidCraft and PhysCAD panels open on the program.

When the panel starts its CAD program, the program's window is made to fill the screen beside the panel the first
time it appears (maximised in Overlay mode), even if it opened at a smaller size first. Loading screens are left
alone, and this only applies for the first 40 seconds, so moving or resizing the program later is respected.
"""
import os
import sys
import time
import traceback

VERSION = "2.9.3"
E = None
ORIG = {}
LAUNCH_WINDOW = 40.0
MIN_W, MIN_H = 700, 500


def log_error(where):
    try:
        base = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "Atelier PDM")
        os.makedirs(base, exist_ok=True)
        with open(os.path.join(base, "atelier_error.log"), "a", encoding="utf-8") as f:
            f.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} · {where} ({VERSION}) =====\n"
                    f"{traceback.format_exc()}\n")
    except Exception:                                           # noqa: BLE001
        pass


def _pm():
    return sys.modules.get("atelier_panel")


def _in_launch(self):
    t0 = self.__dict__.setdefault("_atl_t0", time.monotonic())
    return bool(getattr(getattr(self, "args", None), "launch", False)) and time.monotonic() - t0 < LAUNCH_WINDOW


def _big(ext):
    return bool(ext) and (ext[2] - ext[0]) >= MIN_W and (ext[3] - ext[1]) >= MIN_H


def fit_solidcraft(self, h, ext, work, reserve):
    pm = _pm()
    if pm is not None and h not in self.shrunk and _in_launch(self):
        if not _big(ext):
            return ext
        if not pm.is_zoomed(h):
            self.shrunk[h] = (True, None)                       # closing the panel maximises it again
            self.reserved[h] = reserve
            pm.show_window(h, pm.SW_RESTORE)
            pm.set_visible_rect(h, work[0], work[1], work[2] - reserve, work[3])
            return pm.window_rect(h) or ext
    return ORIG["fit"](self, h, ext, work, reserve)


def track_window(self):
    res = ORIG["track"](self)
    try:
        pm = _pm()
        h = getattr(self, "sc_hwnd", None)
        if pm is not None and h and _in_launch(self) and self.mode_var.get() == "overlay":
            done = self.__dict__.setdefault("_atl_maxed", set())
            if h not in done:
                ext = pm.window_rect(h)
                if _big(ext):
                    done.add(h)
                    if not pm.is_zoomed(h) and not pm.is_iconic(h):
                        pm.show_window(h, pm.SW_MAXIMIZE)
                        self._last_geom = None
    except Exception:                                           # noqa: BLE001
        log_error("panel placement")
    return res


def patch_panel(mod):
    cls = getattr(mod, "Panel", None)
    if cls is None or getattr(cls, "_atl_293", False):
        return False
    if not all(hasattr(cls, n) for n in ("_fit_solidcraft", "_track_window")):
        return False
    cls._atl_293 = True
    ORIG.update(fit=cls._fit_solidcraft, track=cls._track_window)
    cls._fit_solidcraft = fit_solidcraft
    cls._track_window = track_window
    return True


def _hook():
    import tkinter as tk
    if getattr(tk.Misc.mainloop, "_atl_293", False):
        return
    orig = tk.Misc.mainloop

    def mainloop(self, n=0):
        pm = _pm()
        if pm is not None:
            try:
                patch_panel(pm)
            except Exception:                                   # noqa: BLE001
                log_error("2.9.3 panel")
        return orig(self, n)
    mainloop._atl_293 = True
    tk.Misc.mainloop = mainloop


def install(module):
    global E
    E = module
    if getattr(module, "_fix293_installed", False):
        return
    module._fix293_installed = True
    pm = _pm()
    if pm is not None:
        patch_panel(pm)
    try:
        _hook()
    except Exception:                                           # noqa: BLE001
        log_error("2.9.3 install")
