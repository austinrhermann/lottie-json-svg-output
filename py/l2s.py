#!/usr/bin/env python3
"""
l2s — Lottie JSON or an After Effects bake -> animated SVG (tokenized) and/or
Lottie JSON, on the command line.

    l2s icon.json                          # writes icon.svg beside the input
    l2s icon.json -o out/icon.svg
    l2s *.json -d out/                     # batch
    l2s bake.json --format both -d out/    # SVG + Lottie from one AE bake
    l2s icon.json --trigger hover
    l2s icon.json --loop-start 36 --loop-end 246
    l2s icon.json --no-tokens              # keep literal hex fills
    l2s --emit-css                         # print the stylesheet bindings
    l2s icon.json --json                   # machine-readable result (panel uses this)

Exit code is 0 on success even when warnings are present; warnings go to stderr
(or into the JSON payload with --json). Always read them.
"""

import argparse
import glob
import gzip
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from l2s_core import (Options, convert_file, load_tokens, css_bindings,
                      DEFAULT_TOKENS)
from l2s_baked import convert_baked
from l2s_lottie import convert_baked_to_lottie


def is_baked(path):
    """Peek at the head of the file rather than trusting the extension."""
    try:
        with open(path) as f:
            return "l2s-baked/1" in f.read(200)
    except OSError:
        return False


def build_parser():
    p = argparse.ArgumentParser(prog="l2s", add_help=True,
                                description="Lottie/Bodymovin JSON -> animated SVG")
    p.add_argument("inputs", nargs="*", help="Lottie .json file(s)")
    p.add_argument("-o", "--output", help="output .svg path (single input only)")
    p.add_argument("-d", "--out-dir", help="output directory (batch)")
    p.add_argument("-f", "--format", choices=["svg", "lottie", "both"],
                   default="svg",
                   help="svg (default), lottie JSON, or both. 'lottie' and "
                        "'both' need an AE bake as input, not a Lottie JSON.")
    p.add_argument("--trigger", choices=["loop", "hover", "none"], default="loop",
                   help="loop forever, play once on hover/tap, or hold for JS")
    p.add_argument("--loop-start", type=float, help="loop in-point in frames")
    p.add_argument("--loop-end", type=float, help="loop out-point in frames")
    p.add_argument("--precision", type=int, default=2,
                   help="decimal places for coordinates (default 2)")
    p.add_argument("--sample-step", type=int, default=1,
                   help="frames between samples; 1 = every frame (default, exact)")
    p.add_argument("--prefix", help="id namespace prefix (default: input filename)")
    p.add_argument("--simplify", type=float, default=0.0, metavar="PX",
                   help="drop animation samples a linear segment already covers "
                        "to within PX pixels (try 0.1; 0 = keep every frame)")
    p.add_argument("--geometry-only", action="store_true",
                   help="force path-data animation everywhere, never transforms")
    p.add_argument("--no-tokens", action="store_true",
                   help="keep literal hex paint instead of token classes")
    p.add_argument("--tokens", help="path to tokens.json overriding the defaults")
    p.add_argument("--emit-css", action="store_true",
                   help="print the CSS token bindings and exit")
    p.add_argument("--emit-tokens", action="store_true",
                   help="print the default tokens.json and exit")
    p.add_argument("--css", help="also write the CSS token bindings to this path")
    p.add_argument("--json", action="store_true", dest="as_json",
                   help="emit a JSON result object on stdout")
    p.add_argument("--quiet", action="store_true")
    return p


def out_path_for(inp, args, ext=".svg"):
    if args.output and len(args.inputs) == 1:
        root, had = os.path.splitext(args.output)
        return args.output if had.lower() == ext else root + ext
    base = os.path.splitext(os.path.basename(inp))[0] + ext
    return os.path.join(args.out_dir or os.path.dirname(os.path.abspath(inp)), base)


def main(argv=None):
    args = build_parser().parse_args(argv)

    if args.emit_tokens:
        print(json.dumps(DEFAULT_TOKENS, indent=2))
        return 0

    tokens = None if args.no_tokens else load_tokens(args.tokens)

    if args.emit_css:
        print(css_bindings(tokens or load_tokens(args.tokens)))
        return 0

    files = []
    for pat in args.inputs:
        hits = glob.glob(pat)
        files.extend(hits if hits else [pat])
    if not files:
        build_parser().print_usage(sys.stderr)
        sys.stderr.write("l2s: no input files\n")
        return 2

    results = []
    failed = 0

    def make_opts():
        return Options(
            loop_start=args.loop_start,
            loop_end=args.loop_end,
            trigger=args.trigger,
            precision=args.precision,
            sample_step=args.sample_step,
            prefix=args.prefix,
            tokens=tokens,
            geometry_only=args.geometry_only,
            simplify=args.simplify,
        )

    for inp in files:
        baked = is_baked(inp)
        wants = ["svg", "lottie"] if args.format == "both" else [args.format]
        if "lottie" in wants and not baked:
            wants.remove("lottie")
            results.append({"input": inp, "format": "lottie", "ok": False,
                            "error": "Lottie output needs an After Effects bake as "
                                     "input; this file is already Lottie JSON."})
            failed += 1
        for fmt in wants:
            entry = {"input": inp, "format": fmt}
            try:
                opts = make_opts()
                if fmt == "lottie":
                    text, warnings, stats = convert_baked_to_lottie(inp, opts)
                    ext = ".json"
                elif baked:
                    text, warnings, stats = convert_baked(inp, opts)
                    ext = ".svg"
                else:
                    text, warnings, stats = convert_file(inp, opts)
                    ext = ".svg"
                dest = out_path_for(inp, args, ext)
                if os.path.abspath(dest) == os.path.abspath(inp):
                    # a bake and its Lottie output share the .json extension
                    root, e = os.path.splitext(dest)
                    dest = root + "-lottie" + e
                os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
                with open(dest, "w") as f:
                    f.write(text)
                entry.update(ok=True, output=dest, bytes=len(text),
                             gzip_bytes=len(gzip.compress(text.encode("utf-8"), 9)),
                             warnings=warnings, stats=stats)
            except Exception as e:
                failed += 1
                entry.update(ok=False, error=f"{type(e).__name__}: {e}")
            results.append(entry)

    if args.css and tokens:
        with open(args.css, "w") as f:
            f.write(css_bindings(tokens) + "\n")

    if args.as_json:
        print(json.dumps({"results": results}, indent=2))
        return 1 if failed else 0

    if not args.quiet:
        for r in results:
            if not r.get("ok"):
                sys.stderr.write(f"FAILED  {r['input']}: {r['error']}\n")
                continue
            sys.stderr.write(
                f"wrote   {r['output']}  ({r['bytes']:,} bytes, "
                f"{r['gzip_bytes']:,} gzipped, "
                f"{r['stats']['tracks']} tracks, {r['stats']['samples']} samples)\n")
            if r["stats"].get("samples_dropped"):
                sys.stderr.write(
                    f"        dropped {r['stats']['samples_dropped']} samples, "
                    f"worst error {r['stats'].get('simplify_err_px', 0)} px\n")
            for w in r.get("warnings", []):
                sys.stderr.write(f"  warn  {w}\n")
        if any(r.get("warnings") for r in results if r.get("ok")):
            sys.stderr.write("Review warnings against the AE render before shipping.\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
