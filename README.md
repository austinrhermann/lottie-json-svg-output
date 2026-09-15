Lottie / JSON / SVG Export - After Effects Plugin

<img width="3692" height="2292" alt="ljs-0" src="https://github.com/user-attachments/assets/19d40749-4356-4e80-9637-3a96bb3324cd" />


An After Effects panel and a command-line tool that turn one comp — or a queue
of them — into a self-contained animated SVG (CSS/SMIL only, no player, no
`lottie-web`, no JS runtime, design tokens already substituted for the literal
hexes) and/or into Lottie JSON, with no Bodymovin or LottieFiles plugin in the
loop.

```
Window > Extensions > SVG + Lottie Export
```

Tick the comps you want, tick Animated SVG and/or Lottie JSON, hit Export.

**Requirements**: After Effects 2019 (16.0) or newer, and Python 3.8+ on PATH.
Built and used daily on macOS (Apple Silicon). Windows has an installer but is
untested — see [Windows](#windows) below.

---

## Install

```bash
git clone https://github.com/austinrhermann/lottie-json-svg-output.git
cd lottie-json-svg-output
./install.sh
```

Then quit After Effects **fully** and reopen it.

The installer builds a private Python environment at `~/.lottie2svg/venv`,
copies the panel into AE's CEP extensions folder, allows unsigned extensions
(`PlayerDebugMode`, which every locally built panel needs), and writes an `l2s`
command to `~/.lottie2svg/bin`. It never touches system Python and never touches
another Adobe extension. Re-running it upgrades in place; `./install.sh
--uninstall` removes the panel.

### Windows

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

Same idea, `%APPDATA%\Adobe\CEP\extensions` and the `HKCU\Software\Adobe\CSXS.*`
registry keys instead. This path has not been exercised on a real Windows
install — if something goes wrong there it is almost certainly the installer or
the CEP keys rather than the panel, and a report with the details is welcome.

### It installed but the panel is not in the Extensions menu

Almost always `PlayerDebugMode` not landing on the CEP version your build of AE
actually uses. The installer writes it for CEP 9 through 13; check which one is
in play and set it by hand:

```bash
defaults write com.adobe.CSXS.11 PlayerDebugMode 1   # macOS, adjust the number
```

Then quit and reopen AE. If the panel opens but logs *"no interpreter at
…/venv/bin/python3"*, the Python half did not build — re-run the installer from
a terminal and read what pip says.

---

## One bake, two outputs

**The export queue** — the panel lists every comp in the project, pre-ticking
whatever is selected in the Project panel or open in the timeline. Each ticked
comp is baked and emitted in turn, one at a time (a bake drives `comp.time`, so
two at once would fight over the viewer). The list refreshes on the Refresh
button and whenever the panel regains focus.

`jsx/host.jsx` samples each comp directly inside After Effects: every property
at every frame via `valueAtTime()`, and the whole parent/anchor/scale/rotation
chain via AE's own `sourcePointToComp()`. Bodymovin is not involved. **After
Effects is the oracle** — nothing re-derives eased curves from exported bezier
handles, so that entire class of bug does not exist on this path, and hold
keyframes step because AE's own evaluator steps them.

That one bake feeds both emitters, so the SVG and the JSON are the same
animation by construction.

**Convert file… / Watch folder…** — for Lottie JSON you already have, from
Bodymovin or anywhere else. Watch mode
converts whatever lands in a folder, waiting for the file size to settle first
because Bodymovin writes progressively. Render from Bodymovin, the SVG appears.
This path uses python-lottie's evaluator as the oracle, which matches AE;
lottie-web is never used and never should be, because it mis-steps position
keyframes carrying spatial tangents.

---

## Lottie JSON export

Ticking **Lottie JSON** writes a baked Lottie beside the SVG: every animated
property is a per-frame linear keyframe track in comp space, layer transforms
are identity, and no keyframe carries spatial tangents (`ti`/`to`) or a hold
flag. That is deliberate on both counts:

* lottie-web mis-steps position keyframes that carry spatial tangents — it
  jumps most of the way to the next keyframe in about a frame and then holds.
  A baked linear track cannot reach that code path, so players reproduce After
  Effects exactly.
* nothing has to reconstruct easing from bezier handles, which is the same
  accuracy model the SVG route uses.

The trade is size and editability. The JSON is larger than a Bodymovin export
and its keyframes are not the ones you set in AE, so treat it as an output
format, not a round-trip format — the `.aep` stays the source. `--simplify` and
`--precision` apply to it exactly as they do to the SVG, and exact-hold samples
collapse losslessly either way.

Circles become an animated `el` (ellipse) rather than a baked path, the same
detection the SVG route uses, which is most of the size difference on icon work.
Track mattes are preserved: the source layer is emitted with `td` immediately
above the matted layer, which carries `tt`.

---

## Hold keyframes on the Lottie route

An AE **Hold** keyframe (`"h": 1`) holds its value flat until the next
keyframe's time and then jumps. python-lottie 0.7.2 parses the flag but
`Property.get_value()` interpolates straight through it, so per-frame sampling a
hold track would bake a smooth ramp over a step — which reads as "wrong easing",
not "missing feature", and so tends to survive review.

`py/kf.py` rewrites every hold segment as flat-then-snap keyframes on the raw
document before python-lottie ever sees it. It is value-type agnostic, so
position, scale, rotation, opacity, color, path morphs and trim offsets are all
covered in one pass, and eased tracks come through byte-identical. The number of
holds found is reported as a warning on every affected export.

The AE bake route needs none of this; `valueAtTime()` steps them natively.

---

## Corrected upstream: python-lottie position interpolation

`py/aefix.py` patches a defect in python-lottie that affects every position
keyframe carrying spatial tangents — that is, every dot travelling an AE bezier
motion path. On that one branch of `OffsetKeyframe.interpolated_value`:

* the temporal ease from the keyframe's own handles is never applied, so the dot
  moves at an essentially constant rate no matter what the speed graph says;
* the curve is indexed by bezier parameter rather than arc length, so even a
  genuinely linear segment picks up a speed that varies with handle curvature.

Non-spatial keyframes interpolate correctly, which is how this hides. The
symptom is "my eases came out linear". Demonstration: with the patch removed, a
segment with a heavy ease-in returns byte-identical values to the same segment
with a linear ease.

After Effects works the other way round — the temporal ease maps time to a
fraction of DISTANCE TRAVELLED, and that fraction locates the point on the path.
The patch restores that order. Measured after: a linear segment holds constant
speed to within 0.17% (was 29%), and easing changes the result again.

If you ever calibrate against python-lottie directly, note that comparing this
tool's output to `get_value()` is circular and will not catch this.

## Accuracy model

Every animated property is sampled per frame and emitted as a linear track.
There is no easing reconstruction anywhere, so there is no handle-convention to
get wrong. Holds collapse losslessly — a sample is dropped only when it is
exactly equal to both neighbours — and motion never does.

Measured on the bundled synthetic fixture against python-lottie ground truth:

| what | error |
|---|---|
| dot position, 181 frames | 0.0057 px (2-decimal rounding) |
| rotation, 181 frames | 0.00005 deg |
| verified transform fits | 0.0066 px |

## Animated mattes

When the matte source has an animated transform, the clip is animated with it —
the `clipPath`'s own path data is emitted as a per-frame track. A clip frozen at
frame 0 while the dots travel their real paths is what makes dots look like they
have left their mask.

## Clip safety

Content inside a track-matte clip is animated **by geometry only**: circles move
via `cx`/`cy`/`r`, paths via `d`. Never a transform.

Transform animation on clipped content can be promoted to its own GPU
compositor layer, and a promoted layer escapes its ancestor `clip-path`. That
failure is invisible in headless and software rendering, so it cannot be caught
by verification after the fact. It is prevented structurally instead: the
transform optimisation is gated on the element being unclipped, and there is no
code path that can put a transform on clipped content.

## The transform optimisation

For **unclipped** layers only, a least-squares affine fit collapses a per-frame
path track back into nested `translate`/`rotate`/`scale`, or — when the motion is
a rotation about a fixed centre, which is the usual icon case — into a single
`rotate(deg cx cy)` track. On the fixture this recovers the AE anchor exactly
and cuts 25.4 KB to 19.4 KB.

It is checked, not assumed. The fitted transform is re-rendered and measured
against the baked geometry; anything over 0.16 px is rejected and the path data
is baked instead. The achieved residual is reported in the panel and in
`--json`. The fit is also refused whenever a substituted scale would distort a
stroke width or a trim-path dash length.

## Fill and stroke are both painted

AE paints a fill and a stroke together when both items are enabled, and it skips
items whose checkbox is off. Two consequences that bit hard in practice:

* Shape layers frequently carry AE's **default white 2px butt/miter stroke**.
  Against a white comp background it is invisible, so it survives unnoticed in
  the project. Capturing it and then emitting `fill="none" stroke="#ffffff"`
  turns a solid pictogram into a white outline and tokenizes everything to
  `background-stroke`. The sampler now respects the item's enabled flag, and a
  zero-width or fully transparent paint is dropped.
* A phantom stroke also used to disqualify a dot from circle detection, forcing
  242 frames of baked path data where two `cx`/`cy` tracks would do. On a real
  icon that was the difference between 43 KB and 8 KB.

Where AE has the fill above the stroke, `paint-order="stroke fill"` preserves
that order. If a stroke really is intended, it now appears alongside the fill
class rather than replacing it.

## Merge paths

AE's merge modes are mapped onto the nearest SVG `fill-rule`: Merge and Add
become `nonzero`, Exclude Intersections becomes `evenodd`. Subtract and Intersect
have no `fill-rule` equivalent — they need real path booleans — so those warn and
union instead. Applying `evenodd` to an Add merge is what makes overlapping
subpaths cancel and shapes go hollow or vanish.

## Hidden layers are controllers, not artwork

A very common AE pattern is a shape layer with its eye switched off, existing
only so visible layers can be parented to it and inherit its rotation — the
`SPIN` / `Dot Spin` / `Big` / `Small` layers in a typical icon comp.

Bodymovin still exports those layers, marked `hd`, because a child depends on
their transform. A converter that ignores `hd` draws them. They usually sit at
the top of the stack, so they draw LAST — painting a large opaque shape over
everything, which reads as "my dots disappeared" rather than "an invisible layer
became visible". Layer-level `hd` and shape-level hidden flags are now both
honoured on the Lottie route, and the AE route skips layers whose video switch is
off. Either way the transform stays available through the parent chain.

Matte sources are excluded from that skip: AE hides them automatically and their
geometry is exactly what the clip needs.

## Rotation continuity

Decomposing a sampled transform recovers the angle with `atan2`, which returns
[-180, 180]. Any layer turning past half a turn therefore produces a track that
reads `... 178.9, -178.9 ...` between adjacent frames, and SMIL interpolates
that linearly — spinning the element a full turn backwards in one frame. Sampled
angle tracks are unwrapped so the track stays continuous.

Worth noting how this got through the first time: the fit verification compared
the *rendered points* via cos and sin of the angle, which are invariant to a
360-degree offset. The samples were right and the interpolation between them was
not. There is now an explicit continuity check on the angle track itself, which
is the thing the earlier check could not see.

## Making the files smaller

In rough order of how much they buy you:

**1. Serve them compressed.** SVG is text and compresses hard: a real 176 KB icon
came down to 57 KB gzipped, and a 8.3 KB one to 2.6 KB. This costs nothing in
quality and is usually the whole answer. Every export now reports its gzipped
size alongside the raw size, because the raw number is not what ships.

**2. `--simplify 0.1`** (panel: *Simplify px*, on by default at 0.1). Drops
animation samples that a straight segment between their neighbours already
covers to within that many pixels. Typically 40-60% off a path-heavy file.

This is decimation on the TIME axis with the error measured geometrically, which
is a different thing from simplifying the path itself — the latter wrecks timing
and should not be used. Here every candidate for removal is judged by how far the
surviving interpolation would place every point at that frame, so a fast
transition cannot be dropped: dropping it produces a large displacement and the
frame is kept. Measured on a dot snapping 33 px per frame, the first and last
frames of the snap survive at every tolerance from 0.05 to 0.5 px, and only the
frames lying exactly on the ramp are removed. The worst error actually introduced
is reported on every export — read it rather than trusting the setting.

Above roughly 0.3 px, easing on slow moves starts to show as stepping. 0.1 is a
good default; 0.25 is defensible for icons rendered small.

**3. `--precision 1`.** On a 240-unit comp displayed at 48 px, one decimal is
0.02 px on screen. Worth about 10-15% of the path data.

**4. Check WHY a file is large.** Every shape stored as baked path data now says
so, and says what forced it: a track matte (a transform there could escape the
clip), a trim path (a substituted scale would distort the dash lengths), the
geometry-only option, or motion that simply is not a rigid transform. A shape
that only rotates or translates collapses to a couple of hundred bytes — if
something you expect to be cheap is expensive, that message tells you which
constraint to remove in AE. A matte that is not doing real work is the most
common one worth deleting.

Uniformly spaced tracks now omit `keyTimes` entirely, since SMIL spaces samples
evenly by default. That is free and automatic.

## Loop seams

If the last sampled frame does not return to the first frame's pose, the loop
will visibly snap. The converter says so rather than shipping the jump. Trim to
the seamless window with `--loop-start` / `--loop-end`, or match the end pose in
AE.

---

## Tokens

`py/tokens.json` maps the literal colors your comps use onto CSS custom
properties. It is data, not code — edit it and every future export follows.

```json
"dot": {
  "hex": ["1a1414"],
  "class": { "fill": "dot-shape", "stroke": "dot-stroke" },
  "token": "--icon-neutral-primary"
}
```

Each role carries both a fill and a stroke class, because a class that sets
`fill` will not color a stroked path. The emitted SVG carries the class and no
inline paint; your stylesheet owns the rest. `Copy CSS` in the panel (or
`l2s --emit-css`) prints the matching bindings.

The roles shipped in `tokens.json` are an example from one icon library.
Rename them, add your own, or delete them all and untick *Token classes* to keep
literal hex. Changing which token a role points at is a one-line edit here
rather than a re-export of everything.

---

## Command line

```bash
l2s icon.json                        # -> icon.svg beside the input
l2s icon.json -o out/icon.svg
l2s *.json -d out/                   # batch
l2s bake.json --format lottie        # -> bake-lottie.json
l2s bake.json --format both -d out/  # SVG and Lottie from one bake
l2s icon.json --trigger hover        # play once on hover or tap
l2s icon.json --loop-start 36 --loop-end 246
l2s icon.json --no-tokens            # keep literal hex
l2s icon.json --json                 # machine-readable result
l2s --emit-css                       # stylesheet bindings
l2s --emit-tokens                    # a tokens.json to start from
```

It takes either a Lottie JSON or a baked intermediate and tells them apart by
reading the file, not the extension. `--format lottie` and `--format both` need
a bake as input — an existing Lottie JSON only converts one way, to SVG. The
output extension follows the format, and an output that would land on top of its
own input gets a `-lottie` suffix rather than eating the bake.

| flag | why you would reach for it |
|---|---|
| `--precision N` | coordinate decimals; 2 is the default, 1 saves ~7% |
| `--sample-step N` | sample every Nth frame; smaller files, no longer frame-exact |
| `--geometry-only` | never substitute transforms, even where it is safe |
| `--prefix NAME` | id namespace; defaults to the filename |
| `--format` | `svg` (default), `lottie`, or `both` |

IDs and `xlink:href` references are namespaced per icon so several inlined SVGs
on one page cannot collide.

---

## Supported

Shape and null layers · parent chains · group transforms · animated position,
scale, rotation and opacity · path morphs · ellipses · rects · fills · strokes
with caps and joins · trim paths · merge (evenodd) · alpha track mattes · layer
in/out gating.

## Warns and degrades

Inverted mattes (emitted as a normal clip — SVG `clipPath` cannot invert) · luma
mattes (emitted as alpha) · gradients (flattened) · effects, repeaters, path
modifiers · 3D layers (2D transform only) · time remapping · precomps · text ·
rounded-rect corner radius · vertex-count changes between frames (SVG cannot
interpolate those, so the first frame is held).

Warnings go to stderr, or into the `--json` payload, and appear in the panel in
amber. Read them.

---

## Layout

```
CSXS/manifest.xml      panel registration
index.html             panel UI and the tooltip copy
js/main.js             panel logic: queue, bake, shell out, watch, persist
js/CSInterface.js      Adobe's CEP bridge (vendored, do not edit)
jsx/host.jsx           the AE sampler — this is where AE becomes the oracle
py/l2s.py              CLI and dispatch
py/l2s_core.py         Lottie JSON route + shared emission primitives
py/l2s_baked.py        AE-baked route -> SVG, plus the affine fit
py/l2s_lottie.py       AE-baked route -> Lottie JSON
py/kf.py               hold-keyframe semantics for the Lottie JSON route
py/aefix.py            the python-lottie interpolation patch
py/tokens.json         your colour -> design token map
install.sh             macOS installer
install.ps1            Windows installer (untested)
tools/package.sh       build the installable zip for a release
```

The repo root *is* the extension: the installers copy it into AE's extensions
folder under the bundle id in `CSXS/manifest.xml`. Forking it under a different
name means changing that id in three places in the manifest and the
`BUNDLE_ID` at the top of both installers.

Settings live in `~/.lottie2svg/config.json`.

---

## Cost of a bake

Setting `comp.time` makes After Effects re-evaluate the whole comp, so it is set
**once per frame**, with every layer sampled inside that tick. The first version
set it once per layer per frame, which cost a comp's worth of re-evaluation for
each layer. Property references (`ADBE Vector Position` and friends) are also
resolved once during prep instead of being looked up by matchName on every
sample. On a 300-frame, 10-layer icon that is the difference between one
re-evaluation pass and ten.

The comp being baked is fronted with `openInViewer()` and the previously active
item is restored afterwards, because `sourcePointToComp` resolves against the
comp's own time and AE is only dependable about that for the open comp. Expect
the viewer to flicker through the queue; that is the bake, not a bug.

## Known limits

Run your own comps through it and diff against SVGs you already trust before
you rely on it for a batch — that is the best regression suite there is, and
this has been validated against one icon set, not against everything AE can do.
The list under *Warns and degrades* is the honest boundary; anything outside it
says so in the log rather than failing quietly.

Baking `d` per frame is large for complex clipped morphs, and the affine fit
does not apply there by design.

`--trigger hover` uses SMIL event timing (`begin="…mouseenter;…touchstart"`,
`fill="remove"`) rather than the CSS `animation-play-state` pattern. It needs no
JS toggle, but if you want the CSS-class route for button wrapping, that is a
mode worth adding.

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). The short version: the useful bug report
is a bake. Tick *Keep baked JSON*, export, and attach the `-bake.json` — it
reproduces the problem without your project file.

## License

MIT. See [LICENSE](LICENSE).
