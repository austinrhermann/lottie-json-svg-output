#!/usr/bin/env python3
"""
aefix.py — corrects python-lottie's interpolation of position keyframes that
carry spatial tangents (an AE bezier motion path).

The defect, in lottie.objects.properties.OffsetKeyframe.interpolated_value:

    if isinstance(self, PositionKeyframe) and self.in_tan and self.out_tan:
        bezier = Bezier()
        bezier.add_point(self.value, NVector(0, 0), self.out_tan)
        bezier.add_point(end, self.in_tan, NVector(0, 0))
        return bezier.point_at(ratio)          # <-- raw time ratio
    lerpv = self.lerp_factor(ratio)            # <-- easing only reached here
    return self.value.lerp(end, lerpv)

Two things go wrong on that branch, and only on that branch:

  1. `ratio` is the raw normalised time. `lerp_factor(ratio)` — the temporal
     ease from the keyframe's own bezier handles — is never applied. So every
     dot travelling a curved path loses its easing and moves at an essentially
     constant rate. Non-spatial keyframes are interpolated correctly, which is
     why the bug hides in plain sight.

  2. `point_at(ratio)` indexes the curve by BEZIER PARAMETER, not arc length.
     A cubic is not uniformly parameterised, so even a genuinely linear segment
     comes out with a speed that varies with the curvature of the handles.

After Effects does it the other way round: the temporal ease maps time to a
fraction of DISTANCE TRAVELLED, and that distance fraction is what locates the
point on the motion path. This module restores that order.

Importing it installs the patch. It is idempotent and it leaves every other
interpolation path in python-lottie untouched.
"""

import math

from lottie.objects.properties import OffsetKeyframe, PositionKeyframe

#: subdivisions per segment for the arc-length table. AE's own motion-path
#: sampling is finer than this, but 96 keeps the inversion error well under a
#: thousandth of a pixel on icon-scale geometry.
ARC_SAMPLES = 96

_arc_cache = {}


def _cubic(p0, p1, p2, p3, t):
    u = 1.0 - t
    return (p0 * (u * u * u) + p1 * (3 * u * u * t)
            + p2 * (3 * u * t * t) + p3 * (t * t * t))


def _dist(a, b):
    return math.hypot(b[0] - a[0], b[1] - a[1])


def _arc_table(p0, p1, p2, p3):
    """Cumulative chord lengths along the segment, plus the total."""
    key = (tuple(p0)[:2], tuple(p1)[:2], tuple(p2)[:2], tuple(p3)[:2])
    hit = _arc_cache.get(key)
    if hit is not None:
        return hit
    lens = [0.0]
    prev = p0
    total = 0.0
    for i in range(1, ARC_SAMPLES + 1):
        cur = _cubic(p0, p1, p2, p3, i / ARC_SAMPLES)
        total += _dist(prev, cur)
        lens.append(total)
        prev = cur
    if len(_arc_cache) > 4096:
        _arc_cache.clear()
    _arc_cache[key] = (lens, total)
    return (lens, total)


def point_at_arclength(p0, p1, p2, p3, frac):
    """Point at `frac` of the segment's arc length, as After Effects locates it."""
    if frac <= 0:
        return p0
    if frac >= 1:
        return p3
    lens, total = _arc_table(p0, p1, p2, p3)
    if total <= 0:
        return p0
    target = frac * total
    # lens is monotonic; find the bracketing sample and interpolate the parameter
    lo, hi = 0, len(lens) - 1
    while lo < hi - 1:
        mid = (lo + hi) // 2
        if lens[mid] <= target:
            lo = mid
        else:
            hi = mid
    span = lens[hi] - lens[lo]
    frac_in = 0.0 if span <= 0 else (target - lens[lo]) / span
    t = (lo + frac_in) / ARC_SAMPLES
    return _cubic(p0, p1, p2, p3, t)


def _interpolated_value(self, ratio, next_value=None):
    end = next_value if self.end is None else self.end
    if end is None:
        return self.value
    if not self.in_value or not self.out_value:
        return self.value
    if ratio == 1:
        return end
    if ratio == 0:
        return self.value

    # Temporal ease first: time -> fraction of the journey completed.
    perc = self.lerp_factor(ratio)

    if isinstance(self, PositionKeyframe) and self.in_tan and self.out_tan:
        # ...then that fraction locates a point by ARC LENGTH along the path.
        return point_at_arclength(self.value,
                                  self.value + self.out_tan,
                                  end + self.in_tan,
                                  end,
                                  perc)

    return self.value.lerp(end, perc)


def install():
    if getattr(OffsetKeyframe.interpolated_value, "_l2s_patched", False):
        return False
    _interpolated_value._l2s_patched = True
    OffsetKeyframe.interpolated_value = _interpolated_value
    return True


def is_installed():
    return getattr(OffsetKeyframe.interpolated_value, "_l2s_patched", False)


install()
