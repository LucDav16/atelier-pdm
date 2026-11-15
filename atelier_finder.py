"""
Atelier PDM - finding programs on this PC (atelier_finder.py)  2.7.2
====================================================================

Finds CAD / CAM programs however they were installed, and remembers what it finds (per Windows account):

    1. a location you picked with "Locate…"          (remembered in your Atelier settings)
    2. atelier_config.json  ("cam_apps" / "cad_apps" -> "exe")
    3. the system PATH and Windows "App Paths"
    4. Windows' list of installed programs (Settings > Apps) - install folder / icon
    5. the usual install folders: %LOCALAPPDATA%\\Programs, Program Files, Program Files (x86), C:\\, and
       unzipped copies in Downloads / Desktop / Documents
    6. Start-menu and desktop shortcuts, and the copy that's running right now

Also hooks into atelier_pdm:
    * Administration > CAD Apps uses this search (PhysCAD installed per-user is found) and lists CamCraft.
    * Opening a part in its CAD program uses it too.
    * Dialogs never open taller than the screen, and their buttons stay visible (they were squashed into blank
      bars on screens with scaling).
"""
import os
import re
import shutil
import subprocess
import sys

WIN = sys.platform == "win32"
E = None
_CACHE = {}
_HOW = {}
SKIP_STEMS = ("unins", "uninstall", "setup", "install", "update", "crash", "helper", "cli")
NO_WINDOW = 0x08000000

KNOWN = {   # extra exe names / folder names worth trying per program
    "camcraft": ("camcraft", "cam_craft", "cam-craft"),
    "physcad": ("physcad",),
    "freecad": ("freecad",),
    "solidcraft": ("solidcraft",),
}


# =============================================================================
# Settings
# =============================================================================
def _settings():
    try:
        return E.load_settings() or {}
    except Exception:                                           # noqa: BLE001
        return {}


def remember(app, path):
    s = _settings()
    d = dict(s.get("cam_exe") or {})
    d[app] = os.path.normpath(path)
    s["cam_exe"] = d
    E.save_settings(s)
    _CACHE[app.lower()] = d[app]
    _HOW[app.lower()] = "chosen with Locate…"


def forget(app):
    s = _settings()
    d = dict(s.get("cam_exe") or {})
    d.pop(app, None)
    s["cam_exe"] = d
    for k in (f"{app.lower()}_panel_exe",) + (("panel_exe",) if app.lower() == "solidcraft" else ()):
        s.pop(k, None)
    E.save_settings(s)
    clear_cache()


def clear_cache():
    _CACHE.clear()
    _HOW.clear()


def how(app):
    return _HOW.get(app.lower(), "")


# =============================================================================
# Matching
# =============================================================================
def _keys(app):
    k = app.lower().replace(" ", "")
    return tuple(dict.fromkeys((k,) + KNOWN.get(k, ())))


def _good_exe(path, keys, exact_only=False):
    if not path or not path.lower().endswith(".exe") or not os.path.isfile(path):
        return False
    stem = os.path.splitext(os.path.basename(path))[0].lower().replace(" ", "")
    if any(s in stem for s in SKIP_STEMS) and stem not in keys:
        return False
    return stem in keys if exact_only else any(k in stem for k in keys)


def _best(paths, keys):
    """Prefer an exact name (camcraft.exe) over a partial one (camcraft-gui.exe); shortest path wins ties."""
    paths = [p for p in dict.fromkeys(os.path.normpath(p) for p in paths if p) if _good_exe(p, keys)]
    if not paths:
        return None
    exact = [p for p in paths if _good_exe(p, keys, exact_only=True)]
    return sorted(exact or paths, key=lambda p: (len(p), p.lower()))[0]


def _scan_folder(folder, keys, depth=3):
    found = []
    if not os.path.isdir(folder):
        return found
    base_depth = folder.rstrip("\\/").count(os.sep)
    for root, dirs, files in os.walk(folder):
        if root.count(os.sep) - base_depth >= depth:
            dirs[:] = []
        dirs[:] = [d for d in dirs if not d.startswith((".", "$")) and d.lower() not in ("node_modules", "__pycache__")]
        for f in files:
            if f.lower().endswith(".exe"):
                p = os.path.join(root, f)
                if _good_exe(p, keys):
                    found.append(p)
    return found


# =============================================================================
# Sources
# =============================================================================
def from_settings(app):
    s = _settings()
    p = (s.get("cam_exe") or {}).get(app)
    if p and os.path.isfile(p):
        return p, "chosen with Locate…"
    for k in (f"{app.lower()}_panel_exe",) + (("panel_exe",) if app.lower() == "solidcraft" else ()):
        p = s.get(k)
        if p and os.path.isfile(p):
            return p, "found by the panel"
    return None, ""


def from_config(app):
    cfg = getattr(E, "CONFIG", {}) or {}
    for section in ("cam_apps", "cad_apps"):
        sec = cfg.get(section) or {}
        for name, entry in sec.items():
            if name.lower() == app.lower() and isinstance(entry, dict):
                for k in ("exe", "cmd"):
                    p = entry.get(k)
                    if p and os.path.isfile(p) and p.lower().endswith(".exe"):
                        return p, "atelier_config.json"
    return None, ""


def from_path(app, keys):
    for k in keys:
        p = shutil.which(k)
        if p and _good_exe(p, keys):
            return p, "system PATH"
    return None, ""


def _reg_values():
    """[(DisplayName, DisplayIcon, InstallLocation)] for installed programs, plus App Paths as ('', exe, '')."""
    if not WIN:
        return []
    import winreg
    out = []
    roots = [(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
             (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
             (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall")]
    for hive, path in roots:
        try:
            key = winreg.OpenKey(hive, path)
        except OSError:
            continue
        with key:
            for i in range(winreg.QueryInfoKey(key)[0]):
                try:
                    with winreg.OpenKey(key, winreg.EnumKey(key, i)) as sub:
                        def val(n):
                            try:
                                return str(winreg.QueryValueEx(sub, n)[0])
                            except OSError:
                                return ""
                        name = val("DisplayName")
                        if name:
                            out.append((name, val("DisplayIcon"), val("InstallLocation")))
                except OSError:
                    continue
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            key = winreg.OpenKey(hive, r"Software\Microsoft\Windows\CurrentVersion\App Paths")
        except OSError:
            continue
        with key:
            for i in range(winreg.QueryInfoKey(key)[0]):
                try:
                    exe = winreg.EnumKey(key, i)
                    with winreg.OpenKey(key, exe) as sub:
                        out.append(("", str(winreg.QueryValueEx(sub, "")[0]), ""))
                except OSError:
                    continue
    return out


def from_registry(app, keys):
    cands = []
    for name, icon, loc in _reg_values():
        flat = name.lower().replace(" ", "")
        icon = (icon or "").split(",")[0].strip().strip('"')
        if name and not any(k in flat for k in keys):
            continue
        if not name and not _good_exe(icon, keys):
            continue
        if icon:
            cands.append(icon)
        if loc:
            cands += _scan_folder(loc.strip().strip('"'), keys, depth=2)
    p = _best(cands, keys)
    return (p, "Windows installed apps") if p else (None, "")


def _search_roots():
    env = os.environ.get
    home = os.path.expanduser("~")
    roots = [os.path.join(env("LOCALAPPDATA", ""), "Programs"), env("ProgramFiles", r"C:\Program Files"),
             env("ProgramFiles(x86)", ""), env("ProgramW6432", ""), env("LOCALAPPDATA", ""), env("APPDATA", ""),
             "C:\\" if WIN else "", os.path.join(home, "Downloads"), os.path.join(home, "Desktop"),
             os.path.join(home, "Documents"), os.path.join(home, "Apps"), os.path.join(home, "Programs")]
    od = env("OneDrive")
    if od:
        roots += [os.path.join(od, "Desktop"), os.path.join(od, "Documents")]
    return [r for r in dict.fromkeys(roots) if r and os.path.isdir(r)]


def from_folders(app, keys):
    cands = []
    for root in _search_roots():
        try:
            names = os.listdir(root)
        except OSError:
            continue
        for n in names:
            low = n.lower().replace(" ", "")
            p = os.path.join(root, n)
            if any(k in low for k in keys):
                if os.path.isdir(p):
                    cands += _scan_folder(p, keys, depth=3)
                elif _good_exe(p, keys):
                    cands.append(p)
    p = _best(cands, keys)
    return (p, "install folder") if p else (None, "")


def _powershell(script, timeout=20):
    if not WIN:
        return ""
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command",
                            script], capture_output=True, text=True, timeout=timeout, creationflags=NO_WINDOW)
        return r.stdout or ""
    except (OSError, subprocess.SubprocessError):
        return ""


def from_shortcuts_and_running(app, keys):
    pat = "|".join(re.escape(k) for k in keys)
    script = (
        "$ErrorActionPreference='SilentlyContinue';"
        "$sh=New-Object -ComObject WScript.Shell;"
        "$d=@([Environment]::GetFolderPath('Programs'),[Environment]::GetFolderPath('CommonPrograms'),"
        "[Environment]::GetFolderPath('Desktop'),[Environment]::GetFolderPath('CommonDesktopDirectory'));"
        f"foreach($x in $d){{ if($x){{ Get-ChildItem -Path $x -Recurse -Filter *.lnk | "
        f"Where-Object {{ $_.BaseName -replace ' ','' -match '(?i){pat}' }} | "
        "ForEach-Object { $sh.CreateShortcut($_.FullName).TargetPath } } };"
        f"Get-Process | Where-Object {{ $_.ProcessName -match '(?i){pat}' }} | ForEach-Object {{ $_.Path }}")
    lines = [l.strip() for l in _powershell(script).splitlines() if l.strip()]
    p = _best(lines, keys)
    return (p, "Start-menu shortcut / running program") if p else (None, "")


SOURCES = [from_settings, from_config]
SEARCHES = [from_path, from_registry, from_folders, from_shortcuts_and_running]


def find_app(app, refresh=False):
    """Full path of a program's .exe, or None. Results (including 'not found') are cached until refreshed."""
    k = app.lower()
    if not refresh and k in _CACHE:
        return _CACHE[k]
    keys = _keys(app)
    found, src = None, ""
    for fn in SOURCES:
        found, src = fn(app)
        if found:
            break
    if not found:
        for fn in SEARCHES:
            try:
                found, src = fn(app, keys)
            except Exception:                                   # noqa: BLE001 - a broken source mustn't stop the rest
                found, src = None, ""
            if found:
                break
    _CACHE[k] = os.path.normpath(found) if found else None
    _HOW[k] = src if found else "not found"
    return _CACHE[k]


# =============================================================================
# Hooks into atelier_pdm
# =============================================================================
def _cam_apps():
    cam = sys.modules.get("atelier_cam")
    return [a for a in getattr(cam, "CAM_APPS", ["CamCraft", "FreeCAD", "PhysCAD", "SolidCraft"]) if a != "Other"]


def _hook_cad(module):
    orig_launcher = getattr(module, "cad_launcher", None)
    if orig_launcher and not getattr(orig_launcher, "_finder", False):
        def cad_launcher(app):
            try:
                p = orig_launcher(app)
            except Exception:                                   # noqa: BLE001
                p = None
            if p:
                return p
            label = (getattr(module, "CAD_APPS", {}).get(app) or {}).get("label", app)
            return find_app(label)
        cad_launcher._finder = True
        module.cad_launcher = cad_launcher

    orig_status = getattr(module, "cad_status", None)
    if orig_status and not getattr(orig_status, "_finder", False):
        def cad_status():
            rows = list(orig_status())
            have = {str(r.get("label", "")).lower() for r in rows}
            for r in rows:
                if str(r.get("open", "")).startswith("Windows file association"):
                    p = find_app(r.get("label", r.get("key", "")))
                    if p:
                        r["open"] = p
            for app in _cam_apps():
                if app.lower() in have:
                    continue
                p = find_app(app)
                rows.append({"key": app.lower(), "label": app, "types": "CAM job, NC programs",
                             "open": p or "Not found - CAM & Tools › CAM programs › Locate…",
                             "convert": "— (CAM program)", "auto": "Open part in CAM (STEP of current revision)"})
            return rows
        cad_status._finder = True
        module.cad_status = cad_status

    orig_clear = getattr(module, "clear_tool_cache", None)
    if orig_clear and not getattr(orig_clear, "_finder", False):
        def clear_tool_cache():
            orig_clear()
            clear_cache()
        clear_tool_cache._finder = True
        module.clear_tool_cache = clear_tool_cache


def fit(w, h, sw, sh):
    """Width / height clamped to the screen (room left for the taskbar and title bar)."""
    return max(200, min(w, sw - 40)), max(160, min(h, sh - 90))


def _hook_dialogs(module):
    cls = getattr(module, "ThemedDialog", None)
    if cls is None or getattr(cls, "_finder_fixed", False):
        return
    cls._finder_fixed = True
    orig_init = cls.__init__

    def __init__(self, *a, **k):
        orig_init(self, *a, **k)
        try:                                                    # buttons first, so the body can't squash them
            info = self.buttons.pack_info()
            self.buttons.pack_forget()
            self.buttons.pack(side="bottom", fill="x", pady=info.get("pady", 0), before=self.body)
        except Exception:                                       # noqa: BLE001
            pass
    cls.__init__ = __init__

    def geometry(self, new=None):
        import tkinter as tk
        if new:
            m = re.match(r"^(\d+)x(\d+)(.*)$", str(new))
            if m:
                w, h = fit(int(m.group(1)), int(m.group(2)), self.winfo_screenwidth(), self.winfo_screenheight())
                new = f"{w}x{h}{m.group(3)}"
        return tk.Wm.wm_geometry(self, new)
    cls.geometry = geometry


def install(module):
    global E
    E = module
    if getattr(module, "_finder_installed", False):
        return
    module._finder_installed = True
    module.find_app = find_app
    _hook_cad(module)
    _hook_dialogs(module)
