#!/usr/bin/env python3
"""render_terminal.py — turn real terminal output into a figure PNG.

Several features of this project have no user interface: the orchestrator that
brings the stack up, the test suite, the metrics endpoint, the canonical result
envelope.  Retyping their output into a code block would make it prose; this
renders the captured bytes instead, ANSI colours and all, inside a terminal
frame, so the figure is evidence rather than illustration.

    python3 render_terminal.py in.txt fig-shot-startup.png "title"
    some-command | python3 render_terminal.py - fig-shot-x.png "title"

Only Pillow is required.  Lines are wrapped at ``--cols`` (default 118) and the
image is sized to the text, so a caller decides how much output to feed it.
"""

from __future__ import annotations

import argparse
import os
import re
import sys

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))

# One dark palette, close to the terminal these logs were captured in.
BG = (14, 17, 23)
CHROME = (26, 30, 38)
FG = (206, 212, 222)
DIM = (120, 130, 145)
DOTS = ((255, 95, 86), (255, 189, 46), (39, 201, 63))

# SGR colour number -> RGB. 30-37 normal, 90-97 bright; 39 resets.
ANSI = {
    30: (70, 78, 92), 31: (240, 113, 120), 32: (86, 211, 100), 33: (229, 192, 123),
    34: (97, 175, 239), 35: (198, 120, 221), 36: (86, 205, 219), 37: FG,
    90: DIM, 91: (255, 138, 145), 92: (126, 231, 135), 93: (250, 214, 140),
    94: (130, 196, 255), 95: (218, 152, 235), 96: (120, 224, 235), 97: (255, 255, 255),
}

SGR = re.compile(r"\x1b\[([0-9;]*)m")
# Every escape *except* SGR (the trailing "m"), which parse() handles itself —
# stripping those here would silently throw away every colour in the capture.
OTHER_ESC = re.compile(r"\x1b\[[0-9;?]*[A-Za-ln-z]|\r")

MONO = [
    "/home/bk/.fonts/JetBrainsMonoNerdFontPropo-Regular.ttf",
    "/usr/share/fonts/TTF/JetBrainsMono-Regular.ttf",
    "/usr/share/fonts/liberation/LiberationMono-Regular.ttf",
    "/usr/share/fonts/TTF/DejaVuSansMono.ttf",
    "/usr/share/fonts/noto/NotoSansMono-Regular.ttf",
]
MONO_BOLD = [p.replace("-Regular", "-Bold") for p in MONO]


def _font(paths: list[str], size: int) -> ImageFont.FreeTypeFont:
    for p in paths:
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def parse(text: str) -> list[list[tuple[str, tuple[int, int, int], bool]]]:
    """Split ANSI-coloured text into lines of (run, colour, bold) spans."""
    lines = []
    colour, bold = FG, False
    for raw in text.splitlines():
        raw = OTHER_ESC.sub("", raw)
        spans: list[tuple[str, tuple[int, int, int], bool]] = []
        pos = 0
        for m in SGR.finditer(raw):
            if m.start() > pos:
                spans.append((raw[pos : m.start()], colour, bold))
            for code in (m.group(1) or "0").split(";"):
                n = int(code or 0)
                if n == 0:
                    colour, bold = FG, False
                elif n == 1:
                    bold = True
                elif n == 22:
                    bold = False
                elif n in ANSI:
                    colour = ANSI[n]
                elif n == 39:
                    colour = FG
            pos = m.end()
        if pos < len(raw):
            spans.append((raw[pos:], colour, bold))
        lines.append(spans)
    return lines


def wrap(lines, cols):
    out = []
    for spans in lines:
        row, used = [], 0
        for text, colour, bold in spans:
            while text:
                room = cols - used
                if room <= 0:
                    out.append(row)
                    row, used, room = [], 0, cols
                chunk, text = text[:room], text[room:]
                row.append((chunk, colour, bold))
                used += len(chunk)
        out.append(row)
    return out


def render(text: str, out_path: str, title: str, cols: int = 118, size: int = 15) -> None:
    font = _font(MONO, size)
    bold_font = _font(MONO_BOLD, size)
    cw = int(round(font.getlength("M")))
    lh = int(size * 1.55)
    pad, bar = 22, 40

    rows = wrap(parse(text), cols)
    w = pad * 2 + cw * cols
    h = bar + pad * 2 + lh * len(rows)

    img = Image.new("RGB", (w, h), BG)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, w, bar], fill=CHROME)
    for i, c in enumerate(DOTS):
        d.ellipse([18 + i * 20, bar // 2 - 6, 30 + i * 20, bar // 2 + 6], fill=c)
    d.text((w // 2, bar // 2), title, font=font, fill=DIM, anchor="mm")

    y = bar + pad
    for row in rows:
        x = pad
        for chunk, colour, is_bold in row:
            d.text((x, y), chunk, font=bold_font if is_bold else font, fill=colour)
            x += cw * len(chunk)
        y += lh

    img.save(out_path)
    print(f"  saved {out_path}  ({w}x{h}, {len(rows)} lines)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("source", help="text file, or - for stdin")
    ap.add_argument("out", help="output PNG path")
    ap.add_argument("title", nargs="?", default="bash", help="window title")
    ap.add_argument("--cols", type=int, default=118)
    ap.add_argument("--size", type=int, default=15)
    a = ap.parse_args()
    text = sys.stdin.read() if a.source == "-" else open(a.source, encoding="utf-8", errors="replace").read()
    render(text, a.out, a.title, a.cols, a.size)


if __name__ == "__main__":
    main()
