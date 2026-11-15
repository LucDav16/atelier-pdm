"""
Atelier PDM - row alignment, On hold / For quotation only, PDF watermark (atelier_states.py)  2.8.5
====================================================================================================

Rows
    The State column is filled in its own place. The main list inserted a fixed set of values in the original
    column order, so after Status everything was one column out (Folder under State, the date under Folder).

Work states (added to the release workflow - existing workflows get them once, automatically)
    ‖ On hold            - paused. Can't be changed. From In work (anyone, with a reason) or from Awaiting
                           approval (administrators). "Back to In work" returns it.
    ◇ For quotation only - frozen so the design matches what was sent out for prices. From In work (anyone).
                           "Back to In work" returns it.
    Messages for frozen parts now say which action brings them back.

PDF watermark  (account menu › PDF watermark…, administrators)
    Exported PDFs get a large diagonal "FOR QUOTATION ONLY" across every page:
      Off  /  Only parts that are For quotation only (default)  /  Every exported PDF.
    The text can be changed. Right-click a part › Export for quotation (watermarked)… does it for one export
    whatever the setting. Needs the pypdf library (added to the build).
"""
import copy
import io
import json
import math
import os
import re
import sys
import time
import traceback

VERSION = "2.8.5"
E = None
FL = None
ORIG = {}
_FORCE = {"on": False}
MODES = (("off", "Off"), ("quote", "Only parts that are 'For quotation only'"), ("all", "Every exported PDF"))
DEFAULT_TEXT = "FOR QUOTATION ONLY"

NEW_STATES = [
    {"key": "On hold", "label": "On hold", "colour": "amber", "editable": False, "release": False, "review": False,
     "delete": False, "symbol": "‖"},
    {"key": "For quotation only", "label": "For quotation only", "colour": "purple", "editable": False,
     "release": False, "review": False, "delete": False, "symbol": "◇", "quote": True},
]
NEW_ACTIONS = [
    {"label": "Put on hold", "from": "In work", "to": "On hold", "who": "anyone", "note": True, "other": False},
    {"label": "Put on hold", "from": "Awaiting approval", "to": "On hold", "who": "admin", "note": True,
     "other": False},
    {"label": "Back to In work", "from": "On hold", "to": "In work", "who": "anyone", "note": False, "other": False},
    {"label": "For quotation only", "from": "In work", "to": "For quotation only", "who": "anyone", "note": False,
     "other": False},
    {"label": "Back to In work", "from": "For quotation only", "to": "In work", "who": "anyone", "note": False,
     "other": False},
]

# Helvetica-Bold widths (1/1000 em), for centring the watermark
W = {' ': 278, '!': 333, '"': 474, '#': 556, '$': 556, '%': 889, '&': 722, "'": 238, '(': 333, ')': 333, '*': 389,
     '+': 584, ',': 278, '-': 333, '.': 278, '/': 278, ':': 333, ';': 333, '<': 584, '=': 584, '>': 584, '?': 611,
     '@': 975, 'A': 722, 'B': 722, 'C': 722, 'D': 722, 'E': 667, 'F': 611, 'G': 778, 'H': 722, 'I': 278, 'J': 556,
     'K': 722, 'L': 611, 'M': 833, 'N': 722, 'O': 778, 'P': 667, 'Q': 778, 'R': 722, 'S': 667, 'T': 611, 'U': 722,
     'V': 667, 'W': 944, 'X': 667, 'Y': 667, 'Z': 611, '[': 333, '\\': 278, ']': 333, '^': 584, '_': 556, '`': 333,
     'a': 556, 'b': 611, 'c': 556, 'd': 611, 'e': 556, 'f': 333, 'g': 611, 'h': 611, 'i': 278, 'j': 278, 'k': 556,
     'l': 278, 'm': 889, 'n': 611, 'o': 611, 'p': 611, 'q': 611, 'r': 389, 's': 556, 't': 333, 'u': 611, 'v': 556,
     'w': 778, 'x': 556, 'y': 556, 'z': 500, '{': 389, '|': 280, '}': 389, '~': 584}
W.update({str(i): 556 for i in range(10)})


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
# 1. Row alignment
# =============================================================================
def fix_rows(app):
    """Puts each row's values under the right headings when a row has one value fewer than there are columns."""
    cols = [c[0] for c in getattr(app, "GRID_COLS", ())]
    if "state" not in cols:
        return 0
    at = cols.index("state")
    grid, fixed = app.grid, 0

    def walk(parent):
        nonlocal fixed
        for iid in grid.get_children(parent):
            vals = list(grid.item(iid, "values") or ())
            if len(vals) == len(cols) - 1:
                name = (app.row_map.get(iid) or (None,))[0]
                top = parent == ""
                info = E.INDEX.files.get(name or "")
                st = ""
                if top and info is not None and not info.is_library and FL is not None:
                    try:
                        st = FL.badge(name)
                    except Exception:                           # noqa: BLE001
                        st = ""
                vals.insert(at, st)
                grid.item(iid, values=vals)
                fixed += 1
            walk(iid)
    walk("")
    return fixed


def _hook_rows(m):
    cls = getattr(m, "AtelierApp", None)
    if cls is None or getattr(cls, "_atl_states", False):
        return
    cls._atl_states = True
    o_grid = cls.refresh_grid

    def refresh_grid(self, *a, **k):
        res = o_grid(self, *a, **k)
        try:
            fix_rows(self)
        except Exception:                                       # noqa: BLE001
            log_error("row alignment")
        return res
    cls.refresh_grid = refresh_grid
    o_ident = getattr(cls, "update_identity", None)
    if o_ident:
        def update_identity(self, *a, **k):
            res = o_ident(self, *a, **k)
            try:
                if E.is_admin():
                    self.account_menu.add_command(label="⎙  PDF watermark…", command=lambda: watermark_dialog(self))
            except Exception:                                   # noqa: BLE001
                pass
            return res
        cls.update_identity = update_identity


# =============================================================================
# 2. On hold / For quotation only
# =============================================================================
def add_states(c):
    """Adds the new states and actions to a workflow (only what's missing). Returns True if anything changed."""
    keys = [s["key"] for s in c["states"]]
    labels = {s["label"].lower() for s in c["states"]}
    changed = False
    for s in NEW_STATES:
        if s["key"] in keys or s["label"].lower() in labels:
            continue
        at = keys.index("Obsolete") if "Obsolete" in keys else len(keys)
        c["states"].insert(at, copy.deepcopy(s))
        keys.insert(at, s["key"])
        changed = True
    have = {(a["from"], a["to"], a["label"].lower()) for a in c["actions"]}
    for a in NEW_ACTIONS:
        if a["from"] in keys and a["to"] in keys and (a["from"], a["to"], a["label"].lower()) not in have:
            c["actions"].append(copy.deepcopy(a))
            changed = True
    return changed


def migrate_workflow():
    for preset in (FL.DEFAULT, FL.SIMPLE):
        add_states(preset)
    if E.get_setting("lifecycle_states_v285", "") == "1":
        return False
    raw = E.get_setting(FL.SETTING, None)
    changed = False
    if raw:
        try:
            c = json.loads(raw)
            if add_states(c) and not FL.check(c):
                E.set_setting(FL.SETTING, json.dumps(c))
                changed = True
                E.log_audit("[settings]", "Release Workflow Changed", "", "added On hold and For quotation only")
        except (ValueError, KeyError, TypeError):
            pass
    E.set_setting("lifecycle_states_v285", "1")
    FL._CACHE.update(cfg=None, at=0.0)
    try:
        FL._sync_filters()
    except Exception:                                           # noqa: BLE001
        pass
    return changed


def symbol(sd):
    return sd.get("symbol") or ORIG["symbol"](sd)


def colour(sd, t):
    if sd.get("colour") == "purple":
        return "#a78bfa"
    return ORIG["colour"](sd, t)


def blocked(name, action):
    msg = ORIG["blocked"](name, action)
    if not msg:
        return msg
    sd = FL.state_def(FL.state_key(name))
    if sd["release"] or sd["review"]:
        return msg
    ways = [a["label"] for a in FL.actions_from(sd["key"]) if FL.state_def(a["to"])["editable"] and
            FL.can(name, a)[0]]
    if ways:
        return f"'{name}' is {sd['label'].lower()}, so it can't be {action}. To work on it again: Change state › " \
               f"{ways[0]}."
    return msg


def is_quote(name):
    try:
        sd = FL.state_def(FL.state_key(name))
    except Exception:                                           # noqa: BLE001
        return False
    return bool(sd.get("quote")) or "quotation" in sd.get("label", "").lower()


# =============================================================================
# 3. PDF watermark
# =============================================================================
def wm_mode():
    try:
        m = E.get_setting("pdf_watermark_mode", "quote") or "quote"
    except Exception:                                           # noqa: BLE001
        m = "quote"
    return m if m in dict(MODES) else "quote"


def wm_text():
    try:
        return (E.get_setting("pdf_watermark_text", DEFAULT_TEXT) or DEFAULT_TEXT).strip() or DEFAULT_TEXT
    except Exception:                                           # noqa: BLE001
        return DEFAULT_TEXT


def _pdf_str(text):
    t = "".join(ch if 32 <= ord(ch) < 256 else "?" for ch in text)
    return t.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def watermark_page_pdf(w, h, text, x0=0.0, y0=0.0, opacity=0.18):
    """A one-page PDF (bytes) the size of the page, with text large and diagonal across the middle."""
    units = sum(W.get(ch, 667) for ch in text) / 1000.0 or 1.0
    diag = math.hypot(w, h)
    fs = min(0.78 * diag / units, 0.3 * min(w, h) * 2)
    tw, cap = units * fs, 0.72 * fs
    ang = math.atan2(h, w)
    c, s = math.cos(ang), math.sin(ang)
    cx, cy = x0 + w / 2, y0 + h / 2
    tx = cx - c * tw / 2 + s * cap / 2
    ty = cy - s * tw / 2 - c * cap / 2
    content = (f"q /GS1 gs 0.78 0.12 0.12 rg BT /F1 {fs:.2f} Tf {c:.5f} {s:.5f} {-s:.5f} {c:.5f} {tx:.2f} {ty:.2f} Tm "
               f"({_pdf_str(text)}) Tj ET Q").encode("latin-1")
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            (f"<< /Type /Page /Parent 2 0 R /MediaBox [{x0:.2f} {y0:.2f} {x0 + w:.2f} {y0 + h:.2f}] "
             f"/Resources << /Font << /F1 5 0 R >> /ExtGState << /GS1 6 0 R >> >> /Contents 4 0 R >>").encode(),
            b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>",
            f"<< /Type /ExtGState /ca {opacity} /CA {opacity} >>".encode()]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offs = []
    for i, o in enumerate(objs, 1):
        offs.append(out.tell())
        out.write(f"{i} 0 obj\n".encode() + o + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode())
    for o in offs:
        out.write(f"{o:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()


def stamp_pdf(path, text=None):
    """Adds the watermark to every page of path, in place. Returns the number of pages."""
    from pypdf import PdfReader, PdfWriter
    text = text or wm_text()
    reader = PdfReader(path)
    if reader.is_encrypted:
        raise ValueError("the PDF is password-protected")
    writer = PdfWriter()
    for page in reader.pages:
        box = page.mediabox
        wm = PdfReader(io.BytesIO(watermark_page_pdf(float(box.width), float(box.height), text,
                                                     float(box.left), float(box.bottom)))).pages[0]
        page.merge_page(wm, over=True)
        writer.add_page(page)
    tmp = path + ".wm-tmp"
    with open(tmp, "wb") as f:
        writer.write(f)
    os.replace(tmp, path)
    return len(reader.pages)


def should_watermark(name):
    if _FORCE["on"]:
        return True
    m = wm_mode()
    return m == "all" or (m == "quote" and is_quote(name))


def _expected_pdf(name):
    try:
        rec = E.INDEX.records.get(name) or {}
        stem = os.path.splitext(name)[0]
        pp = E.parse_pn(stem)
        pn = rec.get("part_number") or (pp[3] if pp else "00_0000")
        part = E.display_part_name(name, rec)
        rev = rec.get("revision") or E.parse_structured_name(name)[3]
        return os.path.join(E.OUTPUT_DIR, re.sub(r"[^\w.\-]+", "_", f"{pn}_{part}_{rev}").strip("_") + ".pdf")
    except Exception:                                           # noqa: BLE001
        return None


def made_pdfs(name, results, since):
    out = set()
    exp = _expected_pdf(name)
    if exp and os.path.isfile(exp):
        try:
            if max(os.path.getmtime(exp), os.path.getctime(exp)) >= since - 2:
                out.add(exp)
        except OSError:
            pass
    for _lvl, msg in results:
        for m in re.findall(r"([^\s:'\"]+\.pdf)\b", str(msg), re.IGNORECASE):
            p = os.path.join(E.OUTPUT_DIR, os.path.basename(m))
            if os.path.isfile(p) and max(os.path.getmtime(p), os.path.getctime(p)) >= since - 2:
                out.add(p)
    return sorted(out)


def export_deliverables(name, *a, **k):
    since = time.time()
    res = ORIG["export"](name, *a, **k)
    try:
        if not should_watermark(name):
            return res
        pdfs = made_pdfs(name, res, since)
        res = list(res)
        if not pdfs:
            if any("pdf" in str(m).lower() and l == "ok" for l, m in res):
                res.append(("warn", "Couldn't find the PDF to watermark."))
            return res
        for p in pdfs:
            try:
                n = stamp_pdf(p)
                res.append(("ok", f"Watermarked '{wm_text()}' on {n} page{'s' if n != 1 else ''}: "
                                  f"{os.path.basename(p)}"))
            except ImportError:
                res.append(("warn", "The PDF wasn't watermarked: the pypdf library is missing from this build."))
            except Exception as e:                              # noqa: BLE001
                res.append(("warn", f"The PDF wasn't watermarked ({e})."))
    except Exception:                                           # noqa: BLE001
        log_error("watermark")
    return res


def export_for_quotation(app):
    acc = sys.modules.get("atelier_access")
    _FORCE["on"] = True
    try:
        if acc is not None:
            acc.action_export(app)
        else:
            app.action_export()
    finally:
        _FORCE["on"] = False


def watermark_dialog(app):
    import tkinter as tk
    from tkinter import ttk
    if not app.ensure_admin():
        return
    S = app.S
    dlg = E.ThemedDialog(app, "PDF watermark", "PDF watermark",
                         "A large diagonal watermark across every page of exported PDFs. Applies to everyone using "
                         "this vault.", width=480)
    v = tk.StringVar(value=wm_mode())
    for key, text in MODES:
        ttk.Radiobutton(dlg.body, text=text, value=key, variable=v, style="Dialog.TCheckbutton").pack(anchor="w")
    ttk.Label(dlg.body, text="Watermark text", style="CardMuted.TLabel").pack(anchor="w", pady=(S(10), 0))
    tv = tk.StringVar(value=wm_text())
    ttk.Entry(dlg.body, textvariable=tv).pack(fill="x", pady=(S(2), 0), ipady=S(2))
    ttk.Label(dlg.body, text="Right-click a part › Export for quotation (watermarked)… always adds it.",
              style="CardMuted.TLabel", wraplength=S(440)).pack(anchor="w", pady=(S(8), 0))
    err = ttk.Label(dlg.body, text="", style="CardDanger.TLabel")
    err.pack(anchor="w")

    def ok():
        text = " ".join(tv.get().split())
        if not text:
            err.configure(text="Type the watermark text.")
            return E.INVALID
        if len(text) > 60:
            err.configure(text="Keep it short - 60 characters at most.")
            return E.INVALID
        guard = E._admin_guard()
        if guard:
            err.configure(text=guard[1])
            return E.INVALID
        E.set_setting("pdf_watermark_mode", v.get())
        E.set_setting("pdf_watermark_text", text)
        E.log_audit("[settings]", "PDF Watermark Changed", "", f"{dict(MODES)[v.get()]} · '{text}'")
        return True
    dlg.validate = ok
    dlg.add_buttons("Save", "Accent.TButton")
    if dlg.show():
        app.log(f"PDF watermark: {dict(MODES)[wm_mode()]}.", "ok")


def _extra(app, f):
    items = list(ORIG["extra"](app, f))
    info = E.INDEX.files.get(f or "")
    if info is not None and info.ext in E.CAD_MODEL_EXTS:
        items.append(("⇪  Export for quotation (watermarked)…", lambda: export_for_quotation(app), True))
    return items


# =============================================================================
# Help and install
# =============================================================================
HELP_STATES = [
    ("h", "On hold and For quotation only"),
    ("b", "**‖ On hold** - work is paused and the part can't be changed. Anyone can put an In work part on hold "
          "(with a reason); administrators can also hold one that's awaiting approval. **Back to In work** returns "
          "it."),
    ("b", "**◇ For quotation only** - the part is frozen so it matches what was sent out for prices. **Back to In "
          "work** returns it."),
    ("h", "PDF watermark"),
    ("p", "Account menu › **PDF watermark…** (administrators): exported PDFs can carry a large diagonal "
          "**FOR QUOTATION ONLY** across every page - Off, only for parts that are For quotation only, or for every "
          "PDF. The text can be changed. Right-click a part › **Export for quotation (watermarked)…** adds it for one "
          "export whatever the setting."),
]


def _help():
    try:
        import atelier_help_text as H
    except ImportError:
        return
    if getattr(H, "_atl_states", False):
        return
    H._atl_states = True
    for title, blocks in H.CHAPTERS:
        if title == "Release, BOM and safety nets":
            at = next((i for i, (k, v) in enumerate(blocks) if k == "h" and v.startswith("Changing the workflow")),
                      len(blocks))
            blocks[at:at] = HELP_STATES
    H.TIPS[:0] = [("Export for quotation", "Exports with a large FOR QUOTATION ONLY watermark on the PDF.")]


def install(module):
    global E, FL
    E = module
    if getattr(module, "_states_installed", False):
        return
    module._states_installed = True
    for n in ("atelier_polish", "atelier_guide", "atelier_flow", "atelier_access"):
        mm = sys.modules.get(n)
        if mm is not None:
            mm.VERSION = VERSION
    FL = sys.modules.get("atelier_flow")
    P = sys.modules.get("atelier_pro")
    G = sys.modules.get("atelier_guide")
    if FL is not None:
        ORIG["symbol"], ORIG["colour"], ORIG["blocked"] = FL.symbol, FL.colour, FL.blocked
        FL.symbol, FL.colour, FL.blocked = symbol, colour, blocked
        FL.COLOURS.setdefault("purple", "accent")
        if P is not None:
            P._blocked = blocked
        try:
            migrate_workflow()
        except Exception:                                       # noqa: BLE001
            log_error("workflow states")
    if hasattr(module, "export_deliverables"):
        ORIG["export"] = module.export_deliverables
        module.export_deliverables = export_deliverables
    if G is not None and "extra" in G.ORIG:
        ORIG["extra"] = G.ORIG["extra"]
        G.ORIG["extra"] = _extra
    elif P is not None:
        ORIG["extra"] = P.extra_menu_items
        P.extra_menu_items = _extra
    _hook_rows(module)
    _help()
