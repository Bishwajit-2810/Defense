#!/usr/bin/env python3
"""optimise_figures.py — size the figure PNGs for print, in place.

``capture_screens.mjs`` shoots at two device pixels per CSS pixel, which is the
right thing for a crisp capture and the wrong thing for a PDF: at the 158 mm
frame width this report uses, a 3,000-pixel screenshot is 480 dpi, and forty of
them turn an 8 MB document into an 18 MB one for resolution no printer resolves.

This rescales anything wider than ``--max-width`` down to it with Lanczos, which
leaves every screenshot at roughly 300 dpi in the frame, and then quantises to a
256-colour adaptive palette — a user interface and a Mermaid diagram are flat
colour, so the palette is visually lossless and roughly a third of the bytes.
It is idempotent: rerunning it on its own output is a no-op in size terms, so it
can be run after every capture.

    python3 optimise_figures.py                # figures/, max width 1900
    python3 optimise_figures.py --max-width 2400 --dry-run
"""

from __future__ import annotations

import argparse
import glob
import os

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=os.path.join(HERE, "figures"))
    ap.add_argument("--max-width", type=int, default=1900)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    before = after = 0
    for path in sorted(glob.glob(os.path.join(a.dir, "*.png"))):
        size = os.path.getsize(path)
        before += size
        with Image.open(path) as im:
            w, h = im.size
            wide = w > a.max_width
            already_paletted = im.mode == "P"
            if not wide and already_paletted:
                after += size
                continue
            new = (a.max_width, round(h * a.max_width / w)) if wide else (w, h)
            print(f"  {os.path.basename(path):40s} {w}x{h} -> {new[0]}x{new[1]}")
            if a.dry_run:
                after += size
                continue
            out = im.convert("RGB")
            if wide:
                out = out.resize(new, Image.LANCZOS)
            out.quantize(colors=256, method=Image.MEDIANCUT).save(path, optimize=True)
        after += os.path.getsize(path)

    print(f"figures: {before // 1024} KB -> {after // 1024} KB")


if __name__ == "__main__":
    main()
