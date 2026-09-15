# Changelog

## 2.1.0

* Tooltips on every setting, button and queued comp, explaining what each one
  actually does and what the trade-off is.
* The panel picks its interpreter and its reveal-in-file-manager command by
  platform, so it is no longer hard-wired to macOS.
* `install.ps1` for Windows. Untested — see the README.

## 2.0.0

* **Export queue.** Every comp in the project is listed and tickable, pre-ticked
  from the Project panel selection. Comps are baked and emitted one at a time.
* **Lottie JSON export**, from the same After Effects bake that feeds the SVG.
  No Bodymovin and no LottieFiles plugin in the loop. Per-frame linear
  keyframes, no spatial tangents, so lottie-web reproduces AE exactly.
* **Hold keyframes** (`"h": 1`) are honoured on the Lottie-JSON input route.
  python-lottie interpolates straight through them, which turned a step into a
  smooth ramp; `py/kf.py` rewrites hold segments as flat-then-snap before
  python-lottie ever sees the document, and the export says how many it found.
* Baking is much faster: `comp.time` is set once per frame with every layer
  sampled inside that tick, rather than once per layer per frame, and property
  references are resolved once during prep instead of by matchName on every
  sample.
* The panel no longer polls After Effects on a timer; the comp list refreshes on
  demand and when the panel regains focus.
* An output can no longer overwrite its own input bake.
* `--format svg|lottie|both` on the command line.
* Deduplicated `unwrap_degrees`, dropped an unused import, consolidated the
  panel's two python runners into one.

## 1.0.0

Initial version: single-comp animated SVG export, Lottie JSON to SVG conversion,
watch folder, design-token substitution, the affine transform fit, and the
python-lottie spatial-tangent interpolation patch.
