#!/usr/bin/env python3
"""
l2s_core.py — Lottie/Bodymovin JSON  ->  self-contained animated SVG.

Accuracy model
--------------
Every animated property is EVALUATED PER FRAME with python-lottie
(`prop.get_value(frame)` / `Transform.to_matrix(frame)`) and emitted as a linear
SMIL sample track. Nothing re-derives easing from Lottie bezier handles, so
there is no handle-convention bug surface and no dependency on lottie-web's
interpolation (which mis-steps position keyframes carrying spatial tangents).
python-lottie's evaluator matches After Effects; it is the oracle.

Clip safety
-----------
Content inside a track-matte clip is animated by GEOMETRY ONLY:
  - circles  -> <animate attributeName="cx"/"cy"/"r">
  - paths    -> <animate attributeName="d">
Never a transform. Transform animation on clipped content can be promoted to
its own GPU compositor layer, and the promoted layer escapes the ancestor
clip-path. That failure is invisible in headless/software rendering, so it must
be prevented structurally rather than tested away.

Unclipped animated layers use nested <animateTransform> (translate / rotate /
scale as separate nested <g>, matching AE's T * R * S order) because it is far
smaller than baking path data per frame.

Supported: shape + null layers, parent chains, group transforms, animated
position/scale/rotation/opacity, path morphs, ellipses, rects, fills, strokes,
trim paths, merge (evenodd), alpha track mattes, layer in/out gating.
Warns and degrades: luma/inverted mattes, gradients, effects, repeaters, 3D,
time-remap, precomps, text.
"""

import json
import math
import os
import re
import xml.etree.ElementTree as ET

import aefix  # noqa: F401  — corrects position interpolation on import
import kf
from lottie import objects as LO
from lottie.objects import Animation
from lottie.utils.transform import TransformMatrix
from lottie import NVector


# --------------------------------------------------------------------------- #
# warnings
# --------------------------------------------------------------------------- #
class Warnings:
    def __init__(self):
        self.items = []

    def __call__(self, msg):
        if msg not in self.items:
            self.items.append(msg)

    def __iter__(self):
        return iter(self.items)

    def __len__(self):
        return len(self.items)


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def num(x, p=2):
    """Compact fixed-precision number: 12.500 -> '12.5', -0.0 -> '0'."""
    s = f"{x:.{p}f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return "0" if s in ("", "-0", "-") else s


def slug(s):
    s = re.sub(r"[^A-Za-z0-9]+", "-", str(s)).strip("-").lower()
    return s or "icon"


def mat_tuple(m):
    return (m.a, m.b, m.c, m.d, m.tx, m.ty)


def mat_mul(m, n):
    """python-lottie convention: (m * n) applies m first, then n."""
    return m * n


def mat_of(tr, frame):
    """Matrix for a Transform or TransformShape at `frame`."""
    if hasattr(tr, "to_matrix"):
        return tr.to_matrix(frame)
    m = TransformMatrix()
    anchor = tr.anchor_point.get_value(frame) if tr.anchor_point else NVector(0, 0)
    m.translate(-anchor.x, -anchor.y)
    scale = tr.scale.get_value(frame) if tr.scale else NVector(100, 100)
    m.scale(scale.x / 100.0, scale.y / 100.0)
    rot = tr.rotation.get_value(frame) if tr.rotation else 0
    if rot:
        m.rotate(-math.radians(rot))
    pos = tr.position.get_value(frame) if tr.position else NVector(0, 0)
    m.translate(pos.x, pos.y)
    return m


def mat_scalefactor(m):
    return (math.hypot(m.a, m.b) + math.hypot(m.c, m.d)) / 2.0


def mats_equal(a, b, eps=1e-9):
    return all(abs(x - y) <= eps for x, y in zip(mat_tuple(a), mat_tuple(b)))


def hex_of(color_prop, frame=0):
    c = color_prop.get_value(frame)
    if c is None:
        return "#000000"
    vals = list(c)[:3]
    return "#%02x%02x%02x" % tuple(max(0, min(255, round(v * 255))) for v in vals)


#: AE merge modes. SVG's fill-rule can express a union and an exclusive-or;
#: it cannot express subtract or intersect, which need real path booleans.
MERGE_RULE = {1: "nonzero", 2: "nonzero", 5: "evenodd"}
MERGE_NAME = {1: "Merge", 2: "Add", 3: "Subtract", 4: "Intersect",
              5: "Exclude Intersections"}


def merge_fill_rule(merge, warn):
    mode = getattr(merge, "merge_mode", None)
    mode = getattr(mode, "value", mode)
    try:
        mode = int(mode)
    except (TypeError, ValueError):
        mode = 1
    if mode in MERGE_RULE:
        return MERGE_RULE[mode], MERGE_NAME.get(mode, str(mode))
    warn(f"merge mode '{MERGE_NAME.get(mode, mode)}' cannot be expressed as an "
         "SVG fill-rule; subpaths are unioned instead. Pre-combine the paths in "
         "AE if the boolean result matters.")
    return "nonzero", MERGE_NAME.get(mode, str(mode))


def unwrap_degrees(seq):
    """Remove the 2-pi discontinuity from a sampled angle track.

    atan2 returns [-180, 180], so a layer turning past half a turn produces
    ... 178.9, -178.9 ... between adjacent frames. A linear track then spins the
    element ~358 degrees backwards in one frame. Adding the right multiple of
    360 keeps the track continuous, which is what the animation actually does."""
    if not seq:
        return seq
    out = [seq[0]]
    for prev, cur in zip(seq, seq[1:]):
        delta = cur - prev
        while delta > 180.0:
            delta -= 360.0
        while delta < -180.0:
            delta += 360.0
        out.append(out[-1] + delta)
    return out


COORD_RE = re.compile(r"-?\d+(?:\.\d+)?")

#: attributes whose samples are geometry in user units, so a tolerance in pixels
#: is meaningful. Rotation and opacity tracks are tiny and left exact.
SIMPLIFY_PX = {"d", "cx", "cy", "r", "stroke-dashoffset", "stroke-dasharray",
               "stroke-width"}


def _sample_points(attr, value):
    """Parse an emitted sample back into points for error measurement."""
    nums = [float(x) for x in COORD_RE.findall(value)]
    if attr == "d":
        return list(zip(nums[0::2], nums[1::2]))
    return [(n, 0.0) for n in nums]


def decimate(points_per_sample, tol):
    """Douglas-Peucker on the TIME axis, error measured as displacement in px.

    This is not spatial path simplification, which would wreck timing. Each
    candidate for removal is judged by how far the surviving linear
    interpolation would put every point at that frame. A fast transition
    therefore cannot be dropped: dropping it produces a large displacement and
    the frame is kept. That is what makes a pixel tolerance safe here where a
    naive epsilon on the path itself is not.
    """
    n = len(points_per_sample)
    if n < 3:
        return list(range(n))
    keep = {0, n - 1}
    stack = [(0, n - 1)]
    while stack:
        a, b = stack.pop()
        if b - a < 2:
            continue
        A, B = points_per_sample[a], points_per_sample[b]
        if len(A) != len(B):
            keep.update(range(a, b + 1))
            continue
        worst = 0.0
        worst_i = None
        span = b - a
        for k in range(a + 1, b):
            K = points_per_sample[k]
            if len(K) != len(A):
                worst = float("inf")
                worst_i = k
                break
            t = (k - a) / span
            err = 0.0
            for (ax, ay), (bx, by), (kx, ky) in zip(A, B, K):
                err = max(err, math.hypot(ax + t * (bx - ax) - kx,
                                          ay + t * (by - ay) - ky))
            if err > worst:
                worst = err
                worst_i = k
        if worst > tol and worst_i is not None:
            keep.add(worst_i)
            stack.append((a, worst_i))
            stack.append((worst_i, b))
    return sorted(keep)


def measure_decimation(points_per_sample, idxs):
    """Worst displacement the decimated track introduces at ANY original frame."""
    if len(idxs) < 2:
        return 0.0
    worst = 0.0
    for a, b in zip(idxs, idxs[1:]):
        A, B = points_per_sample[a], points_per_sample[b]
        span = b - a
        for k in range(a + 1, b):
            K = points_per_sample[k]
            if len(K) != len(A):
                return float("inf")
            t = (k - a) / span
            for (ax, ay), (bx, by), (kx, ky) in zip(A, B, K):
                worst = max(worst, math.hypot(ax + t * (bx - ax) - kx,
                                              ay + t * (by - ay) - ky))
    return worst


def collapse_holds(rows):
    """Drop a sample only when it is exactly equal to BOTH neighbours.

    Lossless: holds collapse, motion never does. Anything cleverer (RDP,
    time-aware simplification) turns fast transitions into visible steps.
    """
    keep = []
    n = len(rows)
    for i, v in enumerate(rows):
        if 0 < i < n - 1 and rows[i - 1] == v and rows[i + 1] == v:
            continue
        keep.append(i)
    return keep


# --------------------------------------------------------------------------- #
# bezier / shape -> path data
# --------------------------------------------------------------------------- #
def bezier_to_d(bez, M=None, p=2):
    """python-lottie Bezier -> SVG path data, optionally baked through matrix M."""
    verts = bez.vertices
    if not verts:
        return ""
    itan = bez.in_tangents
    otan = bez.out_tangents
    closed = bool(bez.closed)
    n = len(verts)

    def P(k):
        v = verts[k]
        return M.apply(NVector(v[0], v[1])) if M else NVector(v[0], v[1])

    def C(k, tan):
        v = verts[k]
        t = tan[k] if k < len(tan) else (0, 0)
        pt = NVector(v[0] + t[0], v[1] + t[1])
        return M.apply(pt) if M else pt

    d = "M%s %s" % (num(P(0)[0], p), num(P(0)[1], p))
    segs = n if closed else n - 1
    for s in range(segs):
        a = s
        b = (s + 1) % n
        c1 = C(a, otan)
        c2 = C(b, itan)
        pe = P(b)
        d += "C%s %s %s %s %s %s" % (
            num(c1[0], p), num(c1[1], p),
            num(c2[0], p), num(c2[1], p),
            num(pe[0], p), num(pe[1], p),
        )
    if closed:
        d += "Z"
    return d


def bezier_len(bez, M=None):
    verts = bez.vertices
    if not verts:
        return 0.0
    pts = [(M.apply(NVector(v[0], v[1])) if M else NVector(v[0], v[1])) for v in verts]
    n = len(pts)
    segs = n if bez.closed else n - 1
    tot = 0.0
    for s in range(segs):
        a, b = s, (s + 1) % n
        tot += math.hypot(pts[b][0] - pts[a][0], pts[b][1] - pts[a][1])
    return tot


def bezier_arclen(bez, M=None, steps=24):
    """Flattened cubic arc length — accurate enough for trim-path dash math."""
    verts = bez.vertices
    if not verts:
        return 0.0
    itan, otan = bez.in_tangents, bez.out_tangents
    n = len(verts)
    segs = n if bez.closed else n - 1

    def pt(k, off=None):
        v = verts[k]
        x, y = v[0], v[1]
        if off is not None:
            x += off[0]
            y += off[1]
        q = NVector(x, y)
        return M.apply(q) if M else q

    tot = 0.0
    for s in range(segs):
        a, b = s, (s + 1) % n
        p0 = pt(a)
        p1 = pt(a, otan[a] if a < len(otan) else (0, 0))
        p2 = pt(b, itan[b] if b < len(itan) else (0, 0))
        p3 = pt(b)
        prev = p0
        for i in range(1, steps + 1):
            t = i / steps
            u = 1 - t
            x = (u ** 3) * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t ** 3 * p3[0]
            y = (u ** 3) * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t ** 3 * p3[1]
            tot += math.hypot(x - prev[0], y - prev[1])
            prev = (x, y)
    return tot


def circle_from_bezier(bez):
    """If a closed 4-point bezier is (near) circular, return (cx, cy, r) else None.

    AE circles exported as paths are the common case; detecting them lets the
    dot animate via cx/cy, which is the only clip-safe route.

    A rectangle also has 4 vertices equidistant from its centre, so radius
    alone is not enough. A circle path additionally has its vertices at N/E/S/W
    (one axis offset is ~0 per vertex) and carries real tangent handles.
    """
    v = bez.vertices
    if not bez.closed or len(v) != 4:
        return None
    cx = sum(p[0] for p in v) / 4.0
    cy = sum(p[1] for p in v) / 4.0
    rs = [math.hypot(p[0] - cx, p[1] - cy) for p in v]
    r = sum(rs) / 4.0
    if r <= 0:
        return None
    tol = max(0.02 * r, 0.05)
    if max(abs(x - r) for x in rs) > tol:
        return None
    # vertices must sit on the axes through the centre
    for px, py in v:
        if min(abs(px - cx), abs(py - cy)) > tol:
            return None
    # and there must be curvature: a corner-cut square has no tangents
    tans = list(bez.in_tangents) + list(bez.out_tangents)
    if not tans or max(math.hypot(t[0], t[1]) for t in tans) < 0.3 * r:
        return None
    return (cx, cy, r)


# --------------------------------------------------------------------------- #
# token config
# --------------------------------------------------------------------------- #
DEFAULT_TOKENS = {
    "roles": {
        "background": {
            "hex": ["ffffff"],
            "class": {"fill": "background-shape", "stroke": "background-stroke"},
            "token": "--icon-bg-neutral-default-primary",
        },
        "pictogram": {
            "hex": ["c2b8ae"],
            "class": {"fill": "pictogram-shape", "stroke": "pictogram-stroke"},
            "token": "--icon-static-accent-light",
        },
        "dot": {
            "hex": ["1a1414"],
            "class": {"fill": "dot-shape", "stroke": "dot-stroke"},
            "token": "--icon-neutral-primary",
        },
    }
}


def load_tokens(path=None):
    cfg = json.loads(json.dumps(DEFAULT_TOKENS))
    if path and os.path.exists(path):
        with open(path) as f:
            user = json.load(f)
        for role, spec in (user.get("roles") or {}).items():
            base = cfg["roles"].setdefault(role, {"hex": [], "class": {}, "token": ""})
            base.update(spec)
    return cfg


def _norm_hex(h):
    h = h.lstrip("#").lower()
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return h[:6]


_TAG_RE = re.compile(r"<[^>]+>")
_PAINT_RE = re.compile(r'\s+(fill|stroke)\s*=\s*"([^"]*)"')
_CLASS_RE = re.compile(r'(\sclass\s*=\s*")([^"]*)(")')


def tokenize(svg, cfg):
    """Replace known hex fill/stroke with the role's token class, dropping the
    inline paint. Idempotent. Only touches fill=/stroke= attribute values."""
    hex_role = {}
    for role, spec in cfg["roles"].items():
        for h in spec.get("hex", []):
            hex_role[_norm_hex(h)] = role

    def rewrite(tag):
        add = []

        def sub(m):
            prop, val = m.group(1), m.group(2)
            if not val.startswith("#"):
                return m.group(0)
            role = hex_role.get(_norm_hex(val))
            if role is None:
                return m.group(0)
            cls = cfg["roles"][role].get("class", {}).get(prop)
            if not cls:
                return m.group(0)
            if cls not in add:
                add.append(cls)
            return ""

        new = _PAINT_RE.sub(sub, tag)
        if not add:
            return new
        if _CLASS_RE.search(new):
            def merge(cm):
                existing = cm.group(2).split()
                for c in add:
                    if c not in existing:
                        existing.append(c)
                return cm.group(1) + " ".join(existing) + cm.group(3)
            return _CLASS_RE.sub(merge, new, count=1)
        return re.sub(r"(<[\w:.-]+)", r'\1 class="' + " ".join(add) + '"', new, count=1)

    return _TAG_RE.sub(lambda m: rewrite(m.group(0)), svg)


def css_bindings(cfg, prefix=""):
    lines = []
    for role, spec in cfg["roles"].items():
        var = spec.get("token")
        if not var:
            continue
        cls = spec.get("class", {})
        if cls.get("fill"):
            lines.append(f"{prefix}.{cls['fill']} {{ fill: var({var}); }}")
        if cls.get("stroke"):
            lines.append(f"{prefix}.{cls['stroke']} {{ stroke: var({var}); }}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# converter
# --------------------------------------------------------------------------- #
class Options:
    def __init__(self, **kw):
        self.loop_start = kw.get("loop_start")
        self.loop_end = kw.get("loop_end")
        self.trigger = kw.get("trigger", "loop")      # loop | hover | none
        self.precision = kw.get("precision", 2)
        self.prefix = kw.get("prefix")                # id namespace
        self.tokens = kw.get("tokens")                # token cfg dict or None
        self.sample_step = kw.get("sample_step", 1)   # frames between samples
        self.geometry_only = kw.get("geometry_only", False)
        self.simplify = float(kw.get("simplify", 0.0) or 0.0)
        self.title = kw.get("title")


class Converter:
    def __init__(self, doc, opts=None, warn=None):
        self.doc = doc
        self.opts = opts if opts is not None else Options()
        self.warn = warn if warn is not None else Warnings()

        # python-lottie interpolates straight through "h":1 hold keyframes, so
        # per-frame sampling would bake a smooth ramp over a step. Rewrite the
        # holds as flat-then-snap first; see kf.py RULE 1.
        n_holds = kf.expand_holds(doc)
        if n_holds:
            self.warn(f"{n_holds} hold keyframe(s) found and emitted as real steps "
                      "(python-lottie would have interpolated through them)")

        self.anim = Animation.load(doc)
        self.W = doc.get("w", 512)
        self.H = doc.get("h", 512)
        self.fr = float(doc.get("fr", 60))
        self.ip = float(doc.get("ip", 0))
        self.op = float(doc.get("op", 60))

        self.ls = self.ip if self.opts.loop_start is None else float(self.opts.loop_start)
        self.le = self.op if self.opts.loop_end is None else float(self.opts.loop_end)
        self.span = max(1e-6, self.le - self.ls)
        self.dur = self.span / self.fr

        self.layers = {l.index: l for l in self.anim.layers if l.index is not None}
        self.raw_layers = doc.get("layers", [])
        self.pfx = self.opts.prefix or slug(doc.get("nm") or "icon")
        self.root_id = f"{self.pfx}-root"

        self.defs = []
        self.body = []
        self.uid = 0
        self._clip_cache = {}
        self.stats = {"samples": 0, "tracks": 0}

        self.frames = self._frame_grid()
        self._scan()
        self._seam_check()

    def _seam_check(self):
        """A loop whose last frame does not match its first will visibly snap.
        Worth saying out loud rather than shipping the jump."""
        if self.opts.trigger != "loop" or len(self.frames) < 2:
            return
        fa, fb = self.frames[0], self.frames[-1]
        for layer in self.anim.layers:
            if not getattr(layer, "transform", None):
                continue
            if not mats_equal(self.layer_matrix(layer, fa),
                              self.layer_matrix(layer, fb), 1e-4):
                self.warn(
                    f"loop seam: '{layer.name}' does not return to its "
                    "first-frame pose at the end of the range, so the loop will "
                    "snap. Trim to the seamless window with --loop-start/"
                    "--loop-end, or match the end pose in AE.")
                return

    # -- ids ---------------------------------------------------------------- #
    def nid(self, kind):
        self.uid += 1
        return f"{self.pfx}-{kind}{self.uid}"

    # -- sampling grid ------------------------------------------------------ #
    def _frame_grid(self):
        step = max(1, int(self.opts.sample_step))
        f = math.ceil(self.ls)
        out = []
        if f > self.ls:
            out.append(self.ls)
        while f <= self.le:
            out.append(float(f))
            f += step
        if out[-1] < self.le:
            out.append(self.le)
        return out

    def keytimes(self, idxs):
        return ";".join(
            num((self.frames[i] - self.ls) / self.span, 5) for i in idxs
        )

    # -- feature scan ------------------------------------------------------- #
    def _scan(self):
        if self.doc.get("ddd") == 1:
            self.warn("comp is flagged 3D (ddd:1); only 2D transforms are emitted")
        if self.doc.get("assets"):
            self.warn("comp has assets (precomps/images); nested comps are not traversed")
        for l in self.raw_layers:
            ty = l.get("ty")
            if ty not in (3, 4):
                self.warn(f"layer '{l.get('nm')}': type {ty} unsupported "
                          "(only shape=4 and null=3 are converted)")
            tt = l.get("tt")
            if tt in (2, 4):
                self.warn(f"layer '{l.get('nm')}': inverted matte (tt:{tt}) emitted "
                          "as a normal clip — SVG clipPath cannot invert")
            if tt == 3:
                self.warn(f"layer '{l.get('nm')}': luma matte (tt:3) emitted as an "
                          "alpha clip")
            if l.get("ef"):
                self.warn(f"layer '{l.get('nm')}': effects (ef) are not converted")
            if l.get("tm"):
                self.warn(f"layer '{l.get('nm')}': time remapping is not converted")
            if l.get("hasMask"):
                self.warn(f"layer '{l.get('nm')}': layer masks are not converted")
            self._scan_shapes(l.get("shapes") or [], l.get("nm"))

    def _scan_shapes(self, shapes, nm):
        for sh in shapes:
            ty = sh.get("ty")
            if ty in ("gf", "gs"):
                self.warn(f"gradient ({ty}) in '{nm}' flattened to a solid color")
            if ty == "rp":
                self.warn(f"repeater (rp) in '{nm}' is not converted")
            if ty in ("rd", "pb", "tw", "op", "zz"):
                self.warn(f"path modifier '{ty}' in '{nm}' is not converted")
            if sh.get("it"):
                self._scan_shapes(sh["it"], nm)

    # -- matrices ----------------------------------------------------------- #
    def layer_matrix(self, layer, frame):
        """Full comp-space matrix: layer * parent * grandparent ..."""
        M = None
        cur = layer
        seen = set()
        while cur is not None and id(cur) not in seen:
            seen.add(id(cur))
            m = mat_of(cur.transform, frame)
            M = m if M is None else mat_mul(M, m)
            pi = getattr(cur, "parent_index", None)
            cur = self.layers.get(pi) if pi is not None else None
        return M if M is not None else TransformMatrix()

    def layer_matrices(self, layer):
        return [self.layer_matrix(layer, f) for f in self.frames]

    @staticmethod
    def is_static(mats):
        return all(mats_equal(mats[0], m) for m in mats[1:])

    # -- animation attribute emission --------------------------------------- #
    def anim_attrs(self):
        if self.opts.trigger == "hover":
            return (f' dur="{num(self.dur, 4)}s" begin="{self.root_id}.mouseenter;'
                    f'{self.root_id}.touchstart" repeatCount="1" fill="remove"'
                    f' restart="whenNotActive"')
        if self.opts.trigger == "none":
            return f' dur="{num(self.dur, 4)}s" begin="indefinite"'
        return f' dur="{num(self.dur, 4)}s" repeatCount="indefinite"'

    def emit_track(self, attr, values, kind="animate", type_=None, discrete=False):
        """values: one formatted string per sampled frame. Returns tag or ''."""
        if not values:
            return ""
        if all(v == values[0] for v in values):
            return ""
        idxs = self.reduce(attr, values, discrete)
        self.stats["samples"] += len(idxs)
        self.stats["tracks"] += 1
        calc = ' calcMode="discrete"' if discrete else ' calcMode="linear"'
        t = f' type="{type_}"' if type_ else ""
        kt = ""
        if idxs != list(range(len(values))):
            kt = f' keyTimes="{self.keytimes(idxs)}"'
        return (f'<{kind} attributeName="{attr}"{t}{calc}{kt}'
                f' values="{";".join(values[i] for i in idxs)}"'
                f'{self.anim_attrs()}/>')

    def reduce(self, attr, values, discrete):
        """Exact hold collapse, then optional error-bounded time decimation."""
        idxs = collapse_holds(values)
        tol = getattr(self.opts, "simplify", 0.0)
        if discrete or tol <= 0 or attr not in SIMPLIFY_PX or len(idxs) < 3:
            return idxs
        pts = [_sample_points(attr, values[i]) for i in idxs]
        keep = decimate(pts, tol)
        err = measure_decimation(pts, keep)
        if err == float("inf"):
            return idxs
        self.stats["simplify_err_px"] = max(
            self.stats.get("simplify_err_px", 0.0), round(err, 4))
        self.stats["samples_dropped"] = (self.stats.get("samples_dropped", 0)
                                        + len(idxs) - len(keep))
        return [idxs[i] for i in keep]

    # -- shape tree walking -------------------------------------------------- #
    def group_matrix(self, group, frame):
        for sh in group.shapes:
            if isinstance(sh, LO.TransformShape):
                return mat_of(sh, frame)
        return None

    @staticmethod
    def _visible(sh):
        return not getattr(sh, "hidden", False)

    @staticmethod
    def find_paint(group):
        fill = stroke = trim = merge = None
        for sh in group.shapes:
            if getattr(sh, "hidden", False):
                continue
            if isinstance(sh, LO.Fill):
                fill = sh
            elif isinstance(sh, LO.Stroke):
                stroke = sh
            elif isinstance(sh, LO.Trim):
                trim = sh
            elif isinstance(sh, LO.Merge):
                merge = sh
        return fill, stroke, trim, merge

    # -- drawables ----------------------------------------------------------- #
    def emit_drawable(self, shape, mats, paint, clipped, layer_name):
        """Return an SVG fragment for one Path/Ellipse/Rect under matrices `mats`."""
        fill, stroke, trim, _merge = paint
        p = self.opts.precision
        static = self.is_static(mats)

        # ---- paint attributes -------------------------------------------- #
        # AE paints a fill AND a stroke when both are enabled. Emitting only the
        # stroke drops the fill and turns a solid pictogram into an outline.
        if stroke is not None and not (stroke.width.get_value(self.frames[0]) or 0):
            stroke = None
        attrs = []
        if fill is not None:
            attrs.append(f'fill="{hex_of(fill.color, self.frames[0])}"')
            fr = getattr(fill.fill_rule, "value", fill.fill_rule)
            if fr == 2:
                attrs.append('fill-rule="evenodd"')
        else:
            attrs.append('fill="none"')
        if stroke is not None:
            w0 = stroke.width.get_value(self.frames[0]) or 0
            attrs.append(f'stroke="{hex_of(stroke.color, self.frames[0])}"')
            attrs.append(f'stroke-width="{num(w0 * mat_scalefactor(mats[0]), p)}"')
            lc = {1: "butt", 2: "round", 3: "square"}.get(
                getattr(stroke.line_cap, "value", stroke.line_cap) or 2, "round")
            lj = {1: "miter", 2: "round", 3: "bevel"}.get(
                getattr(stroke.line_join, "value", stroke.line_join) or 2, "round")
            attrs.append(f'stroke-linecap="{lc}"')
            attrs.append(f'stroke-linejoin="{lj}"')
        if fill is None and stroke is None:
            self.warn(f"shape in '{layer_name}' has no enabled fill or stroke; "
                      "emitted unpainted")

        for src, name in ((fill, "fill"), (stroke, "stroke")):
            if src is not None and getattr(src, "opacity", None) is not None:
                o = src.opacity.get_value(self.frames[0])
                if o is not None and abs(o - 100) > 1e-6:
                    attrs.append(f'{name}-opacity="{num(o / 100.0, 3)}"')

        # ---- circles: geometry animation (clip-safe) --------------------- #
        circ = self.circle_of(shape)
        if circ is not None and trim is None:
            cxs, cys, rs = [], [], []
            for m, f in zip(mats, self.frames):
                cx, cy, r = self.circle_at(shape, f)
                c = m.apply(NVector(cx, cy))
                cxs.append(num(c[0], p))
                cys.append(num(c[1], p))
                rs.append(num(r * mat_scalefactor(m), p))
            inner = (self.emit_track("cx", cxs)
                     + self.emit_track("cy", cys)
                     + self.emit_track("r", rs))
            head = (f'<circle cx="{cxs[0]}" cy="{cys[0]}" r="{rs[0]}" '
                    + " ".join(attrs))
            return f"{head}>{inner}</circle>" if inner else f"{head}/>"

        # ---- path data ---------------------------------------------------- #
        morph = self.shape_animated(shape)

        if static and not morph:
            d = self.shape_d(shape, self.frames[0], mats[0])
            body = self.trim_frag(trim, shape, mats, attrs)
            return f'<path d="{d}" {" ".join(attrs)}{body[0]}>{body[1]}</path>' \
                if body[1] else f'<path d="{d}" {" ".join(attrs)}{body[0]}/>'

        if clipped or self.opts.geometry_only or morph:
            # Bake `d` per frame: geometry, therefore clip-safe. Also the only
            # correct route for a morph riding an animated transform.
            ds = [self.shape_d(shape, f, m) for f, m in zip(self.frames, mats)]
            if not self.point_compatible(ds):
                self.warn(f"path in '{layer_name}': vertex count changes across "
                          "frames; SVG cannot interpolate that — first frame held")
                ds = [ds[0]] * len(ds)
            track = self.emit_track("d", ds)
            extra = self.trim_frag(trim, shape, mats, attrs)
            head = f'<path d="{ds[0]}" {" ".join(attrs)}{extra[0]}'
            inner = track + extra[1]
            return f"{head}>{inner}</path>" if inner else f"{head}/>"

        # ---- unclipped animated transform: nested animateTransform -------- #
        d = self.shape_d(shape, self.frames[0], None)
        sw_fix = None
        if stroke is not None:
            # stroke-width is in local units now that the transform is live
            w0 = stroke.width.get_value(self.frames[0]) or 0
            attrs = [a for a in attrs if not a.startswith("stroke-width=")]
            attrs.append(f'stroke-width="{num(w0, p)}"')
            sw_fix = True
        extra = self.trim_frag(trim, shape, mats, attrs, local=True)
        inner_path = (f'<path d="{d}" {" ".join(attrs)}{extra[0]}>{extra[1]}</path>'
                      if extra[1] else f'<path d="{d}" {" ".join(attrs)}{extra[0]}/>')
        return self.wrap_animated_transform(mats, inner_path)

    def wrap_animated_transform(self, mats, inner):
        """Nested <g> translate / rotate / scale, per-frame sampled."""
        p = self.opts.precision
        tx, angles, sc = [], [], []
        for m in mats:
            t = m.extract_transform()
            tr = t["translation"]
            tx.append(f"{num(tr[0], p)} {num(tr[1], p)}")
            # python-lottie's extractor returns the negated angle relative to
            # SVG's rotate() (both AE and SVG are clockwise-positive, y-down).
            angles.append(-math.degrees(t["angle"]))
            s = t["scale"]
            sc.append(f"{num(s[0], 5)} {num(s[1], 5)}")
            if abs(t.get("skew_angle", 0) or 0) > 1e-6:
                self.warn("a layer transform contains skew; skew is not emitted")
        # atan2 wraps at +-180; interpolating across the wrap spins the element
        # a full turn backwards in a single frame.
        angles = unwrap_degrees(angles)
        worst = max((abs(b - a) for a, b in zip(angles, angles[1:])), default=0.0)
        if worst > 90.0:
            self.warn(f"a rotating layer moves {worst:.0f} degrees between adjacent "
                      "frames after unwrapping; check it is genuinely spinning.")
        rot = [num(a, 4) for a in angles]
        t0 = tx[0].split()
        r0 = rot[0]
        s0 = sc[0].split()
        out = inner
        out = (f'<g transform="scale({s0[0]},{s0[1]})">'
               + self.emit_track("transform", sc, kind="animateTransform", type_="scale")
               + out + "</g>")
        out = (f'<g transform="rotate({r0})">'
               + self.emit_track("transform", rot, kind="animateTransform", type_="rotate")
               + out + "</g>")
        out = (f'<g transform="translate({t0[0]},{t0[1]})">'
               + self.emit_track("transform", tx, kind="animateTransform", type_="translate")
               + out + "</g>")
        return out

    # -- shape accessors ----------------------------------------------------- #
    @staticmethod
    def shape_animated(shape):
        if isinstance(shape, LO.Path):
            return bool(shape.shape.animated)
        if isinstance(shape, LO.Ellipse):
            return bool(shape.position.animated or shape.size.animated)
        if isinstance(shape, LO.Rect):
            return bool(shape.position.animated or shape.size.animated
                        or (shape.rounded and shape.rounded.animated))
        return False

    def circle_of(self, shape):
        if isinstance(shape, LO.Ellipse):
            s = shape.size.get_value(self.frames[0])
            if s is not None and abs(s[0] - s[1]) <= max(0.02 * s[0], 0.05):
                return True
            self.warn("non-circular ellipse: emitted as a baked path "
                      "(clip-safe geometry animation still applies)")
            return None
        if isinstance(shape, LO.Path) and not shape.shape.animated:
            return True if circle_from_bezier(shape.shape.get_value(self.frames[0])) else None
        return None

    def circle_at(self, shape, frame):
        if isinstance(shape, LO.Ellipse):
            pos = shape.position.get_value(frame)
            size = shape.size.get_value(frame)
            return (pos[0], pos[1], (size[0] + size[1]) / 4.0)
        c = circle_from_bezier(shape.shape.get_value(frame))
        return c

    def shape_d(self, shape, frame, M):
        p = self.opts.precision
        if isinstance(shape, LO.Path):
            return bezier_to_d(shape.shape.get_value(frame), M, p)
        if isinstance(shape, LO.Ellipse):
            pos = shape.position.get_value(frame)
            size = shape.size.get_value(frame)
            rx, ry = size[0] / 2.0, size[1] / 2.0
            k = 0.5519150244935105707435627
            cx, cy = pos[0], pos[1]
            pts = [(cx, cy - ry), (cx + rx, cy), (cx, cy + ry), (cx - rx, cy)]
            tans = [((rx * k, 0), (-rx * k, 0)),
                    ((0, ry * k), (0, -ry * k)),
                    ((-rx * k, 0), (rx * k, 0)),
                    ((0, -ry * k), (0, ry * k))]
            bez = LO.Bezier()
            for (px, py), (o, i) in zip(pts, tans):
                bez.add_point(NVector(px, py), NVector(*i), NVector(*o))
            bez.closed = True
            return bezier_to_d(bez, M, p)
        if isinstance(shape, LO.Rect):
            pos = shape.position.get_value(frame)
            size = shape.size.get_value(frame)
            x, y = pos[0] - size[0] / 2.0, pos[1] - size[1] / 2.0
            corners = [(x, y), (x + size[0], y), (x + size[0], y + size[1]), (x, y + size[1])]
            bez = LO.Bezier()
            for cx, cy in corners:
                bez.add_point(NVector(cx, cy))
            bez.closed = True
            if shape.rounded is not None and (shape.rounded.get_value(frame) or 0) > 0:
                self.warn("rounded-rect corner radius is approximated as square")
            return bezier_to_d(bez, M, p)
        return ""

    @staticmethod
    def point_compatible(ds):
        counts = {d.count("C") for d in ds}
        return len(counts) == 1

    # -- trim paths ---------------------------------------------------------- #
    def trim_frag(self, trim, shape, mats, attrs, local=False):
        """(extra_attrs, inner_tags) implementing a trim path as dash geometry."""
        if trim is None:
            return ("", "")
        p = self.opts.precision
        arrs, offs = [], []
        for f, m in zip(self.frames, mats):
            M = None if local else m
            if isinstance(shape, LO.Path):
                bez = shape.shape.get_value(f)
            else:
                bez = None
            L = bezier_arclen(bez, M) if bez is not None else 0.0
            if L <= 0:
                arrs.append("0 1")
                offs.append("0")
                continue
            s = (trim.start.get_value(f) or 0) / 100.0
            e = (trim.end.get_value(f) or 0) / 100.0
            o = (trim.offset.get_value(f) or 0) / 360.0
            lo, hi = min(s, e), max(s, e)
            vis = max(0.0, (hi - lo)) * L
            vis = max(vis, 0.001 * L)   # keep a hairline dot visible
            start = (lo + o) * L
            arrs.append(f"{num(vis, 3)} {num(L, p)}")
            offs.append(num(-start, p))
        extra = f' stroke-dasharray="{arrs[0]}" stroke-dashoffset="{offs[0]}"'
        inner = (self.emit_track("stroke-dasharray", arrs)
                 + self.emit_track("stroke-dashoffset", offs))
        return (extra, inner)

    # -- layer emission ------------------------------------------------------ #
    def emit_layer(self, layer, clipped):
        if isinstance(layer, LO.NullLayer) or not getattr(layer, "shapes", None):
            return None
        mats_layer = self.layer_matrices(layer)
        name = layer.name or f"layer{layer.index}"
        pieces = []

        def walk(container, mats, inherited=(None, None, None, None)):
            groups = [s for s in container if isinstance(s, LO.Group)
                      and not getattr(s, "hidden", False)]
            loose = [s for s in container
                     if isinstance(s, (LO.Path, LO.Ellipse, LO.Rect))
                     and not getattr(s, "hidden", False)]
            if loose:
                paint = self.merge_paint(self.find_paint_list(container), inherited)
                for sh in loose:
                    frag = self.emit_drawable(sh, mats, paint, clipped, name)
                    if frag:
                        pieces.append(frag)
            for g in groups:
                gm = [self.group_matrix(g, f) for f in self.frames]
                if gm[0] is not None:
                    mats2 = [mat_mul(a, b) for a, b in zip(gm, mats)]
                else:
                    mats2 = mats
                # A fill or stroke declared on an outer group paints the paths in
                # its nested groups too. Without inheriting it, those paths came
                # out fill="none" — a dot that is present but invisible.
                paint = self.merge_paint(self.find_paint(g), inherited)
                drawables = [s for s in g.shapes
                             if isinstance(s, (LO.Path, LO.Ellipse, LO.Rect))
                             and not getattr(s, "hidden", False)]
                merge = paint[3]
                if merge is not None and len(drawables) > 1:
                    frag = self.merge_drawables(drawables, mats2, paint, clipped, name)
                    if frag:
                        pieces.append(frag)
                else:
                    for sh in drawables:
                        frag = self.emit_drawable(sh, mats2, paint, clipped, name)
                        if frag:
                            pieces.append(frag)
                nested = [s for s in g.shapes if isinstance(s, LO.Group)]
                if nested:
                    walk(nested, mats2, paint)

        walk(layer.shapes, mats_layer)
        if not pieces:
            return None
        content = "".join(pieces)

        # layer opacity
        op = layer.transform.opacity
        if op is not None:
            vals = [num((op.get_value(f) or 0) / 100.0, 3) for f in self.frames]
            track = self.emit_track("opacity", vals)
            if track:
                content = f'<g opacity="{vals[0]}">{track}{content}</g>'
            elif abs((op.get_value(self.frames[0]) or 100) - 100) > 1e-6:
                content = f'<g opacity="{vals[0]}">{content}</g>'

        # in/out gating inside the loop window
        ip = float(getattr(layer, "in_point", self.ls) or self.ls)
        opt = float(getattr(layer, "out_point", self.le) or self.le)
        if ip > self.ls + 1e-6 or opt < self.le - 1e-6:
            vis = ["1" if (ip - 1e-6) <= f <= (opt + 1e-6) else "0" for f in self.frames]
            track = self.emit_track("opacity", vis, discrete=True)
            if track:
                content = f'<g opacity="{vis[0]}">{track}{content}</g>'
        return content

    @staticmethod
    def merge_paint(local, inherited):
        """Local fill/stroke/trim/merge wins; anything absent is inherited."""
        return tuple(l if l is not None else i for l, i in zip(local, inherited))

    def find_paint_list(self, container):
        fill = stroke = trim = merge = None
        for sh in container:
            if getattr(sh, "hidden", False):
                continue
            if isinstance(sh, LO.Fill):
                fill = sh
            elif isinstance(sh, LO.Stroke):
                stroke = sh
            elif isinstance(sh, LO.Trim):
                trim = sh
            elif isinstance(sh, LO.Merge):
                merge = sh
        return fill, stroke, trim, merge

    def merge_drawables(self, drawables, mats, paint, clipped, name):
        p = self.opts.precision
        fill, stroke, trim, merge = paint
        rule, mode_name = merge_fill_rule(merge, self.warn)
        self.warn(f"merge paths in '{name}': mode '{mode_name}' emitted as "
                  f"fill-rule {rule}")
        attrs = []
        if fill is not None:
            attrs.append(f'fill="{hex_of(fill.color, self.frames[0])}"')
        else:
            attrs.append('fill="none"')
        attrs.append(f'fill-rule="{rule}"')
        ds = []
        for f, m in zip(self.frames, mats):
            ds.append("".join(self.shape_d(s, f, m) for s in drawables))
        if all(x == ds[0] for x in ds):
            return f'<path d="{ds[0]}" {" ".join(attrs)}/>'
        if not self.point_compatible(ds):
            self.warn(f"merged path in '{name}' is not point-compatible; held")
            return f'<path d="{ds[0]}" {" ".join(attrs)}/>'
        return (f'<path d="{ds[0]}" {" ".join(attrs)}>'
                + self.emit_track("d", ds) + "</path>")

    # -- mattes -------------------------------------------------------------- #
    def clip_for(self, src_layer):
        cached = self._clip_cache.get(src_layer.index)
        if cached:
            return cached
        """Build the clip from the matte source, ANIMATED if the matte moves.

        Freezing the clip at frame 0 while the clipped dots travel their real
        paths is what makes dots appear to leave their mask. Animating the
        clipPath's own path data keeps them in register, and stays geometry, so
        it cannot promote a compositor layer.
        """
        mats = self.layer_matrices(src_layer)
        tracks = []

        def walk(container, mlist):
            for sh in container:
                if isinstance(sh, LO.Group):
                    gms = [self.group_matrix(sh, f) for f in self.frames]
                    m2 = [mat_mul(g, m) if g is not None else m
                          for g, m in zip(gms, mlist)]
                    walk(sh.shapes, m2)
                elif isinstance(sh, (LO.Path, LO.Ellipse, LO.Rect)):
                    tracks.append([self.shape_d(sh, f, m)
                                   for f, m in zip(self.frames, mlist)])

        walk(src_layer.shapes or [], mats)

        paths = []
        for ds in tracks:
            if not ds:
                continue
            if all(x == ds[0] for x in ds):
                paths.append(f'<path d="{ds[0]}"/>')
            elif not self.point_compatible(ds):
                self.warn(f"matte source '{src_layer.name}' changes vertex count "
                          "between frames; the clip is held at the first frame")
                paths.append(f'<path d="{ds[0]}"/>')
            else:
                paths.append(f'<path d="{ds[0]}">{self.emit_track("d", ds)}</path>')
        cid = self.nid("clip")
        self.defs.append(f'<clipPath id="{cid}" clipPathUnits="userSpaceOnUse">'
                         + "".join(paths) + "</clipPath>")
        self._clip_cache[src_layer.index] = cid
        return cid

    # -- assemble ------------------------------------------------------------ #
    def build(self):
        raw = self.raw_layers
        for i in range(len(raw) - 1, -1, -1):
            rl = raw[i]
            if rl.get("td"):
                continue                       # matte source: consumed, not drawn
            if rl.get("hd"):
                # Eye switched off in AE. Bodymovin still exports the layer
                # because a visible child may be parented to it — invisible
                # rotation controllers are exactly this pattern. Its transform
                # stays available through the parent chain; it must not be drawn.
                continue
            layer = self.layers.get(rl.get("ind"))
            if layer is None:
                continue
            tt = rl.get("tt")
            src_ind = None
            if tt:
                # newer Bodymovin names the source explicitly; otherwise AE's
                # rule is "the matte layer sits directly above the matted one",
                # i.e. an earlier index in this top-first array.
                src_ind = rl.get("tp")
                if src_ind is None:
                    for j in range(i - 1, -1, -1):
                        if raw[j].get("td"):
                            src_ind = raw[j].get("ind")
                            break
                if src_ind is None:
                    for j in range(i + 1, len(raw)):
                        if raw[j].get("td"):
                            src_ind = raw[j].get("ind")
                            break
                if src_ind is None:
                    self.warn(f"layer '{rl.get('nm')}' is matted (tt:{tt}) but no "
                              "matte source layer was found; drawn unclipped")
            content = self.emit_layer(layer, clipped=bool(tt))
            if content is None:
                continue
            if tt and src_ind in self.layers:
                cid = self.clip_for(self.layers[src_ind])
                content = f'<g clip-path="url(#{cid})">{content}</g>'
            self.body.append(content)
        return self.svg()

    def svg(self):
        title = self.opts.title or self.doc.get("nm") or "icon"
        defs = f"<defs>{''.join(self.defs)}</defs>" if self.defs else ""
        style = ""
        out = (
            f'<svg xmlns="http://www.w3.org/2000/svg" id="{self.root_id}" '
            f'viewBox="0 0 {num(self.W, 0)} {num(self.H, 0)}" '
            f'width="{num(self.W, 0)}" height="{num(self.H, 0)}" '
            f'role="img" aria-label="{title}">'
            f"{defs}{style}{''.join(self.body)}</svg>"
        )
        if self.opts.tokens:
            out = tokenize(out, self.opts.tokens)
        return out


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #
def convert_dict(doc, opts=None):
    warn = Warnings()
    c = Converter(doc, opts, warn)
    svg = c.build()
    ok, err = validate(svg)
    if not ok:
        warn(f"emitted SVG is not well-formed XML: {err}")
    return svg, list(warn), c.stats


def convert_file(path, opts=None):
    with open(path) as f:
        doc = json.load(f)
    if opts is not None and opts.prefix is None:
        opts.prefix = slug(os.path.splitext(os.path.basename(path))[0])
    return convert_dict(doc, opts)


def validate(svg):
    try:
        ET.fromstring(svg)
        return True, None
    except ET.ParseError as e:
        return False, str(e)
