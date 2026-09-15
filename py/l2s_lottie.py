#!/usr/bin/env python3
"""
l2s_lottie.py — emit Lottie / Bodymovin JSON from the "l2s-baked/1"
intermediate that jsx/host.jsx samples straight out of After Effects.

Why this exists: Bodymovin / the LottieFiles plugin is one more dependency to
keep licensed and installed, and this panel already holds an exact per-frame
sample of the comp. The same bake feeds both emitters, so the SVG and the JSON
are the same animation by construction.

What comes out
--------------
A BAKED Lottie: every animated property is a per-frame linear keyframe track in
comp space, layer transforms are identity, and no keyframe carries spatial
tangents (`ti`/`to`) or a hold flag. That is deliberate:

  * lottie-web mis-steps position keyframes that carry spatial tangents — it
    jumps most of the way to the next keyframe in about a frame and then holds.
    A baked linear track cannot trigger that path, so the player reproduces
    After Effects exactly.
  * nothing has to reconstruct easing from bezier handles, which is the same
    accuracy model the SVG route uses.

The trade is size and editability: the JSON is bigger than a Bodymovin export
and its keyframes are not the ones you set in AE, so it is an output format,
not a round-trip format. Keep the .aep as the source.

Exact-hold samples collapse losslessly, and `--simplify` decimates on the time
axis with the error measured in pixels, exactly as on the SVG route.
"""

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from l2s_core import (Options, Warnings, collapse_holds, decimate,
                      measure_decimation)
from l2s_baked import trim_baked

LOTTIE_VERSION = "5.7.4"

#: linear temporal handles. cubic-bezier(0,0,1,1) is the identity ramp.
EASE_OUT = {"x": [0.0], "y": [0.0]}
EASE_IN = {"x": [1.0], "y": [1.0]}

CAP = {"butt": 1, "round": 2, "square": 3}
JOIN = {"miter": 1, "round": 2, "bevel": 3}
MATTE = {"alpha": 1, "alphaInv": 2, "luma": 3, "lumaInv": 4}

_TOKEN_RE = re.compile(r"([MCZ])|(-?\d+(?:\.\d+)?)")


# --------------------------------------------------------------------------- #
# path string -> lottie bezier
# --------------------------------------------------------------------------- #
def d_to_bezier(d):
    """Parse the `M…C…Z` strings host.jsx emits back into Lottie's
    {i, o, v, c} bezier. Only M/C/Z appear, by construction."""
    verts, out_t, in_t, closed = [], [], [], False
    nums, cmd = [], None

    def flush():
        if cmd == "M" and len(nums) >= 2:
            verts.append([nums[0], nums[1]])
            out_t.append([0.0, 0.0])
            in_t.append([0.0, 0.0])
        elif cmd == "C" and len(nums) >= 6:
            c1 = (nums[0], nums[1])
            c2 = (nums[2], nums[3])
            end = [nums[4], nums[5]]
            if verts:
                out_t[-1] = [c1[0] - verts[-1][0], c1[1] - verts[-1][1]]
            verts.append(end)
            out_t.append([0.0, 0.0])
            in_t.append([c2[0] - end[0], c2[1] - end[1]])

    for m in _TOKEN_RE.finditer(d):
        if m.group(1):
            if cmd:
                flush()
            cmd = m.group(1)
            nums = []
            if cmd == "Z":
                closed = True
                cmd = None
        else:
            nums.append(float(m.group(2)))
    if cmd:
        flush()

    # a closed path's final C lands back on the first vertex; that duplicate
    # carries the in-tangent for vertex 0 and is not itself a vertex.
    if closed and len(verts) > 1 and _same(verts[0], verts[-1]):
        in_t[0] = in_t[-1]
        verts.pop()
        out_t.pop()
        in_t.pop()

    return {"i": in_t, "o": out_t, "v": verts, "c": closed}


def _same(a, b, eps=1e-6):
    return abs(a[0] - b[0]) <= eps and abs(a[1] - b[1]) <= eps


def hex_to_rgba(h):
    h = (h or "#000000").lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return [int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4)] + [1.0]


# --------------------------------------------------------------------------- #
# emitter
# --------------------------------------------------------------------------- #
class LottieEmitter:
    def __init__(self, doc, opts=None, warn=None):
        self.doc = doc
        self.opts = opts if opts is not None else Options()
        self.warn = warn if warn is not None else Warnings()
        for w in doc.get("warnings", []):
            self.warn(w)

        frames = [float(f) for f in doc["frames"]]
        self.f0 = frames[0]
        self.frames = [f - self.f0 for f in frames]
        self.n = len(frames)
        self.fps = float(doc.get("fps") or 60)
        self.op = self.frames[-1]
        self.W = int(doc.get("w", 512))
        self.H = int(doc.get("h", 512))
        self.p = max(0, int(getattr(self.opts, "precision", 2)))
        self.stats = {"tracks": 0, "samples": 0, "layers": 0}

    # -- numbers ---------------------------------------------------------- #
    def r(self, v, p=None):
        v = round(float(v), self.p if p is None else p)
        return int(v) if v == int(v) else v

    # -- generic property tracks ------------------------------------------ #
    def static(self, value):
        return {"a": 0, "k": value}

    def track(self, values, keep=None, wrap=True):
        """An animated (or, if nothing moves, a static) Lottie property.
        `values` is one sample per baked frame, already in Lottie units."""
        if not values:
            return None
        if all(v == values[0] for v in values):
            return self.static(values[0])
        idxs = collapse_holds(values) if keep is None else keep
        self.stats["tracks"] += 1
        self.stats["samples"] += len(idxs)
        kfs = []
        for n, i in enumerate(idxs):
            v = values[i]
            kf = {"t": self.frames[i], "s": v if isinstance(v, list) else [v]}
            if n < len(idxs) - 1:
                kf["o"] = dict(EASE_OUT)
                kf["i"] = dict(EASE_IN)
            kfs.append(kf)
        return {"a": 1, "k": kfs}

    def shape_track(self, ds):
        """A path track. The `d` strings are compared as strings (cheap and
        exact) and only the surviving frames are parsed."""
        if not ds:
            return None
        if all(d == ds[0] for d in ds):
            return {"a": 0, "k": d_to_bezier(ds[0])}
        counts = {d.count("C") for d in ds}
        if len(counts) != 1:
            self.warn("vertex count changes between frames; Lottie cannot "
                      "interpolate that, so the first frame is held")
            return {"a": 0, "k": d_to_bezier(ds[0])}
        idxs = self.simplified(ds, lambda d: _points_of(d))
        self.stats["tracks"] += 1
        self.stats["samples"] += len(idxs)
        kfs = []
        for n, i in enumerate(idxs):
            kf = {"t": self.frames[i], "s": [d_to_bezier(ds[i])]}
            if n < len(idxs) - 1:
                kf["o"] = dict(EASE_OUT)
                kf["i"] = dict(EASE_IN)
            kfs.append(kf)
        return {"a": 1, "k": kfs}

    def simplified(self, values, to_points):
        """Exact hold collapse, then optional error-bounded time decimation —
        the same two steps, and the same tolerance, as the SVG route."""
        idxs = collapse_holds(values)
        tol = float(getattr(self.opts, "simplify", 0.0) or 0.0)
        if tol <= 0 or len(idxs) < 3:
            return idxs
        pts = [to_points(values[i]) for i in idxs]
        keep = decimate(pts, tol)
        err = measure_decimation(pts, keep)
        if err == float("inf"):
            return idxs
        self.stats["simplify_err_px"] = max(
            self.stats.get("simplify_err_px", 0.0), round(err, 4))
        self.stats["samples_dropped"] = (self.stats.get("samples_dropped", 0)
                                         + len(idxs) - len(keep))
        return [idxs[i] for i in keep]

    # -- shape items ------------------------------------------------------- #
    def geometry_items(self, shape):
        circ = shape.get("circle")
        if circ and not shape.get("trim"):
            cx, cy, r = circ["cx"], circ["cy"], circ["r"]
            pos = [[self.r(a), self.r(b)] for a, b in zip(cx, cy)]
            size = [[self.r(2 * v), self.r(2 * v)] for v in r]
            keep = self.simplified(
                list(zip(pos, size)),
                lambda pair: [tuple(pair[0]), (pair[1][0], 0.0)])
            return [{
                "ty": "el",
                "nm": shape.get("name") or "ellipse",
                "d": 1,
                "p": self.track(pos, keep),
                "s": self.track(size, keep),
            }]
        ds = shape.get("d") or []
        if not ds:
            return []
        rounded = [_round_d(d, self.p) for d in ds]
        return [{
            "ty": "sh",
            "nm": shape.get("name") or "path",
            "ind": 0,
            "ks": self.shape_track(rounded),
        }]

    def paint_items(self, shape):
        fl, st = shape.get("fill"), shape.get("stroke")
        if st and (not any(st.get("width") or [0])
                   or not any(st.get("opacity") or [1])):
            st = None
        if fl and not any(fl.get("opacity") or [1]):
            fl = None

        fill_item = stroke_item = None
        if fl:
            fill_item = {
                "ty": "fl",
                "nm": "Fill",
                "c": self.static(hex_to_rgba(fl.get("color"))),
                "o": self.track([self.r(o * 100, 2) for o in fl.get("opacity") or [1]]),
                "r": 2 if fl.get("rule") == "evenodd" else 1,
            }
        if st:
            stroke_item = {
                "ty": "st",
                "nm": "Stroke",
                "c": self.static(hex_to_rgba(st.get("color"))),
                "o": self.track([self.r(o * 100, 2) for o in st.get("opacity") or [1]]),
                "w": self.track([self.r(w) for w in st.get("width") or [0]]),
                "lc": CAP.get(st.get("cap", "round"), 2),
                "lj": JOIN.get(st.get("join", "round"), 2),
            }
        if not fl and not st:
            self.warn(f"'{shape.get('name')}' has no enabled fill or stroke; "
                      "emitted unpainted")

        # earlier in `it` paints on top, matching AE's contents order
        items = []
        if shape.get("fillOverStroke"):
            items = [i for i in (fill_item, stroke_item) if i]
        else:
            items = [i for i in (stroke_item, fill_item) if i]
        return items

    def trim_item(self, shape):
        trim = shape.get("trim")
        if not trim:
            return None
        return {
            "ty": "tm",
            "nm": "Trim Paths",
            "s": self.track([self.r(v, 3) for v in trim.get("start") or [0]]),
            "e": self.track([self.r(v, 3) for v in trim.get("end") or [100]]),
            "o": self.track([self.r(v, 3) for v in trim.get("offset") or [0]]),
            "m": 1,
        }

    def shape_group(self, shape):
        geo = self.geometry_items(shape)
        if not geo:
            return None
        items = geo + self.paint_items(shape)
        tm = self.trim_item(shape)
        if tm:
            items.append(tm)
        items.append({
            "ty": "tr",
            "p": self.static([0, 0]), "a": self.static([0, 0]),
            "s": self.static([100, 100]), "r": self.static(0),
            "o": self.static(100), "sk": self.static(0), "sa": self.static(0),
        })
        return {"ty": "gr", "nm": shape.get("name") or "Group", "it": items}

    # -- layers ------------------------------------------------------------ #
    def layer(self, rec, ind):
        groups = []
        for sh in rec.get("shapes", []):
            g = self.shape_group(sh)
            if g:
                groups.append(g)
        if not groups:
            return None
        op = rec.get("opacity") or [1.0]
        ip = max(0.0, float(rec.get("ip", 0)) - self.f0)
        out = min(self.op, float(rec.get("op", self.op)) - self.f0)
        self.stats["layers"] += 1
        return {
            "ddd": 0, "ind": ind, "ty": 4, "nm": rec.get("name") or f"Layer {ind}",
            "sr": 1, "ao": 0, "bm": 0,
            "ks": {
                "o": self.track([self.r(v * 100, 2) for v in op]),
                "r": self.static(0),
                "p": self.static([0, 0, 0]),
                "a": self.static([0, 0, 0]),
                "s": self.static([100, 100, 100]),
            },
            "shapes": groups,
            "ip": ip,
            "op": out if out > ip else self.op,
            "st": 0,
        }

    def build(self):
        recs = [l for l in self.doc.get("layers", [])
                if l.get("shapes") and l.get("skipped") != "hidden"]
        # AE index 1 is topmost, and so is Lottie index 0 — same order.
        built = []                       # [(rec, layer_dict)] in AE order
        for rec in recs:
            if rec.get("enabled") is False and not rec.get("isMatteSource"):
                continue
            lay = self.layer(rec, len(built) + 1)
            if lay is not None:
                built.append((rec, lay))

        by_ae_index = {rec.get("index"): i for i, (rec, _) in enumerate(built)}

        # a matte source has to sit directly above the layer it mattes
        ordered, placed = [], set()
        for pos, (rec, lay) in enumerate(built):
            if pos in placed:
                continue
            matte = rec.get("matte", "none")
            if matte != "none":
                spos = by_ae_index.get(rec.get("matteSourceIndex"))
                if spos is None:
                    spos = self._nearest_matte_source(built, pos)
                if spos is None:
                    self.warn(f"'{rec.get('name')}' is matted but no matte source "
                              "layer was found; emitted unmatted")
                elif spos not in placed:
                    built[spos][1]["td"] = 1
                    ordered.append(built[spos][1])
                    placed.add(spos)
                    lay["tt"] = MATTE.get(matte, 1)
                    if matte in ("alphaInv", "lumaInv"):
                        self.warn(f"'{rec.get('name')}': inverted track matte is "
                                  "preserved, but check it in the player")
            ordered.append(lay)
            placed.add(pos)

        for i, lay in enumerate(ordered, 1):
            lay["ind"] = i
        for i, lay in enumerate(ordered):
            if lay.get("tt") and i > 0:
                lay["tp"] = ordered[i - 1]["ind"]

        return {
            "v": LOTTIE_VERSION,
            "fr": self.fps,
            "ip": 0,
            "op": self.op,
            "w": self.W,
            "h": self.H,
            "nm": self.doc.get("name") or "icon",
            "ddd": 0,
            "assets": [],
            "layers": ordered,
            "meta": {"g": "l2s (baked from After Effects)"},
        }

    @staticmethod
    def _nearest_matte_source(built, pos):
        """AE 2023+ lets any layer be the matte, so search up then down rather
        than assuming the one directly above. Failing to find it renders the
        matted content unclipped, which looks like dots escaping their mask."""
        for j in range(pos - 1, -1, -1):
            if built[j][0].get("isMatteSource"):
                return j
        for j in range(pos + 1, len(built)):
            if built[j][0].get("isMatteSource"):
                return j
        return None


def _points_of(d):
    nums = [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", d)]
    return list(zip(nums[0::2], nums[1::2]))


def _round_d(d, p):
    def sub(m):
        v = round(float(m.group(0)), p)
        return str(int(v)) if v == int(v) else str(v)
    return re.sub(r"-?\d+(?:\.\d+)?", sub, d)


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #
def convert_baked_to_lottie(path, opts=None):
    with open(path) as f:
        doc = json.load(f)
    if doc.get("format") != "l2s-baked/1":
        raise ValueError("not an l2s-baked/1 document")
    if opts is not None and (opts.loop_start is not None or opts.loop_end is not None):
        doc = trim_baked(doc, opts.loop_start, opts.loop_end)
    warn = Warnings()
    em = LottieEmitter(doc, opts, warn)
    out = em.build()
    return json.dumps(out, separators=(",", ":")), list(warn), em.stats
