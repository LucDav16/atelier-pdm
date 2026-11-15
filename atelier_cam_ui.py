"""
Atelier PDM - CAM & Tools window (atelier_cam_ui.py)  v1.1
==========================================================

Opened from the main window's "CAM & Tools" button, from a CAM row in the main tree, the panels' menu, or the
Start-menu shortcut ("Atelier PDM.exe --cam"). Three tabs:

    Parts & CAM        each part with its CAM setups and NC programs underneath; open the part in CAM, open a
                       setup for editing and save it back, file NC programs, send to machine
    Tool libraries     tools, holders, CAD files and feeds & speeds; import / export for each CAM program
    Machines & posts   machine limits and program folders; posts per CAM program (administrators)

Everything on the buttons is also on the right-click menu, and the usual Windows keys work: Enter opens or
edits, Delete removes, F2 renames, Ctrl+N adds, Ctrl+D duplicates, F5 refreshes.
"""
import copy
import os
import sys
import time
import uuid
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

import atelier_cam as C

E = None          # atelier_pdm, taken from atelier_cam once installed


def _engine():
    global E
    E = C.E
    if E is None:
        raise RuntimeError("atelier_cam isn't installed into atelier_pdm yet.")
    return E


def remember(key, value):
    s = E.load_settings()
    s[key] = value
    E.save_settings(s)


def recall(key, default=None):
    return (E.load_settings() or {}).get(key, default)


def open_folder(path):
    os.makedirs(path, exist_ok=True)
    E.open_path(path)


# =============================================================================
# A small form builder on top of atelier_pdm's themed dialogs
# =============================================================================
def form_dialog(win, title, heading, sub, fields, validate, ok_text="OK", width=480, ok_style="Accent.TButton",
                parent=None):
    """fields: dicts with key, label, kind ('entry' | 'combo' | 'choice' | 'folder' | 'text' | 'check'),
    initial, values, col (0/1), hint. validate(values) returns the result, or raises ValueError(message)."""
    S = win.ctx.S
    dlg = E.ThemedDialog(win.ctx, title, heading, sub, width=width, parent=parent or win)
    grid = ttk.Frame(dlg.body, style="Dialog.TFrame")
    grid.pack(fill="both", expand=True)
    two = any(f.get("col") for f in fields)
    for c in range(2 if two else 1):
        grid.columnconfigure(c, weight=1, uniform="f" if two else None)
    rows = [0, 0]
    vars_, widgets, first = {}, {}, None
    for f in fields:
        col = f.get("col", 0) if two else 0
        span = 2 if (two and f.get("wide")) else 1
        r = max(rows) if span == 2 else rows[col]
        cell = ttk.Frame(grid, style="Dialog.TFrame")
        cell.grid(row=r, column=col, columnspan=span, sticky="ew", padx=(0, S(10) if two and col == 0 and span == 1 else 0),
                  pady=(0, S(8)))
        if span == 2:
            rows[0] = rows[1] = r + 1
        else:
            rows[col] = r + 1
        kind = f.get("kind", "entry")
        if kind != "check":
            ttk.Label(cell, text=f["label"], style="CardMuted.TLabel").pack(anchor="w")
        if kind == "check":
            v = tk.BooleanVar(value=bool(f.get("initial")))
            ttk.Checkbutton(cell, text=f["label"], variable=v, style="Dialog.TCheckbutton").pack(anchor="w")
        elif kind == "text":
            v = tk.Text(cell, height=f.get("height", 4), width=40, wrap="word", relief="flat", bd=0,
                        bg=win.ctx.t["entry_bg"], fg=win.ctx.t["entry_fg"], insertbackground=win.ctx.t["fg"],
                        highlightthickness=1, highlightbackground=win.ctx.t["border"], font=("Segoe UI", 9))
            v.insert("1.0", f.get("initial") or "")
            v.pack(fill="x", pady=(S(2), 0))
        else:
            v = tk.StringVar(value="" if f.get("initial") is None else str(f.get("initial")))
            if kind in ("combo", "choice"):
                w = ttk.Combobox(cell, textvariable=v, values=f.get("values", []),
                                 state="readonly" if kind == "choice" else "normal")
                w.pack(fill="x", pady=(S(2), 0))
                if f.get("on_change"):
                    w.bind("<<ComboboxSelected>>", lambda e, cb=f["on_change"]: cb(vars_, widgets))
            elif kind == "folder":
                rowf = ttk.Frame(cell, style="Dialog.TFrame")
                rowf.pack(fill="x", pady=(S(2), 0))
                w = ttk.Entry(rowf, textvariable=v)
                w.pack(side="left", fill="x", expand=True, ipady=S(1))

                def browse(var=v):
                    p = filedialog.askdirectory(parent=dlg, initialdir=var.get() if os.path.isdir(var.get()) else None)
                    if p:
                        var.set(os.path.normpath(p))
                ttk.Button(rowf, text="Browse…", command=browse).pack(side="left", padx=(S(6), 0))
            else:
                w = ttk.Entry(cell, textvariable=v)
                w.pack(fill="x", pady=(S(2), 0), ipady=S(1))
            first = first or w
            widgets[f["key"]] = w
        if f.get("hint"):
            ttk.Label(cell, text=f["hint"], style="CardMuted.TLabel", wraplength=S(width - 60),
                      justify="left").pack(anchor="w")
        vars_[f["key"]] = v
    err = ttk.Label(dlg.body, text="", style="CardDanger.TLabel", wraplength=S(width - 40), justify="left")
    err.pack(anchor="w", pady=(S(4), 0))
    for f in fields:
        if f.get("on_change"):
            f["on_change"](vars_, widgets)          # fill dependent lists for the starting values

    def values():
        out = {}
        for k, v in vars_.items():
            out[k] = v.get("1.0", "end").strip() if isinstance(v, tk.Text) else v.get()
            if isinstance(out[k], str):
                out[k] = out[k].strip()
        return out

    def check():
        """validate() raises ValueError (or OSError) with a message to show; anything it returns is the result."""
        try:
            return validate(values())
        except (OSError, ValueError) as e:
            err.configure(text=str(e))
            return E.INVALID
    dlg.validate = check
    dlg.add_buttons(ok_text, ok_style)
    dlg.focus_widget = first
    dlg.vars = vars_
    return dlg.show()


def make_tree(parent, cols, height=12, select="browse", tree_col=None):
    """cols: (key, label, width, stretch). tree_col: (label, width) to show the expandable first column.
    Packs itself to fill whatever space is left, so pack any bottom bars BEFORE calling this."""
    wrap = ttk.Frame(parent, style="Dialog.TFrame")
    wrap.pack(side="top", fill="both", expand=True)
    tv = ttk.Treeview(wrap, columns=[c[0] for c in cols], show="tree headings" if tree_col else "headings",
                      height=height, selectmode=select)
    if tree_col:
        tv.heading("#0", text=tree_col[0], anchor="w")
        tv.column("#0", width=tree_col[1], minwidth=80, anchor="w", stretch=True)
    for key, label, w, stretch in cols:
        tv.heading(key, text=label, anchor="w")
        tv.column(key, width=w, minwidth=40, anchor="w", stretch=stretch)
    sb = ttk.Scrollbar(wrap, orient="vertical", command=tv.yview)
    tv.configure(yscrollcommand=sb.set)
    sb.pack(side="right", fill="y")
    tv.pack(side="left", fill="both", expand=True)
    return tv


def bottom_bar(parent, S, pady=(8, 0), padx=0):
    """A button row fixed to the bottom - pack it before the list so it is never squeezed off."""
    f = ttk.Frame(parent, style="Dialog.TFrame")
    f.pack(side="bottom", fill="x", pady=(S(pady[0]), S(pady[1])), padx=padx)
    return f


def button_row(parent, S, pady=(8, 0)):
    f = ttk.Frame(parent, style="Dialog.TFrame")
    f.pack(fill="x", pady=(S(pady[0]), S(pady[1])))
    return f


# =============================================================================
# Right-click menus (the same actions as the buttons)
# =============================================================================
def menu_colors(ctx):
    if hasattr(ctx, "menu_colors"):
        try:
            return ctx.menu_colors()
        except Exception:                                              # noqa: BLE001
            pass
    t = ctx.t
    return dict(bg=t["panel_bg"], fg=t["fg"], activebackground=t.get("sel_bg", t["accent"]),
                activeforeground=t.get("sel_fg", "#ffffff"), disabledforeground=t["muted_fg"], bd=0,
                relief="flat", font=("Segoe UI", 9))


def attach_menu(win, tv, items):
    """items(iid) -> list of (label, command, enabled) or None for a separator. Right-click selects the row
    under the pointer first, like Explorer; right-click on empty space offers the 'new' items."""
    def popup(e):
        iid = tv.identify_row(e.y)
        if iid:
            if iid not in tv.selection():
                tv.selection_set(iid)
            tv.focus(iid)
        else:
            tv.selection_remove(*tv.selection())
        entries = items(iid or None)
        if not entries:
            return "break"
        m = tk.Menu(win, tearoff=0, **menu_colors(win.ctx))
        last_sep = True
        for it in entries:
            if it is None:
                if not last_sep:
                    m.add_separator()
                last_sep = True
                continue
            label, cmd, enabled = (it + (True,))[:3] if len(it) == 2 else it
            m.add_command(label=label, command=cmd, state="normal" if enabled else "disabled")
            last_sep = False
        try:
            m.tk_popup(e.x_root, e.y_root)
        finally:
            m.grab_release()
        return "break"
    tv.bind("<Button-3>", popup)
    tv.bind("<App>", lambda e: popup(type("Ev", (), {"y": tv.bbox(tv.focus())[1] + 4 if tv.focus() and tv.bbox(tv.focus()) else 4,
                                                     "x_root": tv.winfo_rootx() + 40,
                                                     "y_root": tv.winfo_rooty() + 40})()))
    return popup


def keys(tv, **actions):
    """Windows keys on a list: enter, delete, f2, new (Ctrl+N), dup (Ctrl+D)."""
    table = {"enter": ("<Return>",), "delete": ("<Delete>",), "f2": ("<F2>",),
             "new": ("<Control-n>", "<Control-N>"), "dup": ("<Control-d>", "<Control-D>")}
    for k, fn in actions.items():
        for seq in table[k]:
            tv.bind(seq, lambda e, f=fn: (f(), "break")[1])


# =============================================================================
# The window
# =============================================================================
class CamWindow(tk.Toplevel):
    def __init__(self, ctx, standalone=False, select=None, action=None):
        _engine()
        super().__init__(ctx.root)
        self.ctx, self.standalone = ctx, standalone
        S, t = ctx.S, ctx.t
        self.title(f"{E.APP_NAME} · CAM & Tools")
        self.configure(bg=t["panel_bg"])
        self._fit_to_screen(S(1180), S(720))
        self.minsize(min(S(820), self.winfo_screenwidth() - 40), min(S(480), self.winfo_screenheight() - 80))
        try:
            C._ensure_table()
        except Exception as e:                                         # noqa: BLE001
            messagebox.showerror(E.APP_NAME, f"Couldn't open the CAM data in the vault:\n{e}", parent=self)
        self.lib, self.lib_rev = None, None
        outer = ttk.Frame(self, style="Dialog.TFrame", padding=S(14))
        outer.pack(fill="both", expand=True)
        head = ttk.Frame(outer, style="Dialog.TFrame")
        head.pack(fill="x")
        ttk.Label(head, text="CAM & Tools", style="CardTitle.TLabel").pack(side="left")
        self.status = ttk.Label(head, text="", style="CardMuted.TLabel")
        self.status.pack(side="right")
        self.nb = ttk.Notebook(outer)
        self.nb.pack(fill="both", expand=True, pady=(S(10), 0))
        self._build_parts()
        self._build_libraries()
        self._build_machines()
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<F5>", lambda e: self.reload_all())
        self.bind("<Escape>", lambda e: self.close() if not self.standalone else None)
        self.reload_all()
        if select:
            self.reveal(select, action)
        self.lift()
        self.focus_force()

    def _fit_to_screen(self, w, h):
        """Opens at the wanted size, but never bigger than the screen (taskbar allowed for)."""
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w, h = min(w, sw - 40), min(h, sh - 90)
        self.geometry(f"{w}x{h}+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 3)}")

    # ------------------------------------------------------------------ helpers
    def S(self, n):
        return self.ctx.S(n)

    def say(self, msg, level="ok"):
        col = {"ok": self.ctx.t["success"], "warn": self.ctx.t["warning"], "error": self.ctx.t["danger"]}.get(
            level, self.ctx.t["muted_fg"])
        self.status.configure(text=msg if len(msg) < 140 else msg[:137] + "…", foreground=col)
        if level == "error":
            messagebox.showerror(E.APP_NAME, msg, parent=self)

    def guard(self, fn, *a, **k):
        try:
            return True, fn(*a, **k)
        except C.LibraryChanged as e:
            self.say(str(e), "error")
            self.open_library(self.lib["name"] if self.lib else None)
        except (OSError, ValueError, PermissionError, LookupError) as e:
            self.say(str(e), "error")
        except Exception as e:                                         # noqa: BLE001
            self.say(f"Unexpected error: {e}", "error")
        return False, None

    def need_admin(self):
        if E.is_admin():
            return True
        ensure = getattr(self.ctx, "ensure_admin", None)
        if ensure and ensure(parent=self):
            return True
        if not ensure:
            messagebox.showinfo(E.APP_NAME, "Machines and posts are changed by an administrator. Open CAM & Tools "
                                "from the main Atelier PDM window to sign in as one.", parent=self)
        return False

    def tag_colours(self, tv):
        t = self.ctx.t
        tv.tag_configure("outdated", foreground=t["warning"])
        tv.tag_configure("superseded", foreground=t["muted_fg"])
        tv.tag_configure("missing", foreground=t["danger"])
        tv.tag_configure("odd", background=t["row_alt"])
        tv.tag_configure("part", font=("Segoe UI Semibold", 9))
        tv.tag_configure("editing", foreground=t["accent"])

    def reload_all(self):
        self.load_parts()
        self.load_library_list()
        self.load_machines()

    def close(self):
        self.destroy()
        if self.standalone:
            self.ctx.root.destroy()

    def selected_part(self):
        f = getattr(self.ctx, "selected", None)
        if not f and hasattr(self.ctx, "current"):
            try:
                f = self.ctx.current()
            except Exception:                                          # noqa: BLE001
                f = None
        return f if f and os.path.splitext(f)[1].lower() in E.CAD_MODEL_EXTS else None

    def after_vault_change(self):
        try:
            E.INDEX.refresh()
        except Exception:                                              # noqa: BLE001
            pass
        for name in ("refresh_all", "refresh_vault"):
            fn = getattr(self.ctx, name, None)
            if fn:
                try:
                    fn()
                except Exception:                                      # noqa: BLE001
                    pass
                break
        self.load_parts()

    # ================================================================== Parts & CAM
    def _build_parts(self):
        S = self.S
        page = ttk.Frame(self.nb, style="Dialog.TFrame", padding=(0, S(10), 0, 0))
        self.nb.add(page, text="  Parts & CAM  ")
        top = ttk.Frame(page, style="Dialog.TFrame")
        top.pack(side="top", fill="x", pady=(0, S(8)))
        ttk.Label(top, text="⌕", style="CardMuted.TLabel").pack(side="left")
        self.p_search = tk.StringVar()
        ttk.Entry(top, textvariable=self.p_search, width=32).pack(side="left", padx=(S(4), S(12)), ipady=S(1))
        self.p_search.trace_add("write", lambda *_: self.load_parts())
        self.p_all = tk.BooleanVar(value=bool(recall("cam_show_all_parts", False)))
        ttk.Checkbutton(top, text="All parts", variable=self.p_all, style="Dialog.TCheckbutton",
                        command=lambda: (remember("cam_show_all_parts", self.p_all.get()), self.load_parts())
                        ).pack(side="left", padx=(0, S(12)))
        self.p_old = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="Show superseded", variable=self.p_old, style="Dialog.TCheckbutton",
                        command=self.load_parts).pack(side="left")
        ttk.Button(top, text="＋ File NC program…", style="Accent.TButton",
                   command=lambda: self.file_items("nc")).pack(side="right")
        ttk.Button(top, text="＋ File CAM setup…", command=lambda: self.file_items("setup")).pack(
            side="right", padx=(0, S(8)))
        ttk.Button(top, text="Open part in CAM…", command=self.open_part_in_cam).pack(side="right", padx=(0, S(8)))

        row = bottom_bar(page, S)
        self.p_buttons = {}
        for key, text, cmd, style in (("send", "Send to machine", self.send_program, "Success.TButton"),
                                      ("edit", "Open setup for editing", self.edit_setup, "TButton"),
                                      ("save", "Save setup back", self.save_setup, "TButton"),
                                      ("copy", "Open a copy", self.open_copy, "TButton"),
                                      ("toggle", "Supersede / restore", self.toggle_item, "TButton"),
                                      ("folder", "Show in folder", self.show_item, "TButton")):
            b = ttk.Button(row, text=text, style=style, command=cmd)
            b.pack(side="left", padx=(0, S(8)))
            self.p_buttons[key] = b
        ttk.Label(row, text="Right-click for more", style="CardMuted.TLabel").pack(side="right")
        self.p_tree = make_tree(page, (("rev", "Rev", 60, False), ("kind", "Type", 100, False),
                                       ("state", "Status", 200, True), ("machine", "Machine", 130, False),
                                       ("post", "Post", 110, False), ("cam", "CAM program", 130, False),
                                       ("added", "Filed", 120, False), ("by", "By", 80, False)),
                                height=14, tree_col=("Part / file", 340))
        self.tag_colours(self.p_tree)
        self.p_tree.bind("<Double-1>", lambda e: self.default_action() or "break")
        self.p_tree.bind("<<TreeviewSelect>>", lambda e: self.update_part_buttons())
        attach_menu(self, self.p_tree, self.part_menu)
        keys(self.p_tree, enter=self.default_action, delete=self.toggle_item, new=lambda: self.file_items("nc"))

    def _sel(self):
        s = self.p_tree.selection()
        return s[0] if s else None

    def _sel_item(self, kind=None, quiet=False):
        """(filename, row) for a selected setup / program."""
        iid = self._sel()
        r = self._rows.get(iid) if iid else None
        if not r or (kind and r["kind"] != kind):
            if not quiet:
                self.say({"nc": "Select an NC program first.", "setup": "Select a CAM setup first."}.get(
                    kind, "Select a CAM setup or NC program first."), "warn")
            return None, None
        return iid, r

    def _sel_part(self):
        iid = self._sel()
        if not iid:
            return self.selected_part()
        if iid.startswith("P:"):
            return iid[2:]
        r = self._rows.get(iid)
        return r["part_file"] if r else None

    def load_parts(self):
        tv = self.p_tree
        keep, opened = self._sel(), {i for i in tv.get_children("") if tv.item(i, "open")} if tv.get_children("") else set()
        tv.delete(*tv.get_children(""))
        self._rows = {}
        ok, by_part = self.guard(C.items_by_part, self.p_old.get())
        if not ok:
            return
        q = self.p_search.get().strip().lower()
        parts = set(by_part)
        if self.p_all.get():
            parts |= {n for n, i in E.INDEX.files.items() if i.ext in E.CAD_MODEL_EXTS and not i.is_library}
        sel_part = self.selected_part()
        if sel_part and sel_part in E.INDEX.files:
            parts.add(sel_part)
        n_items = n_out = 0
        for i, part in enumerate(sorted(parts, key=lambda p: ((E.INDEX.records.get(p) or {}).get("part_number") or p).lower())):
            rec = E.INDEX.records.get(part) or {}
            rows = by_part.get(part, [])
            label = f"{rec.get('part_number') or ''}  {E.display_part_name(part, rec)}".strip()
            hay = (label + " " + part + " " + " ".join(r["filename"] + " " + (r.get("machine") or "") for r in rows)).lower()
            if q and q not in hay:
                continue
            outd = sum(1 for r in rows if r["state"] == "outdated")
            n_out += outd
            pid = "P:" + part
            summary = (f"{sum(1 for r in rows if r['kind'] == 'setup')} setup(s), "
                       f"{sum(1 for r in rows if r['kind'] == 'nc')} program(s)") if rows else "No CAM yet"
            tv.insert("", "end", iid=pid, text=f"◆  {label}", open=(pid in opened or not opened or pid == keep),
                      tags=("part",) + (("odd",) if i % 2 else ()),
                      values=(rec.get("revision") or "", "Part", summary + (f" · {outd} out of date" if outd else ""),
                              "", "", "", "", ""))
            for r in sorted(rows, key=lambda x: (x["kind"] != "setup", x.get("op") or "", x.get("created") or "")):
                ed = C.editing_state(r["filename"]) if r["kind"] == "setup" else None
                state = r["state_text"] + {"open": " · open for editing", "changed": " · edited, not saved back"}.get(ed, "")
                tv.insert(pid, "end", iid=r["filename"],
                          text=f"{'⚙' if r['kind'] == 'setup' else '▸'}  {r.get('op') or ''}   {r['filename']}",
                          tags=(r["state"],) + (("editing",) if ed == "changed" else ()),
                          values=(r.get("part_rev") or "", C.KIND_LABEL[r["kind"]], state, r.get("machine") or "—",
                                  r.get("post") or "—",
                                  f"{r.get('cam_app') or ''} {r.get('cam_version') or ''}".strip() or "—",
                                  (r.get("created") or "")[:16], r.get("created_by") or ""))
                self._rows[r["filename"]] = r
                n_items += 1
        self.nb.tab(0, text=f"  Parts & CAM ({n_items})  " + ("⚠ " if n_out else ""))
        if keep and tv.exists(keep):
            tv.selection_set(keep)
            tv.see(keep)
        if n_out:
            self.say(f"{n_out} CAM file(s) are out of date with their part.", "warn")
        elif not parts:
            self.say("No CAM files yet. Select a part and use Open part in CAM, or File NC program.", "info")
        self.update_part_buttons()

    def update_part_buttons(self):
        iid = self._sel()
        r = self._rows.get(iid) if iid else None
        kind = r["kind"] if r else None
        state = {"send": kind == "nc", "edit": kind == "setup",
                 "save": kind == "setup" and C.editing_state(iid) is not None,
                 "copy": bool(r), "toggle": bool(r), "folder": bool(r)}
        for k, b in self.p_buttons.items():
            b.state(["!disabled"] if state[k] else ["disabled"])

    def part_menu(self, iid):
        part = self._sel_part()
        has_part = bool(part and part in E.INDEX.files)
        file_items = [("Open part in CAM…", self.open_part_in_cam, has_part),
                      ("File CAM setup…", lambda: self.file_items("setup"), has_part),
                      ("File NC program…", lambda: self.file_items("nc"), has_part)]
        if not iid:
            return [("File NC program…", lambda: self.file_items("nc"), True),
                    ("File CAM setup…", lambda: self.file_items("setup"), True), None,
                    ("Refresh   F5", self.reload_all, True)]
        if iid.startswith("P:"):
            return file_items + [None, ("Show part in Atelier PDM", lambda: self.show_part(part), True),
                                 ("Show part's CAM folders", lambda: self.show_part_folders(part), has_part), None,
                                 ("Refresh   F5", self.reload_all, True)]
        r = self._rows[iid]
        sup = r["status"] == "superseded"
        if r["kind"] == "setup":
            ed = C.editing_state(iid)
            first = [("Open setup for editing   Enter", self.edit_setup, True),
                     ("Save setup back to the vault", self.save_setup, ed is not None),
                     ("Discard my working copy", self.discard_setup, ed is not None)]
        else:
            first = [("Send to machine", self.send_program, True)]
        return first + [("Open a copy", self.open_copy, True), ("Open with CAM program…", self.open_with, True), None,
                        ("Restore (make current)" if sup else "Supersede   Del", self.toggle_item, True),
                        ("Copy file name", lambda: self.copy_text(iid), True),
                        ("Show in folder", self.show_item, True), ("History", lambda: self.history(iid), True),
                        None] + file_items

    def default_action(self):
        iid = self._sel()
        if not iid:
            return
        if iid.startswith("P:"):
            self.p_tree.item(iid, open=not self.p_tree.item(iid, "open"))
            return
        r = self._rows.get(iid)
        if r and r["kind"] == "setup":
            self.edit_setup()
        elif r:
            self.open_copy()

    def reveal(self, filename, action=None):
        self.nb.select(0)
        if not self.p_tree.exists(filename):
            self.p_old.set(True)
            self.load_parts()
        if self.p_tree.exists(filename):
            self.p_tree.see(filename)
            self.p_tree.selection_set(filename)
            self.p_tree.focus(filename)
            self.update_part_buttons()
            {"edit": self.edit_setup, "send": self.send_program, "copy": self.open_copy}.get(action, lambda: None)()

    def copy_text(self, text):
        self.clipboard_clear()
        self.clipboard_append(text)
        self.say(f"Copied: {text}")

    def show_part(self, part):
        for name in ("select_file", "reveal_file"):
            fn = getattr(self.ctx, name, None)
            if fn:
                fn(part)
                return
        info = E.INDEX.files.get(part)
        if info:
            open_folder(os.path.dirname(info.path))

    def show_part_folders(self, part):
        ok, base = self.guard(C.project_folder_for, part)
        if ok:
            open_folder(base)

    def history(self, filename):
        S, t = self.S, self.ctx.t
        dlg = E.ThemedDialog(self.ctx, "History", "History", filename, width=620, resizable=True, parent=self)
        tv = make_tree(dlg.body, (("time", "Time", 130, False), ("user", "User", 90, False),
                                  ("action", "Action", 150, False), ("rev", "Rev", 50, False),
                                  ("c", "Details", 260, True)), height=10)
        try:
            rows = E.get_audit(filename)
        except Exception:                                              # noqa: BLE001
            rows = []
        for i, row in enumerate(rows):
            tv.insert("", "end", values=row, tags=("odd",) if i % 2 else ())
        tv.tag_configure("odd", background=t["row_alt"])
        ttk.Button(dlg.buttons, text="Close", command=dlg.cancel).pack(side="right")
        dlg.geometry(f"{S(700)}x{S(420)}")
        dlg.show()

    # ---- filing setups and programs
    def file_items(self, kind):
        if kind == "nc":
            exts = C.nc_exts()
            types = [("NC programs", " ".join("*" + e for e in exts)), ("All files", "*.*")]
            title = "Choose the posted program(s)"
        else:
            types = [("All files", "*.*")]
            title = "Choose the CAM setup / job file saved from your CAM program"
        start = recall("cam_last_dir") or C.working_dir()
        paths = filedialog.askopenfilenames(parent=self, title=title, filetypes=types,
                                            initialdir=start if os.path.isdir(start) else None)
        if not paths:
            return
        remember("cam_last_dir", os.path.dirname(paths[0]))
        parts = sorted((n for n, i in E.INDEX.files.items() if i.ext in E.CAD_MODEL_EXTS and not i.is_library),
                       key=str.lower)
        if not parts:
            self.say("There are no parts in the vault to file against.", "warn")
            return
        machines = [m["name"] for m in C.list_machines()]
        app = recall("cam_last_app", "CamCraft")
        default_part = self._sel_part() or recall("cam_last_part")
        default_part = default_part if default_part in parts else parts[0]
        one = len(paths) == 1

        def posts_for(vars_, widgets):
            if not all(k in vars_ for k in ("machine", "cam_app", "post")):
                return                                  # called while the form is still being built
            m, a = vars_["machine"].get(), vars_["cam_app"].get()
            names = [p["name"] for p in C.list_posts(m) if p["app"] == a] if m else []
            widgets["post"].configure(values=names)
            if vars_["post"].get() not in names:
                vars_["post"].set(names[0] if names else "")

        def validate(v):
            if v["part"] not in parts:
                raise ValueError("Choose a part from the list.")
            if v.get("machine") and v["machine"] not in machines:
                raise ValueError("Choose a machine from the list, or leave it blank.")
            return v

        what = "NC program" if kind == "nc" else "CAM setup"
        sub = os.path.basename(paths[0]) if one else f"{len(paths)} files - numbered OP1, OP2… in the order chosen."
        fields = [{"key": "part", "label": "Part", "kind": "choice", "values": parts, "initial": default_part,
                   "wide": True},
                  {"key": "op", "label": "Operation", "initial": C.next_op(default_part, kind) if one else "",
                   "hint": "e.g. OP1, OP2, SIDE B" if one else "Left blank: numbered automatically", "col": 0}]
        if kind == "nc":
            fields.append({"key": "machine", "label": "Machine", "kind": "choice", "values": [""] + machines,
                           "initial": recall("cam_last_machine", "") if recall("cam_last_machine", "") in machines else "",
                           "col": 1, "on_change": posts_for})
        fields += [{"key": "cam_app", "label": "CAM program", "kind": "choice", "values": C.CAM_APPS, "initial": app,
                    "col": 0, "on_change": posts_for if kind == "nc" else None},
                   {"key": "cam_version", "label": "CAM program version", "initial": recall(f"cam_version_{app}", ""),
                    "hint": "Recorded with the file, e.g. 0.19.0", "col": 1}]
        if kind == "nc":
            fields.append({"key": "post", "label": "Post", "kind": "combo", "values": [], "col": 0})
        fields += [{"key": "tool_library", "label": "Tool library used", "kind": "choice",
                    "values": [""] + C.list_libraries(), "initial": recall("cam_last_library", ""), "col": 1},
                   {"key": "note", "label": "Note (optional)", "wide": True}]
        fields = [{k: v for k, v in f.items() if v is not None} for f in fields]
        res = form_dialog(self, f"File {what}", f"File {what}", sub, fields, validate, f"File {what}", width=600,
                          ok_style="Success.TButton")
        if not res:
            return
        remember("cam_last_app", res["cam_app"])
        remember("cam_last_part", res["part"])
        if kind == "nc":
            remember("cam_last_machine", res["machine"])
        remember(f"cam_version_{res['cam_app']}", res["cam_version"])
        remember("cam_last_library", res["tool_library"])
        done = []
        for i, p in enumerate(paths):
            op = res["op"] if (one or (res["op"] and i == 0)) else ""
            if kind == "nc":
                ok, name = self.guard(C.add_program, p, res["part"], op, res["machine"], res["post"], res["cam_app"],
                                      res["cam_version"], res["tool_library"], res["note"])
            else:
                ok, name = self.guard(C.add_setup, p, res["part"], op, res["cam_app"], res["cam_version"],
                                      res["tool_library"], res["note"])
            if not ok:
                break
            done.append(name)
        if done:
            self.after_vault_change()
            self.reveal(done[-1])
            self.say(f"Filed {len(done)} {what}(s): {', '.join(done[:3])}{'…' if len(done) > 3 else ''}")

    # ---- opening in CAM programs
    def _cam_app_choice(self, title, heading, sub, initial=None):
        res = form_dialog(self, title, heading, sub, [
            {"key": "app", "label": "CAM program", "kind": "choice", "values": C.CAM_APPS[:-1],
             "initial": initial or recall("cam_last_app", "CamCraft")}], lambda v: v["app"], "Open")
        if res:
            remember("cam_last_app", res)
        return res

    def _launch(self, app, path):
        try:
            how = C.launch_cam(app, path)
        except LookupError:
            if not messagebox.askyesno(E.APP_NAME, f"{app} wasn't found on this PC.\n\nLocate {app}.exe now? It's "
                                       "remembered on this PC.", parent=self):
                return False
            exe = filedialog.askopenfilename(parent=self, title=f"Locate {app}.exe",
                                             filetypes=[(app, "*.exe"), ("All files", "*.*")])
            if not exe:
                return False
            C.remember_cam_exe(app, exe)
            ok, how = self.guard(C.launch_cam, app, path)
            if not ok:
                return False
        except OSError as e:
            self.say(f"Couldn't start {app}: {e}", "error")
            return False
        self.say(f"Opening {os.path.basename(path) if path else app} in {app}.")
        return True

    def open_part_in_cam(self):
        part = self._sel_part()
        if not part or part not in E.INDEX.files:
            self.say("Select a part first (tick 'All parts' to list every part).", "warn")
            return
        app = self._cam_app_choice("Open part in CAM", f"Open {part} in CAM",
                                   "A STEP of the part's current revision is put in your working folder and opened "
                                   "in the CAM program. Save the CAM job there, then use File CAM setup.")
        if not app:
            return
        ok, model = self.guard(C.part_model_for_cam, part)
        if ok and self._launch(app, model):
            remember("cam_last_dir", C.working_dir())
            remember("cam_last_part", part)

    def open_with(self):
        iid, r = self._sel_item()
        if not r:
            return
        app = self._cam_app_choice("Open with", f"Open {iid}", "Opens a working copy.", r.get("cam_app"))
        if not app:
            return
        if r["kind"] == "setup":
            ok, p = self.guard(C.open_setup_for_edit, iid)
        else:
            ok, p = self.guard(C.open_local_copy, iid)
        if ok:
            self._launch(app, p)
            self.load_parts()

    def edit_setup(self):
        iid, r = self._sel_item("setup")
        if not r:
            return
        if r["state"] != "current" and not messagebox.askyesno(
                "Open setup", f"{r['state_text']}.\n\nOpen it anyway? When you save it back it's filed against the "
                "part's current revision.", icon="warning", parent=self):
            return
        ok, p = self.guard(C.open_setup_for_edit, iid)
        if ok:
            self._launch(r.get("cam_app") or recall("cam_last_app", "CamCraft"), p)
            self.load_parts()
            self.say(f"Editing {os.path.basename(p)}. Save it in the CAM program, then 'Save setup back'.")

    def save_setup(self):
        iid, r = self._sel_item("setup")
        if not r:
            return
        st = C.editing_state(iid)
        if st is None:
            self.say("This setup isn't open for editing on this PC.", "warn")
            return
        if st == "open" and not messagebox.askyesno("Save setup back", "The working copy hasn't changed since you "
                                                    "opened it. Save it back anyway?", parent=self):
            return
        note = form_dialog(self, "Save setup back", "Save setup back", "What changed? (goes in the history)",
                           [{"key": "note", "label": "Note (optional)"}], lambda v: v["note"] or " ", "Save back",
                           ok_style="Success.TButton")
        if note is None:
            return
        ok, name = self.guard(C.save_setup_back, iid, note.strip())
        if ok:
            self.after_vault_change()
            self.reveal(name)
            self.say(f"Saved back as {name}." + (" The part had a new revision, so the old setup is superseded."
                                                 if name != iid else ""))

    def discard_setup(self):
        iid, r = self._sel_item("setup")
        if not r:
            return
        if C.editing_state(iid) == "changed" and not messagebox.askyesno(
                "Discard", "Your working copy has changes that aren't in the vault. Stop tracking it?\n\nThe file "
                "stays in your working folder.", icon="warning", parent=self):
            return
        C.close_setup(iid)
        self.load_parts()

    def send_program(self):
        iid, r = self._sel_item("nc")
        if not r:
            return
        try:
            dst = C.send_to_machine(iid)
        except PermissionError as e:
            if not messagebox.askyesno("Send to machine", f"{e}.\n\nThe part has changed since this program was "
                                       "made. Send it anyway?", icon="warning", parent=self):
                return
            ok, dst = self.guard(C.send_to_machine, iid, True)
            if not ok:
                return
        except (OSError, ValueError) as e:
            self.say(str(e), "error")
            return
        self.say(f"Sent to {dst}")

    def open_copy(self):
        iid, r = self._sel_item()
        if r:
            ok, p = self.guard(C.open_local_copy, iid)
            if ok:
                E.open_path(p)
                self.say(f"Opened a copy: {p}  (changes there aren't saved to the vault)")

    def toggle_item(self):
        iid, r = self._sel_item()
        if not r:
            return
        new = "current" if r["status"] == "superseded" else "superseded"
        if new == "superseded" and not messagebox.askyesno("Supersede", f"Mark {iid} as superseded?\n\nIt's kept "
                                                           "and can be restored ('Show superseded').", parent=self):
            return
        self.guard(C.set_program_status, iid, new)
        self.after_vault_change()

    def show_item(self):
        iid, r = self._sel_item()
        if r:
            ok, p = self.guard(C.item_path, iid)
            if ok:
                if sys.platform == "win32" and os.path.exists(p):
                    import subprocess
                    subprocess.Popen(["explorer", "/select,", os.path.normpath(p)])
                else:
                    open_folder(os.path.dirname(p))

    # ================================================================== tool libraries
    def _build_libraries(self):
        S = self.S
        page = ttk.Frame(self.nb, style="Dialog.TFrame", padding=(0, S(10), 0, 0))
        self.nb.add(page, text="  Tool libraries  ")
        pw = ttk.PanedWindow(page, orient="horizontal")
        pw.pack(fill="both", expand=True)
        left = ttk.Frame(pw, style="Dialog.TFrame")
        right = ttk.Frame(pw, style="Dialog.TFrame")
        pw.add(left, weight=1)
        pw.add(right, weight=4)

        ttk.Label(left, text="LIBRARIES", style="CardSection.TLabel").pack(side="top", anchor="w")
        ttk.Label(left, text="Exports go to your working folder\\CAM Exports. PhysCAD imports the Fusion 360 / HSM "
                             "file; FreeCAD takes the tool-bit folder.", style="CardMuted.TLabel",
                  wraplength=S(240), justify="left").pack(side="bottom", anchor="w", pady=(S(8), 0))
        lb = bottom_bar(left, S)
        ttk.Button(lb, text="New…", command=self.new_library).pack(side="left")
        ttk.Button(lb, text="Import…", command=self.import_library).pack(side="left", padx=(S(6), 0))
        self.exp_mb = ttk.Menubutton(lb, text="Export ▾")
        self.exp_mb.pack(side="left", padx=(S(6), 0))
        em = tk.Menu(self.exp_mb, tearoff=0, **menu_colors(self.ctx))
        for key, (label, _f, _k) in C.EXPORTS.items():
            em.add_command(label=label, command=lambda k=key: self.export_library(k))
        em.add_separator()
        em.add_command(label="Open library folder", command=lambda: self.lib and open_folder(C.library_dir(self.lib["name"])))
        self.exp_mb["menu"] = em
        self.l_tree = make_tree(left, (("name", "Library", 180, True), ("n", "Tools", 50, False)), height=14)
        self.l_tree.bind("<<TreeviewSelect>>", lambda e: self.open_library(
            self.l_tree.selection()[0] if self.l_tree.selection() else None))
        attach_menu(self, self.l_tree, self.library_menu)
        keys(self.l_tree, f2=self.rename_library, delete=self.delete_library, new=self.new_library)

        hr = ttk.Frame(right, style="Dialog.TFrame")
        hr.pack(side="top", fill="x", padx=(S(12), 0))
        self.lib_title = ttk.Label(hr, text="Select or create a library", style="CardTitle.TLabel")
        self.lib_title.pack(side="left")
        self.lib_info = ttk.Label(hr, text="", style="CardMuted.TLabel")
        self.lib_info.pack(side="right")

        calc = ttk.Frame(right, style="Card.TFrame", padding=S(10))
        calc.pack(side="bottom", fill="x", padx=(S(12), 0), pady=(S(10), 0))
        ttk.Label(calc, text="FEEDS & SPEEDS ON A MACHINE", style="CardSection.TLabel").grid(row=0, column=0, columnspan=6,
                                                                                         sticky="w")
        ttk.Label(calc, text="Machine", style="CardMuted.TLabel").grid(row=1, column=0, sticky="w", pady=(S(6), 0))
        self.c_machine = tk.StringVar(value=recall("cam_calc_machine", ""))
        self.c_mcombo = ttk.Combobox(calc, textvariable=self.c_machine, state="readonly", width=24)
        self.c_mcombo.grid(row=1, column=1, sticky="w", padx=(S(8), S(16)), pady=(S(6), 0))
        ttk.Label(calc, text="Material", style="CardMuted.TLabel").grid(row=1, column=2, sticky="w", pady=(S(6), 0))
        self.c_mat = tk.StringVar(value=recall("cam_calc_material", ""))
        self.c_matcombo = ttk.Combobox(calc, textvariable=self.c_mat, state="readonly", width=20)
        self.c_matcombo.grid(row=1, column=3, sticky="w", padx=(S(8), S(16)), pady=(S(6), 0))
        self.c_out = ttk.Label(calc, text="", style="CardValue.TLabel", justify="left")
        self.c_out.grid(row=2, column=0, columnspan=6, sticky="w", pady=(S(8), 0))
        for cb in (self.c_mcombo, self.c_matcombo):
            cb.bind("<<ComboboxSelected>>", lambda e: self.update_calc(remember_choice=True))

        tb = bottom_bar(right, S, padx=(S(12), 0))
        for text, cmd, style in (("＋ Add tool", self.add_tool, "Accent.TButton"), ("Edit…", self.edit_tool, "TButton"),
                                 ("Duplicate", self.duplicate_tool, "TButton"), ("Delete", self.delete_tool, "TButton"),
                                 ("Attach CAD…", self.attach_tool_cad, "TButton"),
                                 ("Holders…", self.edit_holders, "TButton"), ("Materials…", self.edit_materials, "TButton")):
            ttk.Button(tb, text=text, style=style, command=cmd).pack(side="left", padx=(0, S(6)))
        body = ttk.Frame(right, style="Dialog.TFrame")
        body.pack(side="top", fill="both", expand=True, padx=(S(12), 0), pady=(S(6), 0))
        self.t_tree = make_tree(body, (("t", "T", 46, False), ("type", "Type", 130, False),
                                       ("desc", "Description", 220, True), ("size", "Size", 110, False),
                                       ("stick", "Stick-out", 70, False), ("holder", "Holder", 130, False),
                                       ("mats", "Feeds & speeds for", 200, True), ("cad", "CAD", 40, False)), height=10)
        self.tag_colours(self.t_tree)
        self.t_tree.bind("<Double-1>", lambda e: (self.edit_tool() if self.t_tree.identify_row(e.y) else self.add_tool(),
                                                  "break")[1])
        self.t_tree.bind("<<TreeviewSelect>>", lambda e: self.update_calc())
        attach_menu(self, self.t_tree, self.tool_menu)
        keys(self.t_tree, enter=self.edit_tool, delete=self.delete_tool, new=self.add_tool, dup=self.duplicate_tool,
             f2=self.edit_tool)

    def library_menu(self, iid):
        base = [("New library…   Ctrl+N", self.new_library, True), ("Import…", self.import_library, True)]
        if not iid:
            return base
        exports = [(f"Export: {label}", lambda k=key: self.export_library(k), True)
                   for key, (label, _f, _k) in C.EXPORTS.items()]
        return base + [None, ("Rename…   F2", self.rename_library, True), ("Duplicate…", self.duplicate_library, True),
                       ("Delete…   Del", self.delete_library, True), None] + exports + [
            None, ("Open library folder", lambda: open_folder(C.library_dir(iid)), True),
            ("Copy name", lambda: self.copy_text(iid), True)]

    def tool_menu(self, iid):
        if not self.lib:
            return [("New library…", self.new_library, True)]
        add = [("Add tool…   Ctrl+N", self.add_tool, True)]
        if not iid:
            return add + [None, ("Holders…", self.edit_holders, True), ("Materials…", self.edit_materials, True)]
        t = next((x for x in self.lib["tools"] if x["id"] == iid), None)
        cad = [("Open CAD: " + os.path.basename(c), lambda c=c: self.open_lib_file(c), True) for c in (t or {}).get("cad", [])]
        return [("Edit…   Enter", self.edit_tool, True), ("Duplicate   Ctrl+D", self.duplicate_tool, True),
                ("Delete   Del", self.delete_tool, True), None, ("Attach CAD…", self.attach_tool_cad, True)] + cad + [
            None] + add + [("Holders…", self.edit_holders, True), ("Materials…", self.edit_materials, True)]

    def open_lib_file(self, rel):
        p = os.path.join(C.library_dir(self.lib["name"]), rel)
        dst = os.path.join(C.working_dir(), "Tool CAD", os.path.basename(rel))
        ok, _ = self.guard(C.vcopy, p, dst)
        if ok:
            E.open_path(dst)

    def load_library_list(self):
        tv = self.l_tree
        keep = self.lib["name"] if self.lib else recall("cam_last_open_library")
        tv.delete(*tv.get_children())
        for name in C.list_libraries():
            try:
                n = len(C.load_library(name)["tools"])
            except ValueError:
                n = "?"
            tv.insert("", "end", iid=name, values=(name, n))
        if keep and tv.exists(keep):
            tv.selection_set(keep)
        elif tv.get_children():
            tv.selection_set(tv.get_children()[0])
        else:
            self.open_library(None)

    def open_library(self, name):
        if not name:
            self.lib = self.lib_rev = None
            self.lib_title.configure(text="Select or create a library")
            self.lib_info.configure(text="")
            self.t_tree.delete(*self.t_tree.get_children())
            self.update_calc()
            return
        ok, lib = self.guard(C.load_library, name)
        if not ok:
            return
        self.lib, self.lib_rev = lib, lib.get("revision", 0)
        remember("cam_last_open_library", name)
        self.lib_title.configure(text=lib["name"])
        self.lib_info.configure(text=f"rev {self.lib_rev} · {lib.get('modified', '')[:16]} · {lib.get('modified_by', '')}")
        self.load_tools()

    def load_tools(self, select=None):
        tv, lib = self.t_tree, self.lib
        tv.delete(*tv.get_children())
        if not lib:
            return
        holders = {h["id"]: h for h in lib.get("holders", [])}
        for i, t in enumerate(sorted(lib["tools"], key=lambda x: int(x.get("number") or 0))):
            h = holders.get(t.get("holder"))
            mats = ", ".join(p.get("material", "") for p in t.get("presets", [])) or "—"
            tv.insert("", "end", iid=t["id"], tags=("odd",) if i % 2 else (), values=(
                f"T{t['number']}", C.TOOL_TYPES.get(t["type"], (t["type"],))[0], t.get("description", ""), C.summary(t),
                C.fmt(t.get("geometry", {}).get("stickout")) or "—", h["description"] if h else "—", mats,
                len(t.get("cad", [])) + (len(h.get("cad", [])) if h else 0) or ""))
        if select and tv.exists(select):
            tv.selection_set(select)
            tv.see(select)
        self.c_matcombo.configure(values=lib.get("materials", []))
        self.c_mcombo.configure(values=[""] + [m["name"] for m in C.list_machines()])
        self.update_calc()

    def save_lib(self, msg, select=None):
        ok, rev = self.guard(C.save_library, self.lib, self.lib_rev)
        if ok:
            self.lib_rev = rev
            self.lib_info.configure(text=f"rev {rev} · just now · {E.USER_NAME}")
            C.log("Tool Library Saved", self.lib["name"], msg)
            self.load_tools(select)
            if self.l_tree.exists(self.lib["name"]):
                self.l_tree.set(self.lib["name"], "n", len(self.lib["tools"]))
            self.say(msg)
        return ok

    def _sel_tool(self):
        sel = self.t_tree.selection()
        if not self.lib:
            self.say("Open a library first.", "warn")
            return None
        if not sel:
            self.say("Select a tool first.", "warn")
            return None
        return next((t for t in self.lib["tools"] if t["id"] == sel[0]), None)

    def _sel_library(self):
        s = self.l_tree.selection()
        if not s:
            self.say("Select a library first.", "warn")
            return None
        return s[0]

    def new_library(self):
        res = form_dialog(self, "New tool library", "New tool library",
                          "One library per machine or per material works well, e.g. 'Tormach - aluminium'.",
                          [{"key": "name", "label": "Library name"}], lambda v: C.create_library(v["name"]), "Create")
        if res:
            self.load_library_list()
            self.l_tree.selection_set(res["name"])
            self.say(f"Created '{res['name']}'.")

    def rename_library(self):
        name = self._sel_library()
        if not name:
            return
        res = form_dialog(self, "Rename library", f"Rename '{name}'", "", [{"key": "name", "label": "New name",
                                                                           "initial": name}],
                          lambda v: C.rename_library(name, v["name"]), "Rename")
        if res:
            self.lib = None
            remember("cam_last_open_library", res)
            self.load_library_list()
            self.say(f"Renamed to '{res}'.")

    def duplicate_library(self):
        name = self._sel_library()
        if not name:
            return

        def validate(v):
            new = C.create_library(v["name"])
            src = C.load_library(name)
            src.update(name=new["name"], revision=new["revision"])
            C.save_library(src, new["revision"])
            for sub in ("Tool CAD", "Holder CAD"):
                a, b = os.path.join(C.library_dir(name), sub), os.path.join(C.library_dir(new["name"]), sub)
                if os.path.isdir(a):
                    for n in os.listdir(a):
                        C.shutil.copy2(os.path.join(a, n), os.path.join(b, n))
            return new["name"]
        res = form_dialog(self, "Duplicate library", f"Duplicate '{name}'", "Tools, holders, CAD files and feeds & "
                          "speeds are copied.", [{"key": "name", "label": "Name for the copy",
                                                   "initial": f"{name} (copy)"}], validate, "Duplicate")
        if res:
            self.lib = None
            remember("cam_last_open_library", res)
            self.load_library_list()
            self.say(f"Copied to '{res}'.")

    def delete_library(self):
        name = self._sel_library()
        if name and messagebox.askyesno("Delete library", f"Delete the tool library '{name}'?\n\nIt's moved to "
                                        "_CAM\\Deleted in the vault, not erased.", icon="warning", parent=self):
            ok, _ = self.guard(C.delete_library, name)
            if ok:
                self.lib = None
                self.load_library_list()
                self.say(f"Deleted '{name}'.")

    def import_library(self):
        p = filedialog.askopenfilename(parent=self, title="Import a tool library", filetypes=[
            ("Tool libraries", "*.json *.tools *.csv"), ("Fusion 360 / HSM", "*.json *.tools"), ("Atelier CSV", "*.csv")])
        if not p:
            return
        res = form_dialog(self, "Import tool library", "Import tool library", os.path.basename(p),
                          [{"key": "name", "label": "Name for the new library",
                            "initial": C.safe_name(os.path.splitext(os.path.basename(p))[0])}],
                          lambda v: C.import_library(p, v["name"]), "Import")
        if res:
            lib, msg = res
            self.load_library_list()
            self.l_tree.selection_set(lib["name"])
            self.say(msg)

    def export_library(self, key):
        if not self.lib:
            self.say("Open a library first.", "warn")
            return
        out = os.path.join(E.LOCAL_WORKSPACE, "CAM Exports")
        machine = C.get_machine(self.c_machine.get()) if key == "csv" else None
        ok, path = self.guard(C.export_library, self.lib, key, out, machine)
        if ok:
            self.say(f"Exported: {path}")
            open_folder(out)

    def edit_materials(self):
        if not self.lib:
            return

        def materials_from(v):
            mats = [m.strip() for m in v["m"].splitlines() if m.strip()]
            if not mats:
                raise ValueError("Add at least one material.")
            return mats
        res = form_dialog(self, "Materials", "Materials", "One per line. Each tool can have feeds & speeds for any of "
                          "these.", [{"key": "m", "label": "Materials", "kind": "text", "height": 10,
                                      "initial": "\n".join(self.lib.get("materials", []))}],
                          materials_from, "Save")
        if res:
            self.lib["materials"] = list(dict.fromkeys(res))
            self.save_lib("Materials saved.")

    # ---- tools
    def add_tool(self):
        if not self.lib:
            self.say("Open or create a library first.", "warn")
            return
        t = C.new_tool(C.next_tool_number(self.lib))
        res = self.tool_dialog(t, new=True)
        if res:
            self.lib["tools"].append(res)
            if not self.save_lib(f"Added T{res['number']}.", res["id"]):
                self.lib["tools"].remove(res)

    def edit_tool(self):
        t = self._sel_tool()
        if not t:
            return
        res = self.tool_dialog(t)
        if res:
            i = self.lib["tools"].index(t)
            self.lib["tools"][i] = res
            if not self.save_lib(f"Saved T{res['number']}.", res["id"]):
                self.lib["tools"][i] = t

    def duplicate_tool(self):
        t = self._sel_tool()
        if not t:
            return
        d = copy.deepcopy(t)
        d.update(id=uuid.uuid4().hex, number=C.next_tool_number(self.lib), cad=[])
        d["description"] = (t.get("description") or "") + " (copy)"
        self.lib["tools"].append(d)
        self.save_lib(f"Duplicated as T{d['number']}.", d["id"])

    def delete_tool(self):
        t = self._sel_tool()
        if t and messagebox.askyesno("Delete tool", f"Delete T{t['number']} {t.get('description', '')}?\n\nIts CAD "
                                     "files stay in the library folder.", icon="warning", parent=self):
            self.lib["tools"].remove(t)
            if not self.save_lib(f"Deleted T{t['number']}."):
                self.lib["tools"].append(t)

    def attach_tool_cad(self):
        t = self._sel_tool()
        if not t:
            return
        p = filedialog.askopenfilename(parent=self, title=f"CAD model for T{t['number']}", filetypes=[
            ("CAD files", " ".join("*" + e for e in C.CAD_EXTS)), ("All files", "*.*")])
        if p:
            ok, rel = self.guard(C.attach_cad, self.lib, t, p, "tool")
            if ok:
                self.save_lib(f"Attached {os.path.basename(rel)} to T{t['number']}.", t["id"])

    def tool_dialog(self, tool, new=False):
        """Full tool editor: identity, geometry, holder, presets. Returns the edited copy or None."""
        S, t = self.S, copy.deepcopy(tool)
        holders = self.lib.get("holders", [])
        hnames = ["(none)"] + [h["description"] or h["id"][:6] for h in holders]
        dlg = E.ThemedDialog(self.ctx, "Tool", "New tool" if new else f"T{tool['number']}",
                             "Lengths in mm. Feeds & speeds are kept per material.", width=760, resizable=True,
                             parent=self)
        top = ttk.Frame(dlg.body, style="Dialog.TFrame")
        top.pack(fill="x")
        v = {}

        def field(parent, key, label, r, c, initial, kind="entry", values=(), width=14):
            ttk.Label(parent, text=label, style="CardMuted.TLabel").grid(row=r, column=c, sticky="w", padx=(0, S(6)),
                                                                         pady=S(2))
            var = tk.StringVar(value="" if initial is None else str(initial))
            if kind == "entry":
                w = ttk.Entry(parent, textvariable=var, width=width)
            else:
                w = ttk.Combobox(parent, textvariable=var, values=list(values), state="readonly", width=width)
            w.grid(row=r, column=c + 1, sticky="ew", padx=(0, S(16)), pady=S(2))
            v[key] = var
            return w

        labels = {k: x[0] for k, x in C.TOOL_TYPES.items()}
        first = field(top, "number", "Tool number", 0, 0, t["number"], width=8)
        field(top, "type", "Type", 0, 2, labels.get(t["type"], labels["flat"]), "combo", labels.values(), 22)
        field(top, "description", "Description", 1, 0, t.get("description"), width=30)
        hcur = next((h["description"] or h["id"][:6] for h in holders if h["id"] == t.get("holder")), "(none)")
        field(top, "holder", "Holder", 1, 2, hcur, "combo", hnames, 22)
        field(top, "vendor", "Vendor", 2, 0, t.get("vendor"), width=30)
        field(top, "product_id", "Product code", 2, 2, t.get("product_id"), width=22)
        ttk.Label(dlg.body, text="GEOMETRY", style="CardSection.TLabel").pack(anchor="w", pady=(S(10), S(2)))
        geo = ttk.Frame(dlg.body, style="Dialog.TFrame")
        geo.pack(fill="x")
        for i, (k, label, unit) in enumerate(C.GEOMETRY_FIELDS):
            field(geo, "g_" + k, f"{label} ({unit})" if unit else label, i // 3, (i % 3) * 2,
                  C.fmt(t["geometry"].get(k), 4), width=9)

        ttk.Label(dlg.body, text="FEEDS & SPEEDS", style="CardSection.TLabel").pack(anchor="w", pady=(S(10), S(2)))
        presets = t.setdefault("presets", [])
        err = ttk.Label(dlg.body, text="", style="CardDanger.TLabel")
        err.pack(side="bottom", anchor="w", pady=(S(4), 0))
        v["notes"] = tk.StringVar(value=t.get("notes", ""))
        ttk.Entry(dlg.body, textvariable=v["notes"]).pack(side="bottom", fill="x", ipady=S(1))
        ttk.Label(dlg.body, text="Notes", style="CardMuted.TLabel").pack(side="bottom", anchor="w", pady=(S(8), 0))
        prow = bottom_bar(dlg.body, S, (4, 0))
        ptv = make_tree(dlg.body, (("mat", "Material", 120, True), ("vc", "Vc m/min", 70, False),
                                   ("fz", "fz mm", 60, False), ("rpm", "rpm", 70, False), ("feed", "Feed mm/min", 90, False),
                                   ("plunge", "Plunge", 60, False), ("ap", "ap", 50, False), ("ae", "ae", 50, False),
                                   ("cool", "Coolant", 80, False)), height=5)

        def geom_now():
            g = {}
            for k, _l, _u in C.GEOMETRY_FIELDS:
                n = C.num(v["g_" + k].get())
                if n is not None:
                    g[k] = int(n) if k == "flutes" else n
            return g

        def type_key():
            lab = v["type"].get()
            return next((k for k, x in labels.items() if x == lab), "flat")

        def refresh_presets():
            ptv.delete(*ptv.get_children())
            probe = dict(t, geometry=geom_now(), type=type_key())
            for i, p in enumerate(presets):
                cd = C.cutting_data(probe, p)
                ptv.insert("", "end", iid=str(i), values=(p.get("material", ""), C.fmt(p.get("vc")), C.fmt(p.get("fz"), 4),
                                                          C.fmt(cd["rpm"]), C.fmt(cd["feed"]), C.fmt(cd["plunge"]),
                                                          C.fmt(p.get("ap")), C.fmt(p.get("ae")), p.get("coolant", "")))

        def edit_preset(idx=None):
            p = dict(presets[idx]) if idx is not None else {"coolant": "flood"}
            mats = self.lib.get("materials", [])

            def validate(val):
                if not val["material"]:
                    raise ValueError("Choose or type a material.")
                out = {"material": val["material"], "coolant": val["coolant"] or "flood"}
                for k, _l, _u in C.PRESET_FIELDS:
                    if val[k]:
                        n = C.num(val[k])
                        if n is None or n < 0:
                            raise ValueError(f"'{val[k]}' isn't a valid number.")
                        out[k] = n
                if out.get("vc") is None and out.get("rpm") is None:
                    raise ValueError("Give a cutting speed (Vc) or a spindle speed.")
                if out.get("fz") is None and out.get("feed") is None:
                    raise ValueError("Give a feed per tooth (fz) or a cutting feed.")
                return out
            fields = [{"key": "material", "label": "Material", "kind": "combo", "values": mats,
                       "initial": p.get("material", mats[0] if mats else ""), "wide": True}]
            for i, (k, label, unit) in enumerate(C.PRESET_FIELDS):
                fields.append({"key": k, "label": f"{label} ({unit})", "initial": C.fmt(p.get(k), 4), "col": i % 2})
            fields.append({"key": "coolant", "label": "Coolant", "kind": "choice", "values": C.COOLANTS,
                           "initial": p.get("coolant", "flood"), "col": 1})
            hint = ("Drills, reamers and taps: enter fz as feed per revolution." if type_key() in
                    ("drill", "spot", "reamer", "tap") else "rpm and feed are worked out from Vc and fz unless you fill them in.")
            res = form_dialog(self, "Feeds & speeds", "Feeds & speeds", hint, fields, validate, "Save", width=520,
                              parent=dlg)
            dlg.grab_set()
            if res:
                if idx is None:
                    presets.append(res)
                else:
                    presets[idx] = res
                if res["material"] not in self.lib.setdefault("materials", []):
                    self.lib["materials"].append(res["material"])
                refresh_presets()

        def sel_idx():
            s = ptv.selection()
            return int(s[0]) if s else None

        def remove():
            i = sel_idx()
            if i is not None:
                presets.pop(i)
                refresh_presets()

        def duplicate():
            i = sel_idx()
            if i is not None:
                presets.append(dict(presets[i]))
                edit_preset(len(presets) - 1)

        ttk.Button(prow, text="＋ Material", command=lambda: edit_preset()).pack(side="left")
        ttk.Button(prow, text="Edit", command=lambda: sel_idx() is not None and edit_preset(sel_idx())).pack(
            side="left", padx=(S(6), 0))
        ttk.Button(prow, text="Remove", command=remove).pack(side="left", padx=(S(6), 0))
        ptv.bind("<Double-1>", lambda e: (edit_preset(sel_idx()) if ptv.identify_row(e.y) else edit_preset(), "break")[1])
        attach_menu(self, ptv, lambda iid: [("Add material…", lambda: edit_preset(), True)] + (
            [None, ("Edit…", lambda: edit_preset(sel_idx()), True), ("Duplicate", duplicate, True),
             ("Remove", remove, True)] if iid else []))
        keys(ptv, enter=lambda: sel_idx() is not None and edit_preset(sel_idx()), delete=remove,
             new=lambda: edit_preset(), dup=duplicate)
        refresh_presets()

        def validate():
            n = C.num(v["number"].get())
            if not n or n != int(n) or n < 1:
                err.configure(text="The tool number must be a whole number from 1.")
                return E.INVALID
            if any(int(x["number"]) == int(n) and x["id"] != t["id"] for x in self.lib["tools"]):
                err.configure(text=f"T{int(n)} is already in this library.")
                return E.INVALID
            g = geom_now()
            if not g.get("diameter"):
                err.configure(text="Enter the cutting diameter.")
                return E.INVALID
            t.update(number=int(n), type=type_key(), description=v["description"].get().strip(),
                     vendor=v["vendor"].get().strip(), product_id=v["product_id"].get().strip(), geometry=g,
                     notes=v["notes"].get().strip(), presets=presets)
            hn = v["holder"].get()
            t["holder"] = next((h["id"] for h in holders if (h["description"] or h["id"][:6]) == hn), None)
            return t
        dlg.validate = validate
        dlg.add_buttons("Save tool", "Success.TButton")
        dlg.focus_widget = first
        sh = self.winfo_screenheight()
        dlg.geometry(f"{S(800)}x{min(S(720), sh - 90)}")
        return dlg.show()

    # ---- holders
    def edit_holders(self):
        if not self.lib:
            return
        S = self.S
        dlg = E.ThemedDialog(self.ctx, "Holders", "Tool holders", "Holders are shared by the tools in this library. "
                             "Add the holder's CAD model so CAM programs can check for collisions.", width=640,
                             resizable=True, parent=self)
        row = bottom_bar(dlg.body, S)
        tv = make_tree(dlg.body, (("d", "Holder", 220, True), ("v", "Vendor", 110, False),
                                  ("seg", "Profile segments", 110, False), ("cad", "CAD files", 140, True)), height=8)

        def load():
            tv.delete(*tv.get_children())
            for h in self.lib.get("holders", []):
                tv.insert("", "end", iid=h["id"], values=(h.get("description", ""), h.get("vendor", ""),
                                                          len(h.get("segments", [])),
                                                          ", ".join(os.path.basename(c) for c in h.get("cad", [])) or "—"))

        def sel():
            s = tv.selection()
            return next((h for h in self.lib["holders"] if s and h["id"] == s[0]), None)

        def edit(h=None):
            new = h is None
            h = h or C.new_holder()
            segs = "\n".join(f"{C.fmt(s.get('height'))} {C.fmt(s.get('lower_diameter'))} {C.fmt(s.get('upper_diameter'))}"
                             for s in h.get("segments", []))

            def validate(v):
                if not v["description"]:
                    raise ValueError("Give the holder a name, e.g. 'BT30 ER32 collet chuck'.")
                out = []
                for i, line in enumerate(x for x in v["segments"].splitlines() if x.strip()):
                    parts = [C.num(p) for p in line.replace(",", " ").split()]
                    if len(parts) != 3 or any(p is None or p < 0 for p in parts):
                        raise ValueError(f"Profile line {i + 1}: enter height, lower diameter and upper diameter.")
                    out.append({"height": parts[0], "lower_diameter": parts[1], "upper_diameter": parts[2]})
                h.update(description=v["description"], vendor=v["vendor"], product_id=v["product_id"],
                         gauge_length=C.num(v["gauge"]), segments=out)
                return h
            res = form_dialog(self, "Holder", "New holder" if new else "Edit holder", "", [
                {"key": "description", "label": "Name", "initial": h.get("description"), "wide": True},
                {"key": "vendor", "label": "Vendor", "initial": h.get("vendor"), "col": 0},
                {"key": "product_id", "label": "Product code", "initial": h.get("product_id"), "col": 1},
                {"key": "gauge", "label": "Gauge length (mm)", "initial": C.fmt(h.get("gauge_length")), "col": 0},
                {"key": "segments", "label": "Profile (optional) - one line per section, from the tool end up: "
                 "height  lower Ø  upper Ø", "kind": "text", "initial": segs, "height": 5, "wide": True},
            ], validate, "Save", width=560, parent=dlg)
            dlg.grab_set()
            if res:
                if new:
                    self.lib.setdefault("holders", []).append(res)
                if self.save_lib(f"Saved holder '{res['description']}'."):
                    load()

        def attach():
            h = sel()
            if not h:
                return
            p = filedialog.askopenfilename(parent=dlg, title="Holder CAD model", filetypes=[
                ("CAD files", " ".join("*" + e for e in C.CAD_EXTS)), ("All files", "*.*")])
            dlg.grab_set()
            if p:
                ok, rel = self.guard(C.attach_cad, self.lib, h, p, "holder")
                if ok and self.save_lib(f"Attached {os.path.basename(rel)} to '{h['description']}'."):
                    load()

        def delete():
            h = sel()
            if h and messagebox.askyesno("Delete holder", f"Delete '{h['description']}'? Tools using it are unlinked.",
                                         parent=dlg):
                self.lib["holders"].remove(h)
                C.check_library(self.lib)
                if self.save_lib(f"Deleted holder '{h['description']}'."):
                    load()
            dlg.grab_set()

        ttk.Button(row, text="＋ Holder", style="Accent.TButton", command=edit).pack(side="left")
        ttk.Button(row, text="Edit", command=lambda: sel() and edit(sel())).pack(side="left", padx=(S(6), 0))
        ttk.Button(row, text="Attach CAD…", command=attach).pack(side="left", padx=(S(6), 0))
        ttk.Button(row, text="Delete", command=delete).pack(side="left", padx=(S(6), 0))
        tv.bind("<Double-1>", lambda e: (edit(sel()) if tv.identify_row(e.y) else edit(), "break")[1])
        attach_menu(self, tv, lambda iid: [("New holder…", edit, True)] + (
            [None, ("Edit…", lambda: edit(sel()), True), ("Attach CAD…", attach, True), ("Delete", delete, True)]
            + [("Open CAD: " + os.path.basename(c), lambda c=c: self.open_lib_file(c), True)
               for c in (sel() or {}).get("cad", [])] if iid else []))
        keys(tv, enter=lambda: sel() and edit(sel()), delete=delete, new=edit)
        load()
        ttk.Button(dlg.buttons, text="Close", command=dlg.cancel).pack(side="right")
        dlg.geometry(f"{S(680)}x{min(S(460), self.winfo_screenheight() - 90)}")
        dlg.show()

    # ---- calculator
    def update_calc(self, remember_choice=False):
        if remember_choice:
            remember("cam_calc_machine", self.c_machine.get())
            remember("cam_calc_material", self.c_mat.get())
        t = None
        sel = self.t_tree.selection()
        if self.lib and sel:
            t = next((x for x in self.lib["tools"] if x["id"] == sel[0]), None)
        if not t:
            self.c_out.configure(text="Select a tool to see its speed and feed on a machine.")
            return
        mat = self.c_mat.get()
        p = next((x for x in t.get("presets", []) if x.get("material") == mat), None)
        if not p:
            have = ", ".join(x.get("material", "") for x in t.get("presets", [])) or "none yet"
            self.c_out.configure(text=f"T{t['number']} has no feeds & speeds for '{mat or '—'}' (has: {have}).")
            return
        m = C.get_machine(self.c_machine.get())
        cd = C.cutting_data(t, p, m)
        txt = (f"T{t['number']} in {mat}:   {C.fmt(cd['rpm']) or '—'} rpm    {C.fmt(cd['feed']) or '—'} mm/min"
               f"    plunge {C.fmt(cd['plunge']) or '—'}    ap {C.fmt(p.get('ap')) or '—'}    ae {C.fmt(p.get('ae')) or '—'}")
        if cd["capped"]:
            txt += f"\nLimited by {m['name']}: {', '.join(cd['capped'])} (feed scaled to keep the chip load)."
        self.c_out.configure(text=txt)

    # ================================================================== machines & posts
    def _build_machines(self):
        S = self.S
        page = ttk.Frame(self.nb, style="Dialog.TFrame", padding=(0, S(10), 0, 0))
        self.nb.add(page, text="  Machines & posts  ")
        pw = ttk.PanedWindow(page, orient="horizontal")
        pw.pack(fill="both", expand=True)
        left = ttk.Frame(pw, style="Dialog.TFrame")
        right = ttk.Frame(pw, style="Dialog.TFrame")
        pw.add(left, weight=1)
        pw.add(right, weight=3)
        ttk.Label(left, text="MACHINES", style="CardSection.TLabel").pack(side="top", anchor="w")
        mb = bottom_bar(left, S)
        ttk.Button(mb, text="New…", command=lambda: self.edit_machine(new=True)).pack(side="left")
        ttk.Button(mb, text="Edit…", command=self.edit_machine).pack(side="left", padx=(S(6), 0))
        ttk.Button(mb, text="Open folder", command=lambda: self._sel_machine() and open_folder(
            C.machine_dir(self._sel_machine()))).pack(side="left", padx=(S(6), 0))
        self.m_tree = make_tree(left, (("name", "Machine", 180, True), ("ctl", "Controller", 100, False)), height=14)
        self.m_tree.bind("<<TreeviewSelect>>", lambda e: self.show_machine())
        self.m_tree.bind("<Double-1>", lambda e: (self.edit_machine(new=not self.m_tree.identify_row(e.y)), "break")[1])
        attach_menu(self, self.m_tree, self.machine_menu)
        keys(self.m_tree, enter=self.edit_machine, f2=self.edit_machine, delete=self.delete_machine,
             new=lambda: self.edit_machine(new=True))

        self.m_info = ttk.Label(right, text="", style="CardValue.TLabel", justify="left")
        self.m_info.pack(side="top", anchor="w", padx=(S(12), 0))
        ttk.Label(right, text="POSTS", style="CardSection.TLabel").pack(side="top", anchor="w", padx=(S(12), 0),
                                                                       pady=(S(12), S(2)))
        ttk.Label(right, text="Each CAM program has its own post format (CamCraft and Fusion-style .cps, FreeCAD "
                              "Python posts, PhysCAD's own). Replacing a post keeps the old one in History, and each "
                              "filed NC program records which post made it.", style="CardMuted.TLabel",
                  wraplength=S(620), justify="left").pack(side="bottom", anchor="w", padx=(S(12), 0), pady=(S(8), 0))
        pb = bottom_bar(right, S, padx=(S(12), 0))
        ttk.Button(pb, text="＋ Add / replace post…", style="Accent.TButton", command=self.add_post).pack(side="left")
        ttk.Button(pb, text="Open posts folder", command=lambda: self._sel_machine() and open_folder(
            os.path.join(C.machine_dir(self._sel_machine()), "Posts"))).pack(side="left", padx=(S(6), 0))
        ttk.Button(pb, text="History", command=lambda: self._sel_machine() and open_folder(
            os.path.join(C.machine_dir(self._sel_machine()), "History"))).pack(side="left", padx=(S(6), 0))
        holder = ttk.Frame(right, style="Dialog.TFrame")
        holder.pack(side="top", fill="both", expand=True, padx=(S(12), 0))
        self.po_tree = make_tree(holder, (("app", "CAM program", 120, False), ("name", "Post file", 240, True),
                                          ("mod", "Modified", 140, False)), height=10)
        self.po_tree.bind("<Double-1>", lambda e: (self.open_post() if self.po_tree.identify_row(e.y) else self.add_post(),
                                                   "break")[1])
        attach_menu(self, self.po_tree, self.post_menu)
        keys(self.po_tree, enter=self.open_post, delete=self.remove_post, new=self.add_post)

    def machine_menu(self, iid):
        new = [("New machine…   Ctrl+N", lambda: self.edit_machine(new=True), True)]
        if not iid:
            return new
        return [("Edit…   Enter", self.edit_machine, True), ("Duplicate…", self.duplicate_machine, True),
                ("Delete…   Del", self.delete_machine, True), None,
                ("Add / replace post…", self.add_post, True),
                ("Open machine folder", lambda: open_folder(C.machine_dir(iid)), True),
                ("Open posts folder", lambda: open_folder(os.path.join(C.machine_dir(iid), "Posts")), True),
                ("Open program folder", self.open_program_folder, bool((C.get_machine(iid) or {}).get("program_folder"))),
                ("Post history", lambda: open_folder(os.path.join(C.machine_dir(iid), "History")), True), None] + new

    def post_menu(self, iid):
        add = [("Add / replace post…   Ctrl+N", self.add_post, bool(self._sel_machine()))]
        if not iid:
            return add
        return [("Open   Enter", self.open_post, True), ("Replace…", self.add_post, True),
                ("Remove (to History)   Del", self.remove_post, True), None,
                ("Show in folder", lambda: self._post_row() and open_folder(os.path.dirname(self._post_row()["path"])), True),
                ("Copy file name", lambda: self._post_row() and self.copy_text(self._post_row()["name"]), True), None] + add

    def load_machines(self):
        tv = self.m_tree
        keep = self._sel_machine()
        tv.delete(*tv.get_children())
        for m in C.list_machines():
            tv.insert("", "end", iid=m["name"], values=(m["name"], m.get("controller", "")))
        if keep and tv.exists(keep):
            tv.selection_set(keep)
        elif tv.get_children():
            tv.selection_set(tv.get_children()[0])
        self.show_machine()
        if hasattr(self, "c_mcombo"):
            self.c_mcombo.configure(values=[""] + [m["name"] for m in C.list_machines()])

    def _sel_machine(self):
        s = self.m_tree.selection() if hasattr(self, "m_tree") else ()
        return s[0] if s else None

    def _post_row(self):
        s = self.po_tree.selection()
        return self._posts.get(s[0]) if s and hasattr(self, "_posts") else None

    def show_machine(self):
        name = self._sel_machine()
        self.po_tree.delete(*self.po_tree.get_children())
        self._posts = {}
        if not name:
            self.m_info.configure(text="No machines yet. Add one with New… (or right-click the list).")
            return
        m = C.get_machine(name) or {}
        lines = [f"{name}   ·   {m.get('controller') or 'controller not set'}",
                 f"Spindle {C.fmt(m.get('spindle_min')) or '0'}-{C.fmt(m.get('spindle_max')) or '?'} rpm   ·   max feed "
                 f"{C.fmt(m.get('feed_max')) or '?'} mm/min   ·   NC files {m.get('nc_ext', '.nc')}",
                 f"Travel X{C.fmt(m.get('travel_x')) or '?'}  Y{C.fmt(m.get('travel_y')) or '?'}  "
                 f"Z{C.fmt(m.get('travel_z')) or '?'}   ·   tool slots {C.fmt(m.get('tool_slots')) or '?'}",
                 f"Program folder: {m.get('program_folder') or 'not set'}"]
        if m.get("notes"):
            lines.append(m["notes"])
        self.m_info.configure(text="\n".join(lines))
        for i, p in enumerate(C.list_posts(name)):
            iid = f"{p['app']}/{p['name']}"
            self._posts[iid] = p
            self.po_tree.insert("", "end", iid=iid, values=(p["app"], p["name"],
                                                            time.strftime("%d %b %Y  %H:%M", time.localtime(p["modified"]))))

    def open_program_folder(self):
        m = C.get_machine(self._sel_machine()) or {}
        f = m.get("program_folder")
        if f and os.path.isdir(f):
            E.open_path(f)
        else:
            self.say(f"Can't reach {f or 'the program folder'}.", "warn")

    def open_post(self):
        p = self._post_row()
        if p:
            E.open_path(p["path"])

    def remove_post(self):
        p = self._post_row()
        name = self._sel_machine()
        if not p or not name or not self.need_admin():
            return
        if messagebox.askyesno("Remove post", f"Remove {p['name']} ({p['app']}) from {name}?\n\nIt's moved to the "
                               "machine's History folder.", parent=self):
            ok, _ = self.guard(C.remove_post, name, p["app"], p["name"])
            if ok:
                self.show_machine()
                self.say(f"Removed {p['name']}.")

    def edit_machine(self, new=False, template=None):
        name = None if new else self._sel_machine()
        if not new and not name:
            self.say("Select a machine first.", "warn")
            return
        if not self.need_admin():
            return
        m = dict(template) if template else ({} if new else (C.get_machine(name) or {}))

        def validate(v):
            for k in ("spindle_max", "spindle_min", "feed_max", "travel_x", "travel_y", "travel_z", "tool_slots"):
                if v[k] and C.num(v[k]) is None:
                    raise ValueError(f"'{v[k]}' isn't a number.")
            if v["program_folder"] and not os.path.isabs(v["program_folder"]):
                raise ValueError("Give the program folder as a full path, e.g. \\\\TORMACH\\gcode or Z:\\Programs.")
            return C.save_machine(dict(v), old_name=name)
        res = form_dialog(self, "Machine", "New machine" if new else f"Edit {name}", "Limits are used to cap feeds & "
                          "speeds from the tool libraries.", [
            {"key": "name", "label": "Name", "initial": m.get("name", ""), "col": 0},
            {"key": "controller", "label": "Controller", "initial": m.get("controller", ""), "col": 1,
             "hint": "e.g. PathPilot, LinuxCNC, GRBL, Haas"},
            {"key": "spindle_max", "label": "Spindle max (rpm)", "initial": C.fmt(m.get("spindle_max")), "col": 0},
            {"key": "spindle_min", "label": "Spindle min (rpm)", "initial": C.fmt(m.get("spindle_min")), "col": 1},
            {"key": "feed_max", "label": "Max cutting feed (mm/min)", "initial": C.fmt(m.get("feed_max")), "col": 0},
            {"key": "nc_ext", "label": "NC file type", "kind": "combo", "values": C.nc_exts(),
             "initial": m.get("nc_ext", ".nc"), "col": 1},
            {"key": "travel_x", "label": "Travel X (mm)", "initial": C.fmt(m.get("travel_x")), "col": 0},
            {"key": "travel_y", "label": "Travel Y (mm)", "initial": C.fmt(m.get("travel_y")), "col": 1},
            {"key": "travel_z", "label": "Travel Z (mm)", "initial": C.fmt(m.get("travel_z")), "col": 0},
            {"key": "tool_slots", "label": "Tool changer slots", "initial": C.fmt(m.get("tool_slots")), "col": 1},
            {"key": "program_folder", "label": "Program folder on the machine (for Send to machine)", "kind": "folder",
             "initial": m.get("program_folder", ""), "wide": True},
            {"key": "notes", "label": "Notes", "kind": "text", "initial": m.get("notes", ""), "height": 3, "wide": True},
        ], validate, "Save machine", width=620)
        if res:
            self.load_machines()
            self.m_tree.selection_set(res)
            self.say(f"Saved '{res}'.")

    def duplicate_machine(self):
        name = self._sel_machine()
        if name:
            m = C.get_machine(name) or {}
            m["name"] = f"{name} (copy)"
            self.edit_machine(new=True, template=m)

    def delete_machine(self):
        name = self._sel_machine()
        if not name or not self.need_admin():
            return
        if messagebox.askyesno("Delete machine", f"Delete '{name}' and its posts?\n\nThey're moved to _CAM\\Deleted "
                               "in the vault. NC programs filed for it keep its name.", icon="warning", parent=self):
            ok, _ = self.guard(C.delete_machine, name)
            if ok:
                self.load_machines()
                self.say(f"Deleted '{name}'.")

    def add_post(self):
        name = self._sel_machine()
        if not name:
            self.say("Select a machine first.", "warn")
            return
        if not self.need_admin():
            return
        cur = self._post_row()
        res = form_dialog(self, "Add post", f"Post for {name}", "Choose which CAM program the post is for, then the "
                          "file.", [{"key": "app", "label": "CAM program", "kind": "choice", "values": C.CAM_APPS,
                                     "initial": (cur or {}).get("app") or recall("cam_last_app", "CamCraft")}],
                          lambda v: v["app"], "Choose file…")
        if not res:
            return
        p = filedialog.askopenfilename(parent=self, title=f"{res} post for {name}")
        if not p:
            return
        ok, out = self.guard(C.add_post, name, res, p)
        if ok:
            self.show_machine()
            self.say(("Replaced" if out[1] else "Added") + f" {os.path.basename(p)} ({res})." +
                     (" The old copy is in History." if out[1] else ""))


# =============================================================================
# Standalone (Start-menu shortcut: Atelier PDM.exe --cam)
# =============================================================================
def main():
    _engine()
    E.enable_dpi_awareness()
    root = tk.Tk()
    root.withdraw()
    E.init_db()
    ctx = E.BootContext(root)
    style = ttk.Style(root)
    style.theme_use("clam")
    E.style_ttk(root, style, ctx.t, ctx.S)
    if not E.sign_in(ctx):
        sys.exit(0)
    CamWindow(ctx, standalone=True)
    root.mainloop()


# =============================================================================
# 2.7.2 - CAM programs tab: where each CAM program is, Locate… / Clear / Detect again
# =============================================================================
def _build_cam_programs(self):
    S = self.S
    page = ttk.Frame(self.nb, style="Dialog.TFrame", padding=(0, S(10), 0, 0))
    self.nb.add(page, text="  CAM programs  ")
    ttk.Label(page, text="Where Atelier finds each CAM program on this PC. If one isn't found, use Locate… and pick "
                         "its .exe - it's remembered for your Windows account.", style="CardMuted.TLabel",
              wraplength=S(900), justify="left").pack(side="top", anchor="w", pady=(0, S(8)))
    row = bottom_bar(page, S)
    for text, cmd, style in (("Locate…", lambda: _cp_locate(self), "Accent.TButton"),
                             ("Detect again", lambda: _cp_load(self, refresh=True), "TButton"),
                             ("Clear", lambda: _cp_clear(self), "TButton"),
                             ("Start program", lambda: _cp_start(self), "TButton"),
                             ("Open folder", lambda: _cp_folder(self), "TButton")):
        ttk.Button(row, text=text, style=style, command=cmd).pack(side="left", padx=(0, S(8)))
    ttk.Label(row, text="Right-click for more", style="CardMuted.TLabel").pack(side="right")
    self.cp_tree = make_tree(page, (("app", "CAM program", 130, False), ("state", "Status", 90, False),
                                    ("path", "Program", 460, True), ("how", "Found by", 220, False)), height=8)
    self.tag_colours(self.cp_tree)
    self.cp_tree.bind("<Double-1>", lambda e: (_cp_start(self) if _cp_sel(self) and C.find_cam_exe(_cp_sel(self))
                                               else _cp_locate(self), "break")[1])
    attach_menu(self, self.cp_tree, lambda iid: [] if not iid else [
        ("Locate…", lambda: _cp_locate(self), True),
        ("Start program   Enter", lambda: _cp_start(self), bool(C.find_cam_exe(iid))),
        ("Open folder", lambda: _cp_folder(self), bool(C.find_cam_exe(iid))),
        ("Copy path", lambda: self.copy_text(C.find_cam_exe(iid) or ""), bool(C.find_cam_exe(iid))), None,
        ("Clear (search again)   Del", lambda: _cp_clear(self), True),
        ("Detect again   F5", lambda: _cp_load(self, refresh=True), True)])
    keys(self.cp_tree, enter=lambda: _cp_start(self), delete=lambda: _cp_clear(self))


def _cp_sel(self):
    s = self.cp_tree.selection()
    return s[0] if s else None


def _cp_load(self, refresh=False):
    tv = self.cp_tree
    keep = _cp_sel(self)
    if refresh:
        _finder_mod().clear_cache()
    tv.delete(*tv.get_children())
    missing = 0
    for app in [a for a in C.CAM_APPS if a != "Other"]:
        p = C.find_cam_exe(app)
        missing += not p
        tv.insert("", "end", iid=app, tags=() if p else ("outdated",),
                  values=(app, "Found" if p else "Not found", p or "—", _finder_mod().how(app) if p else "—"))
    if keep and tv.exists(keep):
        tv.selection_set(keep)
    elif tv.get_children():
        tv.selection_set(tv.get_children()[0])
    if refresh:
        self.say("Searched again." + (f" {missing} not found - select one and use Locate…." if missing else ""),
                 "warn" if missing else "ok")


def _finder_mod():
    import atelier_finder
    return atelier_finder


def _cp_locate(self):
    app = _cp_sel(self)
    if not app:
        return
    cur = C.find_cam_exe(app)
    start = os.path.dirname(cur) if cur else os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs")
    p = filedialog.askopenfilename(parent=self, title=f"Locate {app}.exe", filetypes=[(app, "*.exe"), ("All files", "*.*")],
                                   initialdir=start if os.path.isdir(start) else None)
    if p:
        C.remember_cam_exe(app, p)
        _cp_load(self)
        self.say(f"{app}: {os.path.normpath(p)}")


def _cp_clear(self):
    app = _cp_sel(self)
    if app:
        _finder_mod().forget(app)
        _cp_load(self, refresh=True)


def _cp_start(self):
    app = _cp_sel(self)
    if app:
        self._launch(app, None)


def _cp_folder(self):
    p = C.find_cam_exe(_cp_sel(self) or "")
    if p:
        open_folder(os.path.dirname(p))


_build_machines_271 = CamWindow._build_machines
_reload_all_271 = CamWindow.reload_all


def _build_machines_272(self):
    _build_machines_271(self)
    _build_cam_programs(self)


def _reload_all_272(self):
    _reload_all_271(self)
    if hasattr(self, "cp_tree"):
        _cp_load(self)


CamWindow._build_machines = _build_machines_272
CamWindow.reload_all = _reload_all_272
