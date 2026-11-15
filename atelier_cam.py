"""
Atelier PDM - CAM in the vault (atelier_cam.py)  v1.1
=====================================================

Adds to the vault, shared by every CAM program (CamCraft, FreeCAD, PhysCAD, SolidCraft CAM):

CAM setups and NC programs, linked to a part
    CAM setup (the CAM program's own job file) and the posted G-code are stored against the part and the
    revision they were made from:
        <project>\\CAM Setups\\01_0004 Bracket OP1 A-02.<camcraft job>
        <project>\\NC Programs\\01_0004 Bracket OP1 A-02.tap
    Each records the CAM program and version (and for NC programs the machine, post and tool library). When the
    part is up-issued, or checked in after the file was made, it shows as out of date. In the main window they
    appear under their part in the same tree.
    "Open part in CAM" gives the CAM program a STEP (or STL) of the current part revision. "Open setup for
    editing" puts the setup in your working folder and opens it; "Save setup back" files it again.

Tool libraries (one neutral library, exported to each CAM program's format)
    Tools with geometry, holder, CAD files and feeds & speeds per material. Exports:
        Fusion 360 / HSM tool library (.json)   PhysCAD imports this; also Fusion-style CAM programs
        FreeCAD tool bits + library (.fctb / .fctl)   geometry only - FreeCAD keeps feeds in tool controllers
        CSV (any spreadsheet)                   LinuxCNC tool table (.tbl)
    Imports: Fusion 360 / HSM libraries (.json or .tools) and Atelier CSV.

Machines & posts
    One folder per machine with its settings (spindle limit, max feed, program folder, NC extension) and a
    Posts folder per CAM program. Replacing a post keeps the old one in the machine's history.

Storage:  <vault>\\_CAM\\Tool Libraries\\<library>\\library.json  (+ Tool CAD, Holder CAD)
          <vault>\\_CAM\\Machines\\<machine>\\machine.json        (+ Posts\\<CAM program>, History)
Installed into atelier_pdm by the launcher:  atelier_cam.install(engine)
"""
import csv
import glob
import io
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
import zipfile

CAM_VERSION = "1.1"
E = None                              # the atelier_pdm module, set by install()

DEFAULT_NC_EXTS = [".nc", ".tap", ".ngc", ".gcode", ".gc", ".cnc", ".eia", ".iso", ".mpf"]
CAM_APPS = ["CamCraft", "FreeCAD", "PhysCAD", "SolidCraft", "Other"]
NC_FOLDER = "NC Programs"
SETUP_FOLDER = "CAM Setups"
CAM_DIR_NAME = "_CAM"
LIB_FILE = "library.json"
MACHINE_FILE = "machine.json"
CAD_EXTS = (".step", ".stp", ".stl", ".iges", ".igs", ".obj", ".dxf", ".fcstd", ".sc", ".physcad")
BLOCKED_SETUP_EXTS = {".exe", ".bat", ".cmd", ".py", ".pyw", ".vbs", ".ps1", ".msi", ".dll", ".lnk"}
KIND_LABEL = {"nc": "NC program", "setup": "CAM setup"}

# Tool types: key -> (label, Fusion/HSM type, FreeCAD shape)
TOOL_TYPES = {
    "flat":    ("Flat end mill", "flat end mill", "endmill.fcstd"),
    "ball":    ("Ball end mill", "ball end mill", "ballend.fcstd"),
    "bull":    ("Bull nose end mill", "bull nose end mill", "bullnose.fcstd"),
    "chamfer": ("Chamfer mill", "chamfer mill", "chamfer.fcstd"),
    "vbit":    ("Engraving / V-bit", "chamfer mill", "v-bit.fcstd"),
    "face":    ("Face mill", "face mill", "endmill.fcstd"),
    "slot":    ("Slot mill", "slot mill", "slittingsaw.fcstd"),
    "thread":  ("Thread mill", "thread mill", "thread-mill.fcstd"),
    "drill":   ("Drill", "drill", "drill.fcstd"),
    "spot":    ("Spot drill", "spot drill", "drill.fcstd"),
    "reamer":  ("Reamer", "reamer", "endmill.fcstd"),
    "tap":     ("Tap", "tap right hand", "drill.fcstd"),
    "bore":    ("Boring bar", "boring bar", "endmill.fcstd"),
}
FUSION_TO_KEY = {v[1]: k for k, v in TOOL_TYPES.items() if k != "vbit"}
COOLANTS = ["flood", "mist", "air", "through tool", "none"]
DEFAULT_MATERIALS = ["Aluminium", "Mild steel", "Stainless steel", "Tool steel", "Cast iron", "Brass",
                     "Plastic", "Wood"]

GEOMETRY_FIELDS = [   # key, label, unit
    ("diameter", "Cutting diameter", "mm"), ("flutes", "Flutes", ""), ("flute_length", "Flute length", "mm"),
    ("overall_length", "Overall length", "mm"), ("stickout", "Stick-out from holder", "mm"),
    ("shank_diameter", "Shank diameter", "mm"), ("corner_radius", "Corner radius", "mm"),
    ("tip_angle", "Point / included angle", "°"), ("tip_diameter", "Tip diameter", "mm"),
    ("thread_pitch", "Thread pitch", "mm"),
]
PRESET_FIELDS = [     # key, label, unit
    ("vc", "Cutting speed Vc", "m/min"), ("fz", "Feed per tooth fz", "mm"), ("rpm", "Spindle speed (optional)", "rpm"),
    ("feed", "Cutting feed (optional)", "mm/min"), ("plunge", "Plunge feed", "mm/min"),
    ("ap", "Depth of cut ap", "mm"), ("ae", "Stepover ae", "mm"),
]


# =============================================================================
# Small helpers
# =============================================================================
def now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def num(v, default=None):
    """Float from user text ('6', '6.0', '6,5', '') or default."""
    if v is None:
        return default
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", ".")
    if not s:
        return default
    try:
        return float(s)
    except ValueError:
        return default


def fmt(v, digits=3):
    if v is None or v == "":
        return ""
    v = float(v)
    return str(int(v)) if v == int(v) else f"{v:.{digits}f}".rstrip("0").rstrip(".")


def safe_name(name):
    name = re.sub(r'[\\/:*?"<>|]+', "-", (name or "").strip()).strip(" .")
    return name[:80]


def _json_load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _json_save(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def vcopy(src, dst):
    """Copy that encrypts into / decrypts out of the vault when vault encryption is on."""
    fn = getattr(E, "vault_copy", None) if E is not None else None
    os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
    return fn(src, dst) if fn else shutil.copy2(src, dst)


def file_hash(path):
    try:
        return E.get_file_hash(path)
    except Exception:                                          # noqa: BLE001
        return ""


# =============================================================================
# Feeds & speeds
# =============================================================================
def cutting_data(tool, preset, machine=None):
    """{'rpm', 'feed', 'plunge', 'capped'} for a tool + preset, limited by the machine's spindle and feed.
    Spindle speed comes from Vc unless an rpm is given; feed from fz x flutes x rpm unless a feed is given."""
    g = tool.get("geometry", {})
    d, z = num(g.get("diameter")), num(g.get("flutes"))
    rpm, feed = num(preset.get("rpm")), num(preset.get("feed"))
    vc, fz = num(preset.get("vc")), num(preset.get("fz"))
    if rpm is None and vc and d:
        rpm = vc * 1000.0 / (math.pi * d)
    if feed is None and rpm and fz and z:
        feed = rpm * fz * z
    if feed is None and rpm and fz and tool.get("type") in ("drill", "spot", "reamer", "tap"):
        feed = rpm * fz                                       # drills: fz is feed per revolution
    capped = []
    m = machine or {}
    smax, fmax = num(m.get("spindle_max")), num(m.get("feed_max"))
    smin = num(m.get("spindle_min"))
    if rpm and smax and rpm > smax:
        if feed:
            feed *= smax / rpm                                # keep the chip load
        rpm = smax
        capped.append("spindle")
    if rpm and smin and rpm < smin:
        rpm = smin
        capped.append("spindle min")
    if feed and fmax and feed > fmax:
        feed = fmax
        capped.append("feed")
    plunge = num(preset.get("plunge"))
    if plunge and fmax and plunge > fmax:
        plunge = fmax
    return {"rpm": round(rpm) if rpm else None, "feed": round(feed, 1) if feed else None,
            "plunge": round(plunge, 1) if plunge else None, "capped": capped}


# =============================================================================
# Tool libraries
# =============================================================================
def cam_root():
    return os.path.join(E.VAULT_DIR, CAM_DIR_NAME)


def libs_root():
    return os.path.join(cam_root(), "Tool Libraries")


def machines_root():
    return os.path.join(cam_root(), "Machines")


def new_library(name):
    return {"format": "atelier-tool-library", "version": 1, "name": name, "unit": "mm",
            "materials": list(DEFAULT_MATERIALS), "tools": [], "holders": [], "revision": 0,
            "modified": now(), "modified_by": getattr(E, "USER_NAME", "")}


def new_tool(number=1, kind="flat"):
    return {"id": uuid.uuid4().hex, "number": int(number), "type": kind, "description": "", "vendor": "",
            "product_id": "", "geometry": {}, "holder": None, "presets": [], "cad": [], "notes": ""}


def new_holder(description=""):
    return {"id": uuid.uuid4().hex, "description": description, "vendor": "", "product_id": "",
            "gauge_length": None, "segments": [], "cad": []}


def library_dir(name):
    return os.path.join(libs_root(), safe_name(name))


def list_libraries():
    root = libs_root()
    out = []
    try:
        for n in sorted(os.listdir(root), key=str.lower):
            if os.path.isfile(os.path.join(root, n, LIB_FILE)):
                out.append(n)
    except OSError:
        pass
    return out


def load_library(name):
    lib = _json_load(os.path.join(library_dir(name), LIB_FILE), None)
    if not lib or lib.get("format") != "atelier-tool-library":
        raise ValueError(f"'{name}' isn't an Atelier tool library.")
    lib.setdefault("tools", [])
    lib.setdefault("holders", [])
    lib.setdefault("materials", list(DEFAULT_MATERIALS))
    lib.setdefault("revision", 0)
    return lib


class LibraryChanged(Exception):
    """Someone else saved the library since it was opened."""


def save_library(lib, expected_revision=None):
    """Saves with a revision check, so two people editing the same library can't overwrite each other."""
    name = safe_name(lib["name"])
    if not name:
        raise ValueError("The library needs a name.")
    path = os.path.join(library_dir(name), LIB_FILE)
    if expected_revision is not None and os.path.exists(path):
        on_disk = _json_load(path, {}).get("revision", 0)
        if on_disk != expected_revision:
            raise LibraryChanged(f"'{name}' was changed by someone else. Reopen it to see their changes.")
    problems = check_library(lib)
    if problems:
        raise ValueError(problems[0])
    lib["revision"] = int(lib.get("revision", 0)) + 1
    lib["modified"], lib["modified_by"] = now(), getattr(E, "USER_NAME", "")
    lib["tools"].sort(key=lambda t: int(t.get("number") or 0))
    _json_save(path, lib)
    return lib["revision"]


def check_library(lib):
    problems, seen = [], {}
    for t in lib.get("tools", []):
        n = t.get("number")
        try:
            n = int(n)
        except (TypeError, ValueError):
            problems.append(f"'{t.get('description') or 'A tool'}' has no valid tool number.")
            continue
        if n in seen:
            problems.append(f"Tool number T{n} is used twice ({seen[n]} and {t.get('description') or 'another'}).")
        seen[n] = t.get("description") or f"T{n}"
        if t.get("type") not in TOOL_TYPES:
            problems.append(f"T{n} has an unknown tool type '{t.get('type')}'.")
        if not num(t.get("geometry", {}).get("diameter")):
            problems.append(f"T{n} needs a cutting diameter.")
    ids = {h["id"] for h in lib.get("holders", [])}
    for t in lib.get("tools", []):
        if t.get("holder") and t["holder"] not in ids:
            t["holder"] = None                                  # holder was deleted: unlink quietly
    return problems


def create_library(name):
    name = safe_name(name)
    if not name:
        raise ValueError("Give the library a name.")
    if os.path.exists(os.path.join(library_dir(name), LIB_FILE)):
        raise ValueError(f"A library called '{name}' already exists.")
    lib = new_library(name)
    save_library(lib)
    for sub in ("Tool CAD", "Holder CAD"):
        os.makedirs(os.path.join(library_dir(name), sub), exist_ok=True)
    log("Tool Library Created", name)
    return lib


def rename_library(old, new):
    new = safe_name(new)
    if not new:
        raise ValueError("Give the library a name.")
    if new == old:
        return old
    if os.path.exists(library_dir(new)):
        raise ValueError(f"A library called '{new}' already exists.")
    os.rename(library_dir(old), library_dir(new))
    lib = load_library(new)
    lib["name"] = new
    save_library(lib)
    log("Tool Library Renamed", new, f"was '{old}'")
    return new


def delete_library(name):
    """Moves a library to _CAM\\Deleted (kept, not erased)."""
    src = library_dir(name)
    if not os.path.isdir(src):
        raise ValueError(f"'{name}' doesn't exist.")
    dst = os.path.join(cam_root(), "Deleted", f"{safe_name(name)} {time.strftime('%Y%m%d_%H%M%S')}")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.move(src, dst)
    log("Tool Library Deleted", name, f"kept in {dst}")
    return dst


def next_tool_number(lib):
    used = {int(t["number"]) for t in lib.get("tools", []) if str(t.get("number", "")).isdigit()}
    n = 1
    while n in used:
        n += 1
    return n


def holder_of(lib, tool):
    return next((h for h in lib.get("holders", []) if h["id"] == tool.get("holder")), None)


def attach_cad(lib, item, src, kind):
    """Copies a CAD file (holder or tool model) into the library and links it. kind: 'tool' or 'holder'."""
    if os.path.splitext(src)[1].lower() not in CAD_EXTS:
        raise ValueError("Use a CAD file: " + ", ".join(CAD_EXTS))
    sub = "Holder CAD" if kind == "holder" else "Tool CAD"
    label = item.get("description") or (f"T{item.get('number')}" if kind == "tool" else "holder")
    name = safe_name(f"{label} - {os.path.basename(src)}")
    rel = os.path.join(sub, name)
    vcopy(src, os.path.join(library_dir(lib["name"]), rel))
    if rel not in item.setdefault("cad", []):
        item["cad"].append(rel)
    return rel


def summary(tool):
    g = tool.get("geometry", {})
    bits = [f"Ø{fmt(g.get('diameter'))}" if g.get("diameter") not in (None, "") else ""]
    if g.get("flutes"):
        bits.append(f"{fmt(g.get('flutes'))}F")
    if g.get("corner_radius"):
        bits.append(f"R{fmt(g.get('corner_radius'))}")
    if g.get("tip_angle") and tool.get("type") in ("drill", "spot", "chamfer", "vbit"):
        bits.append(f"{fmt(g.get('tip_angle'))}°")
    return " ".join(b for b in bits if b)


# =============================================================================
# Export / import
# =============================================================================
def to_fusion(lib):
    """Fusion 360 / HSM tool library JSON (millimetres)."""
    holders = {h["id"]: h for h in lib.get("holders", [])}
    data = []
    for t in lib.get("tools", []):
        g = t.get("geometry", {})
        geo = {}
        for ours, theirs in (("diameter", "DC"), ("flute_length", "LCF"), ("overall_length", "OAL"),
                             ("shank_diameter", "SFDM"), ("stickout", "LB"), ("corner_radius", "RE"),
                             ("tip_angle", "SIG"), ("tip_diameter", "tip-diameter"), ("thread_pitch", "TP")):
            v = num(g.get(ours))
            if v is not None:
                geo[theirs] = v
        if num(g.get("flutes")):
            geo["NOF"] = int(num(g.get("flutes")))
        if t.get("type") == "ball" and "RE" not in geo and "DC" in geo:
            geo["RE"] = geo["DC"] / 2
        presets = []
        for p in t.get("presets", []):
            cd = cutting_data(t, p)
            pr = {"guid": "{" + str(uuid.uuid5(uuid.NAMESPACE_URL, t["id"] + p.get("material", ""))) + "}",
                  "name": p.get("material", ""), "description": p.get("notes", ""),
                  "tool-coolant": {"none": "disabled"}.get(p.get("coolant") or "flood", p.get("coolant") or "flood")}
            for k, key in (("n", cd["rpm"]), ("v_c", num(p.get("vc"))), ("f_z", num(p.get("fz"))),
                           ("v_f", cd["feed"]), ("v_f_plunge", cd["plunge"]), ("stepdown", num(p.get("ap"))),
                           ("stepover", num(p.get("ae")))):
                if key is not None:
                    pr[k] = key
            pr["use-stepdown"], pr["use-stepover"] = "stepdown" in pr, "stepover" in pr
            presets.append(pr)
        item = {"guid": "{" + str(uuid.UUID(t["id"])) + "}", "type": TOOL_TYPES[t["type"]][1],
                "unit": "millimeters", "description": t.get("description", ""), "vendor": t.get("vendor", ""),
                "product-id": t.get("product_id", ""), "BMC": "carbide", "geometry": geo,
                "post-process": {"number": int(t["number"]), "length-offset": int(t["number"]),
                                 "diameter-offset": int(t["number"]), "comment": t.get("notes", "")[:80],
                                 "turret": 0, "manual-tool-change": False, "break-control": False},
                "start-values": {"presets": presets}}
        h = holders.get(t.get("holder"))
        if h and h.get("segments"):
            item["holder"] = {"description": h.get("description", ""), "vendor": h.get("vendor", ""),
                              "product-id": h.get("product_id", ""), "type": "holder", "unit": "millimeters",
                              "guid": "{" + str(uuid.UUID(h["id"])) + "}",
                              "segments": [{"height": num(s.get("height"), 0),
                                            "lower-diameter": num(s.get("lower_diameter"), 0),
                                            "upper-diameter": num(s.get("upper_diameter"), 0)}
                                           for s in h["segments"]]}
        data.append(item)
    return {"data": data, "version": 2}


def from_fusion(data, name):
    """Atelier library from a Fusion 360 / HSM tool library (dict). Converts inch libraries to mm."""
    lib = new_library(name)
    holders = {}
    skipped = 0
    for item in data.get("data", []):
        kind = FUSION_TO_KEY.get(str(item.get("type", "")).lower())
        if not kind:
            if item.get("type") == "holder":
                continue
            skipped += 1
            continue
        k = 25.4 if str(item.get("unit", "")).lower().startswith("inch") else 1.0
        geo_in = item.get("geometry", {})
        g = {}
        for theirs, ours in (("DC", "diameter"), ("LCF", "flute_length"), ("OAL", "overall_length"),
                             ("SFDM", "shank_diameter"), ("LB", "stickout"), ("RE", "corner_radius"),
                             ("tip-diameter", "tip_diameter"), ("TP", "thread_pitch")):
            v = num(geo_in.get(theirs))
            if v is not None:
                g[ours] = round(v * k, 4)
        if num(geo_in.get("SIG")) is not None:
            g["tip_angle"] = num(geo_in.get("SIG"))
        if num(geo_in.get("NOF")):
            g["flutes"] = int(num(geo_in.get("NOF")))
        pp = item.get("post-process", {})
        t = new_tool(int(num(pp.get("number"), 0)) or next_tool_number(lib), kind)
        t.update(description=item.get("description", ""), vendor=item.get("vendor", ""),
                 product_id=item.get("product-id", ""), geometry=g, notes=pp.get("comment", ""))
        for p in item.get("start-values", {}).get("presets", []):
            mk = 0.3048 if k != 1.0 else 1.0                       # inch libraries: ft/min
            cool = p.get("tool-coolant", "flood")
            preset = {"material": p.get("name") or "General",
                      "coolant": "none" if cool == "disabled" else (cool if cool in COOLANTS else "flood")}
            for theirs, ours, mul in (("v_c", "vc", mk), ("f_z", "fz", k), ("n", "rpm", 1), ("v_f", "feed", k),
                                      ("v_f_plunge", "plunge", k), ("stepdown", "ap", k), ("stepover", "ae", k)):
                v = num(p.get(theirs))
                if v is not None:
                    preset[ours] = round(v * mul, 4)
            t["presets"].append(preset)
            if preset["material"] not in lib["materials"]:
                lib["materials"].append(preset["material"])
        h = item.get("holder")
        if isinstance(h, dict) and h.get("segments"):
            key = h.get("guid") or json.dumps(h.get("segments"), sort_keys=True)
            if key not in holders:
                hk = 25.4 if str(h.get("unit", "")).lower().startswith("inch") else 1.0
                hold = new_holder(h.get("description") or h.get("product-id") or "Holder")
                hold.update(vendor=h.get("vendor", ""), product_id=h.get("product-id", ""),
                            segments=[{"height": round(num(s.get("height"), 0) * hk, 4),
                                       "lower_diameter": round(num(s.get("lower-diameter"), 0) * hk, 4),
                                       "upper_diameter": round(num(s.get("upper-diameter"), 0) * hk, 4)}
                                      for s in h["segments"]])
                holders[key] = hold
                lib["holders"].append(hold)
            t["holder"] = holders[key]["id"]
        if any(int(x["number"]) == int(t["number"]) for x in lib["tools"]):
            t["number"] = next_tool_number(lib)
        lib["tools"].append(t)
    return lib, skipped


def read_fusion_file(path):
    """Fusion libraries come as .json, or as .tools (a zip holding the JSON)."""
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            names = [n for n in z.namelist() if n.lower().endswith(".json")] or z.namelist()
            return json.loads(z.read(names[0]).decode("utf-8-sig"))
    with open(path, encoding="utf-8-sig") as f:
        return json.load(f)


CSV_COLS = ["number", "type", "description", "vendor", "product_id", "diameter", "flutes", "flute_length",
            "overall_length", "stickout", "shank_diameter", "corner_radius", "tip_angle", "tip_diameter",
            "thread_pitch", "holder", "material", "vc_m_min", "fz_mm", "rpm", "feed_mm_min", "plunge_mm_min",
            "ap_mm", "ae_mm", "coolant", "notes"]


def to_csv(lib, machine=None):
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(CSV_COLS)
    for t in lib.get("tools", []):
        g = t.get("geometry", {})
        h = holder_of(lib, t)
        base = [t["number"], t["type"], t.get("description", ""), t.get("vendor", ""), t.get("product_id", "")]
        base += [fmt(g.get(k)) for k in ("diameter", "flutes", "flute_length", "overall_length", "stickout",
                                          "shank_diameter", "corner_radius", "tip_angle", "tip_diameter",
                                          "thread_pitch")]
        base.append(h["description"] if h else "")
        for p in t.get("presets") or [{}]:
            cd = cutting_data(t, p, machine) if p else {"rpm": None, "feed": None, "plunge": None}
            w.writerow(base + [p.get("material", ""), fmt(p.get("vc")), fmt(p.get("fz"), 4), fmt(cd["rpm"]),
                               fmt(cd["feed"]), fmt(cd["plunge"]), fmt(p.get("ap")), fmt(p.get("ae")),
                               p.get("coolant", ""), t.get("notes", "")])
    return out.getvalue()


def from_csv(text, name):
    lib = new_library(name)
    by_number = {}
    for row in csv.DictReader(io.StringIO(text)):
        n = int(num(row.get("number"), 0) or 0)
        if not n:
            continue
        t = by_number.get(n)
        if t is None:
            kind = (row.get("type") or "flat").strip().lower()
            t = new_tool(n, kind if kind in TOOL_TYPES else "flat")
            t.update(description=row.get("description", ""), vendor=row.get("vendor", ""),
                     product_id=row.get("product_id", ""), notes=row.get("notes", ""))
            for k, _, _ in GEOMETRY_FIELDS:
                v = num(row.get(k))
                if v is not None:
                    t["geometry"][k] = int(v) if k == "flutes" else v
            by_number[n] = t
            lib["tools"].append(t)
        if (row.get("material") or "").strip():
            p = {"material": row["material"].strip(), "coolant": (row.get("coolant") or "flood").strip()}
            for col, key in (("vc_m_min", "vc"), ("fz_mm", "fz"), ("ap_mm", "ap"), ("ae_mm", "ae"),
                             ("plunge_mm_min", "plunge")):
                v = num(row.get(col))
                if v is not None:
                    p[key] = v
            if p.get("vc") is None and num(row.get("rpm")):
                p["rpm"] = num(row.get("rpm"))
            if p.get("fz") is None and num(row.get("feed_mm_min")):
                p["feed"] = num(row.get("feed_mm_min"))
            t["presets"].append(p)
            if p["material"] not in lib["materials"]:
                lib["materials"].append(p["material"])
    return lib


def to_freecad(lib):
    """{filename: text} - one .fctb per tool plus the .fctl library (FreeCAD 1.0 tool-bit format)."""
    files, entries = {}, []
    q = lambda v, u="mm": f"{float(v):.4f} {u}"
    for t in lib.get("tools", []):
        g, kind = t.get("geometry", {}), t["type"]
        d = num(g.get("diameter"), 6.0)
        shank = num(g.get("shank_diameter"), d)
        length = num(g.get("stickout")) or num(g.get("overall_length"), d * 5)
        edge = num(g.get("flute_length"), min(length, d * 3))
        p = {"Diameter": q(d), "Length": q(length)}
        if kind in ("drill", "spot", "tap"):
            p["TipAngle"] = q(num(g.get("tip_angle"), 118), "°")
        elif kind in ("chamfer", "vbit"):
            p.update(CuttingEdgeAngle=q(num(g.get("tip_angle"), 90), "°"), CuttingEdgeHeight=q(edge),
                     ShankDiameter=q(shank), TipDiameter=q(num(g.get("tip_diameter"), 0)))
        elif kind == "slot":
            p.update(BladeThickness=q(edge), CapDiameter=q(shank), CapHeight=q(num(g.get("flute_length"), 3)),
                     ShankDiameter=q(shank))
        elif kind == "thread":
            p.update(Crest=q(num(g.get("thread_pitch"), 1) / 8), CuttingAngle=q(60, "°"),
                     NeckDiameter=q(d * 0.7), NeckLength=q(edge), ShankDiameter=q(shank))
        else:
            p.update(CuttingEdgeHeight=q(edge), ShankDiameter=q(shank))
            if kind == "bull":
                p["FlatRadius"] = q(max(0.0, d / 2 - num(g.get("corner_radius"), 0)))
        fname = safe_name(f"T{t['number']} {t.get('description') or TOOL_TYPES[kind][0]}").replace(" ", "_") + ".fctb"
        bit = {"version": 2, "name": t.get("description") or f"T{t['number']} {TOOL_TYPES[kind][0]}",
               "shape": TOOL_TYPES[kind][2], "parameter": p,
               "attribute": {"Flutes": int(num(g.get("flutes"), 0) or 0), "Material": "Carbide",
                             "SpindleDirection": "Forward"}}
        files["Bit/" + fname] = json.dumps(bit, indent=2)
        entries.append({"nr": int(t["number"]), "path": fname})
    files["Library/" + safe_name(lib["name"]) + ".fctl"] = json.dumps({"version": 1, "tools": entries}, indent=2)
    return files


def to_linuxcnc(lib):
    lines = [f";Atelier PDM tool table - {lib['name']} - {now()}"]
    for t in lib.get("tools", []):
        g = t.get("geometry", {})
        lines.append(f"T{t['number']} P{t['number']} D{num(g.get('diameter'), 0):.4f} Z+0.000000 "
                     f";{(t.get('description') or TOOL_TYPES[t['type']][0])[:60]} {summary(t)}")
    return "\n".join(lines) + "\n"


EXPORTS = {   # key -> (label, function, kind)
    "fusion": ("Fusion 360 / HSM / PhysCAD (.json)", None, "json"),
    "freecad": ("FreeCAD tool bits (.fctb + .fctl)", None, "folder"),
    "csv": ("Spreadsheet (.csv)", None, "csv"),
    "linuxcnc": ("LinuxCNC tool table (.tbl)", None, "tbl"),
}


def export_library(lib, fmt_key, out_dir, machine=None):
    """Writes the export into out_dir and returns the main file or folder created."""
    os.makedirs(out_dir, exist_ok=True)
    base = safe_name(lib["name"])
    if fmt_key == "fusion":
        path = os.path.join(out_dir, base + ".json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(to_fusion(lib), f, indent=2)
    elif fmt_key == "csv":
        path = os.path.join(out_dir, base + ".csv")
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            f.write(to_csv(lib, machine))
    elif fmt_key == "linuxcnc":
        path = os.path.join(out_dir, base + ".tbl")
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(to_linuxcnc(lib))
    elif fmt_key == "freecad":
        path = os.path.join(out_dir, base + " (FreeCAD)")
        for rel, text in to_freecad(lib).items():
            p = os.path.join(path, *rel.split("/"))
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                f.write(text)
    else:
        raise ValueError(f"Unknown export format '{fmt_key}'.")
    log("Tool Library Exported", lib["name"], f"{EXPORTS[fmt_key][0]} -> {path}")
    return path


def import_library(path, name):
    """New library from a Fusion/HSM (.json/.tools) or Atelier CSV file. Returns (library, message)."""
    name = safe_name(name)
    if not name:
        raise ValueError("Give the library a name.")
    if os.path.exists(os.path.join(library_dir(name), LIB_FILE)):
        raise ValueError(f"A library called '{name}' already exists.")
    ext = os.path.splitext(path)[1].lower()
    if ext == ".csv":
        with open(path, encoding="utf-8-sig") as f:
            lib, skipped = from_csv(f.read(), name), 0
    elif ext in (".json", ".tools"):
        lib, skipped = from_fusion(read_fusion_file(path), name)
    else:
        raise ValueError("Import a Fusion 360 / HSM library (.json or .tools) or an Atelier CSV.")
    if not lib["tools"]:
        raise ValueError("No tools were found in that file.")
    for t in lib["tools"]:
        if not num(t["geometry"].get("diameter")):
            t["geometry"]["diameter"] = 1.0
            t["notes"] = (t.get("notes", "") + " [diameter missing in import]").strip()
    save_library(lib)
    for sub in ("Tool CAD", "Holder CAD"):
        os.makedirs(os.path.join(library_dir(name), sub), exist_ok=True)
    msg = f"Imported {len(lib['tools'])} tools and {len(lib['holders'])} holders into '{name}'."
    if skipped:
        msg += f" {skipped} item(s) of a type Atelier doesn't handle were skipped."
    log("Tool Library Imported", name, msg)
    return lib, msg


# =============================================================================
# Machines & posts
# =============================================================================
def machine_dir(name):
    return os.path.join(machines_root(), safe_name(name))


def list_machines():
    out = []
    try:
        for n in sorted(os.listdir(machines_root()), key=str.lower):
            m = _json_load(os.path.join(machines_root(), n, MACHINE_FILE), None)
            if m:
                m["name"] = n
                out.append(m)
    except OSError:
        pass
    return out


def get_machine(name):
    if not name:
        return None
    m = _json_load(os.path.join(machine_dir(name), MACHINE_FILE), None)
    if m:
        m["name"] = safe_name(name)
    return m


def save_machine(m, old_name=None):
    name = safe_name(m.get("name"))
    if not name:
        raise ValueError("Give the machine a name.")
    if old_name and safe_name(old_name) != name:
        if os.path.exists(machine_dir(name)):
            raise ValueError(f"A machine called '{name}' already exists.")
        os.rename(machine_dir(old_name), machine_dir(name))
        _ensure_table()
        with E.db() as c:                                   # filed programs follow the rename
            c.execute("UPDATE cam_programs SET machine=? WHERE machine=?", (name, safe_name(old_name)))
    elif not old_name and os.path.exists(os.path.join(machine_dir(name), MACHINE_FILE)):
        raise ValueError(f"A machine called '{name}' already exists.")
    ext = (m.get("nc_ext") or ".nc").strip().lower()
    m["nc_ext"] = ext if ext.startswith(".") else "." + ext
    for k in ("spindle_max", "spindle_min", "feed_max", "rapid", "travel_x", "travel_y", "travel_z", "tool_slots"):
        if k in m:
            m[k] = num(m[k])
    data = {k: v for k, v in m.items() if k != "name"}
    data["modified"], data["modified_by"] = now(), getattr(E, "USER_NAME", "")
    _json_save(os.path.join(machine_dir(name), MACHINE_FILE), data)
    for app in CAM_APPS:
        os.makedirs(os.path.join(machine_dir(name), "Posts", app), exist_ok=True)
    log("Machine Saved", name)
    return name


def delete_machine(name):
    """Moves a machine (with its posts) to _CAM\\Deleted. Filed programs keep its name for the record."""
    src = machine_dir(name)
    if not os.path.isdir(src):
        raise ValueError(f"'{name}' doesn't exist.")
    dst = os.path.join(cam_root(), "Deleted", f"Machine {safe_name(name)} {time.strftime('%Y%m%d_%H%M%S')}")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.move(src, dst)
    log("Machine Deleted", name, f"kept in {dst}")
    return dst


def posts_dir(machine, app):
    return os.path.join(machine_dir(machine), "Posts", app)


def list_posts(machine):
    out = []
    for app in CAM_APPS:
        d = posts_dir(machine, app)
        try:
            for n in sorted(os.listdir(d), key=str.lower):
                p = os.path.join(d, n)
                if os.path.isfile(p):
                    out.append({"app": app, "name": n, "path": p, "modified": os.path.getmtime(p)})
        except OSError:
            pass
    return out


def add_post(machine, app, src):
    """Copies a post into the machine's Posts\\<app> folder. An existing post of the same name is kept in
    the machine's History folder first."""
    if app not in CAM_APPS:
        raise ValueError(f"Unknown CAM program '{app}'.")
    dst = os.path.join(posts_dir(machine, app), os.path.basename(src))
    replaced = os.path.exists(dst)
    if replaced:
        _post_to_history(machine, dst)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(src, dst)
    log("Post Replaced" if replaced else "Post Added", f"{machine} / {app}", os.path.basename(src))
    return dst, replaced


def _post_to_history(machine, path, move=False):
    hist = os.path.join(machine_dir(machine), "History")
    os.makedirs(hist, exist_ok=True)
    b, e = os.path.splitext(os.path.basename(path))
    dst = os.path.join(hist, f"{b} {time.strftime('%Y%m%d_%H%M%S')}{e}")
    (shutil.move if move else shutil.copy2)(path, dst)
    return dst


def remove_post(machine, app, name):
    p = os.path.join(posts_dir(machine, app), name)
    if not os.path.isfile(p):
        raise ValueError(f"'{name}' isn't in {machine}'s {app} posts.")
    _post_to_history(machine, p, move=True)
    log("Post Removed", f"{machine} / {app}", name)


# =============================================================================
# CAM setups and NC programs (both linked to a part and revision)
# =============================================================================
def _ensure_table():
    with E.db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS cam_programs (
                        filename TEXT PRIMARY KEY, part_file TEXT, part_number TEXT, part_rev TEXT,
                        op TEXT, machine TEXT, post TEXT, cam_app TEXT, cam_version TEXT, tool_library TEXT,
                        status TEXT DEFAULT 'current', note TEXT, hash TEXT, created TEXT, created_by TEXT,
                        part_updated TEXT)""")
        cols = {r[1] for r in c.execute("PRAGMA table_info(cam_programs)").fetchall()}
        if "kind" not in cols:
            c.execute("ALTER TABLE cam_programs ADD COLUMN kind TEXT DEFAULT 'nc'")


def nc_exts():
    cfg = (getattr(E, "CONFIG", {}) or {}).get("nc_exts")
    exts = cfg if isinstance(cfg, list) and cfg else DEFAULT_NC_EXTS
    return [e.lower() if e.startswith(".") else "." + e.lower() for e in exts]


def project_folder_for(part_file):
    info = E.INDEX.files.get(part_file)
    if not info:
        raise ValueError(f"'{part_file}' isn't in the vault. Check it in first.")
    ctx = E.numbering_context(info.folder)
    return E.folder_abs(ctx["project"]) if ctx else os.path.dirname(info.path)


def nc_folder_for(part_file):
    """<project>\\NC Programs, or beside the part when it isn't in a numbered project."""
    return os.path.join(project_folder_for(part_file), NC_FOLDER)


def setup_folder_for(part_file):
    return os.path.join(project_folder_for(part_file), SETUP_FOLDER)


def folder_for(part_file, kind):
    return setup_folder_for(part_file) if kind == "setup" else nc_folder_for(part_file)


def program_name(part_file, op, rev, ext):
    rec = E.INDEX.records.get(part_file) or {}
    pp = E.parse_pn(os.path.splitext(part_file)[0])
    pn = rec.get("part_number") or (pp[3] if pp else "")
    name = E.display_part_name(part_file, rec)
    stem = " ".join(x for x in (pn, name, safe_name(op).upper(), rev) if x)
    return safe_name(stem) + ext.lower()


def next_op(part_file, kind="nc"):
    _ensure_table()
    with E.db() as c:
        ops = [r[0] for r in c.execute("SELECT op FROM cam_programs WHERE part_file=? AND COALESCE(kind,'nc')=?",
                                       (part_file, kind)).fetchall()]
    n = 1
    while f"OP{n}" in {str(o).upper() for o in ops}:
        n += 1
    return f"OP{n}"


def _file_item(kind, src, part_file, op, machine="", post="", cam_app="", cam_version="", tool_library="",
               note=""):
    _ensure_table()
    ext = os.path.splitext(src)[1].lower()
    if kind == "nc" and ext not in nc_exts():
        raise ValueError(f"'{ext}' isn't set up as an NC program type ({' '.join(nc_exts())}). Add it to "
                         "nc_exts in atelier_config.json if your post uses it.")
    if kind == "setup":
        if not ext:
            raise ValueError("The CAM setup file needs a file extension.")
        if ext in BLOCKED_SETUP_EXTS:
            raise ValueError(f"'{ext}' files can't be filed as CAM setups.")
        if ext in nc_exts():
            raise ValueError("That's an NC program - file it with 'File NC program' instead.")
        if ext in getattr(E, "CAD_MODEL_EXTS", set()):
            raise ValueError("That's a part file. A CAM setup is the CAM program's own job file.")
    if not os.path.isfile(src):
        raise ValueError(f"Can't find {src}.")
    if part_file not in E.INDEX.files:
        raise ValueError(f"'{part_file}' isn't in the vault yet. Check it in first.")
    rec = E.INDEX.records.get(part_file) or {}
    rev = rec.get("revision") or "A-01"
    op = safe_name(op or next_op(part_file, kind)).upper()
    folder = folder_for(part_file, kind)
    name = program_name(part_file, op, rev, ext)
    dst = os.path.join(folder, name)
    if os.path.exists(dst) and os.path.normcase(os.path.abspath(dst)) != os.path.normcase(os.path.abspath(src)):
        hist = os.path.join(folder, "_Replaced")
        os.makedirs(hist, exist_ok=True)
        shutil.move(dst, os.path.join(hist, f"{os.path.splitext(name)[0]} {time.strftime('%Y%m%d_%H%M%S')}{ext}"))
    vcopy(src, dst)
    if kind == "setup":
        try:
            E.VAULT_EXTS.add(ext)                              # indexed, and encrypted with the vault
        except AttributeError:
            pass
    pp = E.parse_pn(os.path.splitext(part_file)[0])
    with E.db() as c:
        c.execute("UPDATE cam_programs SET status='superseded' WHERE part_file=? AND UPPER(op)=? AND filename<>? "
                  "AND COALESCE(kind,'nc')=?", (part_file, op, name, kind))
        c.execute("INSERT OR REPLACE INTO cam_programs (filename, part_file, part_number, part_rev, op, machine, "
                  "post, cam_app, cam_version, tool_library, status, note, hash, created, created_by, part_updated, "
                  "kind) VALUES (?,?,?,?,?,?,?,?,?,?, 'current', ?,?,?,?,?,?)",
                  (name, part_file, rec.get("part_number") or (pp[3] if pp else ""), rev, op, machine, post,
                   cam_app, cam_version, tool_library, note, file_hash(dst), now(), E.USER_NAME,
                   rec.get("last_updated") or "", kind))
    what = "CAM Setup" if kind == "setup" else "NC Program"
    E.log_audit(part_file, f"{what} Added", rev, f"{name} · {machine or '-'} · {post or '-'} · "
                                                f"{cam_app} {cam_version}".strip())
    E.log_audit(name, f"{what} Added", rev, f"for {part_file}")
    return name


def add_program(src, part_file, op, machine="", post="", cam_app="", cam_version="", tool_library="", note=""):
    """Files a posted program against the part's current revision. The previous program for the same part and
    operation is marked superseded (and kept). Returns the vault file name."""
    return _file_item("nc", src, part_file, op, machine, post, cam_app, cam_version, tool_library, note)


def add_setup(src, part_file, op, cam_app="", cam_version="", tool_library="", note=""):
    """Files a CAM program's job / setup file against the part's current revision."""
    return _file_item("setup", src, part_file, op, cam_app=cam_app, cam_version=cam_version,
                      tool_library=tool_library, note=note)


def program_status(p):
    """('current' | 'outdated' | 'superseded' | 'missing', text)."""
    if p.get("status") == "superseded":
        return "superseded", "Superseded"
    rec = E.INDEX.records.get(p["part_file"])
    if not rec and p["part_file"] not in E.INDEX.files:
        return "missing", "Part no longer in the vault"
    rev = (rec or {}).get("revision") or "A-01"
    if rev != p.get("part_rev"):
        return "outdated", f"Out of date - part is now {rev}"
    upd = (rec or {}).get("last_updated") or ""
    if upd and (p.get("part_updated") or "") and upd > p["part_updated"] and upd > (p.get("created") or ""):
        return "outdated", "Out of date - part changed since"
    if (rec or {}).get("locked_by"):
        return "current", f"Current (part checked out by {rec['locked_by']})"
    return "current", "Current"


def list_items(include_superseded=False, kind=None):
    _ensure_table()
    with E.db() as c:
        c.row_factory = sqlite3.Row
        rows = [dict(r) for r in c.execute("SELECT * FROM cam_programs ORDER BY part_number, op, created DESC")]
    out = []
    for r in rows:
        r["kind"] = r.get("kind") or "nc"
        if kind and r["kind"] != kind:
            continue
        key, text = program_status(r)
        if key == "superseded" and not include_superseded:
            continue
        r["state"], r["state_text"] = key, text
        out.append(r)
    return out


def list_programs(include_superseded=False):
    return list_items(include_superseded, "nc")


def get_item(filename):
    _ensure_table()
    with E.db() as c:
        c.row_factory = sqlite3.Row
        row = c.execute("SELECT * FROM cam_programs WHERE filename=?", (filename,)).fetchone()
    if not row:
        raise ValueError(f"'{filename}' isn't a filed CAM setup or NC program.")
    r = dict(row)
    r["kind"] = r.get("kind") or "nc"
    r["state"], r["state_text"] = program_status(r)
    return r


def item_path(filename):
    r = get_item(filename)
    return os.path.join(folder_for(r["part_file"], r["kind"]), filename)


program_path = item_path


def items_by_part(include_superseded=False):
    out = {}
    for r in list_items(include_superseded):
        out.setdefault(r["part_file"], []).append(r)
    return out


def set_program_status(filename, status):
    _ensure_table()
    with E.db() as c:
        c.execute("UPDATE cam_programs SET status=? WHERE filename=?", (status, filename))
    E.log_audit(filename, "CAM File " + ("Superseded" if status == "superseded" else "Restored"), "", "")


def send_to_machine(filename, force=False):
    """Copies (decrypting if needed) a program to its machine's program folder. Returns the destination."""
    p = get_item(filename)
    if p["kind"] != "nc":
        raise ValueError("Only NC programs can be sent to a machine.")
    state, text = p["state"], p["state_text"]
    if state != "current" and not force:
        raise PermissionError(text)
    m = get_machine(p["machine"])
    if not m:
        raise ValueError("This program has no machine set, or the machine has been removed.")
    folder = (m.get("program_folder") or "").strip()
    if not folder:
        raise ValueError(f"No program folder is set for '{m['name']}'. Set one on the Machines & posts tab.")
    if not os.path.isdir(folder):
        raise ValueError(f"Can't reach {folder} - is the machine's PC or share switched on?")
    dst = os.path.join(folder, filename)
    vcopy(item_path(filename), dst)
    E.log_audit(filename, "Sent to Machine", p["part_rev"], f"{m['name']}: {dst}" + ("" if state == "current"
                                                                                 else f" (forced: {text})"))
    return dst


def working_dir():
    return os.path.join(E.LOCAL_WORKSPACE, "CAM")


def open_local_copy(filename, folder=None):
    """Decrypted working copy of a setup or program (for viewing outside the vault)."""
    r = get_item(filename)
    out = folder or os.path.join(E.LOCAL_WORKSPACE, NC_FOLDER if r["kind"] == "nc" else "CAM")
    dst = os.path.join(out, filename)
    vcopy(item_path(filename), dst)
    return dst


# ---- editing setups: take a working copy, then save it back ----------------------------------------------
def _editing():
    s = E.load_settings() or {}
    return dict(s.get("cam_editing") or {})


def _set_editing(d):
    s = E.load_settings() or {}
    s["cam_editing"] = d
    E.save_settings(s)


def open_setup_for_edit(filename):
    """Copies a CAM setup to the working folder and remembers it. Returns the working path."""
    r = get_item(filename)
    if r["kind"] != "setup":
        raise ValueError("Only CAM setups are opened for editing. NC programs are re-posted from the setup.")
    dst = os.path.join(working_dir(), filename)
    ed = _editing()
    if os.path.exists(dst) and filename in ed and file_hash(dst) != ed[filename].get("hash"):
        return dst                                              # unsaved edits already there: keep them
    vcopy(item_path(filename), dst)
    ed[filename] = {"path": dst, "hash": file_hash(dst), "opened": now()}
    _set_editing(ed)
    E.log_audit(filename, "CAM Setup Opened", r["part_rev"], dst)
    return dst


def editing_state(filename):
    """None (not open), 'open' (working copy unchanged) or 'changed' (edits not yet saved back)."""
    e = _editing().get(filename)
    if not e or not os.path.exists(e.get("path", "")):
        return None
    return "changed" if file_hash(e["path"]) != e.get("hash") else "open"


def save_setup_back(filename, note=""):
    """Files the working copy of a setup again, against the part's current revision. Returns the new name."""
    r = get_item(filename)
    e = _editing().get(filename)
    if not e or not os.path.exists(e.get("path", "")):
        raise ValueError(f"'{filename}' isn't open for editing on this PC. Use 'Open setup for editing' first.")
    name = add_setup(e["path"], r["part_file"], r["op"], r.get("cam_app") or "", r.get("cam_version") or "",
                     r.get("tool_library") or "", note or r.get("note") or "")
    ed = _editing()
    ed.pop(filename, None)
    ed[name] = {"path": e["path"], "hash": file_hash(e["path"]), "opened": now()}
    if name != filename:                                        # new revision: keep working on the new name
        new_path = os.path.join(working_dir(), name)
        try:
            os.replace(e["path"], new_path)
            ed[name]["path"] = new_path
        except OSError:
            pass
    _set_editing(ed)
    return name


def close_setup(filename):
    ed = _editing()
    ed.pop(filename, None)
    _set_editing(ed)


# ---- the part's geometry for the CAM program -----------------------------------------------------------
def part_model_for_cam(part_file):
    """A STEP (or STL) of the part's current vault revision in the working folder, for importing into CAM.
    Uses a same-named STEP/STL saved with the part, or converts the part when its CAD program allows it."""
    info = E.INDEX.files.get(part_file)
    if not info:
        raise ValueError(f"'{part_file}' isn't in the vault.")
    rec = E.INDEX.records.get(part_file) or {}
    rev = rec.get("revision") or "A-01"
    out_dir = os.path.join(working_dir(), "Models")
    stem = os.path.splitext(part_file)[0]
    base = os.path.splitext(info.path)[0]
    for ext in (".step", ".stp", ".STEP", ".STP"):
        if os.path.exists(base + ext):
            dst = os.path.join(out_dir, f"{stem} {rev}.step")
            vcopy(base + ext, dst)
            return dst
    can = getattr(E, "can_convert", None)
    if can and can(part_file, "step"):
        dst = os.path.join(out_dir, f"{stem} {rev}.step")
        os.makedirs(out_dir, exist_ok=True)
        ok, msg = E.cad_convert(info.path, dst)
        if ok:
            return dst
    for ext in (".stl", ".STL"):
        if os.path.exists(base + ext):
            dst = os.path.join(out_dir, f"{stem} {rev}.stl")
            vcopy(base + ext, dst)
            return dst
    raise ValueError(f"There's no STEP for '{part_file}'. Export '{stem}.step' from its CAD program next to the "
                     "part (or use Export STEP & PDF), then try again.")


# ---- starting CAM programs -----------------------------------------------------------------------------
def _cam_candidates(app):
    roots = [os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs"),
             os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramFiles(x86)", "")]
    out = []
    for r in filter(None, roots):
        out += [os.path.join(r, app, f"{app}.exe"), os.path.join(r, app, f"{app.lower()}.exe")]
        out += sorted(glob.glob(os.path.join(r, f"{app}*", f"{app}.exe")), reverse=True)
        out += sorted(glob.glob(os.path.join(r, f"{app}*", "bin", f"{app}.exe")), reverse=True)
    return out


def find_cam_exe(app):
    """Path of a CAM program: remembered choice, atelier_config.json, the CAD-app launcher, usual folders."""
    s = E.load_settings() or {}
    saved = (s.get("cam_exe") or {}).get(app)
    if saved and os.path.isfile(saved):
        return saved
    cfg = ((getattr(E, "CONFIG", {}) or {}).get("cam_apps") or {}).get(app) or {}
    if cfg.get("exe") and os.path.isfile(cfg["exe"]):
        return cfg["exe"]
    launcher = getattr(E, "cad_launcher", None)
    if launcher and app.lower() in getattr(E, "CAD_APPS", {}):
        try:
            p = launcher(app.lower())
            if p:
                return p
        except Exception:                                      # noqa: BLE001
            pass
    p = shutil.which(app) or shutil.which(app.lower())
    if p:
        return p
    return next((c for c in _cam_candidates(app) if os.path.isfile(c)), None)


def remember_cam_exe(app, path):
    s = E.load_settings() or {}
    d = dict(s.get("cam_exe") or {})
    d[app] = os.path.normpath(path)
    s["cam_exe"] = d
    E.save_settings(s)


def launch_cam(app, path=None):
    """Starts a CAM program, with a file if given. Falls back to the Windows file association for the file.
    Returns what was started, or raises LookupError when the program can't be found."""
    if app and app != "Other":
        exe = find_cam_exe(app)
        if not exe:
            raise LookupError(f"{app} wasn't found on this PC.")
        subprocess.Popen([exe] + ([os.path.abspath(path)] if path else []), cwd=os.path.dirname(exe) or None)
        return exe
    if path:
        E.open_path(os.path.abspath(path))
        return "Windows file association"
    raise LookupError("Choose a CAM program.")


def log(action, target, comment=""):
    try:
        E.log_audit(f"[cam] {target}", action, "", comment)
    except Exception:                                      # noqa: BLE001 - logging must never block work
        pass


# =============================================================================
# Hooking into atelier_pdm
# =============================================================================
def known_setup_exts():
    try:
        _ensure_table()
        with E.db() as c:
            names = [r[0] for r in c.execute("SELECT filename FROM cam_programs WHERE kind='setup'").fetchall()]
    except Exception:                                          # noqa: BLE001
        return set()
    return {os.path.splitext(n)[1].lower() for n in names if os.path.splitext(n)[1]}


def install(module):
    """CAM files become vault files (indexed, linked by part number, encrypted with the vault); the _CAM data
    folder is kept out of the parts browser; the main window gets a CAM & Tools button, shows each part's CAM
    setups and NC programs under it, and right-click / double-click on those open CAM & Tools."""
    global E
    E = module
    if getattr(module, "_cam_installed", False):
        return
    module._cam_installed = True
    module.atelier_cam = sys.modules[__name__]
    for e in set(nc_exts()) | known_setup_exts():
        try:
            module.VAULT_EXTS.add(e)
        except AttributeError:
            module.VAULT_EXTS = set(module.VAULT_EXTS) | {e}
    try:
        module.SKIP_DIRS.add(CAM_DIR_NAME)
        module.SKIP_DIRS.add("_Replaced")
    except AttributeError:
        pass
    app_cls = getattr(module, "AtelierApp", None)
    if app_cls is None:
        return
    _hook_main_window(app_cls)


def _hook_main_window(cls):
    orig_build = getattr(cls, "build_ui", None)
    if orig_build is not None and not getattr(orig_build, "_cam", False):
        def build_ui(self, *a, **k):
            res = orig_build(self, *a, **k)
            bar = getattr(self, "toolbar", None)
            if bar is not None:
                try:
                    from tkinter import ttk
                    ttk.Button(bar, text="🛠  CAM & Tools", style="Tool.TButton",
                               command=lambda: open_window(self)).pack(side="right", padx=(0, 4))
                except Exception:                          # noqa: BLE001 - never break the main window
                    pass
            return res
        build_ui._cam = True
        cls.build_ui = build_ui

    orig_grid = getattr(cls, "refresh_grid", None)
    if orig_grid is not None and not getattr(orig_grid, "_cam", False):
        def refresh_grid(self, *a, **k):
            res = orig_grid(self, *a, **k)
            try:
                add_cam_rows(self)
            except Exception:                              # noqa: BLE001
                pass
            return res
        refresh_grid._cam = True
        cls.refresh_grid = refresh_grid

    orig_dbl = getattr(cls, "on_grid_double_click", None)
    if orig_dbl is not None and not getattr(orig_dbl, "_cam", False):
        def on_grid_double_click(self, event):
            f = cam_file_at(self, event.y)
            if f:
                open_window(self, select=f)
                return "break"
            return orig_dbl(self, event)
        on_grid_double_click._cam = True
        cls.on_grid_double_click = on_grid_double_click

    orig_menu = getattr(cls, "show_grid_menu", None)
    if orig_menu is not None and not getattr(orig_menu, "_cam", False):
        def show_grid_menu(self, event):
            f = cam_file_at(self, event.y)
            if f:
                return cam_row_menu(self, f, event)
            res = orig_menu(self, event)
            return res
        show_grid_menu._cam = True
        cls.show_grid_menu = show_grid_menu


def cam_file_at(app, y):
    grid = getattr(app, "grid", None)
    if grid is None:
        return None
    iid = grid.identify_row(y)
    return (getattr(app, "_cam_rows", {}) or {}).get(iid)


def add_cam_rows(app):
    """Shows each part's CAM setups and NC programs under it in the main window's tree."""
    grid, row_map = app.grid, app.row_map
    app._cam_rows = {}
    by_part = items_by_part(False)
    if not by_part:
        return
    cam_names = {r["filename"] for rows in by_part.values() for r in rows}
    cols = [c[0] for c in getattr(app, "GRID_COLS", ())]
    part_rows = {}
    for iid in grid.get_children(""):
        name = (row_map.get(iid) or (None,))[0]
        if name in cam_names:
            grid.delete(iid)                                    # shown under its part instead
            row_map.pop(iid, None)
        elif name in by_part:
            part_rows[name] = iid
    for part, iid in part_rows.items():
        for r in sorted(by_part[part], key=lambda x: (x["kind"] != "setup", x.get("op") or "")):
            vals = {"pn": "", "part": f"{KIND_LABEL[r['kind']]} {r.get('op') or ''}".strip(),
                    "cad": r.get("cam_app") or "CAM", "name": r["filename"], "rev": r.get("part_rev") or "",
                    "status": ("⚠ " if r["state"] != "current" else "") + r["state_text"],
                    "folder": SETUP_FOLDER if r["kind"] == "setup" else NC_FOLDER,
                    "modified": (r.get("created") or "")[:16]}
            cid = grid.insert(iid, "end", text="    ⚙" if r["kind"] == "setup" else "    ▸",
                              values=tuple(vals.get(c, "") for c in cols) if cols else tuple(vals.values()),
                              tags=("child",))
            row_map[cid] = (part, r["filename"])
            app._cam_rows[cid] = r["filename"]


def cam_row_menu(app, filename, event):
    """Right-click on a CAM row in the main window."""
    import tkinter as tk
    try:
        r = get_item(filename)
    except ValueError:
        return None
    colors = app.menu_colors() if hasattr(app, "menu_colors") else {}
    m = tk.Menu(app.root, tearoff=0, **colors)
    win = lambda **k: open_window(app, select=filename, **k)
    m.add_command(label="Show in CAM & Tools", command=win)
    if r["kind"] == "setup":
        m.add_command(label="Open setup for editing", command=lambda: win(action="edit"))
    else:
        m.add_command(label="Send to machine", command=lambda: win(action="send"))
    m.add_command(label="Open a copy", command=lambda: win(action="copy"))
    m.add_separator()
    m.add_command(label="Show in folder", command=lambda: E.open_path(os.path.dirname(item_path(filename))))
    m.tk_popup(event.x_root, event.y_root)
    return "break"


def open_window(ctx, select=None, action=None):
    import atelier_cam_ui
    return atelier_cam_ui.CamWindow(ctx, select=select, action=action)


# =============================================================================
# 2.7.2 - wider search for CAM programs (atelier_finder.py)
# =============================================================================
try:
    import atelier_finder as _finder

    def find_cam_exe(app):
        """Path of a CAM program - see atelier_finder for everywhere it looks."""
        return _finder.find_app(app)

    def remember_cam_exe(app, path):
        _finder.remember(app, path)

    _install_271 = install

    def install(module):
        _install_271(module)
        _finder.install(module)
except ImportError:
    _finder = None


# =============================================================================
# 2.7.3 - themes, modern controls, themed menus and the theme editor (atelier_themes.py)
# =============================================================================
try:
    import atelier_themes as _themes
    _install_272 = install

    def install(module):
        _install_272(module)
        _themes.install(module)
except ImportError:
    _themes = None


# =============================================================================
# 2.7.4 - your own part-numbering scheme (atelier_numbering.py)
# =============================================================================
try:
    import atelier_numbering as _numbering
    _install_273 = install

    def install(module):
        _install_273(module)
        _numbering.install(module)
except ImportError:
    _numbering = None


# =============================================================================
# 2.7.5 - 3D preview: no grid, more detail, better lighting (atelier_viewer.py)
# =============================================================================
try:
    import atelier_viewer as _viewer
    _install_274 = install

    def install(module):
        _install_274(module)
        _viewer.install(module)
except ImportError:
    _viewer = None


# =============================================================================
# 2.7.6 - Vault logo, dark title bars, Help, hover tips, drag and drop from File Explorer (atelier_ux.py)
# =============================================================================
try:
    import atelier_ux as _ux
    _install_275 = install

    def install(module):
        _install_275(module)
        _ux.install(module)
except ImportError:
    _ux = None


# =============================================================================
# 2.8.0 - release control, where used & BOM, safety nets, backups, filters & gallery, tour (atelier_pro.py)
# =============================================================================
try:
    import atelier_pro as _pro
    _install_276 = install

    def install(module):
        _install_276(module)
        _pro.install(module)
except ImportError:
    _pro = None


# =============================================================================
# 2.8.1 - logo, card outlines, Standard Parts Library, panel opens with CAD and starts docked (atelier_polish.py)
# =============================================================================
try:
    import atelier_polish as _polish
    _install_280 = install

    def install(module):
        _install_280(module)
        _polish.install(module)
except ImportError:
    _polish = None


# =============================================================================
# 2.8.2 - tour rebuilt, template walkthrough, roll back, settings fix (atelier_guide.py)
# =============================================================================
try:
    import atelier_guide as _guide
    _install_281 = install

    def install(module):
        _install_281(module)
        _guide.install(module)
except ImportError:
    _guide = None


# =============================================================================
# 2.8.3 - release workflow, read-only open, error messages (atelier_flow.py)
# =============================================================================
try:
    import atelier_flow as _flow
    _install_282 = install

    def install(module):
        _install_282(module)
        _flow.install(module)
except ImportError:
    _flow = None


# =============================================================================
# 2.8.4 - built-in admin, sign-in modes, Design Files, export summary, dialog layout (atelier_access.py)
# =============================================================================
try:
    import atelier_access as _access
    _install_283 = install

    def install(module):
        _install_283(module)
        _access.install(module)
except ImportError:
    _access = None


# =============================================================================
# 2.8.5 - row alignment, On hold / For quotation only, PDF watermark (atelier_states.py)
# =============================================================================
try:
    import atelier_states as _states
    _install_284 = install

    def install(module):
        _install_284(module)
        _states.install(module)
except ImportError:
    _states = None


# =============================================================================
# 2.9.0 - windows fit, assemblies follow renames, drawings follow parts, server mode, plugin bridge (atelier_v29.py)
# =============================================================================
try:
    import atelier_v29 as _v29
    _install_before_290 = install

    def install(module):
        _install_before_290(module)
        _v29.install(module)
except ImportError:
    _v29 = None


# =============================================================================
# 2.9.1 - pane layout, placeholder spacing, state button, panel edges (atelier_fix291.py)
# =============================================================================
try:
    import atelier_fix291 as _fix291
    _install_before_291 = install

    def install(module):
        _install_before_291(module)
        _fix291.install(module)
except ImportError:
    _fix291 = None


# =============================================================================
# 2.9.2 - companion panel edges: no teal frame, no flashing (atelier_fix292.py)
# =============================================================================
try:
    import atelier_fix292 as _fix292
    _install_before_292 = install

    def install(module):
        _install_before_292(module)
        _fix292.install(module)
except ImportError:
    _fix292 = None


# =============================================================================
# 2.9.3 - companion panel fixes (atelier_fix293.py)
# =============================================================================
try:
    import atelier_fix293 as _fix293
    _install_before_293 = install

    def install(module):
        _install_before_293(module)
        _fix293.install(module)
except ImportError:
    _fix293 = None


# =============================================================================
# 2.9.4 - companion panel (atelier_fix294.py)
# =============================================================================
try:
    import atelier_fix294 as _fix294
    _install_before_294 = install

    def install(module):
        _install_before_294(module)
        _fix294.install(module)
except ImportError:
    _fix294 = None


# =============================================================================
# 2.9.5 - companion panel (atelier_fix295.py)
# =============================================================================
try:
    import atelier_fix295 as _fix295
    _install_before_295 = install

    def install(module):
        _install_before_295(module)
        _fix295.install(module)
except ImportError:
    _fix295 = None
