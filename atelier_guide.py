"""
Atelier PDM - tour, template walkthrough, roll back, settings fix (atelier_guide.py)  2.8.2
===========================================================================================

Tour
    Rebuilt. The guide bubble and the highlight are now drawn inside the Atelier window (not as separate
    pop-up windows, which Windows could hide behind the main window or refuse to focus). Next / Back / Skip,
    or Enter, the arrow keys and Esc. It follows the window if you resize it.

Template walkthrough  (account menu › Template walkthrough…, or New Part when there are no templates)
    Step by step: choose the CAD program and the kind of template, start a blank file in that program, save it
    into the templates folder under the right name (or pick a file you already have), and try it with New Part.
    Right-click a part › Save as template… turns an existing part into a template.

Roll back  (right-click › Roll back…)
    Lists every saved version of a file (from its history), lets you open a copy of any of them, and makes the
    chosen one the current vault version again. The version it replaces is kept in the history first, so a roll
    back can itself be undone. Released, awaiting-approval and obsolete files can't be rolled back.

Settings
    Fixes settings being lost: the main window kept its own copy of your settings and wrote it back over changes
    made elsewhere (so the tour was offered again, and custom themes or the list filter could be forgotten).
    Everyone now shares one copy, and saving merges with what's on disk, so two Atelier windows don't overwrite
    each other either.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time

VERSION = "2.8.2"
E = None
P = None                                                       # atelier_pro
ORIG = {}
_TOUR = {"app": None, "i": 0, "steps": [], "parts": [], "bound": False}
_SET = {"obj": None, "base": None}
_MISSING = object()


# =============================================================================
# Settings: one shared copy, merged on save
# =============================================================================
def _js(v):
    try:
        return json.dumps(v, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return repr(v)


def _copy(d):
    try:
        return json.loads(json.dumps(d, default=str))
    except (TypeError, ValueError):
        return dict(d)


def _merge(obj, old_base, disk):
    """obj keeps its own unsaved changes (keys that differ from old_base); everything else follows disk."""
    keys = set(obj) | set(disk) | set(old_base)
    for k in keys:
        mine, base, theirs = obj.get(k, _MISSING), old_base.get(k, _MISSING), disk.get(k, _MISSING)
        changed_here = _js(mine) != _js(base) if mine is not _MISSING or base is not _MISSING else False
        if changed_here:
            continue
        if theirs is _MISSING:
            obj.pop(k, None)
        else:
            obj[k] = theirs


def load_settings():
    disk = ORIG["load"]() or {}
    if _SET["obj"] is None:
        _SET["obj"], _SET["base"] = disk, _copy(disk)
        return _SET["obj"]
    _merge(_SET["obj"], _SET["base"], disk)
    _SET["base"] = _copy(disk)
    return _SET["obj"]


def save_settings(data):
    base = _SET["base"] if _SET["base"] is not None else {}
    disk = ORIG["load"]() or {}
    for k, v in data.items():
        if _js(v) != _js(base.get(k, _MISSING)):
            disk[k] = v
    for k in list(base):
        if k not in data:
            disk.pop(k, None)
    ORIG["save"](disk)
    obj = _SET["obj"]
    if obj is None:
        _SET["obj"] = data if isinstance(data, dict) else dict(disk)
        obj = _SET["obj"]
    if data is obj:
        obj.clear()
        obj.update(disk)
    else:
        _merge(obj, base, disk)
    _SET["base"] = _copy(disk)


def _fix_settings(m):
    if getattr(m.load_settings, "_atl_guide", False):
        return
    ORIG["load"], ORIG["save"] = m.load_settings, m.save_settings
    load_settings._atl_guide = True
    save_settings._atl_guide = True
    m.load_settings, m.save_settings = load_settings, save_settings


def local(**kw):
    s = E.load_settings()
    if kw:
        s.update(kw)
        E.save_settings(s)
    return s


# =============================================================================
# Small UI helpers
# =============================================================================
def _tk():
    import tkinter as tk
    from tkinter import ttk, messagebox, filedialog
    return tk, ttk, messagebox, filedialog


def _say(app, msg, level="info"):
    try:
        app.log(msg, level)
    except Exception:                                           # noqa: BLE001
        pass


def _err(app, msg, parent=None):
    _say(app, msg, "error")
    _tk()[2].showerror(E.APP_NAME, msg, parent=parent or app.root)


# =============================================================================
# Tour - drawn inside the main window
# =============================================================================
def _find_button(container, text):
    try:
        for w in container.winfo_children():
            try:
                if text.lower() in str(w.cget("text")).lower():
                    return w
            except Exception:                                   # noqa: BLE001
                pass
            hit = _find_button(w, text)
            if hit:
                return hit
    except Exception:                                           # noqa: BLE001
        pass
    return None


def tour_steps(app):
    tb = getattr(app, "toolbar", None)
    g = lambda name: (lambda: getattr(app, name, None))
    b = lambda text: (lambda: _find_button(tb, text))
    return [
        (g("search_entry"), "Search",
         "Type part of a name, part number or description - results come from every folder. Ctrl+F jumps here."),
        (g("_pro_filter_cb"), "Show",
         "Quick filters: Checked out by me, Locked by others, Changed this week, Released, Watching… They look in "
         "every folder. Choose All files to go back."),
        (b("New Project"), "Projects",
         "A project is a numbered folder (01_, 02_ …) with Parts and Assemblies folders. Parts saved in it get "
         "numbers automatically."),
        (b("New Part"), "New parts",
         "Makes a part from a template, gives it the next number and checks it out to you. You can also drag files "
         "in from File Explorer."),
        (g("ftree"), "Folders", "The vault's folders. Right-click for more."),
        (g("grid"), "Files",
         "Everything in the selected folder. Right-click a file for Where used, Bill of materials, Request release, "
         "Roll back and more."),
        (b("Check Out"), "Check out", "Locks a file to you so nobody else changes it, and copies it to your working "
                                      "folder."),
        (b("Check In"), "Check in", "Puts your changes back in the vault and keeps the old version in the history."),
        (g("canvas"), "Preview", "Drag to turn the part, scroll to zoom, double-click to reset."),
        (g("part_entry"), "Properties", "The selected file's name, number and description. Edit, then Save "
                                         "properties."),
        (g("_pro_gallery_btn"), "Gallery", "The files in the list as thumbnails."),
        (g("_pro_appr_btn"), "Releasing", "Finished designs: right-click › Request release. An administrator approves "
                                          "them here."),
        (g("account_mb"), "Your menu", "Check in all, themes, the template walkthrough, help and (administrators) "
                                       "backups. F1 opens Help anywhere."),
    ]


def _visible(w):
    try:
        return w is not None and w.winfo_ismapped() and w.winfo_viewable() and w.winfo_width() > 2
    except Exception:                                           # noqa: BLE001
        return False


def tour_close(save=True):
    for w in _TOUR["parts"]:
        try:
            w.destroy()
        except Exception:                                       # noqa: BLE001
            pass
    _TOUR["parts"] = []
    app = _TOUR["app"]
    _TOUR["app"] = None
    if save and app is not None:
        local(tour_done=True)


def _bubble_place(app, target, bubble, frames, pad=4):
    root = app.root
    root.update_idletasks()
    rx, ry = root.winfo_rootx(), root.winfo_rooty()
    rw, rh = root.winfo_width(), root.winfo_height()
    x, y = target.winfo_rootx() - rx, target.winfo_rooty() - ry
    w, h = target.winfo_width(), target.winfo_height()
    x0, y0, x1, y1 = max(0, x - pad), max(0, y - pad), min(rw, x + w + pad), min(rh, y + h + pad)
    t = 3
    top, bot, left, right = frames
    top.place(x=x0, y=y0, width=x1 - x0, height=t)
    bot.place(x=x0, y=y1 - t, width=x1 - x0, height=t)
    left.place(x=x0, y=y0, width=t, height=y1 - y0)
    right.place(x=x1 - t, y=y0, width=t, height=y1 - y0)
    bw, bh = bubble.winfo_reqwidth(), bubble.winfo_reqheight()
    by = y1 + 10 if y1 + 10 + bh <= rh - 8 else (y0 - bh - 10 if y0 - bh - 10 >= 8 else max(8, (rh - bh) // 2))
    bx = min(max(8, x0), max(8, rw - bw - 8))
    if (y1 - y0) > rh * 0.45 or y0 < by < y1:                   # big target (the file list): sit inside it
        bx, by = max(8, x1 - bw - 16), max(8, y0 + 16)
    bubble.place(x=bx, y=by)
    for f in frames:
        f.lift()
    bubble.lift()


def start_tour(app, offer_sample=True):
    tk, ttk, mb, fd = _tk()
    tour_close(save=False)
    steps = [s for s in tour_steps(app)]
    _TOUR.update(app=app, steps=steps, offer=offer_sample)
    if not _TOUR["bound"]:
        app.root.bind("<Configure>", lambda e: e.widget is app.root and _TOUR["app"] is app and
                      app.root.after_idle(lambda: _tour_show(_TOUR["i"], quiet=True)), add="+")
        _TOUR["bound"] = True
    _tour_show(0)


def _tour_finish():
    app, offer = _TOUR["app"], _TOUR.get("offer")
    tour_close(save=True)
    if app is None or not offer:
        return
    tk, ttk, mb, fd = _tk()
    if mb.askyesno("All done", "Would you like a sample project with a few practice parts to try checking out, "
                   "checking in and releasing?\n\n(The template walkthrough is in your account menu when you want "
                   "to set up your own templates.)", parent=app.root):
        try:
            P.ui_sample(app)
        except Exception as e:                                  # noqa: BLE001
            _err(app, f"Couldn't create the sample project: {e}")


def _tour_show(i, quiet=False):
    app = _TOUR["app"]
    if app is None:
        return
    tk, ttk, mb, fd = _tk()
    steps = _TOUR["steps"]
    step = 1 if i >= _TOUR.get("i", 0) else -1
    while 0 <= i < len(steps) and not _visible(steps[i][0]()):
        i += step
    if i < 0:
        i = 0
        while i < len(steps) and not _visible(steps[i][0]()):
            i += 1
    if i >= len(steps):
        if not quiet:
            _tour_finish()
        return
    _TOUR["i"] = i
    for w in _TOUR["parts"]:
        try:
            w.destroy()
        except Exception:                                       # noqa: BLE001
            pass
    t, root = app.t, app.root
    frames = [tk.Frame(root, bg=t["accent"], bd=0, highlightthickness=0) for _ in range(4)]
    bubble = tk.Frame(root, bg=t["accent"], bd=0, highlightthickness=0)
    body = tk.Frame(bubble, bg=t["panel_bg"], padx=14, pady=10)
    body.pack(padx=2, pady=2)
    get, title, text = steps[i]
    shown = [k for k, s in enumerate(steps) if _visible(s[0]())]
    pos = shown.index(i) + 1 if i in shown else i + 1
    tk.Label(body, text=f"{title}   ·   {pos} of {len(shown)}", bg=t["panel_bg"], fg=t["accent"],
             font=("Segoe UI Semibold", 10)).pack(anchor="w")
    tk.Label(body, text=text, bg=t["panel_bg"], fg=t["fg"], font=("Segoe UI", 9), wraplength=330,
             justify="left").pack(anchor="w", pady=(4, 10))
    row = tk.Frame(body, bg=t["panel_bg"])
    row.pack(fill="x")
    last = pos == len(shown)
    skip = ttk.Button(row, text="Skip tour", command=lambda: tour_close(save=True))
    skip.pack(side="left")
    nxt = ttk.Button(row, text="Finish" if last else "Next ›", style="Accent.TButton",
                     command=lambda: _tour_finish() if last else _tour_show(i + 1))
    nxt.pack(side="right")
    if pos > 1:
        ttk.Button(row, text="‹ Back", command=lambda: _tour_show(i - 1)).pack(side="right", padx=(0, 6))
    for w in (nxt, bubble):
        w.bind("<Return>", lambda e: (nxt.invoke(), "break")[1])
        w.bind("<Right>", lambda e: (nxt.invoke(), "break")[1])
        w.bind("<Left>", lambda e: (_tour_show(i - 1) if pos > 1 else None, "break")[1])
        w.bind("<Escape>", lambda e: (tour_close(save=True), "break")[1])
    _TOUR["parts"] = frames + [bubble]
    try:
        _bubble_place(app, get(), bubble, frames)
    except Exception:                                           # noqa: BLE001
        bubble.place(relx=0.5, rely=0.3, anchor="n")
        bubble.lift()
    nxt.focus_set()


def offer_tour(app):
    if local().get("tour_done"):
        return
    tk, ttk, mb, fd = _tk()
    r = mb.askyesnocancel("Welcome to Atelier PDM", "New to Atelier? Take a one-minute tour of the window.\n\n"
                          "Yes: take the tour     No: not now     Cancel: don't ask again", parent=app.root)
    if r is None:
        local(tour_done=True)
    elif r:
        start_tour(app)


# =============================================================================
# Templates
# =============================================================================
KINDS = (("Part", "models"), ("Assembly", "models"), ("Drawing", "drawings"))


def template_dir():
    return E.TEMPLATES_DIR


def list_templates():
    try:
        return sorted(f for f in os.listdir(template_dir()) if os.path.isfile(os.path.join(template_dir(), f))
                      and not f.lower().endswith((".partial", ".tmp", ".enc-tmp")))
    except OSError:
        return []


def template_apps():
    """[(key, label, model_ext, drawing_ext or None)] for CAD programs Atelier knows."""
    out = []
    for k, a in E.CAD_APPS.items():
        if a.get("models"):
            out.append((k, a.get("label", k), a["models"][0], (a.get("drawings") or [None])[0]))
    return out


def template_name(kind, extra, ext):
    extra = " ".join((extra or "").split())
    return (f"{kind} - {extra}" if extra else kind) + ext


def check_template_name(name):
    stem = os.path.splitext(name)[0]
    problem = E.validate_new_name(stem)
    if problem:
        return problem
    if os.path.exists(os.path.join(template_dir(), name)):
        return f"There's already a template called '{name}'. Add something to the name, e.g. 'Part - Sheet metal'."
    return None


def add_template(src, name):
    """Copies a file into the templates folder under name (encrypted if the vault is)."""
    problem = check_template_name(name)
    if problem:
        raise ValueError(problem)
    dst = os.path.join(template_dir(), name)
    os.makedirs(template_dir(), exist_ok=True)
    copier = getattr(E, "vault_copy", None)
    if copier:
        copier(src, dst)
    else:
        shutil.copy2(src, dst)
    E.log_audit("[templates]", "Template Added", "", f"{name} (from {os.path.basename(src)})")
    return dst


def start_blank(app_key):
    exe = None
    try:
        exe = E.cad_launcher(app_key)
    except Exception:                                           # noqa: BLE001
        exe = None
    if not exe:
        raise LookupError(f"{E.CAD_APPS[app_key].get('label', app_key)} wasn't found on this PC.")
    subprocess.Popen([exe], cwd=os.path.dirname(exe) or None)
    return exe


CHECKLIST = {
    "Part": ["Units (usually millimetres)", "Default material and appearance", "File properties you always fill in "
             "(company, drawn by, project)", "Leave out geometry - keep it blank"],
    "Assembly": ["Units", "File properties", "Any standard reference planes or a base part you always use",
                 "Nothing else"],
    "Drawing": ["Sheet size and title block (company name, logo)", "Standard notes, tolerances and projection symbol",
                "Fields in the title block linked to the part's properties where the program allows"],
}


def template_wizard(app):
    tk, ttk, mb, fd = _tk()
    S = app.S
    apps = template_apps()
    if not apps:
        _err(app, "No CAD programs are set up (Administration › CAD Apps).")
        return None
    dlg = E.ThemedDialog(app, "Template walkthrough", "Create a template",
                         "Templates are the blank starting files New Part uses. You only need to do this once per "
                         "kind of part.", width=560)
    st = {"page": 0, "job": None, "name": "", "made": None}
    app_var = tk.StringVar(value=apps[0][1])
    kind_var = tk.StringVar(value="Part")
    extra_var = tk.StringVar()
    pages = [ttk.Frame(dlg.body, style="Dialog.TFrame") for _ in range(4)]
    step_lbl = ttk.Label(dlg.buttons, text="", style="CardMuted.TLabel")
    step_lbl.pack(side="left")
    nxt = ttk.Button(dlg.buttons, text="Next ›", style="Accent.TButton")
    back = ttk.Button(dlg.buttons, text="‹ Back")
    nxt.pack(side="right")
    back.pack(side="right", padx=(0, S(8)))

    def sel_app():
        return next(a for a in apps if a[1] == app_var.get())

    def ext():
        a = sel_app()
        return a[3] if kind_var.get() == "Drawing" else a[2]

    def target():
        return template_name(kind_var.get(), extra_var.get(), ext() or "")

    # ---- page 1: what
    p = pages[0]
    ttk.Label(p, text="1 · What kind of template?", style="CardTitle.TLabel").pack(anchor="w")
    ttk.Label(p, text="CAD program", style="CardMuted.TLabel").pack(anchor="w", pady=(S(10), 0))
    ttk.Combobox(p, textvariable=app_var, values=[a[1] for a in apps], state="readonly").pack(fill="x", pady=(S(2), 0))
    ttk.Label(p, text="Template for", style="CardMuted.TLabel").pack(anchor="w", pady=(S(10), 0))
    kc = ttk.Combobox(p, textvariable=kind_var, state="readonly")
    kc.pack(fill="x", pady=(S(2), 0))
    ttk.Label(p, text="Extra name (optional)", style="CardMuted.TLabel").pack(anchor="w", pady=(S(10), 0))
    ttk.Entry(p, textvariable=extra_var).pack(fill="x", pady=(S(2), 0), ipady=S(2))
    ttk.Label(p, text="e.g. 'Sheet metal' or 'Aluminium' if you'll have more than one part template.",
              style="CardMuted.TLabel").pack(anchor="w")
    name_lbl = ttk.Label(p, text="", style="CardAccent.TLabel")
    name_lbl.pack(anchor="w", pady=(S(10), 0))
    err1 = ttk.Label(p, text="", style="CardDanger.TLabel", wraplength=S(500))
    err1.pack(anchor="w")

    def refresh_kinds(*_):
        a = sel_app()
        kinds = ["Part", "Assembly"] + (["Drawing"] if a[3] else [])
        kc.configure(values=kinds)
        if kind_var.get() not in kinds:
            kind_var.set("Part")
        name_lbl.configure(text=f"It will be saved as:  {target()}")
    for v in (app_var, kind_var, extra_var):
        v.trace_add("write", refresh_kinds)

    # ---- page 2: make the blank file
    p = pages[1]
    ttk.Label(p, text="2 · Make the blank file", style="CardTitle.TLabel").pack(anchor="w")
    how = ttk.Label(p, text="", style="Card.TLabel", wraplength=S(500), justify="left")
    how.pack(anchor="w", pady=(S(8), S(6)))
    checklist = ttk.Label(p, text="", style="Card.TLabel", wraplength=S(500), justify="left")
    checklist.pack(anchor="w")
    row = ttk.Frame(p, style="Dialog.TFrame")
    row.pack(anchor="w", pady=(S(12), 0))
    start_btn = ttk.Button(row, text="Start a blank file", style="Accent.TButton")
    start_btn.pack(side="left")
    have_btn = ttk.Button(row, text="I already have a file…")
    have_btn.pack(side="left", padx=(S(8), 0))
    msg2 = ttk.Label(p, text="", style="CardMuted.TLabel", wraplength=S(500), justify="left")
    msg2.pack(anchor="w", pady=(S(8), 0))

    def do_start():
        a = sel_app()
        try:
            start_blank(a[0])
            msg2.configure(text=f"Starting {a[1]}… make a new {kind_var.get().lower()}, set it up, then click Next.")
        except Exception as e:                                  # noqa: BLE001
            msg2.configure(text=f"{e} Start it yourself, then click Next.")
    start_btn.configure(command=do_start)

    def do_have():
        a = sel_app()
        f = fd.askopenfilename(parent=dlg, title=f"Choose the {a[1]} file to use as a template",
                               filetypes=[(a[1], f"*{ext()}"), ("All files", "*.*")])
        dlg.grab_set()
        if not f:
            return
        if os.path.splitext(f)[1].lower() != (ext() or "").lower():
            msg2.configure(text=f"That isn't a {ext()} file.")
            return
        try:
            st["made"] = add_template(f, target())
        except (OSError, ValueError) as e:
            msg2.configure(text=str(e))
            return
        show(3)
    have_btn.configure(command=do_have)

    # ---- page 3: save it into the folder
    p = pages[2]
    ttk.Label(p, text="3 · Save it into the templates folder", style="CardTitle.TLabel").pack(anchor="w")
    save_txt = ttk.Label(p, text="", style="Card.TLabel", wraplength=S(500), justify="left")
    save_txt.pack(anchor="w", pady=(S(8), S(4)))
    path_var = tk.StringVar()
    pe = ttk.Entry(p, textvariable=path_var)
    pe.pack(fill="x", ipady=S(2))
    row3 = ttk.Frame(p, style="Dialog.TFrame")
    row3.pack(anchor="w", pady=(S(8), 0))

    def copy_path():
        dlg.clipboard_clear()
        dlg.clipboard_append(path_var.get())
        status.configure(text="Copied. Paste it into the file name box of Save As.")
    ttk.Button(row3, text="Copy path", command=copy_path).pack(side="left")
    ttk.Button(row3, text="Open folder", command=lambda: (os.makedirs(template_dir(), exist_ok=True),
                                                           E.open_path(template_dir()))).pack(side="left",
                                                                                              padx=(S(8), 0))

    def pick_saved():
        f = fd.askopenfilename(parent=dlg, title="Choose the file you saved",
                               filetypes=[("Template", f"*{ext()}"), ("All files", "*.*")])
        dlg.grab_set()
        if f:
            try:
                st["made"] = add_template(f, target())
            except (OSError, ValueError) as e:
                status.configure(text=str(e))
                return
            show(3)
    ttk.Button(row3, text="Saved it somewhere else…", command=pick_saved).pack(side="left", padx=(S(8), 0))
    status = ttk.Label(p, text="", style="CardAccent.TLabel", wraplength=S(500), justify="left")
    status.pack(anchor="w", pady=(S(10), 0))

    def poll():
        st["job"] = None
        if st["page"] != 2:
            return
        dst = os.path.join(template_dir(), target())
        if os.path.isfile(dst) and os.path.getsize(dst) > 0:
            st["made"] = dst
            status.configure(text=f"✓ Found {target()} - click Next.")
            nxt.state(["!disabled"])
        else:
            status.configure(text="Waiting for the file… (this updates by itself)")
            nxt.state(["disabled"])
            st["job"] = dlg.after(1000, poll)

    # ---- page 4: done
    p = pages[3]
    ttk.Label(p, text="4 · Ready", style="CardTitle.TLabel").pack(anchor="w")
    done_txt = ttk.Label(p, text="", style="Card.TLabel", wraplength=S(500), justify="left")
    done_txt.pack(anchor="w", pady=(S(8), S(8)))
    try_btn = ttk.Button(p, text="Try New Part now", style="Success.TButton")
    try_btn.pack(anchor="w")
    another = ttk.Button(p, text="Make another template")
    another.pack(anchor="w", pady=(S(6), 0))

    def show(i):
        if st["job"]:
            dlg.after_cancel(st["job"])
            st["job"] = None
        st["page"] = i
        for k, pg in enumerate(pages):
            if k == i:
                pg.pack(fill="both", expand=True)
            else:
                pg.pack_forget()
        step_lbl.configure(text=f"Step {i + 1} of 4")
        back.state(["!disabled"] if 0 < i < 3 else ["disabled"])
        nxt.state(["!disabled"])
        nxt.configure(text="Close" if i == 3 else "Next ›")
        a = sel_app()
        k = kind_var.get()
        if i == 1:
            how.configure(text=f"Start a new, empty {k.lower()} in {a[1]} and set up only what every new "
                               f"{k.lower()} should have:")
            checklist.configure(text="\n".join("•  " + c for c in CHECKLIST[k]))
            msg2.configure(text="Or, if you already have a suitable file, use that instead.")
        elif i == 2:
            os.makedirs(template_dir(), exist_ok=True)
            path_var.set(os.path.join(template_dir(), target()))
            save_txt.configure(text=f"In {a[1]} use Save As and save it here, with exactly this name:")
            poll()
        elif i == 3:
            done_txt.configure(text=f"✓ {target()} is in the templates folder. New Part now offers it whenever "
                                    f"you start a {k.lower()} in {a[1]}." +
                                    ("\n\nTemplates starting with 'Part' are suggested in Parts folders, "
                                     "'Assembly' in Assemblies folders." if k != "Drawing" else ""))
            E.log_audit("[templates]", "Template Ready", "", target())

    def go_next():
        i = st["page"]
        if i == 0:
            problem = (None if ext() else f"{sel_app()[1]} has no {kind_var.get().lower()} file type.") or \
                check_template_name(target())
            if problem:
                err1.configure(text=problem)
                return
            err1.configure(text="")
            show(1)
        elif i == 1:
            show(2)
        elif i == 2:
            if st["made"]:
                show(3)
        else:
            dlg.result = st["made"]
            dlg.destroy()
    nxt.configure(command=go_next)
    back.configure(command=lambda: show(max(0, st["page"] - 1)))
    try_btn.configure(command=lambda: (setattr(dlg, "result", st["made"]), dlg.destroy(),
                                       app.root.after(150, app.action_new_from_template)))

    def again():
        st["made"] = None
        extra_var.set("")
        show(0)
    another.configure(command=again)
    refresh_kinds()
    show(0)
    dlg.bind("<Return>", lambda e: go_next())
    res = dlg.show()
    if st["job"]:
        try:
            dlg.after_cancel(st["job"])
        except Exception:                                       # noqa: BLE001
            pass
    if st["made"]:
        _say(app, f"Template ready: {os.path.basename(st['made'])}", "ok")
    return res


def ui_save_as_template(app, f):
    info = E.INDEX.files.get(f)
    if not info:
        return
    stem, ext = os.path.splitext(f)
    rec = E.INDEX.records.get(f) or {}
    kind = "Assembly" if (E.parse_pn(stem) or ("", ""))[1] == "A" else "Part"
    default = f"{kind} - {E.display_part_name(f, rec)}"

    def submit(v):
        name = v["n"].strip()
        if name.lower().endswith(ext.lower()):
            name = name[:-len(ext)].strip()
        problem = check_template_name(name + ext)
        if problem:
            return "error", problem
        return name + ext
    name = E.run_form(app, "Save as template", "Save as template",
                      f"A copy of {f} becomes a template for New Part. Open it afterwards and delete anything that "
                      "shouldn't be in every new part.", [("n", "Template name", False, default)], submit,
                      "Save template", width=480)
    if not name:
        return
    try:
        dst = add_template(info.path, name)
    except (OSError, ValueError) as e:
        _err(app, str(e))
        return
    _say(app, f"Saved as template: {name}", "ok")
    if _tk()[2].askyesno("Template saved", f"{name} is now a template.\n\nOpen it to tidy it up (remove geometry "
                         "that shouldn't be in every new part)?", parent=app.root):
        E.open_path(dst)


# =============================================================================
# Roll back
# =============================================================================
def _version_re(f):
    stem, ext = os.path.splitext(f)
    return re.compile(rf"^{re.escape(stem)}_(RELEASE_)?([A-Z]-\d{{2,}})(?:_RELEASED)?_(\d{{8}}_\d{{6}})(?:~\d+)?"
                      rf"{re.escape(ext)}$", re.IGNORECASE)


def versions(f):
    """Saved versions of f, newest first: [{name, rev, release, when, size}]."""
    rx = _version_re(f)
    out = []
    try:
        names = os.listdir(E.HISTORY_DIR)
    except OSError:
        return out
    for n in names:
        m = rx.match(n)
        if not m:
            continue
        p = os.path.join(E.HISTORY_DIR, n)
        try:
            when = time.mktime(time.strptime(m.group(3), "%Y%m%d_%H%M%S"))
        except ValueError:
            when = os.path.getmtime(p)
        out.append({"name": n, "rev": m.group(2).upper(), "release": bool(m.group(1)) or "_RELEASED" in n,
                    "when": when, "size": os.path.getsize(p)})
    out.sort(key=lambda v: (v["when"], v["name"]), reverse=True)
    return out


def _columns():
    try:
        with E.db() as c:
            return {r[1] for r in c.execute("PRAGMA table_info(files)").fetchall()}
    except Exception:                                           # noqa: BLE001
        return set()


def rollback(f, version_name, note):
    """Makes a saved version the current vault version. Returns (level, message)."""
    info = E.INDEX.files.get(f)
    if not info:
        return "error", f"'{f}' isn't in the vault."
    if info.is_library:
        return "error", "Standard Parts Library files can't be rolled back."
    rec = E.get_record(f) or {}
    lock = rec.get("locked_by") or ""
    if lock:
        who = "you - check it in or undo the check-out first" if lock == E.USER_NAME else lock
        return "error", f"'{f}' is checked out by {who}."
    if P is not None:
        stt = P.state_of(f)["state"]
        if stt != P.IN_WORK:
            return "error", (f"'{f}' is {stt.lower()}. Released files are rolled back by starting a new revision "
                             "(Revise) and restoring the old content into it.")
    if not (note or "").strip():
        return "error", "Say why you're rolling back - it goes in the history."
    v = next((x for x in versions(f) if x["name"] == version_name), None)
    src = os.path.join(E.HISTORY_DIR, version_name)
    if not v or not os.path.isfile(src):
        return "error", f"That version ({version_name}) is no longer in the history."
    cur_rev = rec.get("revision") or "A-01"
    E.archive_to_history(info.path, f, cur_rev)                 # the version being replaced is kept first
    tmp = info.path + ".partial"
    shutil.copy2(src, tmp)                                      # raw copy: stays encrypted if the vault is
    os.replace(tmp, info.path)
    new_rev = v["rev"]
    cols = _columns()
    sets, vals = ["revision=?"], [new_rev]
    if "hash" in cols:
        sets.append("hash=?")
        vals.append(E.get_file_hash(info.path))
    if "last_updated" in cols:
        sets.append("last_updated=?")
        vals.append(time.strftime("%Y-%m-%d %H:%M:%S"))
    with E.db() as c:
        c.execute(f"UPDATE files SET {', '.join(sets)} WHERE filename=?", (*vals, f))
    stl = os.path.splitext(info.path)[0] + ".stl"
    if os.path.isfile(stl):                                     # the preview belonged to the newer version
        keep = os.path.join(E.HISTORY_DIR, f"{os.path.splitext(f)[0]}_preview_{time.strftime('%Y%m%d_%H%M%S')}.stl")
        try:
            shutil.move(stl, keep)
        except OSError:
            pass
    try:
        if E.can_make_mesh(f):
            E.generate_stl_companion(info.path)
    except Exception:                                           # noqa: BLE001
        pass
    when = time.strftime("%d %b %Y %H:%M", time.localtime(v["when"]))
    E.log_audit(f, "Rolled Back", new_rev, f"To the version saved {when} (rev {new_rev}); was rev {cur_rev}. {note}")
    E.INDEX.refresh()
    return "ok", (f"'{f}' rolled back to the version saved {when} (rev {new_rev}). The version it replaced "
                  f"(rev {cur_rev}) is kept in the history, so this can be undone.")


def show_rollback(app, f):
    tk, ttk, mb, fd = _tk()
    S = app.S
    vs = versions(f)
    rec = E.INDEX.records.get(f) or {}
    dlg = E.ThemedDialog(app, "Roll back", f"Roll back {f}",
                         f"Current revision {rec.get("revision") or "A-01"}. Pick a saved version to make it the current "
                         "one again. Nothing is deleted - the current version is kept in the history.",
                         width=760, resizable=True)
    wrap = ttk.Frame(dlg.body, style="Dialog.TFrame")
    wrap.pack(fill="both", expand=True)
    tv = ttk.Treeview(wrap, columns=("when", "rev", "kind", "size"), show="headings", height=12, selectmode="browse")
    for c, label, w in (("when", "Saved", 170), ("rev", "Rev", 70), ("kind", "Kind", 200), ("size", "Size", 90)):
        tv.heading(c, text=label, anchor="w")
        tv.column(c, width=S(w), anchor="w", stretch=c == "kind")
    sb = ttk.Scrollbar(wrap, orient="vertical", command=tv.yview)
    tv.configure(yscrollcommand=sb.set)
    sb.pack(side="right", fill="y")
    tv.pack(side="left", fill="both", expand=True)
    for v in vs:
        tv.insert("", "end", iid=v["name"], values=(time.strftime("%d %b %Y  %H:%M", time.localtime(v["when"])),
                                                    v["rev"], "Release copy" if v["release"] else "Earlier check-in",
                                                    f"{v['size'] / 1024:,.0f} KB"))
    if not vs:
        ttk.Label(dlg.body, text="No saved versions yet - versions are kept each time the file is checked in or "
                                 "up-issued.", style="CardMuted.TLabel").pack(anchor="w", pady=(S(6), 0))
    elif tv.get_children():
        tv.selection_set(tv.get_children()[0])
    used = {}
    if P is not None:
        try:
            used = P.where_used(f)
        except Exception:                                       # noqa: BLE001
            used = {}
    if used:
        ttk.Label(dlg.body, text=f"Used in {len(used)} assembl{'y' if len(used) == 1 else 'ies'} - they'll use the "
                                 "rolled-back version.", style="CardWarn.TLabel"
                  ).pack(anchor="w", pady=(S(6), 0))

    def sel():
        s = tv.selection()
        return s[0] if s else None

    def open_copy():
        n = sel()
        if not n:
            return
        out_dir = os.path.join(E.LOCAL_WORKSPACE, "Versions")
        os.makedirs(out_dir, exist_ok=True)
        dst = os.path.join(out_dir, n)
        copier = getattr(E, "vault_copy", None)
        try:
            (copier or shutil.copy2)(os.path.join(E.HISTORY_DIR, n), dst)
            E.open_path(dst)
            _say(app, f"Opened a copy of {n} (changes to it aren't saved to the vault).", "ok")
        except OSError as e:
            _err(app, f"Couldn't open it: {e}", dlg)

    def do_rollback():
        n = sel()
        if not n:
            return

        def submit(v):
            return ("error", "Say why you're rolling back.") if not v["c"].strip() else v["c"].strip()
        note = E.run_form(app, "Roll back", "Roll back", f"{f}\nto {n}", [("c", "Reason (goes in the history)",
                                                                            False, "")], submit, "Roll back",
                          ok_style="Danger.TButton", width=480, parent=dlg)
        dlg.grab_set()
        if not note:
            return
        res = rollback(f, n, note)
        _say(app, res[1], res[0])
        if res[0] == "error":
            mb.showerror(E.APP_NAME, res[1], parent=dlg)
            return
        dlg.result = True
        dlg.destroy()
        app.refresh_all()
    tv.bind("<Double-1>", lambda e: open_copy())
    ttk.Button(dlg.buttons, text="Open a copy", command=open_copy).pack(side="left")
    rb = ttk.Button(dlg.buttons, text="⟲  Roll back to this version…", style="Accent.TButton", command=do_rollback)
    rb.pack(side="right")
    ttk.Button(dlg.buttons, text="Close", command=dlg.cancel).pack(side="right", padx=(0, S(8)))
    if not vs:
        rb.state(["disabled"])
    dlg.geometry(f"{S(800)}x{min(S(520), app.root.winfo_screenheight() - 90)}")
    dlg.show()


# =============================================================================
# Hooks
# =============================================================================
def _extra_items(app, f):
    items = list(ORIG["extra"](app, f)) if "extra" in ORIG else []
    if not f or f not in E.INDEX.files:
        return items
    info = E.INDEX.files[f]
    rec = E.INDEX.records.get(f) or {}
    can_rb = not info.is_library and not rec.get("locked_by") and (P is None or P.state_of(f)["state"] == P.IN_WORK)
    items += [None, ("⟲  Roll back…", lambda: show_rollback(app, f), can_rb and bool(versions(f)))]
    if info.ext in E.CAD_MODEL_EXTS | set(getattr(E, "CAD_DRAWING_EXTS", ())):
        items.append(("🧩  Save as template…", lambda: ui_save_as_template(app, f), True))
    return items


def _hook_app(m):
    cls = getattr(m, "AtelierApp", None)
    if cls is None or getattr(cls, "_atl_guide", False):
        return
    cls._atl_guide = True
    o_ident = getattr(cls, "update_identity", None)
    if o_ident:
        def update_identity(self, *a, **k):
            res = o_ident(self, *a, **k)
            try:
                self.account_menu.add_command(label="🧩  Template walkthrough…", command=lambda: template_wizard(self))
            except Exception:                                   # noqa: BLE001
                pass
            return res
        cls.update_identity = update_identity
    o_new = getattr(cls, "action_new_from_template", None)
    if o_new:
        def action_new_from_template(self, *a, **k):
            if not list_templates():
                tk, ttk, mb, fd = _tk()
                if mb.askyesno("No templates yet", "New Part needs at least one template - a blank starting file "
                               "for your CAD program.\n\nSet one up now with the template walkthrough?",
                               parent=self.root):
                    template_wizard(self)
                return None
            return o_new(self, *a, **k)
        cls.action_new_from_template = action_new_from_template


GUIDE_TIPS = [
    ("Roll back", "Makes an earlier saved version the current one. The current version is kept in the history."),
    ("Save as template", "Turns a copy of this part into a template for New Part."),
    ("Template walkthrough", "Step-by-step help to set up the blank starting files New Part uses."),
]


def install(module):
    global E, P
    E = module
    if getattr(module, "_guide_installed", False):
        return
    module._guide_installed = True
    P = sys.modules.get("atelier_pro")
    pol = sys.modules.get("atelier_polish")
    if pol is not None:
        pol.VERSION = VERSION                                   # shown under the title in the header
    _fix_settings(module)
    if P is not None:
        P.start_tour = start_tour
        P.offer_tour = offer_tour
        if "extra" not in ORIG:
            ORIG["extra"] = P.extra_menu_items
        P.extra_menu_items = _extra_items
    _hook_app(module)
    try:
        import atelier_help_text as H
        have = {k for k, _ in H.TIPS}
        H.TIPS[:0] = [t for t in GUIDE_TIPS if t[0] not in have]
    except ImportError:
        pass
