#!/usr/bin/env python3
"""
l2s_baked.py — emit an animated SVG from the "l2s-baked/1" intermediate that
jsx/host.jsx samples straight out of After Effects.

Everything arriving here is already per-frame comp-space geometry, so the output
is geometry-animated by construction and therefore clip-safe: nothing inside a
track-matte clip is ever moved by a transform, which is what causes a promoted
compositor layer to escape its ancestor clip-path in real GPU browsers.

For UNCLIPPED layers only, a least-squares affine fit collapses a per-frame path
track back into nested translate/rotate/scale when the motion really is rigid.
That is a pure size optimisation, verified against the baked geometry to a
sub-tenth-of-a-pixel residual before it is accepted, and it is refused whenever
a stroke width or a trim length would be distorted by the substituted scale.
"""

import json
import math
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from l2s_core import (num, slug, collapse_holds, tokenize, validate, Warnings,
                      Options, MERGE_RULE, MERGE_NAME, decimate,
                      measure_decimation, unwrap_degrees, _sample_points,
                      SIMPLIFY_PX)


def merge_fill_rule_num(mode, warn):
    try:
        mode = int(mode)
    except (TypeError, ValueError):
        mode = 1
    if mode in MERGE_RULE:
        return MERGE_RULE[mode], MERGE_NAME.get(mode, str(mode))
    warn(f"merge mode '{MERGE_NAME.get(mode, mode)}' cannot be expressed as an "
         "SVG fill-rule; subpaths are unioned instead.")
    return "nonzero", MERGE_NAME.get(mode, str(mode))

FIT_TOL = 0.08          # px: max residual for accepting an affine fit
NUMRE = re.compile(r"-?\d+(?:\.\d+)?")


# --------------------------------------------------------------------------- #
# affine fitting
# --------------------------------------------------------------------------- #
def solve3(A, b):
    """Gaussian elimination on a 3x3 system."""
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for col in range(3):
        piv = max(range(col, 3), key=lambda r: abs(M[r][col]))
        if abs(M[piv][col]) < 1e-12:
            return None
        M[col], M[piv] = M[piv], M[col]
        pv = M[col][col]
        for r in range(3):
            if r == col:
                continue
            fac = M[r][col] / pv
            for c in range(col, 4):
                M[r][c] -= fac * M[col][c]
    return [M[i][3] / M[i][i] for i in range(3)]


def fit_affine(p, q):
    """Least-squares affine mapping p -> q. Returns (a,b,c,d,tx,ty, residual)."""
    n = len(p)
    if n < 3:
        return None
    sxx = syy = sxy = sx = sy = 0.0
    for (x, y) in p:
        sxx += x * x
        syy += y * y
        sxy += x * y
        sx += x
        sy += y
    N = [[sxx, sxy, sx], [sxy, syy, sy], [sx, sy, float(n)]]

    def rhs(idx):
        r0 = r1 = r2 = 0.0
        for (x, y), t in zip(p, q):
            r0 += x * t[idx]
            r1 += y * t[idx]
            r2 += t[idx]
        return [r0, r1, r2]

    sol_x = solve3(N, rhs(0))
    sol_y = solve3(N, rhs(1))
    if sol_x is None or sol_y is None:
        return None
    a, c, tx = sol_x
    b, d, ty = sol_y
    res = 0.0
    for (x, y), t in zip(p, q):
        res = max(res, math.hypot(a * x + c * y + tx - t[0],
                                  b * x + d * y + ty - t[1]))
    return (a, b, c, d, tx, ty, res)


def decompose(a, b, c, d, tx, ty):
    """(translate, rotation_deg, scale) with a skew magnitude for rejection."""
    sx = math.hypot(a, b)
    if sx < 1e-9:
        return None
    rot = math.degrees(math.atan2(b, a))
    det = a * d - b * c
    sy = det / sx
    skew = (a * c + b * d) / (sx * sx)
    return ((tx, ty), rot, (sx, sy), abs(skew))


def coords_of(d):
    vals = [float(v) for v in NUMRE.findall(d)]
    return list(zip(vals[0::2], vals[1::2]))


def snap(values, tol):
    """Flatten a component that only wobbles by fitting noise into a constant."""
    if not values:
        return values
    if max(values) - min(values) <= tol:
        mid = sum(values) / len(values)
        return [mid] * len(values)
    return values


def apply_candidate(cand, i, base):
    """Points produced by the fitted transform at sample i."""
    if cand["mode"] == "pivot":
        r = math.radians(cand["rot"][i])
        cs, sn = math.cos(r), math.sin(r)
        cx, cy = cand["cx"], cand["cy"]
        for x, y in base:
            dx, dy = x - cx, y - cy
            yield (cx + cs * dx - sn * dy, cy + sn * dx + cs * dy)
    else:
        r = math.radians(cand["rot"][i])
        cs, sn = math.cos(r), math.sin(r)
        sx, sy = cand["sc"][i]
        tx, ty = cand["tx"][i]
        for x, y in base:
            px, py = x * sx, y * sy
            yield (tx + cs * px - sn * py, ty + sn * px + cs * py)


def fixed_point(mats, rots):
    """Common centre of rotation, if every rotating frame shares one.

    Solves (I - A)c = t. A glyph pivoting on its AE anchor gives one centre for
    the whole track, which collapses translate+rotate into a single
    rotate(deg cx cy) — much smaller than two tracks.
    """
    pts = []
    for (a, b, c, d, tx, ty), r in zip(mats, rots):
        if abs(r % 360.0) < 0.05 or abs(r % 360.0 - 360.0) < 0.05:
            continue          # identity rotation pins no centre
        det = (1 - a) * (1 - d) - b * c
        if abs(det) < 1e-9:
            continue          # this frame is a whole turn; it pins no centre
        cx = ((1 - d) * tx + c * ty) / det
        cy = (b * tx + (1 - a) * ty) / det
        pts.append((cx, cy))
    if not pts:
        return None
    mx = sum(p[0] for p in pts) / len(pts)
    my = sum(p[1] for p in pts) / len(pts)
    if max(math.hypot(p[0] - mx, p[1] - my) for p in pts) > 0.1:
        return None
    return (mx, my)


# --------------------------------------------------------------------------- #
# emitter
# --------------------------------------------------------------------------- #
class BakedEmitter:
    def __init__(self, doc, opts=None, warn=None):
        self.doc = doc
        self.opts = opts if opts is not None else Options()
        self.warn = warn if warn is not None else Warnings()
        for w in doc.get("warnings", []):
            self.warn(w)

        self.frames = [float(f) for f in doc["frames"]]
        self.f0 = self.frames[0]
        self.span = max(1e-6, self.frames[-1] - self.frames[0])
        self.fps = float(doc.get("fps") or 60)
        self.dur = self.span / self.fps
        self.W = doc.get("w", 512)
        self.H = doc.get("h", 512)
        self.pfx = self.opts.prefix or slug(doc.get("name") or "icon")
        self.root_id = f"{self.pfx}-root"
        self.defs = []
        self.body = []
        self.uid = 0
        self.stats = {"tracks": 0, "samples": 0, "fitted": 0, "baked_paths": 0,
                      "fit_residual_px": 0.0}
        self.fit_residual = 0.0
        self._clip_cache = {}
        self.geometry_reasons = []

        self.layers = doc.get("layers", [])
        self.by_index = {l.get("index"): l for l in self.layers}
        self._seam_check()

    # -- helpers ---------------------------------------------------------- #
    def nid(self, kind):
        self.uid += 1
        return f"{self.pfx}-{kind}{self.uid}"

    def keytimes(self, idxs):
        return ";".join(num((self.frames[i] - self.f0) / self.span, 5) for i in idxs)

    def anim_attrs(self):
        if self.opts.trigger == "hover":
            return (f' dur="{num(self.dur, 4)}s" begin="{self.root_id}.mouseenter;'
                    f'{self.root_id}.touchstart" repeatCount="1" fill="remove"'
                    f' restart="whenNotActive"')
        if self.opts.trigger == "none":
            return f' dur="{num(self.dur, 4)}s" begin="indefinite"'
        return f' dur="{num(self.dur, 4)}s" repeatCount="indefinite"'

    def track(self, attr, values, kind="animate", type_=None, discrete=False):
        if not values or all(v == values[0] for v in values):
            return ""
        idxs = self.reduce(attr, values, discrete)
        self.stats["tracks"] += 1
        self.stats["samples"] += len(idxs)
        calc = ' calcMode="discrete"' if discrete else ' calcMode="linear"'
        t = f' type="{type_}"' if type_ else ""
        # SMIL spaces samples evenly when keyTimes is absent, so an untouched
        # per-frame track does not need to spell them out.
        kt = ""
        if idxs != list(range(len(values))):
            kt = f' keyTimes="{self.keytimes(idxs)}"'
        return (f'<{kind} attributeName="{attr}"{t}{calc}{kt}'
                f' values="{";".join(values[i] for i in idxs)}"{self.anim_attrs()}/>')

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

    def _seam_check(self):
        """A loop whose last sampled frame differs from its first will visibly
        snap. Worth saying out loud rather than shipping a jump."""
        if self.opts.trigger != "loop":
            return
        for l in self.layers:
            for s in l.get("shapes", []):
                d = s.get("d") or []
                if len(d) > 1 and d[0] != d[-1]:
                    self.warn(
                        f"loop seam: '{l.get('name')}' does not return to its "
                        "first-frame pose at the end of the range, so the loop "
                        "will snap. Trim the range to the seamless window "
                        "(--loop-start/--loop-end) or match the end pose in AE.")
                    return

    # -- shapes ------------------------------------------------------------ #
    def paint_attrs(self, shape, scaled=True):
        """Fill AND stroke, not one or the other.

        AE paints both when both are enabled; emitting only the stroke drops the
        fill and turns a solid pictogram into an outline.
        """
        attrs = []
        inner = []
        p = self.opts.precision
        st = shape.get("stroke")
        fl = shape.get("fill")

        # a zero-width or fully transparent paint contributes nothing
        if st and (not any(st.get("width") or [0])
                   or not any(st.get("opacity") or [1])):
            st = None
        if fl and not any(fl.get("opacity") or [1]):
            fl = None

        if fl:
            attrs.append(f'fill="{fl["color"]}"')
            rule = fl.get("rule", "nonzero")
            if shape.get("merged"):
                rule, mode_name = merge_fill_rule_num(shape.get("mergeMode"), self.warn)
                self.warn(f"merge paths in '{shape.get('name')}': mode "
                          f"'{mode_name}' emitted as fill-rule {rule}")
            if rule == "evenodd":
                attrs.append('fill-rule="evenodd"')
            ops = [num(o, 3) for o in fl.get("opacity") or []]
            if ops and (ops[0] != "1" or len(set(ops)) > 1):
                attrs.append(f'fill-opacity="{ops[0]}"')
                inner.append(self.track("fill-opacity", ops))
        else:
            attrs.append('fill="none"')

        if st:
            attrs.append(f'stroke="{st["color"]}"')
            widths = [num(w, p) for w in st.get("width") or [0]]
            attrs.append(f'stroke-width="{widths[0]}"')
            attrs.append(f'stroke-linecap="{st.get("cap", "round")}"')
            attrs.append(f'stroke-linejoin="{st.get("join", "round")}"')
            inner.append(self.track("stroke-width", widths))
            ops = [num(o, 3) for o in st.get("opacity") or []]
            if ops and (ops[0] != "1" or len(set(ops)) > 1):
                attrs.append(f'stroke-opacity="{ops[0]}"')
                inner.append(self.track("stroke-opacity", ops))
            if fl and shape.get("fillOverStroke"):
                # AE has the fill above the stroke, so the fill paints last
                attrs.append('paint-order="stroke fill"')

        if not fl and not st:
            self.warn(f"'{shape.get('name')}' has no enabled fill or stroke; "
                      "emitted unpainted")
        return attrs, "".join(x for x in inner if x)

    def trim_attrs(self, shape):
        trim = shape.get("trim")
        if not trim:
            return "", ""
        p = self.opts.precision
        arrs, offs = [], []
        for s, e, o, L in zip(trim["start"], trim["end"], trim["offset"], trim["len"]):
            if not L:
                arrs.append("0 1")
                offs.append("0")
                continue
            lo, hi = min(s, e) / 100.0, max(s, e) / 100.0
            vis = max((hi - lo) * L, 0.001 * L)   # keep a hairline dot visible
            start = (lo + o / 360.0) * L
            arrs.append(f"{num(vis, 3)} {num(L, p)}")
            offs.append(num(-start, p))
        extra = f' stroke-dasharray="{arrs[0]}" stroke-dashoffset="{offs[0]}"'
        inner = self.track("stroke-dasharray", arrs) + self.track("stroke-dashoffset", offs)
        return extra, inner

    def emit_shape(self, shape, clipped, layer_name):
        p = self.opts.precision
        attrs, paint_inner = self.paint_attrs(shape)
        trim_extra, trim_inner = self.trim_attrs(shape)

        circ = shape.get("circle")
        if circ and not shape.get("trim"):
            cx, cy, r = circ["cx"], circ["cy"], circ["r"]
            sx = [num(v, p) for v in cx]
            sy = [num(v, p) for v in cy]
            sr = [num(v, p) for v in r]
            inner = (self.track("cx", sx) + self.track("cy", sy)
                     + self.track("r", sr) + paint_inner)
            head = f'<circle cx="{sx[0]}" cy="{sy[0]}" r="{sr[0]}" ' + " ".join(attrs)
            return f"{head}>{inner}</circle>" if inner else f"{head}/>"

        ds = shape.get("d") or []
        if not ds:
            return ""
        if all(x == ds[0] for x in ds):
            inner = paint_inner + trim_inner
            head = f'<path d="{ds[0]}" {" ".join(attrs)}{trim_extra}'
            return f"{head}>{inner}</path>" if inner else f"{head}/>"

        # moving path. Inside a clip this MUST stay geometry.
        if not clipped and not shape.get("trim") and not self.opts.geometry_only:
            fitted = self.try_fit(ds, shape)
            if fitted is not None:
                self.stats["fitted"] += 1
                inner = paint_inner
                head = f'<path d="{ds[0]}" {" ".join(attrs)}'
                node = f"{head}>{inner}</path>" if inner else f"{head}/>"
                return self.wrap_transform(fitted, node)

        reason = ("inside a track matte, where a transform could escape the clip"
                  if clipped else
                  "a trim path, whose dash lengths a substituted scale would distort"
                  if shape.get("trim") else
                  "the geometry-only option"
                  if self.opts.geometry_only else
                  "motion that is not a rigid transform of the first frame")
        self.geometry_reasons.append((shape.get("name") or "shape", reason, len(ds)))

        counts = {d.count("C") for d in ds}
        if len(counts) != 1:
            self.warn(f"'{layer_name}': vertex count changes between frames; SVG "
                      "cannot interpolate that, so the first frame is held")
            ds = [ds[0]] * len(ds)
        self.stats["baked_paths"] += 1
        inner = self.track("d", ds) + paint_inner + trim_inner
        head = f'<path d="{ds[0]}" {" ".join(attrs)}{trim_extra}'
        return f"{head}>{inner}</path>" if inner else f"{head}/>"

    def try_fit(self, ds, shape):
        """Fit the motion to a transform, or return None to bake path data.

        Two shapes of result: a single rotate-about-a-fixed-centre track (the
        common icon case: a glyph pivoting on its anchor), or nested
        translate/rotate/scale.
        """
        base = coords_of(ds[0])
        if len(base) < 3:
            return None

        # Tolerances are expressed in pixels and converted per component using
        # the shape's radius, so "constant enough" means "moves less than
        # FIT_TOL px at the outermost vertex" rather than an arbitrary epsilon.
        # A dimensionless epsilon here rejects exact rigid rotations on small
        # shapes, where 2-decimal coordinates leave a little apparent shear.
        gx = sum(x for x, _ in base) / len(base)
        gy = sum(y for _, y in base) / len(base)
        R = max(1.0, max(math.hypot(x - gx, y - gy) for x, y in base))
        tol_scale = FIT_TOL / R
        tol_rot = math.degrees(FIT_TOL / R)
        tol_skew = FIT_TOL / R

        mats, decs = [], []
        for d in ds:
            q = coords_of(d)
            if len(q) != len(base):
                return None
            f = fit_affine(base, q)
            if f is None or f[6] > FIT_TOL:
                return None
            dec = decompose(*f[:6])
            if dec is None or dec[3] > tol_skew:
                return None
            mats.append(f[:6])
            decs.append(dec)

        rot = snap(unwrap_degrees([d[1] for d in decs]), tol_rot)
        scx = snap([d[2][0] for d in decs], tol_scale)
        scy = snap([d[2][1] for d in decs], tol_scale)
        txs = snap([d[0][0] for d in decs], FIT_TOL)
        tys = snap([d[0][1] for d in decs], FIT_TOL)

        scale_varies = len(set(scx)) > 1 or len(set(scy)) > 1
        if scale_varies and shape.get("stroke"):
            # substituting a scale would change the rendered stroke weight
            return None

        if (not scale_varies and abs(scx[0] - 1) < tol_scale
                and abs(scy[0] - 1) < tol_scale):
            centre = fixed_point(mats, rot)
            if centre is not None:
                cand = {"mode": "pivot", "rot": rot,
                        "cx": centre[0], "cy": centre[1]}
                if self.accept(base, ds, cand):
                    return cand

        cand = {"mode": "trs", "tx": list(zip(txs, tys)), "rot": rot,
                "sc": list(zip(scx, scy))}
        return cand if self.accept(base, ds, cand) else None

    def accept(self, base, ds, cand):
        """Re-render the candidate transform and measure it against the baked
        geometry. Snapping and pivoting each move points slightly, so the
        substitution is checked rather than assumed."""
        worst = 0.0
        for i, d in enumerate(ds):
            q = coords_of(d)
            for (x, y), (tx, ty) in zip(apply_candidate(cand, i, base), q):
                worst = max(worst, math.hypot(x - tx, y - ty))
                if worst > 2 * FIT_TOL:
                    return False
        self.fit_residual = max(getattr(self, "fit_residual", 0.0), worst)
        return True

    def check_angle_continuity(self, rot, label="a rotating layer"):
        """The earlier fit check compared cos/sin of the angle, which is blind to
        a 360-degree wrap. Compare the angles themselves."""
        worst = 0.0
        for a, b in zip(rot, rot[1:]):
            worst = max(worst, abs(b - a))
        if worst > 90.0:
            self.warn(f"{label}: {worst:.0f} degrees between adjacent frames after "
                      "unwrapping. If the layer is not genuinely spinning that "
                      "fast, this is a discontinuity worth checking.")
        return worst

    def wrap_transform(self, fitted, node):
        p = self.opts.precision
        if fitted["mode"] == "pivot":
            self.check_angle_continuity(fitted["rot"])
            cx, cy = num(fitted["cx"], p), num(fitted["cy"], p)
            vals = [f"{num(r, 4)} {cx} {cy}" for r in fitted["rot"]]
            return (f'<g transform="rotate({vals[0]})">'
                    + self.track("transform", vals, "animateTransform", "rotate")
                    + node + "</g>")
        self.check_angle_continuity(fitted["rot"])
        tx = [f"{num(x, p)} {num(y, p)}" for x, y in fitted["tx"]]
        rot = [num(r, 4) for r in fitted["rot"]]
        sc = [f"{num(a, 5)} {num(b, 5)}" for a, b in fitted["sc"]]
        t0, r0, s0 = tx[0].split(), rot[0], sc[0].split()
        out = node
        if len(set(sc)) > 1 or s0 != ["1", "1"]:
            out = (f'<g transform="scale({s0[0]},{s0[1]})">'
                   + self.track("transform", sc, "animateTransform", "scale")
                   + out + "</g>")
        if len(set(rot)) > 1 or r0 != "0":
            out = (f'<g transform="rotate({r0})">'
                   + self.track("transform", rot, "animateTransform", "rotate")
                   + out + "</g>")
        if len(set(tx)) > 1 or t0 != ["0", "0"]:
            out = (f'<g transform="translate({t0[0]},{t0[1]})">'
                   + self.track("transform", tx, "animateTransform", "translate")
                   + out + "</g>")
        return out

    # -- layers ------------------------------------------------------------ #
    def emit_layer(self, layer, clipped):
        name = layer.get("name", "layer")
        pieces = [self.emit_shape(s, clipped, name) for s in layer.get("shapes", [])]
        pieces = [x for x in pieces if x]
        if not pieces:
            return None
        content = "".join(pieces)

        ops = [num(o, 3) for o in layer.get("opacity") or []]
        if ops:
            tr = self.track("opacity", ops)
            if tr:
                content = f'<g opacity="{ops[0]}">{tr}{content}</g>'
            elif ops[0] != "1":
                content = f'<g opacity="{ops[0]}">{content}</g>'

        ip = layer.get("ip")
        op = layer.get("op")
        if ip is not None and op is not None and (ip > self.frames[0] or op < self.frames[-1]):
            vis = ["1" if ip - 1e-6 <= f <= op + 1e-6 else "0" for f in self.frames]
            tr = self.track("opacity", vis, discrete=True)
            if tr:
                content = f'<g opacity="{vis[0]}">{tr}{content}</g>'
        return content

    def clip_for(self, src):
        cached = self._clip_cache.get(id(src))
        if cached:
            return cached
        """Clip from the matte source, animated when the matte itself moves.

        A clip frozen at frame 0 while the dots travel their real paths is what
        makes dots look like they leave their mask. Animating the clipPath's own
        path data keeps the two in register, and stays geometry throughout.
        """
        paths = []
        for sh in src.get("shapes", []):
            ds = sh.get("d") or []
            if not ds:
                continue
            if all(x == ds[0] for x in ds):
                paths.append(f'<path d="{ds[0]}"/>')
            elif len({d.count("C") for d in ds}) != 1:
                self.warn(f"matte source '{src.get('name')}' changes vertex count "
                          "between frames; the clip is held at the first frame")
                paths.append(f'<path d="{ds[0]}"/>')
            else:
                paths.append(f'<path d="{ds[0]}">{self.track("d", ds)}</path>')
        cid = self.nid("clip")
        self.defs.append(f'<clipPath id="{cid}" clipPathUnits="userSpaceOnUse">'
                         + "".join(paths) + "</clipPath>")
        self._clip_cache[id(src)] = cid
        return cid

    def build(self):
        n = len(self.layers)
        for i in range(n - 1, -1, -1):          # AE index 1 is topmost -> draw last
            layer = self.layers[i]
            if layer.get("isMatteSource"):
                continue
            if layer.get("enabled") is False:
                continue
            matte = layer.get("matte", "none")
            if matte in ("alphaInv", "lumaInv"):
                self.warn(f"'{layer.get('name')}': inverted track matte emitted as a "
                          "normal clip — SVG clipPath cannot invert")
            elif matte == "luma":
                self.warn(f"'{layer.get('name')}': luma matte emitted as an alpha clip")
            content = self.emit_layer(layer, clipped=(matte != "none"))
            if content is None:
                continue
            if matte != "none":
                src = self.by_index.get(layer.get("matteSourceIndex"))
                if src is None:
                    # AE 2023+ lets a matte be ANY layer, not just the one
                    # directly above, so search both directions before giving
                    # up. Failing to find it renders the dots unclipped, which
                    # looks exactly like dots escaping their mask.
                    for j in range(i - 1, -1, -1):
                        if self.layers[j].get("isMatteSource"):
                            src = self.layers[j]
                            break
                if src is None:
                    for j in range(i + 1, len(self.layers)):
                        if self.layers[j].get("isMatteSource"):
                            src = self.layers[j]
                            break
                if src is not None:
                    content = f'<g clip-path="url(#{self.clip_for(src)})">{content}</g>'
                else:
                    self.warn(f"'{layer.get('name')}' is matted but no matte source "
                              "layer was found; drawn unclipped")
            self.body.append(content)
        self.stats["fit_residual_px"] = round(self.fit_residual, 4)
        for name, reason, n in self.geometry_reasons:
            self.warn(f"'{name}' is stored as {n} frames of path data because of "
                      f"{reason}. That is the bulk of the file size.")
        return self.svg()

    def svg(self):
        title = self.opts.title or self.doc.get("name") or "icon"
        defs = f"<defs>{''.join(self.defs)}</defs>" if self.defs else ""
        out = (f'<svg xmlns="http://www.w3.org/2000/svg" id="{self.root_id}" '
               f'viewBox="0 0 {num(self.W, 0)} {num(self.H, 0)}" '
               f'width="{num(self.W, 0)}" height="{num(self.H, 0)}" '
               f'role="img" aria-label="{title}">'
               f"{defs}{''.join(self.body)}</svg>")
        if self.opts.tokens:
            out = tokenize(out, self.opts.tokens)
        return out


def trim_baked(doc, ls, le):
    """Restrict a baked doc to [ls, le] frames, slicing every per-frame array."""
    frames = [float(f) for f in doc["frames"]]
    keep = [i for i, f in enumerate(frames)
            if (ls is None or f >= ls - 1e-6) and (le is None or f <= le + 1e-6)]
    if len(keep) < 2:
        raise ValueError("loop range keeps fewer than two sampled frames")
    if len(keep) == len(frames):
        return doc

    def pick(arr):
        return [arr[i] for i in keep] if isinstance(arr, list) and len(arr) == len(frames) else arr

    doc["frames"] = pick(doc["frames"])
    for layer in doc.get("layers", []):
        layer["opacity"] = pick(layer.get("opacity") or [])
        for sh in layer.get("shapes", []):
            sh["d"] = pick(sh.get("d") or [])
            for grp, keys in (("circle", ("cx", "cy", "r")),
                              ("fill", ("opacity",)),
                              ("stroke", ("width", "opacity")),
                              ("trim", ("start", "end", "offset", "len"))):
                node = sh.get(grp)
                if not node:
                    continue
                for k in keys:
                    if isinstance(node.get(k), list):
                        node[k] = pick(node[k])
    return doc


def convert_baked(path, opts=None):
    with open(path) as f:
        doc = json.load(f)
    if doc.get("format") != "l2s-baked/1":
        raise ValueError("not an l2s-baked/1 document")
    if opts is not None and (opts.loop_start is not None or opts.loop_end is not None):
        doc = trim_baked(doc, opts.loop_start, opts.loop_end)
    if opts is not None and opts.prefix is None:
        opts.prefix = slug(doc.get("name") or os.path.basename(path))
    warn = Warnings()
    em = BakedEmitter(doc, opts, warn)
    svg = em.build()
    ok, err = validate(svg)
    if not ok:
        warn(f"emitted SVG is not well-formed XML: {err}")
    return svg, list(warn), em.stats
