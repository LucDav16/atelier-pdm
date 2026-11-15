"""
Atelier PDM - accounts, sign-in modes, working folder, export, dialog layout (atelier_access.py)  2.8.4
========================================================================================================

Accounts
    * There is always a built-in "admin" account. If it's missing it's recreated; it can't be deleted or lose
      administrator rights. If it has no password yet, an administrator is asked to set one.
    * Nobody is forced to change their password any more (new users, reset passwords, weak passwords).

Sign-in (Administration › Security › "How people sign in")
    * No sign-in - people are known by their Windows name.
    * Choose your name - pick your name from a list, no password.
    * Name and password.
    Whatever the choice, changing settings (Administration, workflow, numbering, backups…) always needs an
    administrator's password.

Working folder
    The default working folder is now  C:\\Design Files  (no program names). An existing C:\\SolidCraft_Work or
    C:\\Atelier_Work is renamed to it automatically the next time Atelier starts with nothing open in it.

Export STEP & PDF
    Works on several files at once (Ctrl- or Shift-click them in the list) and shows a summary window when it's
    done, with Open folder.

Dialog layout
    Button rows at the bottom of dialogs can no longer be squashed by the list above them (the blank coloured bars
    in Administration). This is applied to every dialog.
"""
import os
import secrets
import sys
import time
import traceback

VERSION = "2.8.4"
E = None
ORIG = {}
BUILTIN = "admin"
MODES = (("off", "No sign-in - use the Windows name"),
         ("name", "Choose your name from a list (no password)"),
         ("password", "Name and password"))
NEW_FOLDER = "Design Files"
OLD_FOLDERS = ("solidcraft_work", "atelier_work")


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
# Dialog layout repair
# =============================================================================
def repair_layout(widget, _depth=0):
    """Anything packed (top) after a part that stretches to fill the space is moved to the bottom, so the
    stretching part can't squash it. Returns the number of widgets moved."""
    moved = 0
    if _depth > 40:
        return 0
    try:
        slaves = widget.pack_slaves()
    except Exception:                                           # noqa: BLE001
        slaves = []
    exp = None
    late = []
    for s in slaves:
        try:
            info = s.pack_info()
        except Exception:                                       # noqa: BLE001
            continue
        side = str(info.get("side", "top"))
        grows = str(info.get("expand", "0")) in ("1", "true", "True") and str(info.get("fill", "")) in ("both", "y")
        if exp is None:
            if grows and side == "top":
                exp = s
            continue
        if side == "top" and not grows:
            late.append(s)
    for s in reversed(late):
        try:
            s.pack_configure(side="bottom", before=exp)
            moved += 1
        except Exception:                                       # noqa: BLE001
            pass
    try:
        kids = widget.winfo_children()
    except Exception:                                           # noqa: BLE001
        kids = []
    for k in kids:
        moved += repair_layout(k, _depth + 1)
    return moved


def _hook_dialogs(m):
    cls = getattr(m, "ThemedDialog", None)
    if cls is None or getattr(cls.show, "_atl_access", False):
        return
    orig = cls.show

    def show(self, *a, **k):
        try:
            repair_layout(self)
        except Exception:                                       # noqa: BLE001
            log_error("dialog layout")
        return orig(self, *a, **k)
    show._atl_access = True
    cls.show = show


# =============================================================================
# Accounts
# =============================================================================
def mode():
    try:
        if E.get_setting("require_login", "1") != "1":
            return "off"
        return E.get_setting("login_mode", "password") or "password"
    except Exception:                                           # noqa: BLE001
        return "password"


def set_mode(new):
    if new not in dict(MODES):
        return "error", "Choose how people sign in."
    guard = E._admin_guard()
    if guard:
        return guard
    E.set_setting("require_login", "0" if new == "off" else "1")
    if new != "off":
        E.set_setting("login_mode", new)
    E.log_audit("[settings]", "Sign-in Changed", "", dict(MODES)[new])
    return "ok", f"Sign-in: {dict(MODES)[new]}. Changing settings still needs an administrator's password."


def ensure_builtin_admin():
    """Recreates the built-in admin account if it's missing, keeps it an administrator, and stops anyone being
    forced to change their password. Returns True if the admin account has no password yet."""
    with E.db() as c:
        row = c.execute("SELECT username, password_hash, is_admin FROM users WHERE username=? COLLATE NOCASE",
                        (BUILTIN,)).fetchone()
        if row is None:
            c.execute("INSERT INTO users (username, password_hash, is_admin, must_change, created) VALUES (?,?,1,0,?)",
                      (BUILTIN, "", E.now_ts()))
            created = True
        else:
            created = False
            if not row[2]:
                c.execute("UPDATE users SET is_admin=1 WHERE username=?", (row[0],))
        c.execute("UPDATE users SET must_change=0 WHERE must_change<>0")
        h = c.execute("SELECT password_hash FROM users WHERE username=? COLLATE NOCASE", (BUILTIN,)).fetchone()[0]
    if created:
        E.log_audit("[users]", "Built-in Admin Restored", "", "the 'admin' account was missing and was recreated")
    return not h


def authenticate(username, password):
    acct, msg = ORIG["authenticate"](username, password)
    if acct:
        acct["must_change"] = False
        try:
            with E.db() as c:
                c.execute("UPDATE users SET must_change=0 WHERE username=?", (acct["username"],))
        except Exception:                                       # noqa: BLE001
            pass
    return acct, msg


def add_user(username, password, admin=False):
    if not password and mode() != "password":
        password = "Np-" + secrets.token_urlsafe(18)            # no password needed in this sign-in mode
        res = ORIG["add_user"](username, password, admin)
        note = " No password is needed with the current sign-in setting; reset it later if you switch to passwords."
    else:
        res = ORIG["add_user"](username, password, admin)
        note = ""
    if res and res[0] == "ok":
        with E.db() as c:
            c.execute("UPDATE users SET must_change=0 WHERE username=? COLLATE NOCASE", (username.strip(),))
        return "ok", f"Added '{username.strip()}'." + note
    return res


def admin_reset_password(username, password):
    res = ORIG["admin_reset_password"](username, password)
    if res and res[0] == "ok":
        with E.db() as c:
            c.execute("UPDATE users SET must_change=0 WHERE username=?", (username,))
        return "ok", f"Password set for '{username}'."
    return res


def delete_user(username):
    if (username or "").lower() == BUILTIN:
        return "error", "The built-in admin account can't be deleted - there must always be an administrator account."
    return ORIG["delete_user"](username)


def set_admin(username, flag):
    if (username or "").lower() == BUILTIN and not flag:
        return "error", "The built-in admin account is always an administrator."
    return ORIG["set_admin"](username, flag)


def init_db(*a, **k):
    res = ORIG["init_db"](*a, **k)
    try:
        ensure_builtin_admin()
    except Exception:                                           # noqa: BLE001
        log_error("built-in admin")
    return res


def pick_name(ctx, heading, cancel_text):
    """'Choose your name' sign-in: no password. Never gives administrator rights."""
    import tkinter as tk
    from tkinter import ttk
    S = ctx.S
    try:
        names = [u["username"] for u in E.list_users()]
    except Exception:                                           # noqa: BLE001
        names = []
    last = (getattr(ctx, "settings", {}) or {}).get("last_user", "")
    dlg = E.ThemedDialog(ctx, f"{E.APP_NAME} · {heading}", heading,
                         "Choose your name. Changing settings will ask for an administrator's password.", width=380)
    ttk.Label(dlg.body, text="Your name", style="CardMuted.TLabel").pack(anchor="w")
    v = tk.StringVar(value=last if last in names else (names[0] if names else ""))
    cb = ttk.Combobox(dlg.body, textvariable=v, values=names, state="readonly")
    cb.pack(fill="x", pady=(S(3), S(6)))
    err = ttk.Label(dlg.body, text="", style="CardDanger.TLabel")
    err.pack(anchor="w")

    def ok():
        n = v.get().strip()
        if not n:
            err.configure(text="Choose your name. An administrator can add it in Administration › Users.")
            return E.INVALID
        try:
            with E.db() as c:
                c.execute("UPDATE users SET last_login=? WHERE username=?", (E.now_ts(), n))
        except Exception:                                       # noqa: BLE001
            pass
        return {"username": n, "is_admin": False, "must_change": False}
    dlg.validate = ok
    dlg.add_buttons("Continue", "Accent.TButton", cancel_text)
    dlg.focus_widget = cb
    return dlg.show()


def run_login(ctx, heading="Sign in", sub=None, initial_user=None, ok_text="Sign in", cancel_text="Exit",
              parent=None):
    if heading in ("Sign in", "Switch user") and mode() == "name":
        return pick_name(ctx, heading, cancel_text)
    return ORIG["run_login"](ctx, heading, sub, initial_user, ok_text, cancel_text, parent)


def ask_admin_password(app):
    """Asked once (and at later starts until done) when the built-in admin account has no password."""
    def submit(v):
        if v["pw"] != v["c"]:
            return "error", "The passwords don't match."
        return E.set_password(BUILTIN, v["pw"])
    res = E.run_form(app, "Admin password", "Set the password for the built-in admin account",
                     "There must always be an administrator account. It's called 'admin' and is needed to change "
                     "settings. Keep the password somewhere safe.",
                     [("pw", "Password", True, ""), ("c", "Confirm password", True, "")], submit, "Set password",
                     "Later", width=420)
    if res:
        app.log("Password set for the built-in admin account.", "ok")


# =============================================================================
# Working folder
# =============================================================================
def _install_json():
    s = sys.modules.get("atelier_setup")
    return s


def migrate_working_folder():
    """Renames C:\\SolidCraft_Work / C:\\Atelier_Work to C:\\Design Files. Returns the new path or None."""
    if (getattr(E, "CONFIG", {}) or {}).get("sandbox_dir"):
        return None
    old = getattr(E, "LOCAL_WORKSPACE", "")
    if not old or os.path.basename(os.path.normpath(old)).lower() not in OLD_FOLDERS:
        return None
    new = os.path.join(os.path.dirname(os.path.normpath(old)), NEW_FOLDER)
    if os.path.exists(new):
        if os.path.isdir(new) and not os.listdir(new) and os.path.isdir(old):
            os.rmdir(new)
        elif not os.path.isdir(old):
            _point_to(new)
            return new
        else:
            return None
    if os.path.isdir(old):
        try:
            os.rename(old, new)
        except OSError:
            return None                                         # something is open in it: try next time
    else:
        os.makedirs(new, exist_ok=True)
    _point_to(new)
    try:
        E.log_audit("[settings]", "Working Folder Renamed", "", f"{old} -> {new}")
    except Exception:                                           # noqa: BLE001
        pass
    return new


def _point_to(new):
    E.LOCAL_WORKSPACE = new
    E.LOCAL_LIB_DIR = os.path.join(new, "_Library")
    E.DISCARD_DIR = os.path.join(new, "_Discarded")
    E.SETTINGS_PATH = os.path.join(new, "atelier_settings.json")
    os.environ["ATELIER_SANDBOX"] = new
    sec = sys.modules.get("atelier_secure")
    if sec is not None:
        try:
            sec._relocate_output()
        except Exception:                                       # noqa: BLE001
            pass
    st = _install_json()
    if st is not None:
        try:
            cfg = st.load_install()
            if cfg and cfg.get("sandbox") != new:
                cfg["sandbox"] = new
                st.save_install(cfg)
        except Exception:                                       # noqa: BLE001
            pass


def default_sandbox():
    return os.path.join("C:\\", NEW_FOLDER) if sys.platform == "win32" else \
        os.path.join(os.path.expanduser("~"), NEW_FOLDER)


# =============================================================================
# Export STEP & PDF
# =============================================================================
def selected_models(app):
    out = []
    try:
        for iid in app.grid.selection():
            f = (app.row_map.get(iid) or (None,))[0]
            info = E.INDEX.files.get(f or "")
            if info is not None and info.ext in E.CAD_MODEL_EXTS and f not in out:
                out.append(f)
    except Exception:                                           # noqa: BLE001
        pass
    if not out and app.selected and app.selected in E.INDEX.files and \
            E.INDEX.files[app.selected].ext in E.CAD_MODEL_EXTS:
        out = [app.selected]
    return out


def export_many(files, progress=None):
    """[(file, [(level, msg)])]."""
    out = []
    for i, f in enumerate(files):
        if progress:
            progress(i, len(files), f)
        try:
            res = E.export_deliverables(f)
        except Exception as e:                                  # noqa: BLE001
            res = [("error", f"Export failed: {e}")]
        out.append((f, res))
    return out


def action_export(app):
    import tkinter as tk
    from tkinter import ttk
    files = selected_models(app)
    if not files:
        app.log("Select one or more parts or assemblies to export (Ctrl- or Shift-click for several).", "warn")
        return
    app.root.config(cursor="watch")

    def prog(i, n, f):
        app.log(f"Exporting {i + 1} of {n}: {f}…")
        app.root.update_idletasks()
    try:
        results = export_many(files, prog)
    finally:
        app.root.config(cursor="")
    for f, res in results:
        for lvl, msg in res:
            app.log(f"{f}: {msg}", lvl)
    S = app.S
    good = sum(1 for _f, res in results if any(l == "ok" for l, _m in res))
    bad = [f for f, res in results if not any(l == "ok" for l, _m in res)]
    out_dir = E.OUTPUT_DIR
    head = (f"Exported {good} of {len(files)} file{'s' if len(files) != 1 else ''}" if good else "Nothing was exported")
    dlg = E.ThemedDialog(app, "Export STEP & PDF", head, f"Saved to {out_dir}", width=640, resizable=True)
    wrap = ttk.Frame(dlg.body, style="Dialog.TFrame")
    wrap.pack(fill="both", expand=True)
    tv = ttk.Treeview(wrap, columns=("res",), show="tree headings", height=min(12, 2 + 3 * len(files)))
    tv.heading("#0", text="File", anchor="w")
    tv.heading("res", text="Result", anchor="w")
    tv.column("#0", width=S(240), anchor="w")
    tv.column("res", width=S(360), anchor="w", stretch=True)
    t = app.t
    for lvl, col in (("ok", t.get("success")), ("warn", t.get("warning")), ("error", t.get("danger"))):
        tv.tag_configure(lvl, foreground=col)
    for f, res in results:
        top = "error" if not any(l == "ok" for l, _m in res) else ("warn" if any(l == "warn" for l, _m in res)
                                                                    else "ok")
        pid = tv.insert("", "end", text=("✔  " if top == "ok" else "⚠  " if top == "warn" else "✖  ") + f,
                        values=("",), open=top != "ok", tags=(top,))
        for lvl, msg in res:
            tv.insert(pid, "end", text="", values=(msg,), tags=(lvl,))
    sb = ttk.Scrollbar(wrap, orient="vertical", command=tv.yview)
    tv.configure(yscrollcommand=sb.set)
    sb.pack(side="right", fill="y")
    tv.pack(side="left", fill="both", expand=True)
    if bad:
        ttk.Label(dlg.body, text="Files marked ✖ weren't exported - open them to see why.",
                  style="CardMuted.TLabel").pack(anchor="w", pady=(S(6), 0))

    def open_folder():
        try:
            os.makedirs(out_dir, exist_ok=True)
            E.open_path(out_dir)
        finally:
            dlg.destroy()
    ttk.Button(dlg.buttons, text="Close", command=dlg.cancel).pack(side="right")
    ob = ttk.Button(dlg.buttons, text="📂  Open folder", style="Accent.TButton", command=open_folder)
    ob.pack(side="right", padx=(0, S(8)))
    if not good:
        ob.state(["disabled"])
    dlg.validate = lambda: None
    dlg.focus_widget = ob
    dlg.show()
    return results


# =============================================================================
# Main window and Administration
# =============================================================================
def _hook_app(m):
    cls = getattr(m, "AtelierApp", None)
    if cls is None or getattr(cls, "_atl_access", False):
        return
    cls._atl_access = True
    o_build = cls.build_ui

    def build_ui(self, *a, **k):
        res = o_build(self, *a, **k)
        try:
            self.grid.configure(selectmode="extended")          # Ctrl / Shift-click several files
        except Exception:                                       # noqa: BLE001
            pass

        def check_admin():
            try:
                if ensure_builtin_admin() and E.is_admin():
                    ask_admin_password(self)
            except Exception:                                   # noqa: BLE001
                log_error("admin password check")
        self.root.after(1800, check_admin)
        return res
    cls.build_ui = build_ui
    cls.action_export = lambda self: action_export(self)


def _security_section(app, parent, dlg, S, run, regrab):
    import tkinter as tk
    from tkinter import ttk
    for w in list(parent.winfo_children()):                     # replace the old "Require sign-in" tick box
        try:
            txt = str(w.cget("text"))
        except Exception:                                       # noqa: BLE001
            continue
        if txt.startswith("Require sign-in") or txt.startswith("When this is off, people are identified"):
            w.pack_forget()
    box = ttk.Frame(parent, style="Dialog.TFrame")
    kids = parent.pack_slaves()
    box.pack(fill="x", anchor="w", before=kids[0] if kids else None, pady=(0, S(16)))
    ttk.Label(box, text="How people sign in", style="CardSection.TLabel").pack(anchor="w")
    v = tk.StringVar(value=mode())
    for key, text in MODES:
        ttk.Radiobutton(box, text=text, value=key, variable=v, style="Dialog.TCheckbutton",
                        command=lambda: run(set_mode(v.get()), lambda: (v.set(mode()), app.update_identity()))
                        ).pack(anchor="w")
    ttk.Label(box, text="Changing settings - Administration, the release workflow, numbering, backups - always needs "
                        "an administrator's password, whichever you choose. The built-in 'admin' account always "
                        "exists and can't be deleted.", style="CardMuted.TLabel", wraplength=S(820),
              justify="left").pack(anchor="w", pady=(S(4), 0))


def _hook_admin():
    sec = sys.modules.get("atelier_secure")
    if sec is None or getattr(sec.admin_section, "_atl_access", False):
        return
    orig = sec.admin_section

    def admin_section(app, parent, dlg, S, run, regrab):
        try:
            _security_section(app, parent, dlg, S, run, regrab)
        except Exception:                                       # noqa: BLE001
            log_error("security section")
        return orig(app, parent, dlg, S, run, regrab)
    admin_section._atl_access = True
    sec.admin_section = admin_section


# =============================================================================
# Install
# =============================================================================
def install(module):
    global E
    E = module
    if getattr(module, "_access_installed", False):
        return
    module._access_installed = True
    for mod_name in ("atelier_polish", "atelier_guide", "atelier_flow"):
        mm = sys.modules.get(mod_name)
        if mm is not None:
            mm.VERSION = VERSION
    try:
        migrate_working_folder()
    except Exception:                                           # noqa: BLE001
        log_error("working folder")
    st = sys.modules.get("atelier_setup")
    if st is not None:
        st.default_sandbox = default_sandbox
    for n, fn in (("authenticate", authenticate), ("add_user", add_user),
                  ("admin_reset_password", admin_reset_password), ("delete_user", delete_user),
                  ("set_admin", set_admin), ("init_db", init_db), ("run_login", run_login)):
        if hasattr(module, n) and n not in ORIG:
            ORIG[n] = getattr(module, n)
            setattr(module, n, fn)
    _hook_dialogs(module)
    _hook_app(module)
    _hook_admin()
    try:
        update_help()
    except Exception:                                           # noqa: BLE001
        log_error("help text")


REPLACE = [
    ("C:\\Atelier_Work", "C:\\Design Files"),
    ("Add users (temporary password changed at first sign-in), reset passwords, make or remove administrators.",
     "Add users, set passwords, make or remove administrators. The built-in 'admin' account always exists."),
    ("Require sign-in; vault encryption.",
     "How people sign in (no sign-in, choose your name, or name and password); vault encryption."),
]
SIGN_IN = [
    ("h", "Signing in and the admin account"),
    ("p", "Administration › Security › **How people sign in**:"),
    ("b", "**No sign-in** - people are known by their Windows name."),
    ("b", "**Choose your name from a list** - no password. Add people in Administration › Users (the password can "
          "be left blank)."),
    ("b", "**Name and password**."),
    ("p", "Whichever you choose, changing settings - Administration, the release workflow, numbering, backups - "
          "always asks for an administrator's password. Nobody is made to change their password."),
    ("p", "There is always a built-in **admin** account. It can't be deleted or lose administrator rights; if it's "
          "ever missing it's recreated, and an administrator is asked to set its password."),
]
EXPORT = [("p", "Select several parts (Ctrl- or Shift-click) to export them all at once. When it's finished a window "
                "lists what was made for each file, with **Open folder**.")]


def update_help():
    import atelier_help_text as H
    if getattr(H, "_atl_access", False):
        return
    H._atl_access = True
    for _title, blocks in H.CHAPTERS:
        for i, (kind, val) in enumerate(blocks):
            if kind == "t":
                blocks[i] = (kind, [[_fix(c) for c in row] for row in val])
            else:
                blocks[i] = (kind, _fix(val))
    for title, blocks in H.CHAPTERS:
        if title == "Administration and security" and not any(v == SIGN_IN[0][1] for k, v in blocks if k == "h"):
            at = next((i for i, (k, v) in enumerate(blocks) if k == "h"), len(blocks))
            blocks[at:at] = SIGN_IN
        if title == "Working with files":
            at = next((i for i, (k, v) in enumerate(blocks) if k == "h" and v.startswith("Export STEP")), None)
            if at is not None:
                end = next((i for i in range(at + 1, len(blocks)) if blocks[i][0] == "h"), len(blocks))
                blocks[end:end] = EXPORT
    H.TIPS[:0] = [("Export STEP", "Makes a STEP and a PDF for each selected part (Ctrl- or Shift-click for several) in "
                                  "Manufacturing_Output, then shows what was made.")]


def _fix(text):
    for a, b in REPLACE:
        text = text.replace(a, b)
    return text
