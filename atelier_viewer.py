"""
Atelier PDM - 3D preview (atelier_viewer.py)  2.7.5
==================================================

Replaces the preview drawing used by the main window and the SolidCraft / PhysCAD panels:

  * No grid - a soft vertical gradient in the theme's colours, and a gentle shadow under the part.
  * More detail - up to 9,000 triangles (was 2,500), so curved faces and small features keep their shape.
  * Better lighting - a key light, a fill light, a little ambient and a soft highlight, instead of one flat
    light. Faces pointing away are skipped, so the extra detail costs little.
  * Smooth orbiting - while you drag or zoom a lighter version is drawn; the full-detail view follows a moment
    after you stop.

Installed by atelier_cam.install(). If anything here fails, the original preview is used.
"""
import math
import os
import time

E = None
HQ_FACES = 9000            # detail kept from the STL
FAST_FACES = 2600          # drawn while orbiting / zooming
SETTLE_MS = 220            # full detail this long after the last movement
INTERACTIVE_S = 0.16       # redraws closer together than this count as "moving"
_CACHE = {}
_STATE = {}                # per canvas: last draw time, pending job, token
ORIG = {}

KEY = (-0.42, 0.58, 0.70)        # upper left, towards the viewer
FILL = (0.62, -0.18, 0.52)       # right, slightly below
HALF = None


def _norm(v):
    l = math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2]) or 1.0
    return (v[0] / l, v[1] / l, v[2] / l)


KEY, FILL = _norm(KEY), _norm(FILL)
HALF = _norm((KEY[0], KEY[1], KEY[2] + 1.0))


def _rgb(c):
    c = str(c).lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


def _mix(a, b, t):
    ra, rb = _rgb(a), _rgb(b)
    return "#{:02x}{:02x}{:02x}".format(*(int(round(x + (y - x) * t)) for x, y in zip(ra, rb)))


def _lum(c):
    r, g, b = _rgb(c)
    return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255


# =============================================================================
# Loading
# =============================================================================
def load_mesh(path):
    """Mesh prepared for drawing: centred, scaled to 1, outward winding, unit normals. Cached by file date."""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    hit = _CACHE.get(path)
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        verts, faces = E.parse_stl_file(path, max_faces=HQ_FACES)
    except TypeError:
        verts, faces = E.parse_stl_file(path)
    mesh = prepare(verts, faces)
    _CACHE[path] = (mtime, mesh)
    if len(_CACHE) > 24:
        _CACHE.pop(next(iter(_CACHE)))
    return mesh


def prepare(verts, faces):
    if not verts or not faces:
        return None
    xs = [v[0] for v in verts]
    ys = [v[1] for v in verts]
    zs = [v[2] for v in verts]
    cx, cy, cz = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2, (min(zs) + max(zs)) / 2
    d = max(max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs)) or 1.0
    tris, vol = [], 0.0
    for i1, i2, i3, _n in faces:
        a, b, c = verts[i1], verts[i2], verts[i3]
        a = ((a[0] - cx) / d, (a[1] - cy) / d, (a[2] - cz) / d)
        b = ((b[0] - cx) / d, (b[1] - cy) / d, (b[2] - cz) / d)
        c = ((c[0] - cx) / d, (c[1] - cy) / d, (c[2] - cz) / d)
        ux, uy, uz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
        vx, vy, vz = c[0] - a[0], c[1] - a[1], c[2] - a[2]
        nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
        ln = math.sqrt(nx * nx + ny * ny + nz * nz)
        if ln < 1e-12:
            continue                                            # degenerate sliver
        vol += a[0] * (b[1] * c[2] - b[2] * c[1]) - a[1] * (b[0] * c[2] - b[2] * c[0]) + a[2] * (b[0] * c[1] - b[1] * c[0])
        tris.append((a, b, c, (nx / ln, ny / ln, nz / ln)))
    if not tris:
        return None
    if vol < 0:                                                 # inside-out file: flip so lighting is right
        tris = [(a, c, b, (-n[0], -n[1], -n[2])) for a, b, c, n in tris]
    closed = abs(vol) / 6 > 1e-4                                 # solid enough to skip back faces safely
    step = max(1, math.ceil(len(tris) / FAST_FACES))
    return {"tris": tris, "fast": tris[::step], "closed": closed, "n": len(tris),
            "v": verts, "f": faces, "c": (cx, cy, cz), "d": d}


# =============================================================================
# Drawing
# =============================================================================
def draw_backdrop(canvas, w, h, t, step=None):
    """Soft vertical gradient in the theme's colours (no grid)."""
    base = t.get("entry_bg", "#111827")
    canvas.configure(bg=base)
    dark = _lum(base) < 0.5
    top = _mix(base, t.get("fg", "#ffffff"), 0.07 if dark else 0.04)
    bottom = _mix(base, "#000000", 0.22 if dark else 0.08)
    bands = 36
    for i in range(bands):
        y0, y1 = int(h * i / bands), int(h * (i + 1) / bands) + 1
        canvas.create_rectangle(0, y0, w + 1, y1, fill=_mix(top, bottom, i / (bands - 1)), outline="")


def _shadow(canvas, cx, y, rx, ry, bg_bottom, dark):
    for k, a in ((1.0, 0.10), (0.78, 0.16), (0.56, 0.22)):
        col = _mix(bg_bottom, "#000000", a if dark else a * 0.6)
        canvas.create_oval(cx - rx * k, y - ry * k, cx + rx * k, y + ry * k, fill=col, outline="")


def render(canvas, mesh, w, h, t, cam, fast=False, tag="atl3d"):
    canvas.delete("all")
    draw_backdrop(canvas, w, h, t)
    tris = mesh["fast"] if fast else mesh["tris"]
    scale = min(w, h) * 0.78 * cam.get("zoom", 1.0)
    rx, ry = math.radians(cam.get("rx", 25.0)), math.radians(cam.get("ry", 45.0))
    cxr, sxr, cyr, syr = math.cos(rx), math.sin(rx), math.cos(ry), math.sin(ry)
    hw, hh = w / 2.0, h / 2.0
    base = _rgb(t.get("mesh") or t.get("accent") or "#4cb5f5")
    cull = mesh["closed"]
    kx, ky, kz = KEY
    fx, fy, fz = FILL
    hx, hy, hz = HALF
    out = []
    min_y, max_y, min_x, max_x = 1e9, -1e9, 1e9, -1e9
    append = out.append
    for a, b, c, n in tris:
        nx, ny, nz = n
        nx1 = nx * cyr + nz * syr
        nz1 = -nx * syr + nz * cyr
        ny2 = ny * cxr - nz1 * sxr
        nz2 = ny * sxr + nz1 * cxr
        if cull and nz2 <= 0:
            continue
        if nz2 < 0:                                             # open mesh: light the side we can see
            nx1, ny2, nz2 = -nx1, -ny2, -nz2
        pts = []
        zs = 0.0
        for p in (a, b, c):
            x1 = p[0] * cyr + p[2] * syr
            z1 = -p[0] * syr + p[2] * cyr
            y2 = p[1] * cxr - z1 * sxr
            zs += p[1] * sxr + z1 * cxr
            sx, sy = hw + x1 * scale, hh - y2 * scale
            pts.append(sx)
            pts.append(sy)
        diff = nx1 * kx + ny2 * ky + nz2 * kz
        fill = nx1 * fx + ny2 * fy + nz2 * fz
        spec = nx1 * hx + ny2 * hy + nz2 * hz
        i = 0.30 + 0.62 * (diff if diff > 0 else 0) + 0.20 * (fill if fill > 0 else 0)
        s = (spec ** 28) * 0.38 if spec > 0 else 0.0
        r = base[0] * i + 255 * s
        g = base[1] * i + 255 * s
        bl = base[2] * i + 255 * s
        col = "#%02x%02x%02x" % (255 if r > 255 else int(r), 255 if g > 255 else int(g), 255 if bl > 255 else int(bl))
        append((zs, pts, col))
        x0, x1_, x2 = pts[0], pts[2], pts[4]
        y0, y1_, y2_ = pts[1], pts[3], pts[5]
        lo, hi = min(x0, x1_, x2), max(x0, x1_, x2)
        if lo < min_x:
            min_x = lo
        if hi > max_x:
            max_x = hi
        yb = max(y0, y1_, y2_)
        if yb > max_y:
            max_y = yb
    if out:
        dark = _lum(t.get("entry_bg", "#111827")) < 0.5
        bottom = _mix(t.get("entry_bg", "#111827"), "#000000", 0.22 if dark else 0.08)
        span = max(20.0, max_x - min_x)
        _shadow(canvas, (min_x + max_x) / 2, min(h - 10, max_y + span * 0.05), span * 0.42, span * 0.07, bottom, dark)
    out.sort(key=lambda o: o[0])
    cp = canvas.create_polygon
    for _z, pts, col in out:
        cp(pts, fill=col, outline=col, width=1, tags=tag)
    triad = getattr(E, "draw_triad", None)
    if triad:
        try:
            triad(canvas, 30, h - 30, 18, cam)
        except Exception:                                       # noqa: BLE001
            pass


def render_mesh(canvas, mesh, w, h, t, cam):
    """Called by the main window and the panels. Light version while moving, full detail once still."""
    if not mesh or "tris" not in mesh:
        return ORIG["render_mesh"](canvas, mesh, w, h, t, cam)
    key = str(canvas)
    st = _STATE.setdefault(key, {"last": 0.0, "job": None, "token": 0})
    now = time.monotonic()
    moving = (now - st["last"]) < INTERACTIVE_S and mesh["n"] > FAST_FACES
    st["last"] = now
    st["token"] += 1
    token = st["token"]
    if st["job"] is not None:
        try:
            canvas.after_cancel(st["job"])
        except Exception:                                       # noqa: BLE001
            pass
        st["job"] = None
    tag = f"atl3d{token}"
    render(canvas, mesh, w, h, t, dict(cam), fast=moving, tag=tag)
    if moving:
        snap = (mesh, w, h, t, dict(cam))

        def settle():
            st["job"] = None
            try:
                if st["token"] != token or not canvas.find_withtag(tag):
                    return                                      # something else has been drawn since
                render(canvas, snap[0], snap[1], snap[2], snap[3], snap[4], fast=False, tag=tag)
            except Exception:                                   # noqa: BLE001
                pass
        try:
            st["job"] = canvas.after(SETTLE_MS, settle)
        except Exception:                                       # noqa: BLE001
            st["job"] = None


def install(module):
    global E
    E = module
    if getattr(module, "_viewer_installed", False):
        return
    if not all(hasattr(module, n) for n in ("render_mesh", "load_mesh", "draw_backdrop", "parse_stl_file")):
        return                                                  # unexpected engine: leave the preview alone
    module._viewer_installed = True
    for n in ("render_mesh", "load_mesh", "draw_backdrop"):
        ORIG[n] = getattr(module, n)
    module.render_mesh = render_mesh
    module.load_mesh = load_mesh
    module.draw_backdrop = draw_backdrop
