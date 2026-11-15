"""
Atelier PDM launcher (atelier_launcher.py)  -  2.7.0
====================================================

The one program the installer puts on the PC ("Atelier PDM.exe"). Start-menu shortcuts run it with:

    Atelier PDM.exe                                  the full Atelier PDM app
    Atelier PDM.exe --panel solidcraft --launch      SolidCraft with the Atelier panel
    Atelier PDM.exe --panel physcad --launch         PhysCAD with the Atelier panel
    Atelier PDM.exe --cam                            CAM & Tools (NC programs, tool libraries, machines & posts)
    Atelier PDM.exe --setup                          run setup again (change vault / connection)

2.7.0  CAM in the vault (atelier_cam.py / atelier_cam_ui.py): CAM & Tools button in the main window, in the
       panels' menu, and its own Start-menu shortcut.
2.6.1  Own icon and taskbar button per mode; with --launch the CAD program starts first (maximised) and the
       panel appears once its window is up.

During development the same works with:  python atelier_launcher.py [options]
"""
import argparse
import os
import subprocess
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import atelier_setup as setup                                       # noqa: E402

LOG = os.path.join(setup.APP_DATA, "atelier_error.log")
WIN = sys.platform == "win32"

# Taskbar identity and icon per mode. The installer gives each shortcut the same ID, so pinned shortcuts and
# running windows line up.
APP_IDS = {None: "AtelierPDM.App", "solidcraft": "AtelierPDM.Panel.SolidCraft",
           "physcad": "AtelierPDM.Panel.PhysCAD", "cam": "AtelierPDM.CAM"}
ICONS = {None: "atelier.ico", "solidcraft": "atelier_solidcraft.ico", "physcad": "atelier_physcad.ico",
         "cam": "atelier_cam.ico"}
CAD_WAIT_SECONDS = 90
SW_SHOWMAXIMIZED = 3


# =============================================================================
# Small helpers
# =============================================================================
def self_command(*args):
    """Command line that starts this launcher again with args."""
    if getattr(sys, "frozen", False):
        return [sys.executable, *args]
    exe = sys.executable
    if WIN:
        pw = os.path.join(os.path.dirname(exe), "pythonw.exe")
        exe = pw if os.path.exists(pw) else exe
    return [exe, os.path.abspath(__file__), *args]


def relaunch(*args):
    subprocess.Popen(self_command(*args), cwd=HERE, close_fds=True)


def log_error(text):
    try:
        os.makedirs(setup.APP_DATA, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n{text}\n")
    except OSError:
        pass


def message(kind, text, title="Atelier PDM"):
    try:
        import tkinter as tk
        from tkinter import messagebox
        r = tk.Tk()
        r.withdraw()
        try:
            if kind == "retry":
                return messagebox.askretrycancel(title, text, parent=r)
            if kind == "yesno":
                return messagebox.askyesno(title, text, parent=r)
            (messagebox.showerror if kind == "error" else messagebox.showinfo)(title, text, parent=r)
        finally:
            r.destroy()
    except Exception:                                                # noqa: BLE001 - last resort
        print(text, file=sys.stderr)
    return None


def resource(name):
    """Path of a bundled file (icons), whether running from source or as the installed program."""
    bases = [getattr(sys, "_MEIPASS", None), HERE, os.path.join(HERE, "_internal")]
    for base in filter(None, bases):
        for sub in ("icons", ""):
            p = os.path.join(base, sub, name)
            if os.path.isfile(p):
                return p
    return None


# =============================================================================
# Taskbar identity and window icon
# =============================================================================
def set_identity(mode):
    """Gives this process its own taskbar button and makes every Tk window use the mode's icon."""
    if WIN:
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(ctypes.c_wchar_p(APP_IDS[mode]))
        except (AttributeError, OSError):
            pass
    ico = resource(ICONS[mode])
    if not ico:
        return None
    try:
        import tkinter as tk
    except ImportError:
        return ico
    if getattr(tk.Tk, "_atelier_icon", None) == ico:
        return ico
    orig = getattr(tk.Tk, "_atelier_orig_init", tk.Tk.__init__)

    def init(self, *a, **k):
        orig(self, *a, **k)
        try:
            self.iconbitmap(default=ico)                            # -default: dialogs and pop-ups too
        except tk.TclError:
            pass
    tk.Tk._atelier_orig_init = orig
    tk.Tk.__init__ = init
    tk.Tk._atelier_icon = ico
    return ico


# =============================================================================
# Vault
# =============================================================================
def reach_vault(cfg):
    """True once the vault folder is reachable; offers Retry / setup when it isn't."""
    while not os.path.isdir(cfg["vault"]):
        where = {"cloud": "Check the cloud drive app is running and signed in.",
                 "server": "Check you're on the network and the server is switched on."}.get(cfg.get("mode"), "")
        if message("retry", f"Can't reach the vault:\n{cfg['vault']}\n\n{where}\n\n"
                            "Retry, or Cancel to change the setup.") is not True:
            if message("yesno", "Run setup to choose a different vault?"):
                relaunch("--setup")
            return False
    return True


def load_engine(cfg):
    os.environ["ATELIER_VAULT"] = cfg["vault"]
    if cfg.get("sandbox"):
        os.environ["ATELIER_SANDBOX"] = cfg["sandbox"]
    import atelier_pdm as engine
    try:
        import atelier_secure
        atelier_secure.install(engine)                              # no-op if atelier_pdm already did it
    except ImportError:
        pass
    try:
        import atelier_cam
        atelier_cam.install(engine)                                 # NC programs, tool libraries, CAM button
    except ImportError:
        pass
    return engine


# =============================================================================
# Starting the CAD program before the panel
# =============================================================================
def find_cad_exe(engine, panel, app):
    """Same search order as the panel: remembered location, atelier_config.json, PATH, usual folders."""
    try:
        saved = (engine.load_settings() or {}).get(f"{getattr(app, 'settings_prefix', '')}panel_exe")
    except Exception:                                                # noqa: BLE001
        saved = None
    if saved and os.path.isfile(saved):
        return saved
    cfg = (getattr(engine, "CONFIG", {}) or {}).get("cad_apps") or {}
    exe = (cfg.get(app.key) or {}).get("exe")
    if exe and os.path.isfile(exe):
        return exe
    try:
        found = engine.cad_launcher(app.key)
        if found:
            return found
    except Exception:                                                # noqa: BLE001
        pass
    cands = getattr(panel, "candidate_exe_paths", None)
    if cands:
        return next((p for p in cands(app.key, app.label) if os.path.isfile(p)), None)
    return None


def start_maximised(exe):
    kw = {"cwd": os.path.dirname(exe) or None, "close_fds": True}
    if WIN:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = SW_SHOWMAXIMIZED
        kw["startupinfo"] = si
    return subprocess.Popen([exe], **kw)


def wait_for_window(find, names, timeout=CAD_WAIT_SECONDS, proc=None, sleep=time.sleep, clock=time.monotonic):
    """Window handle of the CAD program once it appears, or None (timed out, or the program quit)."""
    end = clock() + timeout
    while clock() < end:
        h = find(names)
        if h:
            return h
        if proc is not None and proc.poll() is not None and proc.returncode not in (0, None):
            return None                                             # it crashed / refused to start
        sleep(0.25)
    return None


def prepare_cad(engine, panel, key, sleep=time.sleep):
    """Starts the CAD program maximised (unless it's already open) and waits for its window.
    Returns True when the launcher started it, so the panel mustn't start a second copy."""
    apps = getattr(panel, "APPS", None)
    find = getattr(panel, "find_window", None)
    if not apps or key not in apps or find is None:
        return False                                                # older panel: it starts the program itself
    app = apps[key]
    names = {n.lower() for n in app.exe_names}
    if find(names):
        return True                                                 # already running: just attach to it
    exe = find_cad_exe(engine, panel, app)
    if not exe:
        return False                                                # the panel will offer "Locate <App>.exe"
    names.add(os.path.basename(exe).lower())
    try:
        proc = start_maximised(exe)
    except OSError as e:
        log_error(f"Couldn't start {exe}: {e}")
        return False
    h = wait_for_window(find, names, proc=proc, sleep=sleep)
    if h:
        sleep(0.8)                                                  # let it finish sizing itself
        zoomed = getattr(panel, "is_zoomed", lambda _h: True)
        if not zoomed(h):
            getattr(panel, "show_window", lambda *_: None)(h, SW_SHOWMAXIMIZED)
    return True


def skip_first_launch(panel):
    """The panel's --launch starts the CAD program; the launcher already has, so skip that one call."""
    cls = getattr(panel, "Panel", None)
    orig = getattr(cls, "launch_solidcraft", None) if cls else None
    if orig is None or getattr(orig, "_atelier_skip", False):
        return
    state = {"skip": True}

    def launch(self):
        if state["skip"]:
            state["skip"] = False
            return None
        return orig(self)
    launch._atelier_skip = True
    cls.launch_solidcraft = launch


def add_cam_to_panel(panel):
    """Adds 'CAM & Tools…' to the panel's menu (opens in the panel's own window and sign-in)."""
    cls = getattr(panel, "Panel", None)
    orig = getattr(cls, "build_menu", None) if cls else None
    if orig is None or getattr(orig, "_atelier_cam", False):
        return

    def build_menu(self, *a, **k):
        res = orig(self, *a, **k)
        try:
            import atelier_cam
            self.menu.add_separator()
            self.menu.add_command(label="🛠  CAM & Tools…", command=lambda: atelier_cam.open_window(self))
        except Exception:                                            # noqa: BLE001 - never break the menu
            pass
        return res
    build_menu._atelier_cam = True
    cls.build_menu = build_menu


# =============================================================================
# Main
# =============================================================================
def main(argv=None):
    ap = argparse.ArgumentParser(prog="Atelier PDM")
    ap.add_argument("--setup", action="store_true", help="run setup (choose or change the vault)")
    ap.add_argument("--panel", choices=("solidcraft", "physcad"), help="start the companion panel for a CAD app")
    ap.add_argument("--cam", action="store_true", help="CAM & Tools: NC programs, tool libraries, machines & posts")
    ap.add_argument("--launch", action="store_true", help="with --panel: also start the CAD program")
    ap.add_argument("--float", action="store_true", help="with --panel: floating window")
    ap.add_argument("--port", type=int, help="with --panel solidcraft: fixed control port")
    args = ap.parse_args(argv)
    mode = args.panel or ("cam" if args.cam else None)
    set_identity(mode)

    cfg = setup.load_install()
    if args.setup or not cfg:
        res = setup.run_wizard(cfg)
        if not res:
            return
        _cfg, open_now = res
        if open_now or args.panel or args.cam:
            extra = (["--panel", args.panel] + (["--launch"] if args.launch else [])) if args.panel else \
                (["--cam"] if args.cam else [])
            relaunch(*extra)                                         # start fresh with the new settings
        return
    if not reach_vault(cfg):
        return
    engine = load_engine(cfg)
    if args.cam:
        import atelier_cam_ui
        atelier_cam_ui.main()
        return
    if not args.panel:
        engine.main()
        return

    import atelier_panel as panel
    panel.shutil = getattr(engine, "secure_shutil", panel.shutil)  # read-only copies decrypt correctly
    panel.Panel.open_full_app = lambda self: relaunch()
    add_cam_to_panel(panel)
    if args.launch and prepare_cad(engine, panel, args.panel):
        skip_first_launch(panel)
    argv2 = ["--app", args.panel] + (["--launch"] if args.launch else []) + (["--float"] if args.float else [])
    if args.port:
        argv2 += ["--port", str(args.port)]
    panel.main(argv2)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:                                                # noqa: BLE001
        tb = traceback.format_exc()
        log_error(tb)
        message("error", "Atelier PDM hit a problem and has to close.\n\n"
                         f"{tb.strip().splitlines()[-1]}\n\nDetails were saved to:\n{LOG}")
