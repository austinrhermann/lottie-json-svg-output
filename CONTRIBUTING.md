# Contributing

The useful bug report is a **bake**. Tick *Keep baked JSON* in the panel, run the
export, and attach the `<comp>-bake.json` it leaves in the output folder along
with the wrong SVG or Lottie. That file is the exact input both emitters read, so
it reproduces the problem without your `.aep`, your fonts or your AE version.
`l2s <comp>-bake.json -o out.svg` re-runs it offline.

## Where things live

| you want to change | file |
|---|---|
| what gets sampled out of After Effects | `jsx/host.jsx` |
| the panel, the queue, settings | `index.html`, `js/main.js` |
| bake -> animated SVG | `py/l2s_baked.py` |
| bake -> Lottie JSON | `py/l2s_lottie.py` |
| existing Lottie JSON -> SVG | `py/l2s_core.py` |
| hold keyframes, easing rules | `py/kf.py` |
| the python-lottie interpolation patch | `py/aefix.py` |
| colour -> design token mapping | `py/tokens.json` |

`jsx/host.jsx` is ExtendScript, which is ES3: `var` only, no arrow functions, no
`let`/`const`, no `JSON`, no `Array.prototype.forEach`. `js/main.js` runs in CEP's
Chromium and can use ES5 comfortably; it is deliberately kept promise-based and
dependency-free so there is no build step.

## Before you open a PR

* Re-export a comp you already have a known-good SVG for and diff them. That is
  the only regression suite that catches the interesting failures.
* Read the warnings the export prints. Most real breakage announces itself there
  first.
* If you touch timing, read the easing and hold sections in `README.md` and the
  header comment in `py/kf.py` before deciding the existing behaviour is wrong.
  Both rules are counter-intuitive and both have silently corrupted output
  before.
* Clip safety is structural, not a convention: content inside a track matte is
  animated by geometry (`cx`/`cy`/`r`/`d`) and never by a transform. A promoted
  compositor layer escapes its ancestor `clip-path` in real GPU browsers and is
  invisible in headless rendering, so this cannot be tested after the fact. Do
  not add a code path that can put a transform on clipped content.
