"""
Atelier PDM - release workflow, read-only open, error reporting (atelier_flow.py)  2.8.3
=========================================================================================

Release workflow (lifecycle)
    Every part has a state, shown in a State column in the file list and in Properties, with a
    "Change state ▾" button listing what can be done next. Right-click shows the same actions.
    The workflow is set in Administration › Release workflow (also account menu › Release workflow…):
      * States - name, colour, whether the file can be changed (checked out, renamed), whether entering it keeps
        a release copy, whether it waits for approval (shows in Approvals), whether it can be deleted.
      * Actions - name, from-state, to-state, who may do it (anyone / administrators / the person who asked),
        whether a note is required, and whether it must be someone other than the person who asked.
      * Presets: "With approval" (request › approve) and "Simple" (release directly).
    Default: In work › Request release › Awaiting approval › Approve & release › Released (or Return / Withdraw);
    administrators can also Release now; Make obsolete / Reinstate. Revise starts a new revision of a released part.

Opening parts
    Opening a part you haven't checked out asks: Check out & open, Open read-only, (if someone else has it)
    Notify me when it's free, or Cancel. Read-only opens a protected copy in <working folder>\\_ReadOnly - with the
    parts an assembly uses - never the vault file itself.

Errors
    A problem in any button or menu now shows a message and is written to atelier_error.log, instead of
    nothing happening.
"""
import copy
import json
import os
import shutil
import stat
import subprocess
import sys
import time
import traceback

VERSION = "2.8.3"
E = None
P = None                                   # atelier_pro
G = None                                   # atelier_guide
ORIG = {}
SETTING = "lifecycle_config"
_CACHE = {"cfg": None, "at": 0.0}
TTL = 20
WHO = {"anyone": "Anyone", "admin": "Administrators", "requester": "Person who asked (or an administrator)"}
COLOURS = {"grey": "muted_fg", "blue": "accent", "amber": "warning", "green": "success", "red": "danger"}

DEFAULT = {"version": 1, "states": [
    {"key": "In work", "label": "In work", "colour": "blue", "editable": True, "release": False, "review": False,
     "delete": True},
    {"key": "Awaiting approval", "label": "Awaiting approval", "colour": "amber", "editable": False,
     "release": False, "review": True, "delete": False},
    {"key": "Released", "label": "Released", "colour": "green", "editable": False, "release": True, "review": False,
     "delete": False},
    {"key": "Obsolete", "label": "Obsolete", "colour": "red", "editable": False, "release": False, "review": False,
     "delete": True}],
    "actions": [
    {"label": "Request release", "from": "In work", "to": "Awaiting approval", "who": "anyone", "note": False,
     "other": False},
    {"label": "Approve & release", "from": "Awaiting approval", "to": "Released", "who": "admin", "note": False,
     "other": False},
    {"label": "Return for changes", "from": "Awaiting approval", "to": "In work", "who": "admin", "note": True,
     "other": False},
    {"label": "Withdraw request", "from": "Awaiting approval", "to": "In work", "who": "requester", "note": False,
     "other": False},
    {"label": "Release now", "from": "In work", "to": "Released", "who": "admin", "note": False, "other": False},
    {"label": "Make obsolete", "from": "In work", "to": "Obsolete", "who": "admin", "note": True, "other": False},
    {"label": "Make obsolete", "from": "Released", "to": "Obsolete", "who": "admin", "note": True, "other": False},
    {"label": "Reinstate", "from": "Obsolete", "to": "In work", "who": "admin", "note": False, "other": False}]}

SIMPLE = {"version": 1, "states": copy.deepcopy([s for s in DEFAULT["states"] if s["key"] != "Awaiting approval"]),
          "actions": [
    {"label": "Release", "from": "In work", "to": "Released", "who": "anyone", "note": False, "other": False},
    {"label": "Make obsolete", "from": "In work", "to": "Obsolete", "who": "admin", "note": True, "other": False},
    {"label": "Make obsolete", "from": "Released", "to": "Obsolete", "who": "admin", "note": True, "other": False},
    {"label": "Reinstate", "from": "Obsolete", "to": "In work", "who": "admin", "note": False, "other": False}]}
PRESETS = {"With approval (standard)": DEFAULT, "Simple - release directly": SIMPLE}


# =============================================================================
# Error reporting
# =============================================================================
def log_error(where, text=None):
    try:
        base = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "Atelier PDM")
        os.makedirs(base, exist_ok=True)
        with open(os.path.join(base, "atelier_error.log"), "a", encoding="utf-8") as f:
            f.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} · {where} ({VERSION}) =====\n"
                    f"{text or traceback.format_exc()}\n")
        return os.path.join(base, "atelier_error.log")
    except Exception:                                           # noqa: BLE001
        return ""


def install_error_handler(root):
    def report(exc, val, tb):
        text = "".join(traceback.format_exception(exc, val, tb))
        path = log_error("button or menu", text)
        try:
            from tkinter import messagebox
            messagebox.showerror(getattr(E, "APP_NAME", "Atelier PDM"),
                                 f"That didn't work:\n\n{val}\n\nDetails were saved to:\n{path}", parent=root)
        except Exception:                                       # noqa: BLE001
            pass
    try:
        root.report_callback_exception = report
    except Exception:                                           # noqa: BLE001
        pass


# =============================================================================
# Workflow configuration
# =============================================================================
def _from_old_settings():
    """First use: start from the release settings chosen in 2.8.0."""
    c = copy.deepcopy(DEFAULT)
    try:
        if E.get_setting("release_approval", "1") == "0":
            c = copy.deepcopy(DEFAULT)
            c["actions"].insert(0, {"label": "Release", "from": "In work", "to": "Released", "who": "anyone",
                                    "note": False, "other": False})
        if E.get_setting("release_other_approver", "0") == "1":
            for a in c["actions"]:
                if a["label"] == "Approve & release":
                    a["other"] = True
    except Exception:                                           # noqa: BLE001
        pass
    return c


def check(c):
    """Problems with a workflow (empty list = fine)."""
    out = []
    states = c.get("states") or []
    keys = [s.get("key") for s in states]
    if not states:
        return ["Add at least one state."]
    if not states[0].get("editable"):
        out.append(f"The first state ('{states[0].get('label')}') is where new files start, so files in it must be "
                   "changeable.")
    if len(set(keys)) != len(keys) or any(not k for k in keys):
        out.append("Every state needs a different name.")
    labels = [s.get("label", "").strip().lower() for s in states]
    if len(set(labels)) != len(labels) or any(not x for x in labels):
        out.append("Every state needs a different name.")
    seen = set()
    for a in c.get("actions") or []:
        if a.get("from") not in keys or a.get("to") not in keys:
            out.append(f"The action '{a.get('label')}' uses a state that no longer exists.")
        elif a["from"] == a["to"]:
            out.append(f"'{a.get('label')}' goes from a state to itself.")
        if not (a.get("label") or "").strip():
            out.append("Every action needs a name.")
        if a.get("who") not in WHO:
            out.append(f"'{a.get('label')}': choose who can do it.")
        k = (a.get("from"), (a.get("label") or "").strip().lower())
        if k in seen:
            out.append(f"Two actions from '{a.get('from')}' are both called '{a.get('label')}'.")
        seen.add(k)
    return out


def cfg(refresh=False):
    now = time.time()
    if not refresh and _CACHE["cfg"] is not None and now - _CACHE["at"] < TTL:
        return _CACHE["cfg"]
    c = None
    try:
        raw = E.get_setting(SETTING, None)
        if raw:
            c = json.loads(raw)
            if check(c):
                c = None
    except Exception:                                           # noqa: BLE001
        c = None
    if c is None:
        c = _from_old_settings()
    for s in c["states"]:
        for k, d in (("editable", False), ("release", False), ("review", False), ("delete", False),
                     ("colour", "grey")):
            s.setdefault(k, d)
    for a in c["actions"]:
        for k, d in (("note", False), ("other", False), ("who", "anyone")):
            a.setdefault(k, d)
    _CACHE.update(cfg=c, at=now)
    return c


def save(c):
    problems = check(c)
    if problems:
        raise ValueError(problems[0])
    if not P._admin_ok():
        raise PermissionError("Only an administrator can change the release workflow.")
    in_use = used_states()
    removed = [k for k in in_use if k not in {s["key"] for s in c["states"]}]
    if removed:
        raise ValueError(f"Files are still in '{removed[0]}' - move them to another state before removing it.")
    E.set_setting(SETTING, json.dumps(c))
    _CACHE.update(cfg=None, at=0.0)
    E.log_audit("[settings]", "Release Workflow Changed", "",
                " › ".join(s["label"] for s in c["states"]) + f" · {len(c['actions'])} actions")
    _sync_filters()


def used_states():
    try:
        with E.db() as c:
            return {r[0]: r[1] for r in c.execute("SELECT state, COUNT(*) FROM lifecycle GROUP BY state").fetchall()}
    except Exception:                                           # noqa: BLE001
        return {}


def initial():
    return cfg()["states"][0]["key"]


def state_def(key):
    for s in cfg()["states"]:
        if s["key"] == key:
            return s
    st = copy.deepcopy(cfg()["states"][0])                      # unknown (removed) state: treat as the first one
    st.update(key=key, label=key)
    return st


def state_key(name):
    k = P.state_of(name)["state"]
    return k if k else initial()


def label(key):
    return state_def(key)["label"]


def symbol(sd):
    return "✔" if sd["release"] else "◐" if sd["review"] else "○" if sd["editable"] else "⊘"


def badge(name):
    sd = state_def(state_key(name))
    return f"{symbol(sd)}  {sd['label']}"


def colour(sd, t):
    return t.get(COLOURS.get(sd.get("colour"), "muted_fg"), t.get("fg"))


def editable(name):
    info = E.INDEX.files.get(name)
    if info is not None and info.is_library:
        return True
    return bool(state_def(state_key(name))["editable"])


def actions_from(key):
    return [a for a in cfg()["actions"] if a["from"] == key]


# =============================================================================
# Doing an action
# =============================================================================
def who_ok(a, row):
    if a["who"] == "anyone":
        return True, ""
    if P._admin_ok():
        return True, ""
    if a["who"] == "admin":
        return False, "administrators only"
    if (row.get("requested_by") or "").lower() == P.user().lower():
        return True, ""
    return False, "only the person who asked, or an administrator"


def can(name, a):
    """(allowed, reason)."""
    info = E.INDEX.files.get(name)
    if info is None:
        return False, "not in the vault yet - check it in first"
    if info.is_library:
        return False, "Standard Parts Library files don't have a release state"
    lock = (E.INDEX.records.get(name) or {}).get("locked_by") or ""
    if lock:
        return False, f"checked out by {'you' if lock == P.user() else lock} - check it in first"
    row = P.state_of(name)
    if (row.get("state") or initial()) != a["from"]:
        return False, f"it's {label(row.get('state') or initial()).lower()}"
    ok, why = who_ok(a, row)
    if not ok:
        return False, why
    if a.get("other") and (row.get("requested_by") or "").lower() == P.user().lower():
        return False, "someone other than the person who asked must do this"
    return True, ""


def perform(name, a, note=""):
    ok, why = can(name, a)
    if not ok:
        return "error", f"Can't {a['label'].lower()} '{name}': {why}."
    if a.get("note") and not (note or "").strip():
        return "error", f"'{a['label']}' needs a note - say why."
    rev = (E.INDEX.records.get(name) or {}).get("revision") or "A-01"
    to = state_def(a["to"])
    if to["release"]:
        try:
            P.keep_release_copy(E.INDEX.files[name].path, name, rev)
        except Exception as e:                                  # noqa: BLE001
            return "error", f"Couldn't keep a release copy: {e}"
    row = P.state_of(name)
    from_review = state_def(a["from"])["review"]
    fields = dict(decided_by=P.user(), decided_at=P.now(), note=note or a["label"], revision=rev)
    if not from_review:
        fields.update(requested_by=P.user(), requested_at=P.now())
    else:
        fields.update(requested_by=row.get("requested_by"), requested_at=row.get("requested_at"))
    P._set_state(name, a["to"], **fields)
    if to["review"]:
        try:
            P.watch(name)
        except Exception:                                       # noqa: BLE001
            pass
    E.log_audit(name, a["label"], rev, note or f"{label(a['from'])} › {to['label']}")
    extra = ""
    if to["review"]:
        extra = " It's waiting for approval (Approvals on the toolbar)."
    elif to["release"]:
        extra = " A release copy is kept in the history. To change it later: Revise."
    return "ok", f"{a['label']}: '{name}' is now {to['label']} (rev {rev}).{extra}"


# =============================================================================
# Rules applied to the existing commands
# =============================================================================
def blocked(name, action):
    if editable(name):
        return None
    sd = state_def(state_key(name))
    if sd["release"]:
        return (f"'{name}' is {sd['label'].lower()}, so it can't be {action}. To change it, start a new revision: "
                "right-click › Revise.")
    if sd["review"]:
        return f"'{name}' is {sd['label'].lower()}, so it can't be {action} until it's decided."
    return f"'{name}' is {sd['label'].lower()}, so it can't be {action}. An administrator can change its state."


def up_issue(filename, new_rev, comment=""):
    sd = state_def(state_key(filename))
    if not sd["editable"] and not sd["release"]:
        return "error", blocked(filename, "up-issued")
    res = P.ORIG["up_issue"](filename, new_rev, comment)
    if res and res[0] == "ok" and not sd["editable"]:
        P._set_state(filename, initial(), decided_by=P.user(), decided_at=P.now(), note=f"Revised to {new_rev}",
                     revision=new_rev, requested_by=P.user(), requested_at=P.now())
        return "ok", res[1] + f" It's {label(initial())} again until the new revision is released."
    return res


def delete_component(filename):
    sd = state_def(state_key(filename))
    if not sd["delete"]:
        return "error", (f"'{filename}' is {sd['label'].lower()}, so it can't be deleted. An administrator can make "
                         "it obsolete instead.")
    res = P.ORIG["delete_component"](filename)
    if res and res[0] == "ok":
        try:
            with E.db() as c:
                c.execute("DELETE FROM lifecycle WHERE filename=?", (filename,))
        except Exception:                                       # noqa: BLE001
            pass
    return res


def pending():
    review = [s["key"] for s in cfg()["states"] if s["review"]]
    if not review:
        return []
    try:
        import sqlite3
        with E.db() as c:
            c.row_factory = sqlite3.Row
            q = ",".join("?" * len(review))
            return [dict(r) for r in c.execute(f"SELECT * FROM lifecycle WHERE state IN ({q}) ORDER BY requested_at",
                                               review).fetchall()]
    except Exception:                                           # noqa: BLE001
        return []


def _sync_filters():
    base = ["All files", "Checked out by me", "Locked by others", "Changed this week"]
    P.FILTERS[:] = base + [s["label"] for s in cfg()["states"]] + ["Watching"]


def matches_filter(name, flt, states=None, watched=None):
    labels = {s["label"]: s["key"] for s in cfg()["states"]}
    if flt in labels:
        st = (states if states is not None else P.all_states()).get(name) or initial()
        return st == labels[flt]
    return ORIG["matches_filter"](name, flt, states, watched)


class _GuideView:
    """What the roll back feature asks: 'can this be changed?'"""
    IN_WORK = "__changeable__"

    def state_of(self, name):
        return {"state": self.IN_WORK if editable(name) else label(state_key(name))}

    def __getattr__(self, n):
        return getattr(P, n)


# =============================================================================
# Read-only open
# =============================================================================
def readonly_dir():
    return os.path.join(E.LOCAL_WORKSPACE, "_ReadOnly")


def _copy_ro(src, dst):
    if os.path.exists(dst):
        try:
            os.chmod(dst, stat.S_IWRITE | stat.S_IREAD)
        except OSError:
            pass
    copier = getattr(E, "vault_copy", None)
    (copier or shutil.copy2)(src, dst)
    os.chmod(dst, stat.S_IREAD)


def _deps(name, seen):
    info = E.INDEX.files.get(name)
    if not info or name in seen:
        return
    seen.add(name)
    try:
        refs = [str(d).lower() for d in (E.get_dependencies(info.path) or [])]
    except Exception:                                           # noqa: BLE001
        refs = []
    if not refs:
        return
    for n, i in list(E.INDEX.files.items()):
        if n not in seen and i.ext in E.CAD_MODEL_EXTS and any(n.lower() in r for r in refs):
            _deps(n, seen)


def make_readonly_copy(name):
    """Protected copies of the file (and the parts an assembly uses) in <working folder>\\_ReadOnly."""
    if name not in E.INDEX.files:
        raise ValueError(f"'{name}' isn't in the vault.")
    os.makedirs(readonly_dir(), exist_ok=True)
    files = set()
    _deps(name, files)
    for n in files:
        _copy_ro(E.INDEX.files[n].path, os.path.join(readonly_dir(), n))
    stl = os.path.splitext(E.INDEX.files[name].path)[0] + ".stl"
    if os.path.isfile(stl):
        try:
            _copy_ro(stl, os.path.join(readonly_dir(), os.path.splitext(name)[0] + ".stl"))
        except OSError:
            pass
    rev = (E.INDEX.records.get(name) or {}).get("revision") or ""
    E.log_audit(name, "Opened Read-Only", rev, f"{len(files)} file(s) copied to {readonly_dir()}")
    return os.path.join(readonly_dir(), name), len(files)


def open_readonly(name):
    path, n = make_readonly_copy(name)
    app_key = E.cad_app_of(name)
    exe = None
    try:
        exe = E.cad_launcher(app_key) if app_key else None
    except Exception:                                           # noqa: BLE001
        exe = None
    if exe:
        subprocess.Popen([exe, path], cwd=os.path.dirname(exe) or None)
    else:
        E.open_path(path)
    pol = sys.modules.get("atelier_polish")
    if pol is not None and app_key:
        try:
            pol.start_panel(app_key)
        except Exception:                                       # noqa: BLE001
            pass
    where = E.CAD_APPS.get(app_key, {}).get("label", "its program") if app_key else "its program"
    extra = f" with the {n - 1} part(s) it uses" if n > 1 else ""
    return "ok", (f"Opened a read-only copy of '{name}' in {where}{extra}. Changes can't be saved over it - check it "
                  "out to edit.")


def ask_open(app, name):
    """'check' / 'readonly' / 'notify' / None."""
    import tkinter as tk
    from tkinter import ttk
    S = app.S
    rec = E.INDEX.records.get(name) or {}
    lock = rec.get("locked_by") or ""
    can_edit = editable(name)
    if lock:
        sub = f"{lock} has it checked out. You can open a read-only copy to look at it."
    elif not can_edit:
        sub = f"It's {label(state_key(name)).lower()}, so it can't be checked out. You can open a read-only copy."
    else:
        sub = "Check it out to make changes, or open a read-only copy to look at it."
    dlg = E.ThemedDialog(app, "Open", f"Open {name}", sub, width=470)
    choice = {"v": None}

    def pick(v):
        choice["v"] = v
        dlg.destroy()
    ttk.Button(dlg.buttons, text="Cancel", command=lambda: pick(None)).pack(side="right")
    ttk.Button(dlg.buttons, text="👁  Open read-only", style="Accent.TButton" if (lock or not can_edit) else "TButton",
               command=lambda: pick("readonly")).pack(side="right", padx=(0, S(6)))
    if lock and lock != P.user():
        ttk.Button(dlg.buttons, text="🔔  Notify me when it's free", command=lambda: pick("notify")).pack(
            side="right", padx=(0, S(6)))
    if not lock and can_edit:
        ttk.Button(dlg.buttons, text="↓  Check out & open", style="Success.TButton", command=lambda: pick("check")).pack(
            side="right", padx=(0, S(6)))
    dlg.validate = lambda: None
    dlg.show()
    return choice["v"]


def _hook_open(cls):
    o_open = cls.action_open

    def action_open(self, *a, **k):
        f = self.selected
        info = E.INDEX.files.get(f or "")
        if not f or info is None or info.is_library:
            return o_open(self, *a, **k)
        key = E.file_state(f)[0]
        if key in ("mine", "new"):
            return o_open(self, *a, **k)
        if info.ext not in (E.CAD_MODEL_EXTS | set(getattr(E, "CAD_DRAWING_EXTS", ()))):
            return o_open(self, *a, **k)
        choice = ask_open(self, f)
        if choice == "readonly":
            try:
                res = open_readonly(f)
            except Exception as e:                              # noqa: BLE001
                res = ("error", f"Couldn't open a read-only copy: {e}")
            self.report(res, refresh=False)
        elif choice == "notify":
            P.watch(f)
            self.log(f"You'll be told when '{f}' is checked in.", "ok")
        elif choice == "check":
            res = E.check_out(f, "Checked out to open")
            if res[0] == "error":
                self.report(res, refresh=False)
                return None
            self.refresh_all()
            self.selected = f
            return o_open(self, *a, **k)
        return None
    cls.action_open = action_open


# =============================================================================
# Main window: State column, Properties, menus, approvals
# =============================================================================
def transition_items(app, f):
    """[(label, command, enabled)] - the actions for f from its current state."""
    out = []
    if not f or f not in E.INDEX.files or E.INDEX.files[f].is_library:
        return out
    for a in actions_from(state_key(f)):
        ok, why = can(f, a)
        lab = a["label"] + "…"
        if not ok and why in ("administrators only", "only the person who asked, or an administrator"):
            lab += f"   ({why})"
        out.append((lab, lambda a=a: ui_perform(app, f, a), ok))
    sd = state_def(state_key(f))
    if sd["release"]:
        lock = (E.INDEX.records.get(f) or {}).get("locked_by")
        out.append(("✎  Revise (minor)…", lambda: app.action_up_issue("minor"), not lock))
        out.append(("✎  Revise (major)…", lambda: app.action_up_issue("major"), not lock))
    return out


def ui_perform(app, f, a):
    def submit(v):
        n = v["n"].strip()
        if a.get("note") and not n:
            return "error", "Please add a note - it goes in the history."
        return n or " "
    to = state_def(a["to"])
    sub = f"{label(a['from'])}  ›  {to['label']}"
    if to["release"]:
        sub += "\nA release copy is kept, and the file is protected until it's revised."
    elif to["review"]:
        sub += "\nIt can't be changed while it waits."
    note = E.run_form(app, a["label"], f"{a['label']}: {f}", sub,
                      [("n", "Note" + ("" if a.get("note") else " (optional)"), False, "")], submit, a["label"],
                      ok_style="Success.TButton" if to["release"] else "Accent.TButton", width=480)
    if note is None:
        return
    res = perform(f, a, note.strip())
    P._report(app, res)
    P.update_approvals_button(app)


def extra_items(app, f):
    if not f or f not in E.INDEX.files:
        return []
    info = E.INDEX.files[f]
    rec = E.INDEX.records.get(f) or {}
    lock = rec.get("locked_by") or ""
    model = info.ext in E.CAD_MODEL_EXTS
    items = [None, ("👁  Open read-only", lambda: P._report(app, open_readonly(f), refresh=False),
                    info.ext in E.CAD_MODEL_EXTS | set(getattr(E, "CAD_DRAWING_EXTS", ()))),
             ("⧉  Where used…", lambda: P.show_structure(app, f, "used"), model),
             ("☷  Bill of materials…", lambda: P.show_structure(app, f, "bom"), model)]
    tr = transition_items(app, f)
    if tr:
        items += [None] + [("◆  " + l if not l.startswith("✎") else l, c, e) for l, c, e in tr]
    if lock and lock != P.user():
        if f in P.watching():
            items.append(("🔔  Stop notifying me", lambda: (P.unwatch(f), P._say(app, "Stopped watching.", "ok")), True))
        else:
            items.append(("🔔  Notify me when it's free", lambda: (P.watch(f), P._say(app, f"You'll be told when "
                                                                                            f"{lock} checks it in.", "ok")),
                          True))
    return items


def _state_menu(app, menu):
    menu.delete(0, "end")
    f = app.selected
    items = transition_items(app, f)
    if not items:
        menu.add_command(label="Nothing to do from this state" if f in E.INDEX.files else
                         "Check it in first - new files get a state when they're in the vault", state="disabled")
        return
    for lab, cmd, ok in items:
        menu.add_command(label=lab, command=cmd, state="normal" if ok else "disabled")
    if not any(ok for _l, _c, ok in items):
        lock = (E.INDEX.records.get(f) or {}).get("locked_by")
        if lock:
            menu.add_separator()
            menu.add_command(label="Check it in first", state="disabled")


def update_props(app):
    row = getattr(app, "_flow_row", None)
    if row is None:
        return
    lab, mb = row
    f = app.selected
    if not f or f not in E.INDEX.files or E.INDEX.files[f].is_library:
        lab.configure(text="—" if not f or f not in E.INDEX.files else "Library part", foreground=app.t["muted_fg"])
        mb.state(["disabled"])
        return
    sd = state_def(state_key(f))
    row_ = P.state_of(f)
    who = row_.get("decided_by") or row_.get("requested_by") or ""
    when = (row_.get("decided_at") or row_.get("requested_at") or "")[:10]
    lab.configure(text=f"{symbol(sd)}  {sd['label']}" + (f"   ·  {who} {when}" if who else ""),
                  foreground=colour(sd, app.t))
    mb.state(["!disabled"])


def show_approvals(app):
    import tkinter as tk
    from tkinter import ttk
    S = app.S
    dlg = E.ThemedDialog(app, "Approvals", "Waiting for a decision",
                         "Files in a state that waits for approval. Select one and choose what to do.", width=900,
                         resizable=True)
    wrap = ttk.Frame(dlg.body, style="Dialog.TFrame")
    wrap.pack(fill="both", expand=True)
    tv = ttk.Treeview(wrap, columns=("file", "state", "rev", "by", "when", "note"), show="headings", height=12,
                      selectmode="browse")
    for c, l, w in (("file", "File", 260), ("state", "State", 130), ("rev", "Rev", 50), ("by", "Asked by", 110),
                    ("when", "When", 120), ("note", "Note", 220)):
        tv.heading(c, text=l, anchor="w")
        tv.column(c, width=S(w), anchor="w", stretch=c in ("file", "note"))
    tv.pack(fill="both", expand=True)
    bar = ttk.Frame(dlg.body, style="Dialog.TFrame")
    bar.pack(fill="x", pady=(S(8), 0))

    def buttons():
        for w in bar.winfo_children():
            w.destroy()
        sel = tv.selection()
        f = sel[0] if sel else None
        if not f:
            ttk.Label(bar, text="Nothing waiting." if not tv.get_children() else "Select a file.",
                      style="CardMuted.TLabel").pack(side="left")
            return
        for lab, cmd, ok in transition_items(app, f):
            b = ttk.Button(bar, text=lab, command=lambda c=cmd: (c(), dlg.grab_set(), load()))
            b.pack(side="left", padx=(0, S(6)))
            if not ok:
                b.state(["disabled"])
        ttk.Button(bar, text="Show in list", command=lambda: (P._select_in_app(app, f), dlg.cancel())).pack(
            side="right")

    def load():
        keep = tv.selection()
        tv.delete(*tv.get_children())
        for r in pending():
            tv.insert("", "end", iid=r["filename"], values=(r["filename"], label(r["state"]), r.get("revision") or "",
                                                            r.get("requested_by") or "",
                                                            (r.get("requested_at") or "")[:16], r.get("note") or ""))
        kids = tv.get_children()
        if keep and tv.exists(keep[0]):
            tv.selection_set(keep[0])
        elif kids:
            tv.selection_set(kids[0])
        buttons()
        P.update_approvals_button(app)
    tv.bind("<<TreeviewSelect>>", lambda e: buttons())
    ttk.Button(dlg.buttons, text="Close", command=dlg.cancel).pack(side="right")
    load()
    dlg.geometry(f"{S(940)}x{min(S(560), app.root.winfo_screenheight() - 90)}")
    dlg.show()


def _hook_app(m):
    cls = getattr(m, "AtelierApp", None)
    if cls is None or getattr(cls, "_atl_flow", False):
        return
    cls._atl_flow = True
    cols = list(getattr(cls, "GRID_COLS", ()))
    if cols and not any(c[0] == "state" for c in cols):
        at = next((i + 1 for i, c in enumerate(cols) if c[0] == "status"), len(cols))
        cols.insert(at, ("state", "State", 130))
        cls.GRID_COLS = tuple(cols) if isinstance(getattr(cls, "GRID_COLS"), tuple) else cols
    o_row = cls._row_values

    def _row_values(self, name):
        r = o_row(self, name)
        try:
            in_vault = name in E.INDEX.files and not E.INDEX.files[name].is_library
            st = state_key(name) if in_vault else None
            r["state"] = badge(name) if in_vault else ""
            if st:
                r["status"] = str(r.get("status", "")).split("  ·  ")[0]
        except Exception:                                       # noqa: BLE001
            r.setdefault("state", "")
        return r
    cls._row_values = _row_values

    o_build = cls.build_ui

    def build_ui(self, *a, **k):
        res = o_build(self, *a, **k)
        install_error_handler(self.root)
        import tkinter as tk
        from tkinter import ttk
        try:
            host = self.p_pill.master
            row = ttk.Frame(host, style="CardInner.TFrame")
            row.pack(after=self.p_pill, anchor="w", fill="x", pady=(0, self.S(8)))
            ttk.Label(row, text="STATE", style="CardMuted.TLabel").pack(side="left")
            lab = ttk.Label(row, text="—", style="Card.TLabel")
            lab.pack(side="left", padx=(self.S(10), 0))
            mb = ttk.Menubutton(row, text="Change state ▾", style="TMenubutton")
            menu = tk.Menu(mb, tearoff=0, **(self.menu_colors() if hasattr(self, "menu_colors") else {}))
            menu.configure(postcommand=lambda: _state_menu(self, menu))
            mb["menu"] = menu
            mb.pack(side="right")
            self._flow_row = (lab, mb)
        except Exception:                                       # noqa: BLE001
            log_error("properties state row")
        return res
    cls.build_ui = build_ui

    o_sel = getattr(cls, "on_grid_select", None)
    if o_sel:
        def on_grid_select(self, *a, **k):
            res = o_sel(self, *a, **k)
            try:
                update_props(self)
            except Exception:                                   # noqa: BLE001
                pass
            return res
        cls.on_grid_select = on_grid_select
    o_grid = cls.refresh_grid

    def refresh_grid(self, *a, **k):
        res = o_grid(self, *a, **k)
        try:
            update_props(self)
        except Exception:                                       # noqa: BLE001
            pass
        return res
    cls.refresh_grid = refresh_grid
    _hook_open(cls)

    o_ident = getattr(cls, "update_identity", None)
    if o_ident:
        def update_identity(self, *a, **k):
            res = o_ident(self, *a, **k)
            try:
                if E.is_admin():
                    self.account_menu.add_command(label="◆  Release workflow…", command=lambda: workflow_dialog(self))
            except Exception:                                   # noqa: BLE001
                pass
            return res
        cls.update_identity = update_identity


# =============================================================================
# Release workflow editor
# =============================================================================
def build_editor(app, parent, owner, regrab=lambda: None):
    """The editor, built into parent (an Administration tab or its own dialog)."""
    import tkinter as tk
    from tkinter import ttk, messagebox
    S = app.S
    work = copy.deepcopy(cfg(refresh=True))
    top = ttk.Frame(parent, style="Dialog.TFrame")
    top.pack(fill="x")
    ttk.Label(top, text="How parts move from work to release. Applies to everyone using this vault.",
              style="CardMuted.TLabel").pack(side="left")
    pv = tk.StringVar(value="")
    pc = ttk.Combobox(top, textvariable=pv, values=list(PRESETS), state="readonly", width=26)
    pc.pack(side="right")
    ttk.Label(top, text="Start from  ", style="CardMuted.TLabel").pack(side="right")
    ttk.Label(parent, text="STATES  (the first is where new files start)", style="CardSection.TLabel").pack(
        anchor="w", pady=(S(10), S(2)))
    sw = ttk.Frame(parent, style="Dialog.TFrame")
    sw.pack(fill="x")
    stv = ttk.Treeview(sw, columns=("name", "files", "change", "copy", "wait", "del", "col"), show="headings",
                       height=5, selectmode="browse")
    for c, l, w in (("name", "State", 170), ("files", "Files", 50), ("change", "Can be changed", 110),
                    ("copy", "Keeps release copy", 120), ("wait", "Waits for approval", 125), ("del", "Can delete", 80),
                    ("col", "Colour", 70)):
        stv.heading(c, text=l, anchor="w")
        stv.column(c, width=S(w), anchor="w", stretch=c == "name")
    stv.pack(side="left", fill="x", expand=True)
    sb1 = ttk.Frame(parent, style="Dialog.TFrame")
    sb1.pack(fill="x", pady=(S(4), 0))
    ttk.Label(parent, text="ACTIONS", style="CardSection.TLabel").pack(anchor="w", pady=(S(12), S(2)))
    aw = ttk.Frame(parent, style="Dialog.TFrame")
    aw.pack(fill="both", expand=True)
    atv = ttk.Treeview(aw, columns=("label", "from", "to", "who", "note", "other"), show="headings", height=8,
                       selectmode="browse")
    for c, l, w in (("label", "Action", 170), ("from", "From", 140), ("to", "To", 140), ("who", "Who", 190),
                    ("note", "Note needed", 90), ("other", "Different person", 110)):
        atv.heading(c, text=l, anchor="w")
        atv.column(c, width=S(w), anchor="w", stretch=c == "label")
    atv.pack(side="left", fill="both", expand=True)
    sb2 = ttk.Frame(parent, style="Dialog.TFrame")
    sb2.pack(fill="x", pady=(S(4), 0))
    status = ttk.Label(parent, text="", style="CardAccent.TLabel", wraplength=S(820), justify="left")
    status.pack(anchor="w", pady=(S(8), 0))
    yes = lambda b: "✓" if b else ""

    def lab_of(key):
        return next((s["label"] for s in work["states"] if s["key"] == key), key)

    def load(sel_s=None, sel_a=None):
        used = used_states()
        stv.delete(*stv.get_children())
        for i, s in enumerate(work["states"]):
            stv.insert("", "end", iid=str(i), values=(("▶ " if i == 0 else "") + s["label"], used.get(s["key"], 0),
                                                      yes(s["editable"]), yes(s["release"]), yes(s["review"]),
                                                      yes(s["delete"]), s.get("colour", "grey")))
        atv.delete(*atv.get_children())
        for i, a in enumerate(work["actions"]):
            atv.insert("", "end", iid=str(i), values=(a["label"], lab_of(a["from"]), lab_of(a["to"]),
                                                      WHO.get(a["who"], a["who"]), yes(a["note"]), yes(a["other"])))
        if sel_s is not None and stv.exists(str(sel_s)):
            stv.selection_set(str(sel_s))
        if sel_a is not None and atv.exists(str(sel_a)):
            atv.selection_set(str(sel_a))
        problems = check(work)
        status.configure(text=("⚠ " + problems[0]) if problems else "")

    def state_form(s=None):
        new = s is None
        s = copy.deepcopy(s) if s else {"key": "", "label": "", "colour": "grey", "editable": False, "release": False,
                                        "review": False, "delete": False}
        d = E.ThemedDialog(app, "State", "New state" if new else f"Edit '{s['label']}'", "", width=440, parent=owner)
        nv = tk.StringVar(value=s["label"])
        ttk.Label(d.body, text="Name", style="CardMuted.TLabel").pack(anchor="w")
        ttk.Entry(d.body, textvariable=nv).pack(fill="x", pady=(S(2), S(8)), ipady=S(2))
        cv = tk.StringVar(value=s.get("colour", "grey"))
        ttk.Label(d.body, text="Colour", style="CardMuted.TLabel").pack(anchor="w")
        ttk.Combobox(d.body, textvariable=cv, values=list(COLOURS), state="readonly").pack(fill="x", pady=(S(2), S(8)))
        flags = {}
        for k, l in (("editable", "Files can be changed (checked out, renamed, rolled back)"),
                     ("release", "Entering this state keeps a release copy (Revise starts a new revision)"),
                     ("review", "Waits for approval (listed in Approvals)"),
                     ("delete", "Files can be deleted")):
            flags[k] = tk.BooleanVar(value=bool(s.get(k)))
            ttk.Checkbutton(d.body, text=l, variable=flags[k], style="Dialog.TCheckbutton").pack(anchor="w")
        err = ttk.Label(d.body, text="", style="CardDanger.TLabel")
        err.pack(anchor="w", pady=(S(6), 0))

        def ok():
            name = " ".join(nv.get().split())
            if not name:
                err.configure(text="Give the state a name.")
                return E.INVALID
            if any(x["label"].lower() == name.lower() and x is not orig for x in work["states"]):
                err.configure(text="There's already a state with that name.")
                return E.INVALID
            s.update(label=name, colour=cv.get(), **{k: v.get() for k, v in flags.items()})
            if new:
                s["key"] = name
            return s
        orig = None if new else next((x for x in work["states"] if x["key"] == s["key"]), None)
        d.validate = ok
        d.add_buttons("Save", "Accent.TButton")
        r = d.show()
        regrab()
        return r

    def action_form(a=None):
        new = a is None
        a = copy.deepcopy(a) if a else {"label": "", "from": work["states"][0]["key"],
                                        "to": work["states"][-1]["key"], "who": "admin", "note": False,
                                        "other": False}
        labels = [s["label"] for s in work["states"]]
        key_of = {s["label"]: s["key"] for s in work["states"]}
        d = E.ThemedDialog(app, "Action", "New action" if new else f"Edit '{a['label']}'", "", width=460, parent=owner)
        vals = {}
        for k, l, kind, choices, init in (("label", "Name (shown on the menu)", "e", None, a["label"]),
                                          ("from", "From state", "c", labels, lab_of(a["from"])),
                                          ("to", "To state", "c", labels, lab_of(a["to"])),
                                          ("who", "Who can do it", "c", list(WHO.values()), WHO[a["who"]])):
            ttk.Label(d.body, text=l, style="CardMuted.TLabel").pack(anchor="w")
            v = tk.StringVar(value=init)
            (ttk.Entry(d.body, textvariable=v) if kind == "e" else
             ttk.Combobox(d.body, textvariable=v, values=choices, state="readonly")).pack(fill="x", pady=(S(2), S(8)))
            vals[k] = v
        nb = tk.BooleanVar(value=a["note"])
        ob = tk.BooleanVar(value=a["other"])
        ttk.Checkbutton(d.body, text="A note is required", variable=nb, style="Dialog.TCheckbutton").pack(anchor="w")
        ttk.Checkbutton(d.body, text="Must be someone other than the person who asked", variable=ob,
                        style="Dialog.TCheckbutton").pack(anchor="w")
        err = ttk.Label(d.body, text="", style="CardDanger.TLabel")
        err.pack(anchor="w", pady=(S(6), 0))

        def ok():
            name = " ".join(vals["label"].get().split())
            if not name:
                err.configure(text="Give the action a name.")
                return E.INVALID
            if vals["from"].get() == vals["to"].get():
                err.configure(text="From and To must be different states.")
                return E.INVALID
            who = next(k for k, v in WHO.items() if v == vals["who"].get())
            a.update(label=name, **{"from": key_of[vals["from"].get()], "to": key_of[vals["to"].get()]}, who=who,
                     note=nb.get(), other=ob.get())
            return a
        d.validate = ok
        d.add_buttons("Save", "Accent.TButton")
        r = d.show()
        regrab()
        return r

    def sel(tv):
        s = tv.selection()
        return int(s[0]) if s else None

    def add_state():
        r = state_form()
        if r:
            work["states"].append(r)
            load(len(work["states"]) - 1)

    def edit_state():
        i = sel(stv)
        if i is not None:
            r = state_form(work["states"][i])
            if r:
                work["states"][i] = r
                load(i)

    def del_state():
        i = sel(stv)
        if i is None:
            return
        s = work["states"][i]
        n = used_states().get(s["key"], 0)
        if n:
            messagebox.showinfo(E.APP_NAME, f"{n} file(s) are {s['label']}. Move them to another state first.",
                                parent=owner)
            regrab()
            return
        if i == 0:
            messagebox.showinfo(E.APP_NAME, "The first state is where new files start, so it can't be removed. Move "
                                "another state to the top first.", parent=owner)
            regrab()
            return
        work["states"].pop(i)
        work["actions"] = [a for a in work["actions"] if s["key"] not in (a["from"], a["to"])]
        load()

    def move(d):
        i = sel(stv)
        if i is None or not 0 <= i + d < len(work["states"]):
            return
        work["states"][i], work["states"][i + d] = work["states"][i + d], work["states"][i]
        load(i + d)

    def add_action():
        r = action_form()
        if r:
            work["actions"].append(r)
            load(sel_a=len(work["actions"]) - 1)

    def edit_action():
        i = sel(atv)
        if i is not None:
            r = action_form(work["actions"][i])
            if r:
                work["actions"][i] = r
                load(sel_a=i)

    def del_action():
        i = sel(atv)
        if i is not None:
            work["actions"].pop(i)
            load()

    def preset(_e=None):
        p = PRESETS.get(pv.get())
        if not p:
            return
        used = used_states()
        missing = [k for k in used if k not in {s["key"] for s in p["states"]}]
        if missing:
            messagebox.showinfo(E.APP_NAME, f"Files are still '{missing[0]}', which this preset doesn't have. Move "
                                "them first.", parent=owner)
            regrab()
            pv.set("")
            return
        work.clear()
        work.update(copy.deepcopy(p))
        load()
        status.configure(text=f"'{pv.get()}' loaded - click Save workflow to use it.")

    def do_save():
        try:
            save(work)
        except (ValueError, PermissionError) as e:
            status.configure(text="⚠ " + str(e))
            return
        status.configure(text="✓ Saved. Everyone using the vault gets it within a few seconds.")
        try:
            app.refresh_all()
        except Exception:                                       # noqa: BLE001
            pass
    pc.bind("<<ComboboxSelected>>", preset)
    for bar, items in ((sb1, (("＋ State…", add_state), ("Edit…", edit_state), ("Remove", del_state),
                              ("▲", lambda: move(-1)), ("▼", lambda: move(1)))),
                       (sb2, (("＋ Action…", add_action), ("Edit…", edit_action), ("Remove", del_action)))):
        for text, cmd in items:
            ttk.Button(bar, text=text, command=cmd).pack(side="left", padx=(0, S(6)))
    ttk.Button(sb2, text="Save workflow", style="Success.TButton", command=do_save).pack(side="right")
    stv.bind("<Double-1>", lambda e: edit_state())
    atv.bind("<Double-1>", lambda e: edit_action())
    load()
    return do_save


def workflow_dialog(app):
    if not app.ensure_admin():
        return
    dlg = E.ThemedDialog(app, "Release workflow", "Release workflow", "", width=900, resizable=True)
    build_editor(app, dlg.body, dlg, regrab=lambda: dlg.grab_set())
    from tkinter import ttk
    ttk.Button(dlg.buttons, text="Close", command=dlg.cancel).pack(side="right")
    dlg.geometry(f"{app.S(940)}x{min(app.S(640), app.root.winfo_screenheight() - 90)}")
    dlg.show()


def _hook_admin():
    sec = sys.modules.get("atelier_secure")
    if sec is None or getattr(sec.admin_section, "_atl_flow", False):
        return
    orig = sec.admin_section

    def admin_section(app, parent, dlg, S, run, regrab):
        res = orig(app, parent, dlg, S, run, regrab)
        try:
            from tkinter import ttk
            w = parent
            while w is not None and w.winfo_class() != "TNotebook":
                w = w.master
            if w is not None:
                page = ttk.Frame(w, style="Dialog.TFrame", padding=S(14))
                w.add(page, text="  Release workflow  ")
                build_editor(app, page, dlg, regrab)
        except Exception:                                       # noqa: BLE001
            log_error("administration tab")
        return res
    admin_section._atl_flow = True
    sec.admin_section = admin_section


# =============================================================================
# Install
# =============================================================================
HELP_TIPS = [("Change state", "What can happen next to this part - request release, approve, make obsolete…"),
             ("Open read-only", "Opens a protected copy to look at. Check out to make changes.")]


def install(module):
    global E, P, G
    E = module
    if getattr(module, "_flow_installed", False):
        return
    module._flow_installed = True
    P = sys.modules.get("atelier_pro")
    G = sys.modules.get("atelier_guide")
    pol = sys.modules.get("atelier_polish")
    if pol is not None:
        pol.VERSION = VERSION
    if G is not None:
        G.VERSION = VERSION
    if P is None:
        return
    P._blocked = blocked
    P.pending = pending
    P.show_approvals = show_approvals
    ORIG["matches_filter"] = P.matches_filter
    P.matches_filter = matches_filter
    for n, fn in (("up_issue", up_issue), ("delete_component", delete_component)):
        if n in P.ORIG:
            setattr(module, n, fn)
    if G is not None:
        G.ORIG["extra"] = extra_items
        G.P = _GuideView()
    else:
        P.extra_menu_items = extra_items
    try:
        _sync_filters()
    except Exception:                                           # noqa: BLE001
        pass
    _hook_app(module)
    _hook_admin()
    try:
        import atelier_help_text as H
        have = {k for k, _ in H.TIPS}
        H.TIPS[:0] = [t for t in HELP_TIPS if t[0] not in have]
    except ImportError:
        pass
