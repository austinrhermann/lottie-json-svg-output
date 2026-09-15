#!/usr/bin/env python3
"""
kf.py — Lottie keyframe semantics that a per-frame sampler still has to get right.

This tool samples every property per frame and emits linear tracks, which
removes the whole easing-handle bug surface. Two things survive that decision,
both carried over from the icon pipeline:

RULE 1 — a keyframe carrying "h": 1 is an After Effects HOLD keyframe.
    The value stays flat until the NEXT keyframe's time and then jumps.
    python-lottie 0.7.2 parses `Keyframe.hold` but `Property.get_value()`
    interpolates straight through it, so per-frame sampling a hold track with
    python-lottie bakes a smooth ramp over a step. That reads as "wrong
    easing", not "missing feature", so it survives review.

    `expand_holds()` fixes this once, centrally, on the raw document before it
    is ever loaded: every hold segment is rewritten as flat-then-snap keyframes
    with linear handles. It is value-type agnostic, so it covers position,
    scale, rotation, opacity, color, path morphs and trim offsets in one pass,
    and it leaves eased tracks byte-identical.

RULE 2 — segment easing lives on the segment's STARTING keyframe: `o` is the
    start handle, `i` is the end handle, both read off the keyframe the segment
    leaves. Nothing here reconstructs easing (that is the point of per-frame
    sampling), but `seg_ease()` is kept because hold expansion has to write
    correct handles, and because anything auditing a source needs the rule.

The AE bake route does not need any of this — AE's own valueAtTime() honours
holds natively. This is for the Lottie-JSON input route only.
"""

import copy

__all__ = ["is_hold", "has_hold", "seg_ease", "hold_value", "breakpoints",
           "expand_holds", "scan_spatial_tangents", "LINEAR_O", "LINEAR_I"]

LINEAR_O = {"x": [0.0], "y": [0.0]}
LINEAR_I = {"x": [1.0], "y": [1.0]}

#: how far before the next keyframe the snap happens, in frames. Half a frame
#: puts the entire ramp inside one frame interval, so every integer frame
#: samples either the held value or the new one — never a point on the ramp.
SNAP_FRAMES = 0.5


# --------------------------------------------------------------------------- #
# inspection
# --------------------------------------------------------------------------- #
def is_hold(kf):
    """True if the segment LEAVING this keyframe is a hold/step."""
    return isinstance(kf, dict) and kf.get("h") in (1, True)


def has_hold(kfs):
    """True if any keyframe in this track is a hold."""
    return isinstance(kfs, list) and any(is_hold(k) for k in kfs)


def _handle(h, dx, dy):
    if not isinstance(h, dict):
        return (dx, dy)
    x, y = h.get("x", dx), h.get("y", dy)
    if isinstance(x, (list, tuple)):
        x = x[0] if x else dx
    if isinstance(y, (list, tuple)):
        y = y[0] if y else dy
    return (float(x), float(y))


def seg_ease(kf):
    """(ox, oy, ix, iy) for the segment leaving `kf`; None for a hold."""
    if is_hold(kf):
        return None
    ox, oy = _handle(kf.get("o"), 0.333, 0.0)
    ix, iy = _handle(kf.get("i"), 0.667, 1.0)
    return (ox, oy, ix, iy)


def hold_value(kfs, t):
    """Step-sample a hold track at frame `t`: the last keyframe at or before
    `t` wins. The AE / lottie-web semantic."""
    order = sorted(kfs, key=lambda k: k["t"])
    v = order[0].get("s")
    for k in order:
        if k["t"] <= t:
            v = k.get("s")
        else:
            break
    return v


def breakpoints(*kf_lists):
    """Sorted union of keyframe times across tracks — the combined step
    schedule when a stepped transform is driven by more than one hold track."""
    return sorted({float(k["t"]) for kfs in kf_lists if kfs for k in kfs
                   if isinstance(k, dict) and "t" in k})


# --------------------------------------------------------------------------- #
# hold expansion
# --------------------------------------------------------------------------- #
def _is_keyframe_list(k):
    return (isinstance(k, list) and k
            and isinstance(k[0], dict) and "t" in k[0])


def _expand_track(kfs):
    """Rewrite hold segments as flat-then-snap. Returns (new_kfs, n_expanded)."""
    order = sorted(kfs, key=lambda k: k.get("t", 0))
    out, n = [], 0
    for i, k in enumerate(order):
        nxt = order[i + 1] if i + 1 < len(order) else None
        if not is_hold(k) or nxt is None:
            if is_hold(k):                      # trailing hold: nothing follows
                k = dict(k)
                k.pop("h", None)
            out.append(k)
            continue

        t0, t1 = float(k.get("t", 0)), float(nxt.get("t", 0))
        gap = t1 - t0
        if gap <= 1e-9:                         # coincident: the step is the jump
            k = dict(k)
            k.pop("h", None)
            out.append(k)
            continue

        eps = min(SNAP_FRAMES, gap / 2.0)
        held = k.get("s")

        plateau = dict(k)
        plateau.pop("h", None)
        plateau.pop("ti", None)
        plateau.pop("to", None)
        plateau["o"], plateau["i"] = dict(LINEAR_O), dict(LINEAR_I)
        if "e" in k:
            plateau["e"] = copy.deepcopy(held)

        snap = copy.deepcopy(plateau)
        snap["t"] = t1 - eps
        if "e" in k:
            snap["e"] = copy.deepcopy(nxt.get("s"))

        out.append(plateau)
        out.append(snap)
        n += 1
    return out, n


def expand_holds(doc):
    """Walk a raw Lottie document and materialise every hold keyframe as a
    step. Mutates `doc` in place; returns the number of hold segments rewritten.

    Call this before handing the document to python-lottie. No-op on documents
    without holds, so it is safe unconditionally."""
    count = [0]

    def walk(node):
        if isinstance(node, dict):
            k = node.get("k")
            if _is_keyframe_list(k) and has_hold(k):
                node["k"], n = _expand_track(k)
                count[0] += n
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(doc)
    return count[0]


# --------------------------------------------------------------------------- #
# audit
# --------------------------------------------------------------------------- #
def scan_spatial_tangents(doc):
    """Layer names whose animated position carries `ti`/`to`. Those tracks are
    the ones lottie-web mis-steps and python-lottie mis-eases without aefix —
    useful for telling whether an SVG built by an older tool is suspect."""
    hits = []
    for layer in doc.get("layers", []):
        p = (layer.get("ks") or {}).get("p") or {}
        if p.get("a") != 1 or not isinstance(p.get("k"), list):
            continue
        for kf in p["k"]:
            if isinstance(kf, dict) and any(any(kf.get(t) or ()) for t in ("ti", "to")):
                hits.append(layer.get("nm") or layer.get("ind"))
                break
    return hits
