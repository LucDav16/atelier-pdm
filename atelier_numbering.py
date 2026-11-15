"""
Atelier PDM - your own part-numbering scheme (atelier_numbering.py)  2.7.4
==========================================================================

Part-numbering scheme (account menu > Part numbering scheme…, administrators)
    Standard scheme (the default): parts in a project's Parts folder get 01_0001, 01_0002…, assemblies in its
    Assemblies folder get 01_A001… - exactly as before.
    "Keep assemblies separate from parts" (ticked by default). Untick it to number everything in a project from
    one series, wherever it's saved in the project - the way Fusion, PhysCAD and SolidCraft keep parts and
    assemblies together.
    Each type of item (Part, Assembly, and any you add - Drawing, Fixture, Purchased…) has a number format
    and the folder names that use it. Formats are built from tokens:
        {PRJ}     the project number from the NN_ folder name (leave it out for one vault-wide series)
        {N:4}     the serial number, padded to 4 digits ({N} = no padding)
        {YY} {YYYY} {MM}   year / month the number was made
        anything else is fixed text, e.g.  {PRJ}_{N:4}   ->  01_0007
                                           {PRJ}-A{N:3}  ->  01-A003
                                           AV{YY}-{N:5}  ->  AV26-00012
    The scheme is stored in the vault, so everyone using it gets the same numbers. Existing part numbers are
    never changed; only new numbers follow a new format, and each type's serial carries on from where it was.

Installed by atelier_cam.install(). If the scheme can't be read, the standard scheme is used.
"""
import copy
import html
import json
import os
import re
import sys
import time

E = None
SETTING = "numbering_scheme"
DEFAULT = {"version": 1, "separate": True, "types": [
    {"key": "P", "label": "Part", "template": "{PRJ}_{N:4}", "folders": ["Parts", "Part", "Prt", "Components"]},
    {"key": "A", "label": "Assembly", "template": "{PRJ}_A{N:3}", "folders": ["Assemblies", "Assembly", "Asm", "Assy"]},
]}
TOKEN_RE = re.compile(r"\{(PRJ|N(?::(\d))?|YYYY|YY|MM)\}")
BAD_LITERAL = re.compile(r'[\\/:*?"<>|\s{}]')
BAD_FOLDER = re.compile(r'[\\/:*?"<>|]')
LEGACY = re.compile(r"^(\d{2,3})_(?:(A)(\d{3})|(\d{4}))(?![0-9A-Za-z])")
SPARE_KEYS = "DFTJKLMNQRSUVWXYZBCEGHIO"
TTL = 60                                       # seconds before re-reading the scheme (another PC may change it)
_STATE = {"scheme": None, "at": 0.0, "compiled": [], "by_key": {}}
ORIG = {}


# =============================================================================
# Formats
# =============================================================================
def compile_template(t):
    """(regex, serial width or None, uses {PRJ}, [problems])."""
    t = (t or "").strip()
    parts, pos, n, prj, width, problems = [], 0, 0, 0, None, []
    for m in TOKEN_RE.finditer(t):
        parts.append(re.escape(t[pos:m.start()]))
        pos = m.end()
        tok = m.group(1)
        if tok == "PRJ":
            prj += 1
            parts.append(r"(?P<prj>\d{2,3})" if prj == 1 else r"\d{2,3}")
        elif tok.startswith("N"):
            n += 1
            if m.group(2):
                width = int(m.group(2))
                parts.append(rf"(?P<n>\d{{{max(1, width)}}})" if n == 1 else r"\d+")
            else:
                parts.append(r"(?P<n>\d{1,6})" if n == 1 else r"\d+")
        elif tok == "YYYY":
            parts.append(r"\d{4}")
        else:
            parts.append(r"\d{2}")
    parts.append(re.escape(t[pos:]))
    if not t:
        problems.append("The format is empty.")
    if n != 1:
        problems.append("The format needs exactly one serial number, for example {N:4}.")
    if width is not None and not 1 <= width <= 8:
        problems.append("Pad the serial to between 1 and 8 digits, for example {N:4}.")
    if prj > 1:
        problems.append("Use {PRJ} at most once.")
    literal = TOKEN_RE.sub("", t)
    if "{" in literal or "}" in literal:
        problems.append("Unknown token - use {PRJ}, {N}, {N:4}, {YY}, {YYYY} or {MM}.")
    elif BAD_LITERAL.search(literal):
        problems.append('Fixed text can\'t contain spaces or any of  \\ / : * ? " < > |')
    return "^" + "".join(parts) + r"(?![0-9A-Za-z])", width, prj >= 1, problems


def render(template, prj, n, when=None):
    when = when or time.localtime()

    def sub(m):
        tok = m.group(1)
        if tok == "PRJ":
            return prj if prj not in (None, "*") else "00"
        if tok.startswith("N"):
            return f"{int(n):0{int(m.group(2))}d}" if m.group(2) else str(int(n))
        return time.strftime({"YYYY": "%Y", "YY": "%y", "MM": "%m"}[tok], when)
    return TOKEN_RE.sub(sub, template)


def max_serial(width):
    return 10 ** width - 1 if width else 999999


# =============================================================================
# The scheme
# =============================================================================
def separate(s=None):
    """True when assemblies have their own folders and number series (the standard scheme)."""
    s = s if s is not None else scheme()
    return bool(s.get("separate", True)) and any(t.get("key") == "A" for t in s.get("types", []))


def primary(s=None):
    """Key of the type that everything is numbered as when assemblies aren't kept separate."""
    s = s if s is not None else scheme()
    return next((t["key"] for t in s.get("types", []) if t.get("key") != "A"), None)


def check(s):
    """Problems with a scheme (empty list = fine)."""
    problems = []
    types = (s or {}).get("types") if isinstance(s, dict) else None
    if not isinstance(types, list) or not types:
        return ["Add at least one type."]
    if not s.get("separate", True) and not any(t.get("key") != "A" for t in types):
        return ["Parts and assemblies share one series, so keep a Part type."]
    if len(types) > 12:
        problems.append("Use 12 types or fewer.")
    keys, folders, compiled = set(), {}, []
    for t in types:
        label = str(t.get("label") or "").strip()
        key = str(t.get("key") or "")
        if not label:
            problems.append("Every type needs a name.")
        if not re.fullmatch(r"[A-Z]", key) or key in keys:
            problems.append(f"'{label}' has a missing or repeated internal key.")
        keys.add(key)
        rx, width, has_prj, errs = compile_template(t.get("template"))
        problems += [f"{label}: {e}" for e in errs]
        fl = [str(f).strip() for f in (t.get("folders") or []) if str(f).strip()]
        if not fl:
            problems.append(f"{label}: give at least one folder name.")
        for f in fl:
            if BAD_FOLDER.search(f):
                problems.append(f"{label}: '{f}' isn't a valid folder name.")
            other = folders.get(f.lower())
            if other and other != label:
                problems.append(f"The folder name '{f}' is used by both {other} and {label}.")
            folders[f.lower()] = label
        if not errs:
            compiled.append((t, re.compile(rx), width, has_prj))
    if problems:
        return problems
    # every type's numbers must read back as that type (no two formats that look alike)
    for t, _rx, width, has_prj in compiled:
        for prj in ("01", "123"):
            for n in (7, max_serial(width)):
                text = render(t["template"], prj, n) + " Motor Mount"
                got = _parse_with(compiled, text)
                if not got or got[1] != t["key"] or got[2] != n or (has_prj and got[0] != prj):
                    clash = next((x[0]["label"] for x in compiled if got and x[0]["key"] == got[1]), "another type")
                    return [f"{t['label']} numbers like {render(t['template'], prj, n)} can be mistaken for "
                            f"{clash} numbers. Make the formats differ in their fixed text."]
    return []


def _parse_with(compiled, text):
    for t, rx, _w, has_prj in compiled:
        m = rx.match(text or "")
        if m:
            return (m.group("prj") if has_prj else "*", t["key"], int(m.group("n")), m.group(0))
    return None


def _use(s):
    compiled = []
    for t in s["types"]:
        rx, width, has_prj, errs = compile_template(t["template"])
        if not errs:
            compiled.append((t, re.compile(rx), width, has_prj))
    _STATE.update(scheme=s, compiled=compiled, by_key={t["key"]: (t, w, p) for t, _r, w, p in compiled})
    if E is not None:                                            # folder names the rest of the program looks at
        lower = lambda t: {f.lower() for f in t["folders"]}
        a = next((t for t in s["types"] if t["key"] == "A"), None)
        sep = separate(s)
        E.ASM_FOLDER_NAMES = lower(a) if (a and sep) else set()
        E.PART_FOLDER_NAMES = set().union(*(lower(t) for t in s["types"] if t["key"] != "A" or not sep)) or set()
        E.DEFAULT_PROJECT_SUBFOLDERS = tuple(t["folders"][0] for t in s["types"]
                                             if t.get("folders") and (sep or t["key"] != "A"))


def scheme(refresh=False):
    now = time.time()
    if refresh or _STATE["scheme"] is None or now - _STATE["at"] > TTL:
        try:
            raw = E.get_setting(SETTING, None)
            s = json.loads(raw) if raw else copy.deepcopy(DEFAULT)
            if check(s):
                s = copy.deepcopy(DEFAULT)
        except Exception:                                        # noqa: BLE001 - database not ready yet
            s = _STATE["scheme"] or copy.deepcopy(DEFAULT)
        _use(s)
        _STATE["at"] = now
    return _STATE["scheme"]


def compiled():
    scheme()
    return _STATE["compiled"]


def type_of(key):
    scheme()
    return _STATE["by_key"].get(key)


def has_prj(key):
    t = type_of(key)
    return True if t is None else t[2]


def set_scheme(s):
    """Saves a new scheme for the whole vault (administrators)."""
    guard = getattr(E, "_admin_guard", None)
    if guard and guard():
        raise PermissionError("Only an administrator can change the numbering scheme.")
    problems = check(s)
    if problems:
        raise ValueError(problems[0])
    old = scheme(refresh=True)
    E.set_setting(SETTING, json.dumps(s))
    _use(s)
    _STATE["at"] = time.time()
    summary = ("separate assemblies; " if separate(s) else "parts and assemblies in one series; ") + \
        "; ".join(f"{t['label']} {t['template']}" for t in s["types"])
    E.log_audit("[settings]", "Numbering Scheme Changed", "", f"{summary} (was: "
                + "; ".join(f"{t['label']} {t['template']}" for t in old["types"]) + ")")


def examples(t, prj="01"):
    return [render(t["template"], prj, n) for n in (1, 7, 42)]


# =============================================================================
# Replacements for atelier_pdm's numbering functions
# =============================================================================
def parse_pn(text):
    got = _parse_with(compiled(), text)
    if got:
        return got
    m = LEGACY.match(text or "")                                 # numbers made under the standard scheme
    if m:
        if m.group(2):
            return m.group(1), "A", int(m.group(3)), m.group(0)
        return m.group(1), "P", int(m.group(4)), m.group(0)
    return None


def format_pn(prefix, kind, n):
    t = type_of(kind)
    if t is None:
        return ORIG["format_pn"](prefix, kind, n)
    return render(t[0]["template"], prefix, n)


class _AnyNumber:
    """Stands in for atelier_pdm.PN_RE: matches a part number in any current or older format."""
    pattern = "atelier part number"

    def match(self, text):
        for _t, rx, _w, _p in compiled():
            m = rx.match(text or "")
            if m:
                return m
        return LEGACY.match(text or "")


class _MaxSerial(dict):
    def __getitem__(self, kind):
        t = type_of(kind)
        return max_serial(t[1]) if t else 9999

    def __contains__(self, kind):
        return True

    def get(self, kind, default=None):
        return self[kind]


def folder_kind(name):
    low = (name or "").strip().lower()
    s = scheme()
    for t in s["types"]:
        if low in {f.lower() for f in t["folders"]}:
            return t["key"] if (t["key"] != "A" or separate(s)) else primary(s)
    return None


def numbering_context(rel):
    if not rel or rel in ("*", getattr(E, "VAULT_ROOT_LABEL", "Vault Root"), "."):
        return None
    parts = os.path.normpath(rel).split(os.sep)
    for i, p in enumerate(parts):
        m = E.PROJECT_RE.match(p)
        if m:
            if int(m.group(1)) == 0:
                return None                                      # 00_ folders are templates
            kind = None
            for sub in parts[i + 1:]:
                k = folder_kind(sub)
                if k:
                    kind = k
            if kind is None and not separate():
                kind = primary()                                 # one series: anywhere in the project
            prefix = m.group(1) if (kind is None or has_prj(kind)) else "*"
            return {"prefix": prefix, "project": os.sep.join(parts[:i + 1]), "kind": kind}
    return None


def _series(prefix, kind):
    return prefix if has_prj(kind) else "*"


def _kind(kind):
    """Assemblies use the Part series when they aren't kept separate."""
    return primary() if (kind == "A" and not separate()) else kind


def reserve_number(prefix, kind):
    kind = _kind(kind)
    return ORIG["reserve_number"](_series(prefix, kind), kind)


def peek_next_number(prefix, kind):
    if kind == "A" and not separate():
        return "— (numbered as parts)"
    if type_of(kind) is None:
        return None
    return ORIG["peek_next_number"](_series(prefix, kind), kind)


def _patch_engine(module):
    for name in ("format_pn", "reserve_number", "peek_next_number", "parse_pn", "numbering_context"):
        ORIG[name] = getattr(module, name)
    module.parse_pn = parse_pn
    module.format_pn = format_pn
    module.numbering_context = numbering_context
    module.reserve_number = reserve_number
    module.peek_next_number = peek_next_number
    module.PN_RE = _AnyNumber()
    module.MAX_SERIAL = _MaxSerial()
    module.numbering_scheme = scheme


# =============================================================================
# Scheme designer (administrators)
# =============================================================================
TOKEN_HELP = ("{PRJ} project number (from the NN_ folder)     {N:4} serial padded to 4 digits     {N} serial, "
              "no padding     {YY} / {YYYY} year     {MM} month     anything else is fixed text, e.g. {PRJ}-A{N:3}")


def designer(app):
    import tkinter as tk
    from tkinter import ttk, messagebox
    if not app.ensure_admin():
        return
    S = app.S
    work = copy.deepcopy(scheme(refresh=True))
    dlg = E.ThemedDialog(app, "Part numbering", "Part numbering scheme",
                         "Each type has a number format and the folder names that use it. New parts saved in a "
                         "project's folder of that name get the next number. Existing numbers are never changed.",
                         width=820, resizable=True)
    sep_var = tk.BooleanVar(value=bool(work.get("separate", True)))
    ttk.Checkbutton(dlg.body, text="Keep assemblies separate from parts (their own folder and number series)",
                    variable=sep_var, style="Dialog.TCheckbutton",
                    command=lambda: (work.__setitem__("separate", sep_var.get()), load(sel() and sel()["key"]))
                    ).pack(side="top", anchor="w")
    sep_note = ttk.Label(dlg.body, text="", style="CardMuted.TLabel", wraplength=S(780), justify="left")
    sep_note.pack(side="top", anchor="w", pady=(0, S(8)))
    ttk.Label(dlg.body, text=TOKEN_HELP, style="CardMuted.TLabel", wraplength=S(780), justify="left").pack(
        side="top", anchor="w", pady=(0, S(8)))
    err = ttk.Label(dlg.body, text="", style="CardDanger.TLabel", wraplength=S(780), justify="left")
    err.pack(side="bottom", anchor="w", pady=(S(6), 0))
    row = ttk.Frame(dlg.body, style="Dialog.TFrame")
    row.pack(side="bottom", fill="x", pady=(S(8), 0))
    wrap = ttk.Frame(dlg.body, style="Dialog.TFrame")
    wrap.pack(side="top", fill="both", expand=True)
    cols = (("label", "Type", 120), ("template", "Format", 150), ("ex", "Examples (project 01)", 230),
            ("folders", "Folders that use it", 260))
    tv = ttk.Treeview(wrap, columns=[c[0] for c in cols], show="headings", height=7, selectmode="browse")
    for k, label, w in cols:
        tv.heading(k, text=label, anchor="w")
        tv.column(k, width=S(w), anchor="w", stretch=k in ("ex", "folders"))
    sb = ttk.Scrollbar(wrap, orient="vertical", command=tv.yview)
    tv.configure(yscrollcommand=sb.set)
    sb.pack(side="right", fill="y")
    tv.pack(side="left", fill="both", expand=True)

    tv.tag_configure("unused", foreground=app.t["muted_fg"])

    def load(select=None):
        tv.delete(*tv.get_children())
        sep = separate(work)
        prim = next((x["label"] for x in work["types"] if x["key"] == primary(work)), "Part")
        sep_note.configure(text=(
            "Parts go in a project's Parts folder, assemblies in its Assemblies folder, each with its own series."
            if sep else f"Everything saved anywhere in a project - parts and assemblies - is numbered as a {prim}, "
            "from one series. Other types you add still use their own folders."))
        for t in work["types"]:
            _rx, _w, _p, errs = compile_template(t["template"])
            ex = "  ·  ".join(examples(t)) if not errs else "⚠ " + errs[0]
            unused = t["key"] == "A" and not sep
            folders = "not used - assemblies are numbered as " + prim + "s" if unused else ", ".join(t["folders"])
            tv.insert("", "end", iid=t["key"], values=(t["label"], t["template"], ex, folders),
                      tags=("unused",) if unused else ())
        if select and tv.exists(select):
            tv.selection_set(select)
        problems = check(work)
        err.configure(text=("⚠ " + problems[0]) if problems else "")

    def sel():
        s = tv.selection()
        return next((t for t in work["types"] if s and t["key"] == s[0]), None)

    def edit(t=None):
        new = t is None
        if new:
            used = {x["key"] for x in work["types"]}
            key = next((k for k in SPARE_KEYS if k not in used), None)
            if key is None:
                err.configure(text="No more types can be added.")
                return
            t = {"key": key, "label": "", "template": "{PRJ}_D{N:3}", "folders": []}

        def submit(v):
            label = v["label"].strip()
            if not label:
                return "error", "Give the type a name, e.g. Drawing or Fixture."
            if any(x["label"].lower() == label.lower() and x["key"] != t["key"] for x in work["types"]):
                return "error", f"There's already a type called '{label}'."
            _rx, _w, _p, errs = compile_template(v["template"])
            if errs:
                return "error", errs[0]
            fl = [f.strip() for f in re.split(r"[,;]", v["folders"]) if f.strip()]
            if not fl:
                return "error", "Give at least one folder name, e.g. Drawings."
            bad = [f for f in fl if BAD_FOLDER.search(f)]
            if bad:
                return "error", f"'{bad[0]}' isn't a valid folder name."
            return dict(t, label=label, template=v["template"].strip(), folders=list(dict.fromkeys(fl)))
        res = E.run_form(app, "Number type", "New type" if new else f"Edit {t['label']}",
                         TOKEN_HELP, [("label", "Type name", False, t["label"]),
                                      ("template", "Number format", False, t["template"]),
                                      ("folders", "Folder names that use it (comma separated) - the first is "
                                                  "created in new projects", False, ", ".join(t["folders"]))],
                         submit, "Save", width=560, parent=dlg)
        dlg.grab_set()
        if res:
            if new:
                work["types"].append(res)
            else:
                work["types"][work["types"].index(t)] = res
            load(res["key"])

    def remove():
        t = sel()
        if not t:
            return
        if len(work["types"]) == 1:
            err.configure(text="Keep at least one type.")
            return
        if messagebox.askyesno("Remove type", f"Remove '{t['label']}'?\n\nFiles already numbered keep their "
                               "numbers; new files in its folders won't be numbered.", parent=dlg):
            work["types"].remove(t)
            load()
        dlg.grab_set()

    def move(step):
        t = sel()
        if t:
            i = work["types"].index(t)
            j = max(0, min(len(work["types"]) - 1, i + step))
            work["types"].insert(j, work["types"].pop(i))
            load(t["key"])

    def reset():
        work.clear()
        work.update(copy.deepcopy(DEFAULT))
        sep_var.set(True)
        load()

    for text, cmd, style in (("＋ Add type…", lambda: edit(), "Accent.TButton"), ("Edit…", lambda: sel() and edit(sel()), "TButton"),
                             ("Remove", remove, "TButton"), ("Move up", lambda: move(-1), "TButton"),
                             ("Move down", lambda: move(1), "TButton"), ("Standard scheme", reset, "TButton")):
        ttk.Button(row, text=text, style=style, command=cmd).pack(side="left", padx=(0, S(6)))
    tv.bind("<Double-1>", lambda e: (edit(sel()) if tv.identify_row(e.y) else edit(), "break")[1])
    tv.bind("<Return>", lambda e: sel() and edit(sel()))
    tv.bind("<Delete>", lambda e: remove())

    def popup(e):
        iid = tv.identify_row(e.y)
        if iid:
            tv.selection_set(iid)
        m = tk.Menu(dlg, tearoff=0, **(app.menu_colors() if hasattr(app, "menu_colors") else {}))
        m.add_command(label="Add type…", command=lambda: edit())
        if iid:
            m.add_command(label="Edit…", command=lambda: edit(sel()))
            m.add_command(label="Remove", command=remove)
            m.add_separator()
            m.add_command(label="Move up", command=lambda: move(-1))
            m.add_command(label="Move down", command=lambda: move(1))
        m.tk_popup(e.x_root, e.y_root)
    tv.bind("<Button-3>", popup)
    load()

    def validate():
        problems = check(work)
        if problems:
            err.configure(text="⚠ " + problems[0])
            return E.INVALID
        work["separate"] = sep_var.get()
        current = scheme(refresh=True)
        if work == dict(current, separate=current.get("separate", True)):
            return "unchanged"
        if not messagebox.askyesno("Change numbering", "Save this numbering scheme for the whole vault?\n\n"
                                   "• Existing part numbers stay exactly as they are.\n"
                                   "• New numbers follow the new formats; each type's serial carries on.\n"
                                   "• Everyone sees the change within a minute.", parent=dlg):
            dlg.grab_set()
            return E.INVALID
        try:
            set_scheme(copy.deepcopy(work))
        except (ValueError, PermissionError) as e:
            err.configure(text=str(e))
            return E.INVALID
        return "saved"
    dlg.validate = validate
    dlg.add_buttons("Save scheme", "Accent.TButton")
    dlg.geometry(f"{S(860)}x{S(470)}")
    if dlg.show() == "saved":
        app.log("Numbering scheme saved. New part numbers use the new formats.", "ok")
        try:
            app.refresh_all()
        except Exception:                                        # noqa: BLE001
            pass


# =============================================================================
# Hooks
# =============================================================================
def _hook_ui(module):
    cls = getattr(module, "AtelierApp", None)
    orig = getattr(cls, "update_identity", None) if cls else None
    if orig is not None and not getattr(orig, "_numbering", False):
        def update_identity(self, *a, **k):
            res = orig(self, *a, **k)
            try:
                m = self.account_menu
                m.add_separator()
                m.add_command(label="🔢  Part numbering scheme…", command=lambda: designer(self))
            except Exception:                                    # noqa: BLE001
                pass
            return res
        update_identity._numbering = True
        cls.update_identity = update_identity

def install(module):
    global E
    E = module
    if getattr(module, "_numbering_installed", False):
        return
    module._numbering_installed = True
    module.atelier_numbering = sys.modules[__name__]
    _patch_engine(module)
    _use(copy.deepcopy(DEFAULT))                                 # until the vault's own scheme is read
    _STATE["at"] = 0.0
    _hook_ui(module)
