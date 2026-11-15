"""
Atelier PDM - first-run setup wizard (atelier_setup.py)
=======================================================

Runs the first time the installed program starts (or with --setup). It asks where the vault lives -
this PC, a cloud drive, or a network server / NAS - creates a new vault or joins an existing one, sets up
the first administrator, and optionally turns on vault encryption. The answers are saved per Windows
account in  %LOCALAPPDATA%\\Atelier PDM\\install.json.
"""
import json
import os
import re
import string
import sys
import time

APP_NAME = "Atelier PDM"
APP_DATA = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "Atelier PDM")
INSTALL_JSON = os.path.join(APP_DATA, "install.json")
USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{1,31}$")
WEAK = {"password", "admin1", "123456", "atelier", "qwerty"}
DB_NAMES = ("atelier_index.db", "atellier_index.db")
MODES = {
    "local": ("This PC only", "Everything stays on this computer. Simplest; nothing to share."),
    "cloud": ("Cloud drive", "Google Drive, OneDrive or Dropbox. Use the same vault on several PCs - "
                             "one person at a time works best."),
    "server": ("Network server or NAS", "A shared folder on a server, NAS or Raspberry Pi "
                                        "(e.g. \\\\raspberrypi\\atelier). Best for a small team."),
}


# =============================================================================
# Saved install settings
# =============================================================================
def load_install():
    try:
        with open(INSTALL_JSON, encoding="utf-8") as f:
            cfg = json.load(f)
        return cfg if cfg.get("vault") else None
    except (OSError, ValueError):
        return None


def save_install(cfg):
    os.makedirs(APP_DATA, exist_ok=True)
    tmp = INSTALL_JSON + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, INSTALL_JSON)


# =============================================================================
# Pure helpers (tested without a screen)
# =============================================================================
def has_vault(folder):
    return any(os.path.isfile(os.path.join(folder, n)) for n in DB_NAMES)


def is_encrypted_vault(folder):
    return os.path.isfile(os.path.join(folder, "atelier_vault.key"))


def writable(folder):
    """None if we can create files in folder (creating it if needed), else a reason."""
    try:
        os.makedirs(folder, exist_ok=True)
        probe = os.path.join(folder, f".atelier_write_test_{os.getpid()}")
        with open(probe, "w") as f:
            f.write("ok")
        os.remove(probe)
        return None
    except OSError as e:
        return f"Can't write to that folder ({e.strerror or e})."


def cloud_roots():
    """Likely cloud-drive folders on this PC, best first."""
    out = []
    for letter in string.ascii_uppercase[3:]:                     # Google Drive for desktop: G:\My Drive
        for sub in ("My Drive", "Mon Drive", "Meine Ablage"):
            p = f"{letter}:\\{sub}"
            if sys.platform == "win32" and os.path.isdir(p):
                out.append(("Google Drive", p))
    home = os.path.expanduser("~")
    for label, p in (("OneDrive", os.environ.get("OneDrive")), ("OneDrive", os.environ.get("OneDriveConsumer")),
                     ("OneDrive (work)", os.environ.get("OneDriveCommercial")),
                     ("Google Drive", os.path.join(home, "Google Drive")),
                     ("Google Drive", os.path.join(home, "My Drive")),
                     ("Dropbox", os.path.join(home, "Dropbox"))):
        if p and os.path.isdir(p) and all(p != q for _, q in out):
            out.append((label, p))
    return out


def default_vault(mode):
    if mode == "cloud":
        roots = cloud_roots()
        return os.path.join(roots[0][1], "Atelier Vault") if roots else ""
    if mode == "server":
        return ""
    docs = os.path.join(os.path.expanduser("~"), "Documents")
    return os.path.join(docs if os.path.isdir(docs) else os.path.expanduser("~"), "Atelier Vault")


def default_sandbox():
    return r"C:\Design Files" if sys.platform == "win32" else os.path.join(os.path.expanduser("~"), "Design Files")


def check_vault_choice(action, folder):
    """(error or None, adjusted action, note). action is 'new' or 'existing'."""
    folder = (folder or "").strip().strip('"')
    if not folder:
        return "Choose a folder for the vault.", action, ""
    if action == "existing":
        if not os.path.isdir(folder):
            return "That folder can't be found. Is the drive or server connected?", action, ""
        if not has_vault(folder):
            return "There's no Atelier vault in that folder (no atelier_index.db).", action, ""
        problem = writable(folder)
        return problem, action, ("This vault is encrypted - you'll be asked for its passphrase once."
                                 if is_encrypted_vault(folder) else "")
    if os.path.isdir(folder) and has_vault(folder):
        return None, "existing", "That folder already holds a vault, so it will be used as it is."
    return writable(folder), action, ""


def check_account(user, pw, pw2):
    user = (user or "").strip()
    if not USERNAME_RE.match(user):
        return "Usernames are 2-32 characters: letters, numbers, dot, dash or underscore."
    if len(pw) < 6:
        return "Passwords must be at least 6 characters."
    if pw.lower() in WEAK or pw.lower() == user.lower():
        return "That password is too easy to guess."
    if pw != pw2:
        return "The passwords don't match."
    return None


def check_passphrase(p1, p2, minimum=10):
    if len(p1) < minimum:
        return f"Use at least {minimum} characters - a short sentence works well."
    if p1 != p2:
        return "The passphrases don't match."
    return None


def create_vault(state, engine=None, secure=None):
    """Sets up a new vault: database, first administrator, optional encryption. Returns the install config.
    engine/secure are the atelier_pdm / atelier_secure modules (imported here after the paths are set)."""
    vault, sandbox = state["vault"], state["sandbox"]
    os.makedirs(vault, exist_ok=True)
    os.makedirs(sandbox, exist_ok=True)
    os.environ["ATELIER_VAULT"], os.environ["ATELIER_SANDBOX"] = vault, sandbox
    if engine is None:
        import atelier_pdm as engine
    if secure is None:
        import atelier_secure as secure
    secure.install(engine)
    engine.init_db()
    with engine.db() as c:
        if c.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1:
            c.execute("UPDATE users SET username=?, password_hash=?, must_change=0 WHERE username='admin'",
                      (state["user"], engine.hash_password(state["pw"])))
    engine.USER_NAME = state["user"]
    s = engine.load_settings()
    s["last_user"] = state["user"]
    engine.save_settings(s)
    engine.log_audit("[setup]", "Vault Created", "", f"{MODES[state['mode']][0]} by {state['user']}")
    if state.get("encrypt"):
        secure.enable(state["passphrase"], state["recovery"], by=state["user"])
        engine.log_audit("[settings]", "Encryption On", "", "at setup")
    return make_config(state)


def make_config(state):
    return {"vault": state["vault"], "sandbox": state["sandbox"], "mode": state["mode"],
            "set_up": time.strftime("%Y-%m-%d %H:%M:%S"), "version": 1}


# =============================================================================
# The wizard window
# =============================================================================
C = {"bg": "#0b1120", "panel": "#121b2d", "card": "#152035", "card_sel": "#173a5e", "border": "#1f2b42",
     "fg": "#e6ebf2", "muted": "#8a97ad", "accent": "#38bdf8", "danger": "#f87171", "ok": "#34d399",
     "entry": "#0b1120", "btn": "#1d2840"}


def run_wizard(existing=None):
    """Shows the wizard. Returns (config, open_now) or None if cancelled."""
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox

    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        pass
    root = tk.Tk()
    root.title(f"{APP_NAME} setup")
    root.configure(bg=C["bg"])
    scale = max(1.0, root.winfo_fpixels("1i") / 96.0)
    S = lambda n: int(round(n * scale))
    root.geometry(f"{S(640)}x{S(560)}")
    root.minsize(S(600), S(520))
    F = "Segoe UI"

    st = ttk.Style(root)
    st.theme_use("clam")
    st.configure(".", background=C["panel"], foreground=C["fg"], font=(F, 10), fieldbackground=C["entry"],
                 bordercolor=C["border"], lightcolor=C["border"], darkcolor=C["border"], focuscolor=C["accent"])
    st.configure("TFrame", background=C["panel"])
    st.configure("Bg.TFrame", background=C["bg"])
    st.configure("TLabel", background=C["panel"], foreground=C["fg"])
    st.configure("H.TLabel", font=("Segoe UI Semibold", 15))
    st.configure("Sub.TLabel", foreground=C["muted"], font=(F, 9))
    st.configure("Err.TLabel", foreground=C["danger"], font=(F, 9))
    st.configure("Note.TLabel", foreground=C["accent"], font=(F, 9))
    st.configure("Step.TLabel", background=C["bg"], foreground=C["muted"], font=(F, 9))
    st.configure("TEntry", foreground=C["fg"], insertcolor=C["fg"], padding=(S(8), S(5)))
    st.map("TEntry", bordercolor=[("focus", C["accent"])], fieldbackground=[("disabled", C["panel"])],
           foreground=[("disabled", C["muted"])])
    st.configure("TButton", background=C["btn"], foreground=C["fg"], borderwidth=0, padding=(S(16), S(7)))
    st.map("TButton", background=[("active", "#26334f"), ("disabled", C["panel"])],
           foreground=[("disabled", C["muted"])])
    st.configure("Accent.TButton", background=C["accent"], foreground="#0b0f14", font=("Segoe UI Semibold", 10))
    st.map("Accent.TButton", background=[("active", "#62ccfa"), ("disabled", C["btn"])])
    for name, bg in (("TRadiobutton", C["panel"]), ("TCheckbutton", C["panel"])):
        st.configure(name, background=bg, foreground=C["fg"], indicatorbackground=C["entry"],
                     indicatorforeground=C["accent"], padding=(0, S(3)))
        st.map(name, background=[("active", bg)], indicatorbackground=[("selected", C["accent"])])

    state = {"mode": (existing or {}).get("mode", "cloud"), "action": "existing" if existing else "new",
             "vault": (existing or {}).get("vault", ""), "sandbox": (existing or {}).get("sandbox") or default_sandbox(),
             "user": "", "pw": "", "encrypt": False, "passphrase": "", "recovery": "", "open": True}
    try:
        import atelier_secure as _sec
        crypto_ok = _sec.crypto_available()
        new_code = _sec.new_recovery_code
    except ImportError:
        crypto_ok, new_code = False, None
    result = {}

    # ---- frame
    head = ttk.Frame(root, style="Bg.TFrame", padding=(S(24), S(18), S(24), S(6)))
    head.pack(fill="x")
    logo = tk.Canvas(head, width=S(30), height=S(30), bg=C["bg"], highlightthickness=0)
    logo.pack(side="left")
    s30, h = S(30), S(15)
    logo.create_polygon(h, 1, s30 - 2, s30 * .27, h, s30 * .54, 2, s30 * .27, fill="#9fdcfb", outline="")
    logo.create_polygon(2, s30 * .27, h, s30 * .54, h, s30 - 1, 2, s30 * .73, fill=C["accent"], outline="")
    logo.create_polygon(h, s30 * .54, s30 - 2, s30 * .27, s30 - 2, s30 * .73, h, s30 - 1, fill="#2183ad", outline="")
    tk.Label(head, text=f"  {APP_NAME} setup", bg=C["bg"], fg=C["fg"], font=("Segoe UI Semibold", 13)).pack(side="left")
    step_lbl = ttk.Label(head, text="", style="Step.TLabel")
    step_lbl.pack(side="right")
    card = ttk.Frame(root, padding=S(24))
    card.pack(fill="both", expand=True, padx=S(16), pady=(S(6), 0))
    foot = ttk.Frame(root, style="Bg.TFrame", padding=(S(16), S(12)))
    foot.pack(fill="x")
    err = ttk.Label(card, text="", style="Err.TLabel", wraplength=S(560), justify="left")
    b_next = ttk.Button(foot, text="Next", style="Accent.TButton")
    b_back = ttk.Button(foot, text="Back")
    b_cancel = ttk.Button(foot, text="Cancel")
    b_next.pack(side="right")
    b_back.pack(side="right", padx=(0, S(8)))
    b_cancel.pack(side="left")
    body = {"frame": None}

    def new_page(title, sub=None):
        if body["frame"] is not None:
            body["frame"].destroy()
        f = ttk.Frame(card)
        f.pack(fill="both", expand=True)
        body["frame"] = f
        ttk.Label(f, text=title, style="H.TLabel").pack(anchor="w")
        if sub:
            ttk.Label(f, text=sub, style="Sub.TLabel", wraplength=S(560), justify="left").pack(anchor="w",
                                                                                             pady=(S(4), S(14)))
        err.configure(text="")
        err.pack_forget()
        return f

    def show_error(msg):
        err.configure(text=msg or "")
        if msg:
            err.pack(in_=body["frame"], anchor="w", side="bottom", pady=(S(8), 0))
        return not msg

    def entry(parent, var, show=None, width=None):
        e = ttk.Entry(parent, textvariable=var, show=show or "", width=width or 52)
        e.pack(anchor="w", fill="x", pady=(S(3), S(10)), ipady=S(1))
        return e

    def label(parent, text, style="TLabel"):
        ttk.Label(parent, text=text, style=style, wraplength=S(560), justify="left").pack(anchor="w")

    # ---- pages: each returns a validate() callable
    def p_welcome():
        f = new_page(f"Welcome to {APP_NAME}",
                     "This sets Atelier PDM up on this PC: where your vault lives, the first administrator and, "
                     "for cloud and server vaults, encryption. It takes about a minute.")
        if existing:
            label(f, f"Current setup:  {MODES.get(existing.get('mode'), ('?',))[0]}  ·  {existing.get('vault')}",
                  "Note.TLabel")
            label(f, "Running setup again only changes how this PC connects. Your vault isn't changed.", "Sub.TLabel")
        return lambda: True

    def p_where():
        f = new_page("Where should the vault live?",
                     "The vault holds your parts, drawings, history and the part-number database.")
        var = tk.StringVar(value=state["mode"])
        for key, (title, desc) in MODES.items():
            ttk.Radiobutton(f, text=title, value=key, variable=var).pack(anchor="w", pady=(S(6), 0))
            ttk.Label(f, text=desc, style="Sub.TLabel", wraplength=S(520), justify="left").pack(anchor="w",
                                                                                             padx=(S(26), 0))

        def ok():
            if var.get() != state["mode"] or "encrypt_default" not in state:
                if var.get() != state["mode"] and not existing:
                    state["vault"] = ""
                state["encrypt_default"] = True
                state["encrypt"] = var.get() != "local" and crypto_ok      # suggested; can be changed later
            state["mode"] = var.get()
            return True
        return ok

    def p_folder():
        mode = state["mode"]
        f = new_page("Vault folder", {"local": "Pick a folder on this PC.",
                                       "cloud": "Pick a folder inside your synced cloud drive.",
                                       "server": "Type or browse to the shared folder, e.g. \\\\raspberrypi\\atelier "
                                                 "or a mapped drive such as Z:\\Atelier."}[mode])
        act = tk.StringVar(value=state["action"])
        row = ttk.Frame(f)
        row.pack(anchor="w", pady=(0, S(8)))
        ttk.Radiobutton(row, text="Start a new vault", value="new", variable=act).pack(side="left")
        ttk.Radiobutton(row, text="Use an existing vault", value="existing", variable=act).pack(side="left",
                                                                                              padx=(S(18), 0))
        path = tk.StringVar(value=state["vault"] or default_vault(mode))
        prow = ttk.Frame(f)
        prow.pack(fill="x")
        e = ttk.Entry(prow, textvariable=path)
        e.pack(side="left", fill="x", expand=True, ipady=S(1))

        def browse():
            p = filedialog.askdirectory(parent=root, title="Choose the vault folder",
                                        initialdir=path.get() if os.path.isdir(path.get()) else None)
            if p:
                path.set(os.path.normpath(p))
        ttk.Button(prow, text="Browse…", command=browse).pack(side="left", padx=(S(8), 0))
        if mode == "cloud":
            roots = cloud_roots()
            if roots:
                ttk.Label(f, text="Found on this PC:", style="Sub.TLabel").pack(anchor="w", pady=(S(12), S(2)))
                for lab, rp in roots[:4]:
                    lk = tk.Label(f, text=f"{lab}:  {rp}", bg=C["panel"], fg=C["accent"], cursor="hand2",
                                  font=(F, 9, "underline"))
                    lk.pack(anchor="w")
                    lk.bind("<Button-1>", lambda ev, rp=rp: path.set(os.path.join(rp, "Atelier Vault")))
            else:
                ttk.Label(f, text="No Google Drive, OneDrive or Dropbox folder was found. Install the drive's "
                                  "desktop app, or browse to its folder.", style="Sub.TLabel",
                          wraplength=S(560)).pack(anchor="w", pady=(S(12), 0))
            ttk.Label(f, text="Tip: on a cloud drive, have only one person working in the vault at a time - "
                              "two PCs editing at once can conflict.", style="Sub.TLabel",
                      wraplength=S(560), justify="left").pack(anchor="w", pady=(S(12), 0))
        note = ttk.Label(f, text="", style="Note.TLabel", wraplength=S(560), justify="left")
        note.pack(anchor="w", pady=(S(10), 0))

        def ok():
            problem, action, msg = check_vault_choice(act.get(), path.get())
            if problem:
                return show_error(problem)
            state["vault"] = os.path.normpath(path.get().strip().strip('"'))
            changed = action != act.get()
            state["action"] = action
            if changed and msg:
                act.set(action)
                note.configure(text=msg + "  Click Next to continue.")
                return False
            if msg and action == "existing" and note.cget("text") != msg:
                note.configure(text=msg)
            return True
        return ok

    def p_account():
        f = new_page("First administrator",
                     "This account can add users, release locks and change settings. Each person gets their own "
                     "account later in Administration › Users.")
        u, p1, p2 = tk.StringVar(value=state["user"] or os.environ.get("USERNAME", "")), tk.StringVar(), tk.StringVar()
        label(f, "Username", "Sub.TLabel")
        entry(f, u)
        label(f, "Password (at least 6 characters)", "Sub.TLabel")
        entry(f, p1, "•")
        label(f, "Confirm password", "Sub.TLabel")
        entry(f, p2, "•")

        def ok():
            problem = check_account(u.get(), p1.get(), p2.get())
            if problem:
                return show_error(problem)
            state["user"], state["pw"] = u.get().strip(), p1.get()
            return True
        return ok

    def p_security():
        f = new_page("Encryption",
                     "Encrypts the contents of every file in the vault, so the cloud drive or server only ever "
                     "holds unreadable data. Your working folder on this PC is never encrypted.")
        var = tk.BooleanVar(value=state["encrypt"])
        cb = ttk.Checkbutton(f, text="Encrypt files stored in the vault", variable=var)
        cb.pack(anchor="w")
        if not crypto_ok:
            cb.state(["disabled"])
            var.set(False)
            label(f, "Not available: the encryption library is missing from this build.", "Err.TLabel")
        elif state["mode"] == "local":
            label(f, "Usually not needed for a vault on this PC (Windows BitLocker covers that).", "Sub.TLabel")
        p1, p2 = tk.StringVar(value=state["passphrase"]), tk.StringVar(value=state["passphrase"])
        sub = ttk.Frame(f)
        sub.pack(fill="x", pady=(S(12), 0))
        label(sub, "Vault passphrase (at least 10 characters - a short sentence works well)", "Sub.TLabel")
        e1 = entry(sub, p1, "•")
        label(sub, "Confirm passphrase", "Sub.TLabel")
        e2 = entry(sub, p2, "•")
        label(sub, "Everyone using this vault enters it once on each PC. File names, folders and part numbers "
                   "stay readable; only file contents are encrypted.", "Sub.TLabel")

        def sync(*_):
            for e in (e1, e2):
                e.state(["!disabled"] if var.get() else ["disabled"])
        var.trace_add("write", sync)
        sync()

        def ok():
            if var.get():
                problem = check_passphrase(p1.get(), p2.get())
                if problem:
                    return show_error(problem)
                if not state["recovery"]:
                    state["recovery"] = new_code()
            state["encrypt"], state["passphrase"] = var.get(), p1.get() if var.get() else ""
            return True
        return ok

    def p_recovery():
        code = state["recovery"]
        f = new_page("Save your recovery code",
                     "If the passphrase is ever forgotten, this code is the only other way to open the vault. "
                     "Store it somewhere safe, away from this PC - a password manager or a printed copy.")
        e = ttk.Entry(f, font=("Consolas", 13))
        e.insert(0, code)
        e.state(["readonly"])
        e.pack(fill="x", ipady=S(4))
        row = ttk.Frame(f)
        row.pack(anchor="w", pady=(S(10), 0))

        def copy():
            root.clipboard_clear()
            root.clipboard_append(code)

        def save():
            p = filedialog.asksaveasfilename(parent=root, title="Save recovery code", defaultextension=".txt",
                                             initialfile="Atelier PDM recovery code.txt")
            if p:
                with open(p, "w", encoding="utf-8") as fh:
                    fh.write(f"Atelier PDM vault recovery code\nVault: {state['vault']}\nCode:  {code}\n")
        ttk.Button(row, text="Copy", command=copy).pack(side="left")
        ttk.Button(row, text="Save to file…", command=save).pack(side="left", padx=(S(8), 0))
        done = tk.BooleanVar(value=False)
        ttk.Checkbutton(f, text="I've stored the recovery code somewhere safe", variable=done).pack(anchor="w",
                                                                                                    pady=(S(16), 0))
        return lambda: done.get() or show_error("Tick the box once the recovery code is stored.")

    def p_sandbox():
        f = new_page("Working folder on this PC",
                     "Checked-out files are copied here while you work on them. It's never encrypted and never "
                     "shared. Keep it on a local drive, not inside the cloud drive.")
        var = tk.StringVar(value=state["sandbox"])
        prow = ttk.Frame(f)
        prow.pack(fill="x")
        ttk.Entry(prow, textvariable=var).pack(side="left", fill="x", expand=True, ipady=S(1))

        def browse():
            p = filedialog.askdirectory(parent=root, title="Choose the working folder")
            if p:
                var.set(os.path.normpath(p))
        ttk.Button(prow, text="Browse…", command=browse).pack(side="left", padx=(S(8), 0))

        def ok():
            p = os.path.normpath(var.get().strip().strip('"')) if var.get().strip() else ""
            if not p:
                return show_error("Choose a working folder.")
            vn = os.path.normcase(os.path.abspath(state["vault"]))
            pn = os.path.normcase(os.path.abspath(p))
            if pn == vn or pn.startswith(vn + os.sep):
                return show_error("The working folder can't be inside the vault.")
            problem = writable(p)
            if problem:
                return show_error(problem)
            state["sandbox"] = p
            return True
        return ok

    def p_finish():
        f = new_page("Ready", "Check the details, then click Finish.")
        rows = [("Vault", f"{MODES[state['mode']][0]}  ·  {state['vault']}"),
                ("", "New vault" if state["action"] == "new" else "Existing vault"),
                ("Working folder", state["sandbox"])]
        if state["action"] == "new":
            rows += [("Administrator", state["user"]), ("Encryption", "On" if state["encrypt"] else "Off")]
        elif is_encrypted_vault(state["vault"]):
            rows += [("Encryption", "On - you'll be asked for the passphrase once")]
        grid = ttk.Frame(f)
        grid.pack(anchor="w", fill="x")
        for r, (k, v) in enumerate(rows):
            ttk.Label(grid, text=k, style="Sub.TLabel").grid(row=r, column=0, sticky="nw", pady=S(2))
            ttk.Label(grid, text=v, wraplength=S(440), justify="left").grid(row=r, column=1, sticky="w",
                                                                           padx=(S(16), 0), pady=S(2))
        var = tk.BooleanVar(value=True)
        ttk.Checkbutton(f, text=f"Open {APP_NAME} now", variable=var).pack(anchor="w", pady=(S(18), 0))
        busy = ttk.Label(f, text="", style="Note.TLabel")
        busy.pack(anchor="w", pady=(S(6), 0))

        def ok():
            state["open"] = var.get()
            busy.configure(text="Setting up…")
            root.config(cursor="watch")
            root.update()
            try:
                cfg = create_vault(state) if state["action"] == "new" else make_config(state)
                save_install(cfg)
            except Exception as e:                                       # noqa: BLE001
                root.config(cursor="")
                busy.configure(text="")
                return show_error(f"Setup failed: {e}")
            root.config(cursor="")
            result["cfg"] = cfg
            return True
        return ok

    def flow():
        pages = [p_welcome, p_where, p_folder]
        if state["action"] == "new":
            pages += [p_account, p_security]
            if state["encrypt"]:
                pages.append(p_recovery)
        pages += [p_sandbox, p_finish]
        return pages

    pos = {"i": 0, "validate": lambda: True}

    def show(i):
        pages = flow()
        pos["i"] = max(0, min(i, len(pages) - 1))
        pos["validate"] = pages[pos["i"]]()
        last = pos["i"] == len(pages) - 1
        step_lbl.configure(text=f"Step {pos['i'] + 1} of {len(pages)}")
        b_next.configure(text="Finish" if last else "Next")
        b_back.state(["disabled"] if pos["i"] == 0 else ["!disabled"])

    def nxt(_e=None):
        before = flow()[pos["i"]]
        if not pos["validate"]():
            return
        if before is p_finish:
            root.destroy()
            return
        pages = flow()                                   # the answers can add or remove pages
        show(pages.index(before) + 1 if before in pages else pos["i"] + 1)

    def back():
        show(pos["i"] - 1)

    def cancel():
        if messagebox.askyesno(f"{APP_NAME} setup", "Cancel setup?", parent=root):
            result.clear()
            root.destroy()

    b_next.configure(command=nxt)
    b_back.configure(command=back)
    b_cancel.configure(command=cancel)
    root.bind("<Return>", nxt)
    root.protocol("WM_DELETE_WINDOW", cancel)
    show(0)
    root.mainloop()
    if "cfg" not in result:
        return None
    return result["cfg"], state["open"]
