/**
 * host.jsx — After Effects side of the exporter.
 *
 * Instead of round-tripping through Bodymovin, this samples the comp directly:
 * for every frame it asks After Effects for the value of every property it
 * needs. AE is therefore the ground truth for timing, easing and the whole
 * parent/anchor/scale/rotation chain — nothing re-derives eased curves from
 * exported bezier handles, so there is no handle-convention bug surface, and
 * hold keyframes step because AE's own valueAtTime() steps them.
 *
 * The layer -> comp transform per frame is recovered exactly by mapping three
 * basis points through AE's own sourcePointToComp (a 2D layer transform is
 * affine, so three points determine it). Shape-group transforms are composed
 * from sampled values in layer space.
 *
 * Output: a "l2s-baked/1" JSON file. py/l2s_baked.py turns it into an animated
 * SVG; py/l2s_lottie.py turns the same bake into Lottie JSON.
 *
 * Cost model: setting comp.time forces AE to re-evaluate the comp, so it is set
 * ONCE PER FRAME with every layer sampled inside that tick — not once per layer
 * per frame, which is what the first version did. Property references are
 * resolved once during prep instead of looked up by matchName on every sample.
 * Both matter a lot on long comps and deep shape trees.
 */

/* global app, File, CompItem, ShapeLayer, CameraLayer, TrackMatteType */

// --------------------------------------------------------------------------- //
// tiny JSON writer (ExtendScript has no JSON)
// --------------------------------------------------------------------------- //
function jesc(s) {
    s = String(s);
    var out = "";
    for (var i = 0; i < s.length; i++) {
        var ch = s.charAt(i), c = s.charCodeAt(i);
        if (ch === '"') out += '\\"';
        else if (ch === "\\") out += "\\\\";
        else if (ch === "\n") out += "\\n";
        else if (ch === "\r") out += "\\r";
        else if (ch === "\t") out += "\\t";
        else if (c < 32 || c > 126) {
            var h = c.toString(16);
            while (h.length < 4) h = "0" + h;
            out += "\\u" + h;
        } else out += ch;
    }
    return '"' + out + '"';
}

function jnum(n) {
    if (n === null || n === undefined || isNaN(n)) return "0";
    if (n === Math.round(n)) return String(n);
    return String(Math.round(n * 1e5) / 1e5);
}

function jval(v) {
    if (v === null || v === undefined) return "null";
    var t = typeof v;
    if (t === "number") return jnum(v);
    if (t === "boolean") return v ? "true" : "false";
    if (t === "string") return jesc(v);
    if (v instanceof Array) {
        var a = [];
        for (var i = 0; i < v.length; i++) a.push(jval(v[i]));
        return "[" + a.join(",") + "]";
    }
    var parts = [];
    for (var k in v) {
        if (!v.hasOwnProperty(k)) continue;
        if (v[k] === undefined) continue;
        parts.push(jesc(k) + ":" + jval(v[k]));
    }
    return "{" + parts.join(",") + "}";
}

// --------------------------------------------------------------------------- //
// affine helpers: apply(M,v) = (a*x + c*y + tx, b*x + d*y + ty)
// mul(M,N) applies N first, then M.
// --------------------------------------------------------------------------- //
function mIdent() { return { a: 1, b: 0, c: 0, d: 1, tx: 0, ty: 0 }; }

function mMul(M, N) {
    return {
        a: M.a * N.a + M.c * N.b,
        b: M.b * N.a + M.d * N.b,
        c: M.a * N.c + M.c * N.d,
        d: M.b * N.c + M.d * N.d,
        tx: M.a * N.tx + M.c * N.ty + M.tx,
        ty: M.b * N.tx + M.d * N.ty + M.ty
    };
}

function mApply(M, x, y) {
    return [M.a * x + M.c * y + M.tx, M.b * x + M.d * y + M.ty];
}

function mScale(M) {
    return (Math.sqrt(M.a * M.a + M.b * M.b) + Math.sqrt(M.c * M.c + M.d * M.d)) / 2;
}

function mFromParts(pos, anchor, scale, rot, skew, skewAxis) {
    var M = { a: 1, b: 0, c: 0, d: 1, tx: pos[0], ty: pos[1] };
    var r = (rot || 0) * Math.PI / 180;
    if (r) {
        var cs = Math.cos(r), sn = Math.sin(r);
        M = mMul(M, { a: cs, b: sn, c: -sn, d: cs, tx: 0, ty: 0 });
    }
    if (skew) {
        var ax = (skewAxis || 0) * Math.PI / 180;
        var tn = Math.tan(skew * Math.PI / 180);
        var ca = Math.cos(ax), sa = Math.sin(ax);
        var Ra = { a: ca, b: sa, c: -sa, d: ca, tx: 0, ty: 0 };
        var Rb = { a: ca, b: -sa, c: sa, d: ca, tx: 0, ty: 0 };
        M = mMul(M, mMul(Ra, mMul({ a: 1, b: 0, c: tn, d: 1, tx: 0, ty: 0 }, Rb)));
    }
    M = mMul(M, { a: scale[0] / 100, b: 0, c: 0, d: scale[1] / 100, tx: 0, ty: 0 });
    M = mMul(M, { a: 1, b: 0, c: 0, d: 1, tx: -anchor[0], ty: -anchor[1] });
    return M;
}

// --------------------------------------------------------------------------- //
// property access
// --------------------------------------------------------------------------- //
function isEnabled(it) {
    // A Fill or Stroke item that is unchecked in AE must not be captured. AE
    // adds a default white stroke to shape layers, which is invisible against a
    // white comp background but very much not invisible once tokenized.
    try {
        if (it.enabled === false) return false;
    } catch (e) { /* older API: assume enabled */ }
    return true;
}

function prop(group, name) {
    if (!group) return null;
    try { return group.property(name) || null; } catch (e) { return null; }
}

function pv(p, t, dflt) {
    if (!p) return dflt;
    try { return p.valueAtTime(t, false); } catch (e) { return dflt; }
}

function hex3(c) {
    function h(x) {
        var v = Math.round(Math.max(0, Math.min(1, x)) * 255).toString(16);
        return v.length < 2 ? "0" + v : v;
    }
    return "#" + h(c[0]) + h(c[1]) + h(c[2]);
}

function n2(x) { return Math.round(x * 100) / 100; }

// --------------------------------------------------------------------------- //
// layer -> comp affine matrix at the comp's current time
// --------------------------------------------------------------------------- //
function layerMatrixAt(layer) {
    var o = layer.sourcePointToComp([0, 0]);
    var ex = layer.sourcePointToComp([1, 0]);
    var ey = layer.sourcePointToComp([0, 1]);
    return {
        a: ex[0] - o[0], b: ex[1] - o[1],
        c: ey[0] - o[0], d: ey[1] - o[1],
        tx: o[0], ty: o[1]
    };
}

/** The three-basis-point trick assumes the layer transform is affine. That
 *  holds for 2D layers and fails for 3D ones under perspective. Check it
 *  against a fourth point rather than assuming, so a broken assumption reports
 *  itself instead of producing plausible-looking wrong geometry. */
function affineResidual(layer, M) {
    var probe = [137, -91];
    var actual = layer.sourcePointToComp(probe);
    var pred = mApply(M, probe[0], probe[1]);
    return Math.sqrt((actual[0] - pred[0]) * (actual[0] - pred[0]) +
                     (actual[1] - pred[1]) * (actual[1] - pred[1]));
}

// --------------------------------------------------------------------------- //
// path data
// --------------------------------------------------------------------------- //
function shapeToD(sh, M) {
    var v = sh.vertices, it = sh.inTangents, ot = sh.outTangents;
    var closed = sh.closed, n = v.length;
    if (!n) return "";
    function P(k) { return mApply(M, v[k][0], v[k][1]); }
    function C(k, tan) { return mApply(M, v[k][0] + tan[k][0], v[k][1] + tan[k][1]); }
    var p0 = P(0);
    var d = "M" + n2(p0[0]) + " " + n2(p0[1]);
    var segs = closed ? n : n - 1;
    for (var s = 0; s < segs; s++) {
        var a = s, b = (s + 1) % n;
        var c1 = C(a, ot), c2 = C(b, it), pe = P(b);
        d += "C" + n2(c1[0]) + " " + n2(c1[1]) + " " + n2(c2[0]) + " " + n2(c2[1]) +
             " " + n2(pe[0]) + " " + n2(pe[1]);
    }
    if (closed) d += "Z";
    return d;
}

function shapeArcLen(sh, M) {
    var v = sh.vertices, it = sh.inTangents, ot = sh.outTangents;
    var n = v.length, segs = sh.closed ? n : n - 1, tot = 0, STEPS = 24;
    for (var s = 0; s < segs; s++) {
        var a = s, b = (s + 1) % n;
        var p0 = mApply(M, v[a][0], v[a][1]);
        var p1 = mApply(M, v[a][0] + ot[a][0], v[a][1] + ot[a][1]);
        var p2 = mApply(M, v[b][0] + it[b][0], v[b][1] + it[b][1]);
        var p3 = mApply(M, v[b][0], v[b][1]);
        var prev = p0;
        for (var i = 1; i <= STEPS; i++) {
            var t = i / STEPS, u = 1 - t;
            var x = u * u * u * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t * t * t * p3[0];
            var y = u * u * u * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t * t * t * p3[1];
            tot += Math.sqrt((x - prev[0]) * (x - prev[0]) + (y - prev[1]) * (y - prev[1]));
            prev = [x, y];
        }
    }
    return tot;
}

/** Circle test in LAYER space: 4 vertices on the axes through the centre,
 *  equal radii, real tangents. A rectangle passes the radius test alone. */
function circleOf(sh) {
    var v = sh.vertices;
    if (!sh.closed || v.length !== 4) return null;
    var cx = (v[0][0] + v[1][0] + v[2][0] + v[3][0]) / 4;
    var cy = (v[0][1] + v[1][1] + v[2][1] + v[3][1]) / 4;
    var rs = [], i;
    for (i = 0; i < 4; i++) {
        rs.push(Math.sqrt((v[i][0] - cx) * (v[i][0] - cx) + (v[i][1] - cy) * (v[i][1] - cy)));
    }
    var r = (rs[0] + rs[1] + rs[2] + rs[3]) / 4;
    if (r <= 0) return null;
    var tol = Math.max(0.02 * r, 0.05);
    for (i = 0; i < 4; i++) if (Math.abs(rs[i] - r) > tol) return null;
    for (i = 0; i < 4; i++) {
        if (Math.min(Math.abs(v[i][0] - cx), Math.abs(v[i][1] - cy)) > tol) return null;
    }
    var maxT = 0;
    for (i = 0; i < 4; i++) {
        maxT = Math.max(maxT, Math.sqrt(sh.outTangents[i][0] * sh.outTangents[i][0] +
                                        sh.outTangents[i][1] * sh.outTangents[i][1]));
    }
    if (maxT < 0.3 * r) return null;
    return { cx: cx, cy: cy, r: r };
}

// --------------------------------------------------------------------------- //
// shape tree walk: collect drawables with their paint + transform chain.
// Every property reference is resolved HERE, once, never per frame.
// --------------------------------------------------------------------------- //
function transformRefs(tr) {
    return {
        p: prop(tr, "ADBE Vector Position"),
        a: prop(tr, "ADBE Vector Anchor"),
        s: prop(tr, "ADBE Vector Scale"),
        r: prop(tr, "ADBE Vector Rotation"),
        sk: prop(tr, "ADBE Vector Skew"),
        sa: prop(tr, "ADBE Vector Skew Axis")
    };
}

function fillRefs(f) {
    return {
        c: prop(f, "ADBE Vector Fill Color"),
        o: prop(f, "ADBE Vector Fill Opacity"),
        rule: prop(f, "ADBE Vector Fill Rule")
    };
}

function strokeRefs(s) {
    return {
        c: prop(s, "ADBE Vector Stroke Color"),
        w: prop(s, "ADBE Vector Stroke Width"),
        o: prop(s, "ADBE Vector Stroke Opacity"),
        lc: prop(s, "ADBE Vector Stroke Line Cap"),
        lj: prop(s, "ADBE Vector Stroke Line Join")
    };
}

function trimRefs(t) {
    return {
        s: prop(t, "ADBE Vector Trim Start"),
        e: prop(t, "ADBE Vector Trim End"),
        o: prop(t, "ADBE Vector Trim Offset")
    };
}

function collectDrawables(contents, warn) {
    var out = [];

    function walk(group, chain, inherited) {
        var localTr = null, fill = null, stroke = null, trim = null, merge = null;
        var fillIdx = 99, strokeIdx = 99;
        var i, it, mn;
        for (i = 1; i <= group.numProperties; i++) {
            it = group.property(i);
            if (!it) continue;
            mn = it.matchName;
            if (mn === "ADBE Vector Transform Group") localTr = it;
            else if (mn === "ADBE Vector Graphic - Fill") {
                if (isEnabled(it)) { fill = it; fillIdx = i; }
            } else if (mn === "ADBE Vector Graphic - Stroke") {
                if (isEnabled(it)) { stroke = it; strokeIdx = i; }
            } else if (mn === "ADBE Vector Filter - Trim") trim = it;
            else if (mn === "ADBE Vector Filter - Merge") merge = it;
            else if (mn === "ADBE Vector Graphic - G-Fill" || mn === "ADBE Vector Graphic - G-Stroke") {
                warn("gradient in '" + group.name + "' flattened to a solid color");
            } else if (mn === "ADBE Vector Filter - Repeater") {
                warn("repeater in '" + group.name + "' is not converted");
            } else if (mn === "ADBE Vector Filter - RC" || mn === "ADBE Vector Filter - PB" ||
                       mn === "ADBE Vector Filter - Twist" || mn === "ADBE Vector Filter - Offset" ||
                       mn === "ADBE Vector Filter - Zigzag") {
                warn("path modifier in '" + group.name + "' is not converted");
            }
        }

        var chain2 = chain.slice(0);
        if (localTr) chain2.push(transformRefs(localTr));

        var paint = {
            fill: fill ? fillRefs(fill) : inherited.fill,
            stroke: stroke ? strokeRefs(stroke) : inherited.stroke,
            trim: trim ? trimRefs(trim) : inherited.trim,
            merge: merge || inherited.merge,
            // items lower in the AE list render first; if the fill sits above
            // the stroke it paints over it, which SVG needs paint-order for
            fillOverStroke: (fill && stroke) ? (fillIdx < strokeIdx)
                                             : (inherited.fillOverStroke || false)
        };

        var drawables = [];
        for (i = 1; i <= group.numProperties; i++) {
            it = group.property(i);
            if (!it) continue;
            mn = it.matchName;
            if (mn === "ADBE Vector Shape - Group") {
                drawables.push({ kind: "path", name: it.name,
                                 prop: it.property("ADBE Vector Shape") });
            } else if (mn === "ADBE Vector Shape - Ellipse") {
                drawables.push({ kind: "ellipse", name: it.name,
                                 size: prop(it, "ADBE Vector Ellipse Size"),
                                 pos: prop(it, "ADBE Vector Ellipse Position") });
            } else if (mn === "ADBE Vector Shape - Rect") {
                drawables.push({ kind: "rect", name: it.name,
                                 size: prop(it, "ADBE Vector Rect Size"),
                                 pos: prop(it, "ADBE Vector Rect Position"),
                                 round: prop(it, "ADBE Vector Rect Roundness") });
            }
        }

        var mergeMode = merge ? pv(prop(merge, "ADBE Vector Merge Type"), 0, 1)
                              : (inherited.mergeMode || 1);
        for (i = 0; i < drawables.length; i++) {
            drawables[i].chain = chain2;
            drawables[i].paint = paint;
            drawables[i].merged = (paint.merge && drawables.length > 1) ? true : false;
            drawables[i].mergeMode = mergeMode;
            out.push(drawables[i]);
        }
        paint.mergeMode = mergeMode;

        for (i = 1; i <= group.numProperties; i++) {
            it = group.property(i);
            if (it && it.matchName === "ADBE Vector Group") {
                walk(it.property("ADBE Vectors Group"), chain2, paint);
            }
        }
    }

    walk(contents, [], { fill: null, stroke: null, trim: null, merge: null,
                         mergeMode: 1, fillOverStroke: false });
    return out;
}

function chainMatrixAt(chain, t) {
    var M = mIdent();
    for (var i = 0; i < chain.length; i++) {
        var c = chain[i];
        M = mMul(M, mFromParts(
            pv(c.p, t, [0, 0]), pv(c.a, t, [0, 0]), pv(c.s, t, [100, 100]),
            pv(c.r, t, 0), pv(c.sk, t, 0), pv(c.sa, t, 0)));
    }
    return M;
}

function ellipseShape(dr, t) {
    var size = pv(dr.size, t, [0, 0]);
    var pos = pv(dr.pos, t, [0, 0]);
    var rx = size[0] / 2, ry = size[1] / 2, k = 0.5519150244935105;
    var cx = pos[0], cy = pos[1];
    return {
        vertices: [[cx, cy - ry], [cx + rx, cy], [cx, cy + ry], [cx - rx, cy]],
        outTangents: [[rx * k, 0], [0, ry * k], [-rx * k, 0], [0, -ry * k]],
        inTangents: [[-rx * k, 0], [0, -ry * k], [rx * k, 0], [0, ry * k]],
        closed: true,
        _circle: Math.abs(rx - ry) <= Math.max(0.02 * rx, 0.05)
            ? { cx: cx, cy: cy, r: (rx + ry) / 2 } : null
    };
}

function rectShape(dr, t, warn) {
    var size = pv(dr.size, t, [0, 0]);
    var pos = pv(dr.pos, t, [0, 0]);
    if (pv(dr.round, t, 0) > 0) warn("rounded rectangle corner radius is approximated as square");
    var x = pos[0] - size[0] / 2, y = pos[1] - size[1] / 2;
    var z = [[0, 0], [0, 0], [0, 0], [0, 0]];
    return {
        vertices: [[x, y], [x + size[0], y], [x + size[0], y + size[1]], [x, y + size[1]]],
        inTangents: z, outTangents: z, closed: true, _circle: null
    };
}

// --------------------------------------------------------------------------- //
// comp lookup  (the panel works from a queue, not from whatever is frontmost)
// --------------------------------------------------------------------------- //
function compById(id) {
    var items = app.project.items;
    for (var i = 1; i <= items.length; i++) {
        var it = items[i];
        if (it instanceof CompItem && it.id === id) return it;
    }
    return null;
}

function compInfo(c, selected) {
    return {
        id: c.id,
        name: c.name,
        w: c.width, h: c.height,
        fps: Math.round(1 / c.frameDuration * 1000) / 1000,
        frames: Math.round(c.duration / c.frameDuration),
        layers: c.numLayers,
        selected: !!selected
    };
}

/** Every comp in the project, flagged with whether it is selected in the
 *  Project panel or open in the timeline, so the panel can prime its queue. */
function L2S_listComps() {
    var out = [], i, chosen = {};
    try {
        var sel = app.project.selection;
        for (i = 0; i < sel.length; i++) {
            if (sel[i] instanceof CompItem) chosen[sel[i].id] = true;
        }
    } catch (e) { /* no selection */ }
    try {
        var act = app.project.activeItem;
        if (act && act instanceof CompItem) chosen[act.id] = true;
    } catch (e2) { /* no active item */ }

    var items = app.project.items;
    for (i = 1; i <= items.length; i++) {
        if (items[i] instanceof CompItem) out.push(compInfo(items[i], chosen[items[i].id]));
    }
    out.sort(function (a, b) { return a.name < b.name ? -1 : (a.name > b.name ? 1 : 0); });
    return jval({ ok: true, comps: out,
                  projectPath: (app.project.file ? app.project.file.fsName : "") });
}

// --------------------------------------------------------------------------- //
// bake
// --------------------------------------------------------------------------- //
function L2S_bake(destPath, useWorkArea, compId) {
    var warnings = [];
    function warn(m) {
        for (var i = 0; i < warnings.length; i++) if (warnings[i] === m) return;
        warnings.push(m);
    }

    var comp = (compId === undefined || compId === null || compId < 0)
        ? app.project.activeItem : compById(compId);
    if (!comp || !(comp instanceof CompItem)) {
        return jval({ ok: false, error: "Composition not found. Select a comp in the Project panel." });
    }

    var fd = comp.frameDuration;
    var t0 = useWorkArea ? comp.workAreaStart : 0;
    var t1 = useWorkArea ? (comp.workAreaStart + comp.workAreaDuration) : comp.duration;
    var f0 = Math.round(t0 / fd);
    var f1 = Math.round(t1 / fd);
    if (f1 - f0 < 1) {
        return jval({ ok: false, error: comp.name + ": range is shorter than one frame." });
    }

    var frames = [], i, f, fi;
    for (f = f0; f <= f1; f++) frames.push(f);

    // sourcePointToComp resolves against the comp's own time, and AE is only
    // dependable about that for the comp that is open — so front this one and
    // put the viewer back where it was afterwards.
    var prevActive = null;
    try { prevActive = app.project.activeItem; } catch (e0) {}
    try { comp.openInViewer(); } catch (e1) {}
    var savedTime = comp.time;

    var prep = [], layersOut = [];
    try {
        // ---- prep: one pass over the layer tree, no time sampling yet ------ //
        for (var li = 1; li <= comp.numLayers; li++) {
            var layer = comp.layer(li);
            var rec = {
                index: li,
                name: layer.name,
                enabled: layer.enabled,
                type: (layer instanceof ShapeLayer) ? "shape" : "other",
                ip: Math.round(layer.inPoint / fd),
                op: Math.round(layer.outPoint / fd),
                matte: "none",
                isMatteSource: false,
                shapes: [],
                opacity: []
            };

            try { if (layer.isTrackMatte) rec.isMatteSource = true; } catch (e) {}
            try {
                var tmt = layer.trackMatteType;
                if (tmt === TrackMatteType.ALPHA) rec.matte = "alpha";
                else if (tmt === TrackMatteType.ALPHA_INVERTED) rec.matte = "alphaInv";
                else if (tmt === TrackMatteType.LUMA) rec.matte = "luma";
                else if (tmt === TrackMatteType.LUMA_INVERTED) rec.matte = "lumaInv";
            } catch (e2) {}
            try {
                if (layer.trackMatteLayer) rec.matteSourceIndex = layer.trackMatteLayer.index;
            } catch (e3) {}

            // A hidden layer is not drawn. It is still needed in the parent
            // chain, but AE resolves that inside sourcePointToComp, so there is
            // nothing to sample. Matte sources are hidden by AE and DO need
            // their geometry, so they are excluded from this skip.
            if (!layer.enabled && !rec.isMatteSource) {
                rec.skipped = "hidden";
                layersOut.push(rec);
                continue;
            }
            if (!(layer instanceof ShapeLayer)) {
                if (layer.enabled && !(layer instanceof CameraLayer) && !layer.nullLayer) {
                    warn("layer '" + layer.name + "' is not a shape layer and was skipped");
                }
                layersOut.push(rec);
                continue;
            }

            if (layer.threeDLayer) warn("layer '" + layer.name + "' is 3D; only its 2D transform is used");
            try { if (layer.mask.numProperties > 0) warn("layer '" + layer.name + "' has masks; masks are not converted"); } catch (e4) {}
            try { if (layer.effect.numProperties > 0) warn("layer '" + layer.name + "' has effects; effects are not converted"); } catch (e5) {}

            var draws = collectDrawables(layer.property("ADBE Root Vectors Group"), warn);
            var acc = [];
            for (i = 0; i < draws.length; i++) {
                acc.push({
                    name: draws[i].name, d: [], len: [],
                    cx: [], cy: [], r: [], isCircle: true,
                    merged: draws[i].merged, mergeMode: draws[i].mergeMode,
                    fillOverStroke: draws[i].paint.fillOverStroke,
                    fillColor: null, fillOpacity: [], fillRule: "nonzero",
                    strokeColor: null, strokeWidth: [], strokeOpacity: [],
                    cap: "round", join: "round",
                    trimStart: [], trimEnd: [], trimOffset: [], hasTrim: false
                });
            }
            prep.push({
                layer: layer, rec: rec, draws: draws, acc: acc,
                opacity: layer.property("ADBE Transform Group").property("ADBE Opacity")
            });
            layersOut.push(rec);
        }

        // ---- sample: comp.time moves ONCE per frame ------------------------ //
        for (fi = 0; fi < frames.length; fi++) {
            comp.time = frames[fi] * fd;
            for (var pi = 0; pi < prep.length; pi++) {
                samplePrep(prep[pi], frames[fi] * fd, fi, warn);
            }
        }
    } catch (err) {
        comp.time = savedTime;
        restoreViewer(prevActive);
        return jval({ ok: false, error: "Bake failed in '" + comp.name + "': " +
                      err.toString() + " (line " + err.line + ")" });
    }

    comp.time = savedTime;
    restoreViewer(prevActive);

    for (var qi = 0; qi < prep.length; qi++) finishPrep(prep[qi], frames.length);

    var doc = {
        format: "l2s-baked/1",
        name: comp.name,
        w: comp.width, h: comp.height,
        fps: Math.round(1 / fd * 1000) / 1000,
        frameStart: f0, frameEnd: f1, frameCount: f1 - f0,
        frames: frames,
        layers: layersOut,
        warnings: warnings
    };

    var file = new File(destPath);
    file.encoding = "UTF-8";
    if (!file.open("w")) return jval({ ok: false, error: "Cannot write " + destPath });
    file.write(jval(doc));
    file.close();

    var worst = 0;
    for (var wi = 0; wi < layersOut.length; wi++) {
        if (layersOut[wi].affineResidual > worst) worst = layersOut[wi].affineResidual;
    }
    return jval({ ok: true, path: destPath, comp: comp.name, frames: frames.length,
                  affineWorst: worst, warnings: warnings });
}

function restoreViewer(prev) {
    try { if (prev && prev instanceof CompItem) prev.openInViewer(); } catch (e) {}
}

function samplePrep(P, t, fi, warn) {
    var layer = P.layer, rec = P.rec, draws = P.draws, acc = P.acc;
    var ML = layerMatrixAt(layer);
    if (fi === 0) {
        var resid = affineResidual(layer, ML);
        if (resid > 0.05) {
            warn("layer '" + layer.name + "': its transform is not affine (residual " +
                 Math.round(resid * 100) / 100 + " px) — probably a 3D layer or a " +
                 "camera is in play. Geometry from this layer will be wrong; make it 2D.");
        }
        rec.affineResidual = Math.round(resid * 1000) / 1000;
    }
    rec.opacity.push(Math.round((pv(P.opacity, t, 100) / 100) * 1000) / 1000);

    for (var i = 0; i < draws.length; i++) {
        var dr = draws[i], A = acc[i];
        var M = mMul(ML, chainMatrixAt(dr.chain, t));
        var sh;
        if (dr.kind === "path") sh = pv(dr.prop, t, null);
        else if (dr.kind === "ellipse") sh = ellipseShape(dr, t);
        else sh = rectShape(dr, t, warn);
        if (!sh || !sh.vertices || !sh.vertices.length) continue;

        A.d.push(shapeToD(sh, M));

        var circ = (sh._circle !== undefined) ? sh._circle : circleOf(sh);
        if (circ && !dr.paint.trim) {
            var cc = mApply(M, circ.cx, circ.cy);
            A.cx.push(n2(cc[0]));
            A.cy.push(n2(cc[1]));
            A.r.push(n2(circ.r * mScale(M)));
        } else {
            A.isCircle = false;
        }

        if (dr.paint.fill) {
            A.fillColor = hex3(pv(dr.paint.fill.c, t, [0, 0, 0, 1]));
            A.fillOpacity.push(Math.round(pv(dr.paint.fill.o, t, 100)) / 100);
            A.fillRule = (pv(dr.paint.fill.rule, t, 1) === 2) ? "evenodd" : "nonzero";
        }
        if (dr.paint.stroke) {
            A.strokeColor = hex3(pv(dr.paint.stroke.c, t, [0, 0, 0, 1]));
            A.strokeWidth.push(n2(pv(dr.paint.stroke.w, t, 0) * mScale(M)));
            A.strokeOpacity.push(Math.round(pv(dr.paint.stroke.o, t, 100)) / 100);
            var lc = pv(dr.paint.stroke.lc, t, 2);
            A.cap = lc === 1 ? "butt" : (lc === 3 ? "square" : "round");
            var lj = pv(dr.paint.stroke.lj, t, 2);
            A.join = lj === 1 ? "miter" : (lj === 3 ? "bevel" : "round");
        }
        if (dr.paint.trim) {
            A.hasTrim = true;
            A.trimStart.push(pv(dr.paint.trim.s, t, 0));
            A.trimEnd.push(pv(dr.paint.trim.e, t, 100));
            A.trimOffset.push(pv(dr.paint.trim.o, t, 0));
            A.len.push(n2(shapeArcLen(sh, M)));
        }
    }
}

function finishPrep(P, nFrames) {
    for (var i = 0; i < P.acc.length; i++) {
        var A = P.acc[i];
        if (!A.d.length) continue;
        var out = {
            name: A.name, d: A.d, merged: A.merged, mergeMode: A.mergeMode,
            fillOverStroke: A.fillOverStroke
        };
        if (A.isCircle && A.cx.length === nFrames) {
            out.circle = { cx: A.cx, cy: A.cy, r: A.r };
        }
        if (A.fillColor) {
            out.fill = { color: A.fillColor, opacity: A.fillOpacity, rule: A.fillRule };
        }
        if (A.strokeColor) {
            out.stroke = { color: A.strokeColor, width: A.strokeWidth,
                           opacity: A.strokeOpacity, cap: A.cap, join: A.join };
        }
        if (A.hasTrim) {
            out.trim = { start: A.trimStart, end: A.trimEnd,
                         offset: A.trimOffset, len: A.len };
        }
        P.rec.shapes.push(out);
    }
}
