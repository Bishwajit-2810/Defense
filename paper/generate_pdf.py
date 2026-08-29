#!/usr/bin/env python3
"""
generate_pdf.py — render ``final_paper.txt`` into ``final_paper.pdf``.

The source paper is a plain-text file marked up with line-oriented directives
(``@P|``, ``@H2|``, ``@TABLE|`` ...).  This script parses that file and lays it
out as an A4 Final Year Design Project report following the Daffodil
International University FYDP template: unnumbered title page, roman-numbered
front matter (Approval, Declaration, Acknowledgements, Abstract, Table of
Contents, List of Figures, List of Tables), then arabic-numbered chapters and an
IEEE reference list.

Usage
-----
    python3 generate_pdf.py                     # final_paper.txt -> final_paper.pdf
    python3 generate_pdf.py in.txt out.pdf
    python3 generate_pdf.py --render-charts     # export every @CHART to figures/

Only ``reportlab`` is required to build the PDF.  Charts declared with
``@CHART`` are drawn from their inline data with ``reportlab.graphics``.  A
figure declared with ``@FIGURE|fig:name|...`` embeds ``figures/fig-name.png``
when that file exists, and otherwise renders an empty placeholder frame carrying
the caption and description, so a photograph or screenshot can be dropped in
later.

The diagram PNGs are produced from the Mermaid sources in ``diagrams/`` by
``python3 generate_pdf.py --render-diagrams``, which needs Node and a Chrome
binary.  That step is separate from the build so that the PDF can be produced on
a machine with neither.

``--render-charts`` exports every ``@CHART`` block to ``figures/fig-<name>.png``
so that every figure in the report exists as an image on disk beside the
diagrams and screenshots.  The PDF build does not read those files - it draws
the charts as vector graphics - so the export is for reuse in slides and posters
rather than for the build.  It needs ``pdftoppm`` from poppler.

Markup reference
----------------
Front matter
    ``@TITLE|text``            paper title (repeat for extra title lines)
    ``@FIELD|key|value``       title-page metadata (see ``TITLE_FIELDS``)
    ``@AUTHOR|name|id``        one per student
    ``@FRONT|NAME|Heading``    start an unnumbered front-matter section
    ``@SIGN|name|line|line``   signature block (Approval / Declaration)
Body
    ``@CHAPTER|n|Title``       start chapter *n*
    ``@INTRO|text``            the chapter's one-paragraph outline
    ``@H2|1.1 Title``          section
    ``@H3|1.1.1 Title``        subsection
    ``@H4|Title``              run-in heading, not in the table of contents
    ``@P|text``                body paragraph
    ``@BULLET|text``           bulleted item
    ``@NUM|text``              numbered item (numbering is literal, in the text)
    ``@NOTE|text``             boxed remark
    ``@CODE|text``             monospaced line
    ``@PAGEBREAK``             force a page break
Blocks (numbered automatically per chapter; the second field is a *label*)
    ``@TABLE|tab:name|Caption`` .. ``@ENDTABLE`` with ``@TH|a|b`` and ``@TR|a|b``
    ``@FIGURE|fig:name|Caption`` .. ``@ENDFIGURE`` with ``@FIGDESC|text``
    ``@CHART|kind|fig:name|Caption`` .. ``@ENDCHART`` with ``@LABELS|``,
        ``@SERIES|``, ``@YLABEL|``, ``@FIGDESC|``;  *kind* is bar | hbar | pie | line

Cross-references
    Write ``{{fig:name}}`` or ``{{tab:name}}`` anywhere in the text and it is
    replaced by the resolved number ("3.4").  Numbers are assigned in document
    order, restarting at each chapter, so inserting a figure never requires
    renumbering anything and an unknown label is an error rather than a wrong
    number.
References
    ``@REFS``                  start the reference list
    ``@REF|IEEE entry``        one numbered reference

Inline markup inside any text: ``**bold**``, ``*italic*``, ``` `mono` ```.
"""

from __future__ import annotations

import html
import os
import re
import sys

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.utils import ImageReader
from reportlab.platypus import (
    BaseDocTemplate,
    Flowable,
    Image,
    Frame,
    KeepTogether,
    NextPageTemplate,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)
from reportlab.platypus.tableofcontents import TableOfContents

HERE = os.path.dirname(os.path.abspath(__file__))
DIAGRAM_SRC = os.path.join(HERE, "diagrams")     # mermaid sources
FIGURE_DIR = os.path.join(HERE, "figures")       # rendered PNGs
CHART_DPI = 200                                  # raster resolution for --render-charts
MAX_FIG_H = 212 * 2.834645669  # 212 mm: the frame is 250 mm tall, so a tall
                               # figure plus its caption still fits on one page.
                               # Raising this is the only lever on legibility for
                               # height-bound diagrams: reportlab scales the PNG
                               # to fit, so a shorter box shrinks the type with it.

# ---------------------------------------------------------------------------
# Page geometry
# ---------------------------------------------------------------------------

PAGE_W, PAGE_H = A4
MARGIN_L = 30 * mm
MARGIN_R = 22 * mm
MARGIN_T = 25 * mm
MARGIN_B = 22 * mm
FRAME_W = PAGE_W - MARGIN_L - MARGIN_R

# ---------------------------------------------------------------------------
# Palette
# ---------------------------------------------------------------------------

INK = colors.HexColor("#111318")
MUTED = colors.HexColor("#5a6472")
RULE = colors.HexColor("#c3ccd8")
FAINT = colors.HexColor("#e8edf3")
ACCENT = colors.HexColor("#1f4e79")
NOTE_BG = colors.HexColor("#f4f7fb")

SERIES_COLORS = [
    colors.HexColor("#1f4e79"),
    colors.HexColor("#2c9d8f"),
    colors.HexColor("#c9772e"),
    colors.HexColor("#8b6fd4"),
    colors.HexColor("#b03a48"),
    colors.HexColor("#6b7a8f"),
    colors.HexColor("#4d8f3a"),
]


# ---------------------------------------------------------------------------
# Fonts
# ---------------------------------------------------------------------------

FONT_CANDIDATES = {
    "Body": [
        ("/usr/share/fonts/liberation/LiberationSerif-Regular.ttf",
         "/usr/share/fonts/liberation/LiberationSerif-Bold.ttf",
         "/usr/share/fonts/liberation/LiberationSerif-Italic.ttf",
         "/usr/share/fonts/liberation/LiberationSerif-BoldItalic.ttf"),
        ("/usr/share/fonts/TTF/DejaVuSerif.ttf",
         "/usr/share/fonts/TTF/DejaVuSerif-Bold.ttf",
         "/usr/share/fonts/TTF/DejaVuSerif-Italic.ttf",
         "/usr/share/fonts/TTF/DejaVuSerif-BoldItalic.ttf"),
    ],
    "Head": [
        ("/usr/share/fonts/liberation/LiberationSans-Regular.ttf",
         "/usr/share/fonts/liberation/LiberationSans-Bold.ttf",
         "/usr/share/fonts/liberation/LiberationSans-Italic.ttf",
         "/usr/share/fonts/liberation/LiberationSans-BoldItalic.ttf"),
        ("/usr/share/fonts/TTF/DejaVuSans.ttf",
         "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
         "/usr/share/fonts/TTF/DejaVuSans-Oblique.ttf",
         "/usr/share/fonts/TTF/DejaVuSans-BoldOblique.ttf"),
    ],
    "Mono": [
        ("/usr/share/fonts/liberation/LiberationMono-Regular.ttf",
         "/usr/share/fonts/liberation/LiberationMono-Bold.ttf",
         "/usr/share/fonts/liberation/LiberationMono-Italic.ttf",
         "/usr/share/fonts/liberation/LiberationMono-BoldItalic.ttf"),
        ("/usr/share/fonts/TTF/DejaVuSansMono.ttf",
         "/usr/share/fonts/TTF/DejaVuSansMono-Bold.ttf",
         "/usr/share/fonts/TTF/DejaVuSansMono-Oblique.ttf",
         "/usr/share/fonts/TTF/DejaVuSansMono-BoldOblique.ttf"),
    ],
}

BUILTIN = {
    "Body": ("Times-Roman", "Times-Bold", "Times-Italic", "Times-BoldItalic"),
    "Head": ("Helvetica", "Helvetica-Bold", "Helvetica-Oblique", "Helvetica-BoldOblique"),
    "Mono": ("Courier", "Courier-Bold", "Courier-Oblique", "Courier-BoldOblique"),
}


def register_fonts() -> dict:
    """Register TTF families where available, else fall back to the base-14 set."""
    resolved = {}
    for family, options in FONT_CANDIDATES.items():
        chosen = None
        for quad in options:
            if all(os.path.exists(p) for p in quad):
                chosen = quad
                break
        if chosen is None:
            resolved[family] = BUILTIN[family]
            continue
        names = (family, family + "-Bold", family + "-Italic", family + "-BoldItalic")
        try:
            for name, path in zip(names, chosen):
                pdfmetrics.registerFont(TTFont(name, path))
            pdfmetrics.registerFontFamily(
                family, normal=names[0], bold=names[1], italic=names[2], boldItalic=names[3]
            )
            resolved[family] = names
        except Exception:  # pragma: no cover - defensive: bad/locked font file
            resolved[family] = BUILTIN[family]
    return resolved


FONTS = register_fonts()
F_BODY, F_BODY_B, F_BODY_I, _ = FONTS["Body"]
F_HEAD, F_HEAD_B, F_HEAD_I, _ = FONTS["Head"]
F_MONO, F_MONO_B, _, _ = FONTS["Mono"]


# ---------------------------------------------------------------------------
# Styles
# ---------------------------------------------------------------------------

def build_styles() -> dict:
    ss = getSampleStyleSheet()
    s = {}

    # allowWidows / allowOrphans = 0: never strand a single line of a paragraph
    # alone at the top or bottom of a page.
    s["Body"] = ParagraphStyle(
        "Body", parent=ss["Normal"], fontName=F_BODY, fontSize=11.5, leading=16.4,
        alignment=TA_JUSTIFY, spaceBefore=0, spaceAfter=7, textColor=INK,
        allowWidows=0, allowOrphans=0,
    )
    s["BodyFirst"] = ParagraphStyle("BodyFirst", parent=s["Body"], spaceBefore=2)
    s["Intro"] = ParagraphStyle(
        "Intro", parent=s["Body"], fontName=F_BODY_I, textColor=MUTED,
        spaceAfter=11, leftIndent=0,
    )
    s["Bullet"] = ParagraphStyle(
        "Bullet", parent=s["Body"], leftIndent=15, bulletIndent=3,
        spaceAfter=4, alignment=TA_JUSTIFY,
    )
    s["Num"] = ParagraphStyle("Num", parent=s["Bullet"], leftIndent=20, bulletIndent=3)
    s["Note"] = ParagraphStyle(
        "Note", parent=s["Body"], fontSize=10.6, leading=15, leftIndent=9,
        rightIndent=9, spaceBefore=5, spaceAfter=5, textColor=colors.HexColor("#22303f"),
    )
    s["Code"] = ParagraphStyle(
        "Code", parent=s["Body"], fontName=F_MONO, fontSize=9.2, leading=12.6,
        alignment=TA_LEFT, leftIndent=12, spaceBefore=1, spaceAfter=1,
        textColor=colors.HexColor("#22303f"),
    )

    s["ChapterNum"] = ParagraphStyle(
        "ChapterNum", parent=ss["Normal"], fontName=F_HEAD, fontSize=13, leading=17,
        alignment=TA_CENTER, textColor=MUTED, spaceAfter=4,
    )
    s["ChapterTitle"] = ParagraphStyle(
        "ChapterTitle", parent=ss["Normal"], fontName=F_HEAD_B, fontSize=22, leading=27,
        alignment=TA_CENTER, textColor=ACCENT, spaceAfter=16,
    )
    s["H2"] = ParagraphStyle(
        "H2", parent=ss["Normal"], fontName=F_HEAD_B, fontSize=13.6, leading=18,
        spaceBefore=15, spaceAfter=6, textColor=ACCENT, keepWithNext=1,
    )
    s["H3"] = ParagraphStyle(
        "H3", parent=ss["Normal"], fontName=F_HEAD_B, fontSize=11.8, leading=15.5,
        spaceBefore=11, spaceAfter=4, textColor=colors.HexColor("#25415c"), keepWithNext=1,
    )
    s["H4"] = ParagraphStyle(
        "H4", parent=ss["Normal"], fontName=F_BODY_B, fontSize=11.5, leading=15,
        spaceBefore=9, spaceAfter=3, textColor=INK, keepWithNext=1,
    )
    s["FrontHead"] = ParagraphStyle(
        "FrontHead", parent=ss["Normal"], fontName=F_HEAD_B, fontSize=17, leading=22,
        alignment=TA_CENTER, textColor=ACCENT, spaceAfter=15,
    )

    s["Caption"] = ParagraphStyle(
        "Caption", parent=ss["Normal"], fontName=F_HEAD, fontSize=9.6, leading=13,
        alignment=TA_CENTER, textColor=MUTED, spaceBefore=5, spaceAfter=2,
    )
    s["TableCaption"] = ParagraphStyle(
        "TableCaption", parent=s["Caption"], spaceBefore=8, spaceAfter=5, keepWithNext=1,
    )
    # A tall table cannot fit beside its caption on any page, and keepWithNext
    # then defers both to a fresh frame - wasting most of a page before the
    # table splits normally.  Long tables use this unglued variant instead.
    s["TableCaptionLong"] = ParagraphStyle(
        "TableCaptionLong", parent=s["Caption"], spaceBefore=8, spaceAfter=5,
    )
    s["FigDesc"] = ParagraphStyle(
        "FigDesc", parent=ss["Normal"], fontName=F_BODY_I, fontSize=9.6, leading=13.4,
        alignment=TA_JUSTIFY, textColor=MUTED, spaceBefore=1, spaceAfter=11,
        leftIndent=14, rightIndent=14,
    )
    s["Cell"] = ParagraphStyle(
        "Cell", parent=ss["Normal"], fontName=F_BODY, fontSize=8.9, leading=11.6,
        alignment=TA_LEFT, textColor=INK,
    )
    s["CellHead"] = ParagraphStyle(
        "CellHead", parent=s["Cell"], fontName=F_HEAD_B, fontSize=8.9, leading=11.6,
        textColor=colors.white,
    )

    s["TitleMain"] = ParagraphStyle(
        "TitleMain", parent=ss["Normal"], fontName=F_HEAD_B, fontSize=16, leading=21.5,
        alignment=TA_CENTER, textColor=ACCENT, spaceAfter=6,
    )
    s["TitleLine"] = ParagraphStyle(
        "TitleLine", parent=ss["Normal"], fontName=F_BODY, fontSize=12, leading=17,
        alignment=TA_CENTER, textColor=INK, spaceAfter=2,
    )
    s["TitleSmall"] = ParagraphStyle(
        "TitleSmall", parent=s["TitleLine"], fontSize=10.6, leading=15, textColor=MUTED,
    )
    s["TitleBold"] = ParagraphStyle(
        "TitleBold", parent=s["TitleLine"], fontName=F_BODY_B, fontSize=12.5,
    )

    s["Ref"] = ParagraphStyle(
        "Ref", parent=s["Body"], fontSize=10.2, leading=13.6, alignment=TA_LEFT,
        leftIndent=22, firstLineIndent=-22, spaceAfter=6,
    )

    s["TOC0"] = ParagraphStyle(
        "TOC0", parent=ss["Normal"], fontName=F_HEAD_B, fontSize=11.4, leading=17,
        spaceBefore=8, textColor=INK,
    )
    s["TOC1"] = ParagraphStyle(
        "TOC1", parent=ss["Normal"], fontName=F_BODY, fontSize=10.6, leading=14.6,
        leftIndent=16, textColor=INK,
    )
    s["TOC2"] = ParagraphStyle(
        "TOC2", parent=ss["Normal"], fontName=F_BODY, fontSize=10.2, leading=14,
        leftIndent=34, textColor=MUTED,
    )
    s["TOCFlat"] = ParagraphStyle(
        "TOCFlat", parent=ss["Normal"], fontName=F_BODY, fontSize=10.4, leading=15,
        leftIndent=14, firstLineIndent=-14, textColor=INK,
    )
    return s


ST = build_styles()


# ---------------------------------------------------------------------------
# Inline markup
# ---------------------------------------------------------------------------

_RE_RANGE = re.compile(r"(?<=\d)\s-\s(?=\d)")     # 150 - 700  ->  150-700 (en dash)
_RE_DASH = re.compile(r"(?<=\S)\s-\s(?=\S)")       # parenthetical dash -> spaced en dash
_RE_MONO = re.compile(r"`([^`]+)`")
_RE_BOLD = re.compile(r"\*\*(.+?)\*\*")
_RE_ITAL = re.compile(r"(?<![\*\w])\*(?!\s)(.+?)(?<!\s)\*(?!\*)")


def rich(text: str) -> str:
    """Escape a source string and apply the inline ``**``/``*``/`` ` `` markup.

    The source is written in ASCII, so the two dash conventions a formal report
    needs are applied here: an unspaced en dash closes a numeric range, and a
    spaced en dash sets off a parenthetical clause.
    """
    out = html.escape(text, quote=False)
    out = _RE_RANGE.sub("\u2013", out)
    out = _RE_DASH.sub(" \u2013 ", out)
    out = _RE_MONO.sub(lambda m: '<font face="%s" size="9.6">%s</font>' % (F_MONO, m.group(1)), out)
    out = _RE_BOLD.sub(lambda m: "<b>%s</b>" % m.group(1), out)
    out = _RE_ITAL.sub(lambda m: "<i>%s</i>" % m.group(1), out)
    return out


# ---------------------------------------------------------------------------
# Custom flowables
# ---------------------------------------------------------------------------

class HRule(Flowable):
    """A thin horizontal rule used to separate front-matter blocks."""

    def __init__(self, width=None, thickness=0.6, color=RULE, space=4, centred=True):
        super().__init__()
        self.width = width
        self.thickness = thickness
        self.color = color
        self.space = space
        self.centred = centred
        self.height = thickness + space

    def wrap(self, aw, ah):
        self._w = min(self.width, aw) if self.width else aw
        self._x = (aw - self._w) / 2.0 if self.centred else 0.0
        return (aw, self.height)

    def draw(self):
        y = self.space / 2.0
        self.canv.setStrokeColor(self.color)
        self.canv.setLineWidth(self.thickness)
        self.canv.line(self._x, y, self._x + self._w, y)


class FigurePlaceholder(Flowable):
    """An empty, clearly-marked frame where an image will be pasted in."""

    def __init__(self, height=68 * mm, label="Figure placeholder"):
        super().__init__()
        self.height = height
        self.label = label

    def wrap(self, aw, ah):
        self._w = aw
        return (self._w, self.height)

    def draw(self):
        c = self.canv
        w, h = self._w, self.height
        c.saveState()
        c.setFillColor(colors.HexColor("#fbfcfe"))
        c.setStrokeColor(RULE)
        c.setLineWidth(0.9)
        c.setDash(4, 3)
        c.rect(0, 0, w, h, stroke=1, fill=1)
        c.setDash()
        # corner ticks, so the frame reads as a reserved area rather than a box
        c.setStrokeColor(colors.HexColor("#aab6c6"))
        c.setLineWidth(1.4)
        t = 9
        for (x, y, dx, dy) in ((0, 0, 1, 1), (w, 0, -1, 1), (0, h, 1, -1), (w, h, -1, -1)):
            c.line(x, y, x + dx * t, y)
            c.line(x, y, x, y + dy * t)
        c.setFillColor(colors.HexColor("#8593a6"))
        c.setFont(F_HEAD, 9.6)
        c.drawCentredString(w / 2.0, h / 2.0 + 4, self.label)
        c.setFont(F_HEAD, 8.2)
        c.drawCentredString(w / 2.0, h / 2.0 - 9, "[ image to be inserted ]")
        c.restoreState()


class NoteBox(Flowable):
    """A tinted, left-ruled remark box wrapping one paragraph."""

    def __init__(self, para: Paragraph, pad=7):
        super().__init__()
        self.para = para
        self.pad = pad

    def wrap(self, aw, ah):
        self._w = aw
        pw, ph = self.para.wrap(aw - 2 * self.pad - 4, ah)
        self._ph = ph
        self.height = ph + 2 * self.pad
        return (self._w, self.height)

    def draw(self):
        c = self.canv
        c.saveState()
        c.setFillColor(NOTE_BG)
        c.setStrokeColor(FAINT)
        c.setLineWidth(0.5)
        c.rect(0, 0, self._w, self.height, stroke=1, fill=1)
        c.setFillColor(ACCENT)
        c.rect(0, 0, 2.6, self.height, stroke=0, fill=1)
        c.restoreState()
        self.para.drawOn(c, self.pad + 4, self.pad)


def figure_path(ref):
    """Map a figure label such as ``fig:router`` to ``figures/fig-router.png``."""
    return os.path.join(FIGURE_DIR, ref.replace(":", "-") + ".png")


def image_flowable(ref):
    """Return a scaled Image for *ref*, or None when no rendering exists.

    The diagrams are rasterised at three device pixels per SVG unit, which keeps
    the effective resolution above 300 dpi at the widths used here.  Tall
    diagrams are bounded by height rather than width so that the caption is
    never pushed off the page.
    """
    path = figure_path(ref)
    if not os.path.exists(path):
        return None
    try:
        iw, ih = ImageReader(path).getSize()
    except Exception:                                  # unreadable or truncated
        return None
    if not iw or not ih:
        return None
    scale = min(FRAME_W / float(iw), MAX_FIG_H / float(ih))
    img = Image(path, width=iw * scale, height=ih * scale)
    img.hAlign = "CENTER"
    return img


class TocEntry(Flowable):
    """Zero-height marker that reports a figure/table to its index on layout."""

    def __init__(self, kind, text, key):
        super().__init__()
        self.kind = kind
        self.text = text
        self.key = key
        self.width = 0
        self.height = 0

    def wrap(self, aw, ah):
        return (0, 0)

    def draw(self):
        pass


# ---------------------------------------------------------------------------
# Charts
# ---------------------------------------------------------------------------

def _nice_max(v: float) -> float:
    if v <= 0:
        return 1.0
    import math
    exp = math.floor(math.log10(v))
    base = 10 ** exp
    for m in (1, 1.25, 1.5, 2, 2.5, 3, 4, 5, 7.5, 10):
        if m * base >= v:
            return m * base
    return 10 * base


def make_chart(kind, labels, series, ylabel=None, width=None, height=None):
    """Build a Drawing for one ``@CHART`` block.

    Geometry is computed before the Drawing is created, because the room a
    category axis needs depends on whether its labels fit horizontally; if they
    do not they are angled, and an angled label needs vertical space that would
    otherwise be taken out of the caption below.
    """
    from reportlab.graphics.charts.barcharts import HorizontalBarChart, VerticalBarChart
    from reportlab.graphics.charts.legends import Legend
    from reportlab.graphics.charts.linecharts import HorizontalLineChart
    from reportlab.graphics.charts.piecharts import Pie
    from reportlab.graphics.shapes import Drawing, String

    width = width or FRAME_W
    height = height or (78 * mm)
    names = [s[0] for s in series]
    data = [s[1] for s in series]
    multi = len(series) > 1
    label_font_size = 8.1

    # ---- pie -------------------------------------------------------------
    if kind == "pie":
        d = Drawing(width, height)
        values = data[0]
        total = float(sum(values)) or 1.0
        pie = Pie()
        pie.x = 14
        pie.y = 12
        pie.width = height - 26
        pie.height = height - 26
        pie.data = values
        pie.labels = None
        pie.sideLabels = 0
        pie.slices.strokeColor = colors.white
        pie.slices.strokeWidth = 1.1
        for i in range(len(values)):
            pie.slices[i].fillColor = SERIES_COLORS[i % len(SERIES_COLORS)]
            pie.slices[i].popout = 0
        d.add(pie)
        leg = Legend()
        leg.x = pie.width + 40
        leg.y = height - 20
        leg.alignment = "right"
        leg.boxAnchor = "nw"
        leg.fontName = F_HEAD
        leg.fontSize = 8.6
        leg.dx = 7
        leg.dy = 7
        leg.deltay = 12.5
        leg.columnMaximum = 12
        leg.colorNamePairs = [
            (SERIES_COLORS[i % len(SERIES_COLORS)],
             "%s  %.1f%%" % (labels[i], 100.0 * values[i] / total))
            for i in range(len(values))
        ]
        d.add(leg)
        return d

    # ---- horizontal bars -------------------------------------------------
    if kind == "hbar":
        gutter = min(FRAME_W * 0.34,
                     max(pdfmetrics.stringWidth(l, F_HEAD, label_font_size)
                         for l in labels) + 12)
        legend_room = 22 if multi else 0
        d = Drawing(width, height)
        ch = HorizontalBarChart()
        ch.x = gutter
        ch.y = 12 + legend_room
        ch.width = width - gutter - 20
        ch.height = height - ch.y - (18 if ylabel else 8)
        ch.categoryAxis.labels.fontName = F_HEAD
        ch.categoryAxis.labels.fontSize = label_font_size
        ch.categoryAxis.labels.dx = -4
        ch.categoryAxis.labels.boxAnchor = "e"
        legend_y = 6
    else:
        # ---- vertical bars / lines --------------------------------------
        axis_gutter = 42
        avail_w = width - axis_gutter - 20
        slot = avail_w / max(1, len(labels))
        widest = max(pdfmetrics.stringWidth(l, F_HEAD, label_font_size) for l in labels)
        if widest <= slot * 0.92:
            angle, label_room = 0, 16
        else:
            angle, label_room = 30, min(74.0, widest * 0.52 + 14)
        legend_room = 22 if multi else 0
        bottom = 8 + legend_room + label_room
        top_room = 20 if ylabel else 12
        height = max(height, bottom + 110 + top_room)

        d = Drawing(width, height)
        ch = HorizontalLineChart() if kind == "line" else VerticalBarChart()
        ch.x = axis_gutter
        ch.y = bottom
        ch.width = avail_w
        ch.height = height - bottom - top_room
        ch.categoryAxis.labels.fontName = F_HEAD
        ch.categoryAxis.labels.fontSize = label_font_size
        ch.categoryAxis.labels.angle = angle
        ch.categoryAxis.labels.dy = -6
        ch.categoryAxis.labels.boxAnchor = "n" if angle == 0 else "ne"
        legend_y = 6

    ch.data = data
    ch.categoryAxis.categoryNames = labels
    ch.valueAxis.valueMin = 0
    ch.valueAxis.valueMax = _nice_max(max(max(r) for r in data))
    ch.valueAxis.labels.fontName = F_HEAD
    ch.valueAxis.labels.fontSize = 8
    ch.valueAxis.strokeColor = RULE
    ch.categoryAxis.strokeColor = RULE
    ch.valueAxis.gridStrokeColor = FAINT
    ch.valueAxis.visibleGrid = 1

    if kind == "line":
        for i in range(len(data)):
            ch.lines[i].strokeColor = SERIES_COLORS[i % len(SERIES_COLORS)]
        ch.lines.strokeWidth = 1.7
    else:
        ch.barSpacing = 1.5
        ch.groupSpacing = 10 if multi else 14
        ch.bars.strokeColor = None
        for i in range(len(data)):
            ch.bars[i].fillColor = SERIES_COLORS[i % len(SERIES_COLORS)]
        if not multi:
            ch.barLabels.fontName = F_HEAD
            ch.barLabels.fontSize = 7.4
            ch.barLabelFormat = "%s"
            ch.barLabels.nudge = 7

    d.add(ch)

    if ylabel:
        d.add(String(4, d.height - 11, ylabel,
                     fontName=F_HEAD, fontSize=8.2, fillColor=MUTED))

    if multi:
        leg = Legend()
        leg.x = 42
        leg.y = legend_y
        leg.boxAnchor = "sw"
        leg.alignment = "right"
        leg.fontName = F_HEAD
        leg.fontSize = 8.4
        leg.dx = 7
        leg.dy = 7
        leg.deltax = min(150, (width - 60) / max(1, len(names)))
        leg.columnMaximum = 1
        leg.colorNamePairs = [
            (SERIES_COLORS[i % len(SERIES_COLORS)], names[i]) for i in range(len(names))
        ]
        d.add(leg)
    return d


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------

def _table_metrics(n_cols):
    """Font size and cell padding, tightened for wide tables.

    A twenty-column timeline cannot carry the same padding as a four-column
    comparison: at 5 pt either side the padding alone would consume more than
    the frame, and every column would be squeezed below its own header.
    """
    if n_cols <= 8:
        return 8.9, 5.0
    if n_cols <= 14:
        return 8.4, 3.5
    return 8.0, 2.5


def _weights(header, rows, font_size, pad):
    """Column widths: a floor from the widest unbreakable word, the rest shared.

    A purely proportional split starves short headings such as "Year", which
    then wrap one character per line.  So each column first claims the width of
    its longest single word, and only the surplus is divided by demand.
    """
    n = len(header) if header else max(len(r) for r in rows)
    body = ([header] if header else []) + rows
    slack = 1.5          # guards against a word landing exactly on the boundary
    gutter = 2 * pad + slack

    floors, demand = [], []
    for i in range(n):
        widest_word = 0.0
        widest_cell = 0.0
        for r in body:
            cell = re.sub(r"[*`]", "", r[i] if i < len(r) else "")
            font = F_HEAD_B if (header and r is header) else F_BODY
            for word in cell.split() or [""]:
                widest_word = max(widest_word, pdfmetrics.stringWidth(word, font, font_size))
            widest_cell = max(widest_cell, pdfmetrics.stringWidth(cell, font, font_size))
        floors.append(min(widest_word + gutter, FRAME_W * 0.30))
        demand.append(min(widest_cell + gutter, FRAME_W * 0.55))

    total_floor = sum(floors)
    if total_floor >= FRAME_W:                       # pathological: scale to fit
        return [FRAME_W * f / total_floor for f in floors]

    surplus = FRAME_W - total_floor
    extra = [max(0.0, d - f) for d, f in zip(demand, floors)]
    pool = sum(extra)
    if pool <= 0:
        return [f + surplus / n for f in floors]
    return [f + surplus * e / pool for f, e in zip(floors, extra)]


def make_table(header, rows):
    n_cols = len(header) if header else max(len(r) for r in rows)
    font_size, pad = _table_metrics(n_cols)
    body_style = ParagraphStyle("cell%d" % n_cols, parent=ST["Cell"],
                                fontSize=font_size, leading=font_size * 1.3)
    head_style = ParagraphStyle("chead%d" % n_cols, parent=ST["CellHead"],
                                fontSize=font_size, leading=font_size * 1.3)

    cells = []
    if header:
        cells.append([Paragraph(rich(c), head_style) for c in header])
    for r in rows:
        cells.append([Paragraph(rich(c), body_style) for c in r])

    t = Table(cells, colWidths=_weights(header, rows, font_size, pad),
              repeatRows=1 if header else 0, hAlign="LEFT", splitByRow=1)
    style = [
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), pad),
        ("RIGHTPADDING", (0, 0), (-1, -1), pad),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4.5),
        ("LINEBELOW", (0, 0), (-1, -1), 0.35, FAINT),
        ("BOX", (0, 0), (-1, -1), 0.7, RULE),
    ]
    if header:
        style += [
            ("BACKGROUND", (0, 0), (-1, 0), ACCENT),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("LINEBELOW", (0, 0), (-1, 0), 0.7, ACCENT),
            ("TOPPADDING", (0, 0), (-1, 0), 5),
            ("BOTTOMPADDING", (0, 0), (-1, 0), 5.5),
        ]
        for i in range(2, len(cells), 2):
            style.append(("BACKGROUND", (0, i), (-1, i), colors.HexColor("#f7f9fc")))
    t.setStyle(TableStyle(style))
    return t


# ---------------------------------------------------------------------------
# Indexes (contents / figures / tables)
# ---------------------------------------------------------------------------

class Index(TableOfContents):
    """A TableOfContents that listens for its own notification kind."""

    entryKind = "TOCEntry"

    def notify(self, kind, stuff):
        if kind == self.entryKind:
            self.addEntry(*stuff)


def make_toc():
    toc = Index()
    toc.entryKind = "TOCEntry"
    toc.levelStyles = [ST["TOC0"], ST["TOC1"], ST["TOC2"]]
    toc.dotsMinLevel = 0
    return toc


def make_list_index(kind):
    idx = Index()
    idx.entryKind = kind
    idx.levelStyles = [ST["TOCFlat"]]
    idx.dotsMinLevel = 0
    return idx


# ---------------------------------------------------------------------------
# Document template
# ---------------------------------------------------------------------------

def _roman(n: int) -> str:
    vals = ((1000, "m"), (900, "cm"), (500, "d"), (400, "cd"), (100, "c"), (90, "xc"),
            (50, "l"), (40, "xl"), (10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i"))
    out = []
    for v, sym in vals:
        while n >= v:
            out.append(sym)
            n -= v
    return "".join(out)


class ReportDoc(BaseDocTemplate):
    """A4 report: unnumbered cover, roman front matter, arabic body."""

    def __init__(self, filename, **kw):
        super().__init__(filename, pagesize=A4,
                         leftMargin=MARGIN_L, rightMargin=MARGIN_R,
                         topMargin=MARGIN_T, bottomMargin=MARGIN_B, **kw)
        frame = Frame(MARGIN_L, MARGIN_B, FRAME_W,
                      PAGE_H - MARGIN_T - MARGIN_B, id="main",
                      leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
        self.addPageTemplates([
            PageTemplate(id="cover", frames=[frame]),
            PageTemplate(id="numbered", frames=[frame], onPageEnd=self._footer),
        ])
        # ``body_start`` is the physical page on which Chapter 1 opens; it is
        # discovered during layout.  ``body_start_prev`` carries the previous
        # pass's value, because the index flowables are laid out *before* the
        # chapters and so cannot see the current pass's answer.
        self.body_start = None
        self.body_start_prev = None

    def beforeDocument(self):
        # multiBuild re-runs the layout; carry the converged value forward and
        # start the new pass clean.
        if self.body_start is not None:
            self.body_start_prev = self.body_start
        self.body_start = None

    def _footer(self, canvas, doc):
        canvas.saveState()
        canvas.setStrokeColor(FAINT)
        canvas.setLineWidth(0.5)
        canvas.line(MARGIN_L, MARGIN_B - 9, PAGE_W - MARGIN_R, MARGIN_B - 9)
        if self.body_start is None:
            label = _roman(max(1, doc.page - 1))
        else:
            label = str(doc.page - self.body_start + 1)
        canvas.setFont(F_HEAD, 9)
        canvas.setFillColor(MUTED)
        canvas.drawCentredString(PAGE_W / 2.0, MARGIN_B - 20, label)
        canvas.restoreState()

    def label_for(self, page):
        """Map a physical page to its printed label (roman front, arabic body)."""
        start = self.body_start if self.body_start is not None else self.body_start_prev
        if start is None or page < start:
            return _roman(max(1, page - 1))
        return str(page - start + 1)

    def afterFlowable(self, flowable):
        if isinstance(flowable, TocEntry):
            self.canv.bookmarkPage(flowable.key)
            self.notify(flowable.kind, (0, flowable.text, self.page, flowable.key))
            return
        if not isinstance(flowable, Paragraph):
            return
        name = getattr(flowable.style, "name", "")
        text = flowable.getPlainText()
        if name == "ChapterTitle":
            if self.body_start is None and getattr(flowable, "_isBody", False):
                self.body_start = self.page
            label = getattr(flowable, "_tocLabel", text)
            key = getattr(flowable, "_tocKey", None)
            if key:
                self.canv.bookmarkPage(key)
                self.notify("TOCEntry", (0, label, self.page, key))
        elif name in ("H2", "H3"):
            key = getattr(flowable, "_tocKey", None)
            if key:
                self.canv.bookmarkPage(key)
                self.notify("TOCEntry", (1 if name == "H2" else 2, text,
                                         self.page, key))
        elif name == "FrontHead":
            key = getattr(flowable, "_tocKey", None)
            if key:
                self.canv.bookmarkPage(key)
                self.notify("TOCEntry", (0, text, self.page, key))


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

TITLE_FIELDS = ("degree", "supervised_by", "cosupervised_by", "dept", "university",
                "city", "date", "byline")

LABEL_RE = re.compile(r"^@(FIGURE|TABLE)\|([a-z]+:[a-z0-9_-]+)\|", re.I)
CHART_LABEL_RE = re.compile(r"^@CHART\|[a-z]+\|([a-z]+:[a-z0-9_-]+)\|", re.I)
CHAPTER_RE = re.compile(r"^@CHAPTER\|([^|]+)\|")
REF_RE = re.compile(r"\{\{([a-z]+:[a-z0-9_-]+)\}\}", re.I)


class Parser:
    """Turn the marked-up source into a platypus story."""

    def __init__(self, text):
        self.lines = text.splitlines()
        self.i = 0
        self.title_lines = []
        self.fields = {}
        self.authors = []
        self.story = []
        self.key_n = 0
        self.numbers = {}

    # -- helpers ----------------------------------------------------------
    def _key(self, prefix):
        self.key_n += 1
        return "%s%03d" % (prefix, self.key_n)

    def _peek(self):
        return self.lines[self.i] if self.i < len(self.lines) else None

    def _break(self):
        """Start a new page, unless one has just been started."""
        if self.story and isinstance(self.story[-1], PageBreak):
            return
        self.story.append(PageBreak())

    def _split(self, line, n=None):
        parts = line.split("|")
        return [p.strip() for p in parts]

    # -- main -------------------------------------------------------------
    def parse(self):
        # Pass 1: title-page metadata is collected wherever it appears.
        rest = []
        for line in self.lines:
            s = line.strip()
            if s.startswith("@TITLE|"):
                self.title_lines.append(s.split("|", 1)[1].strip())
            elif s.startswith("@FIELD|"):
                p = self._split(s)
                self.fields[p[1]] = p[2] if len(p) > 2 else ""
            elif s.startswith("@AUTHOR|"):
                p = self._split(s)
                self.authors.append((p[1], p[2] if len(p) > 2 else ""))
            else:
                rest.append(line)
        self.lines = rest
        self._number_blocks()
        self.lines = [self._resolve_refs(ln) for ln in self.lines]
        self.i = 0

        self._emit_title_page()

        while self.i < len(self.lines):
            line = self.lines[self.i].strip()
            self.i += 1
            if not line or line.startswith("#"):
                continue
            self._dispatch(line)
        return self.story

    def _number_blocks(self):
        """Assign figure and table numbers in document order, per chapter.

        Numbering is derived rather than written down, so inserting a figure in
        the middle of a chapter cannot leave a stale number behind it - which is
        the same single-source-of-truth rule the system under study applies to
        its stream names.
        """
        chapter, fig_n, tab_n = "0", 0, 0
        for raw in self.lines:
            line = raw.strip()
            m = CHAPTER_RE.match(line)
            if m:
                chapter, fig_n, tab_n = m.group(1).strip(), 0, 0
                continue
            m = LABEL_RE.match(line) or CHART_LABEL_RE.match(line)
            if not m:
                continue
            kind = "TABLE" if line.upper().startswith("@TABLE") else "FIGURE"
            label = m.group(m.lastindex)
            if label in self.numbers:
                raise ValueError("duplicate block label %r" % label)
            if kind == "TABLE":
                tab_n += 1
                self.numbers[label] = "%s.%d" % (chapter, tab_n)
            else:
                fig_n += 1
                self.numbers[label] = "%s.%d" % (chapter, fig_n)

    def _resolve_refs(self, line):
        def sub(m):
            label = m.group(1)
            if label not in self.numbers:
                raise ValueError("cross-reference to unknown label %r" % label)
            return self.numbers[label]
        return REF_RE.sub(sub, line)

    def _number_for(self, label):
        if label not in self.numbers:
            raise ValueError("block %r was never numbered" % label)
        return self.numbers[label]

    def _dispatch(self, line):
        S = self.story
        if line == "@PAGEBREAK":
            self._break()
        elif line == "@TOC":
            S.append(self._front_head("Table of Contents", toc=False))
            S.append(make_toc())
        elif line == "@LOF":
            self._break()
            S.append(self._front_head("List of Figures", toc=False))
            S.append(make_list_index("LOFEntry"))
        elif line == "@LOT":
            self._break()
            S.append(self._front_head("List of Tables", toc=False))
            S.append(make_list_index("LOTEntry"))
        elif line.startswith("@FRONT|"):
            p = self._split(line)
            heading = p[2] if len(p) > 2 else p[1].title()
            self._break()
            S.append(self._front_head(heading, toc=True))
        elif line.startswith("@CHAPTER|"):
            p = self._split(line)
            self._emit_chapter(p[1], p[2])
        elif line.startswith("@INTRO|"):
            S.append(Paragraph(rich(line.split("|", 1)[1]), ST["Intro"]))
        elif line.startswith("@H2|"):
            S.append(self._head(line.split("|", 1)[1], "H2"))
        elif line.startswith("@H3|"):
            S.append(self._head(line.split("|", 1)[1], "H3"))
        elif line.startswith("@H4|"):
            S.append(Paragraph(rich(line.split("|", 1)[1]), ST["H4"]))
        elif line.startswith("@P|"):
            S.append(Paragraph(rich(line.split("|", 1)[1]), ST["Body"]))
        elif line.startswith("@BULLET|"):
            S.append(Paragraph(rich(line.split("|", 1)[1]), ST["Bullet"],
                               bulletText="•"))
        elif line.startswith("@NUM|"):
            txt = line.split("|", 1)[1]
            m = re.match(r"^(\(?\w{1,4}[.)])\s+(.*)$", txt)
            if m:
                S.append(Paragraph(rich(m.group(2)), ST["Num"], bulletText=m.group(1)))
            else:
                S.append(Paragraph(rich(txt), ST["Num"], bulletText="•"))
        elif line.startswith("@NOTE|"):
            S.append(NoteBox(Paragraph(rich(line.split("|", 1)[1]), ST["Note"])))
            S.append(Spacer(1, 5))
        elif line.startswith("@CODE|"):
            S.append(Paragraph(html.escape(line.split("|", 1)[1]), ST["Code"]))
        elif line.startswith("@SIGN|"):
            self._emit_sign(self._split(line)[1:])
        elif line == "@RULE":
            S.append(HRule())
        elif line.startswith("@TABLE|"):
            self._emit_table(line)
        elif line.startswith("@FIGURE|"):
            self._emit_figure(line)
        elif line.startswith("@CHART|"):
            self._emit_chart(line)
        elif line == "@REFS":
            self._break()
            S.append(self._front_head("References", toc=True))
            S.append(Paragraph(
                "References follow the IEEE citation style. Every entry carries a "
                "Digital Object Identifier that was resolved against the Crossref or "
                "DataCite registry at the time of writing.", ST["Intro"]))
        elif line.startswith("@REF|"):
            S.append(Paragraph(rich(line.split("|", 1)[1]), ST["Ref"]))
        elif line.startswith("@SPACE|"):
            S.append(Spacer(1, float(line.split("|", 1)[1]) * mm))
        else:
            raise ValueError("unknown directive on line %d: %r" % (self.i, line[:70]))

    # -- emitters ---------------------------------------------------------
    def _front_head(self, text, toc=True):
        p = Paragraph(rich(text), ST["FrontHead"])
        if toc:
            p._tocKey = self._key("front")
        return p

    def _head(self, text, style):
        p = Paragraph(rich(text), ST[style])
        p._tocKey = self._key(style.lower())
        return p

    def _emit_chapter(self, num, title):
        S = self.story
        self._break()
        S.append(Spacer(1, 12 * mm))
        S.append(Paragraph("Chapter %s" % num, ST["ChapterNum"]))
        p = Paragraph(rich(title), ST["ChapterTitle"])
        p._isBody = True
        p._tocKey = self._key("chap")
        p._tocLabel = "%s %s" % (num, title)
        S.append(p)
        S.append(HRule(width=FRAME_W * 0.34, thickness=1.2, color=ACCENT, space=14))
        S.append(Spacer(1, 4))

    def _emit_sign(self, parts):
        parts = [x for x in parts if x]
        if len(parts) == 1:
            # A bare label such as "Submitted by:" introduces the blocks that
            # follow; it gets no signature rule of its own.
            st = ParagraphStyle("sgnlabel", parent=ST["TitleBold"], alignment=TA_LEFT,
                                spaceBefore=5 * mm, spaceAfter=0, leading=14)
            self.story.append(Paragraph(rich(parts[0]), st))
            return
        block = [Spacer(1, 9 * mm),
                 HRule(width=66 * mm, thickness=0.7, color=INK, space=3, centred=False),
                 Spacer(1, 1)]
        for j, seg in enumerate(parts):
            if not seg:
                continue
            style = ST["TitleBold"] if j == 0 else ST["TitleSmall"]
            st = ParagraphStyle("sgn%d" % j, parent=style, alignment=TA_LEFT,
                                spaceAfter=0, leading=13.2)
            block.append(Paragraph(rich(seg), st))
        block.append(Spacer(1, 2 * mm))
        self.story.append(KeepTogether(block))

    def _collect_block(self, end, allowed):
        """Read a block up to *end*.

        A missing terminator, or a directive that does not belong inside the
        block, is an error rather than something to skip: the whole point of
        this report is that a silent no-op is worse than a loud failure, and an
        unterminated block would swallow the rest of the document.
        """
        start = self.i
        rows = []
        while self.i < len(self.lines):
            line = self.lines[self.i].strip()
            self.i += 1
            if line == end:
                return rows
            if not line or line.startswith("#"):
                continue
            if not line.startswith(allowed):
                raise ValueError(
                    "line %d: %r is not valid inside the block opened on line %d; "
                    "expected one of %s or %s"
                    % (self.i, line[:60], start, " / ".join(allowed), end))
            rows.append(line)
        raise ValueError("block opened on line %d was never closed with %s" % (start, end))

    def _caption(self, kind, number, text, style_key):
        label = "%s %s: %s" % (kind, number, text)
        return Paragraph(rich(label), ST[style_key]), label

    def _emit_table(self, line):
        p = self._split(line)
        number, caption = self._number_for(p[1]), p[2]
        header, rows = None, []
        for raw in self._collect_block("@ENDTABLE", ("@TH|", "@TR|")):
            cells = [c.strip() for c in raw.split("|")[1:]]
            if raw.startswith("@TH|"):
                header = cells
            elif raw.startswith("@TR|"):
                rows.append(cells)
        style_key = "TableCaption" if len(rows) <= 9 else "TableCaptionLong"
        cap, label = self._caption("Table", number, caption, style_key)
        self.story.append(Spacer(1, 4))
        self.story.append(TocEntry("LOTEntry", label, self._key("tbl")))
        self.story.append(cap)
        self.story.append(make_table(header, rows))
        self.story.append(Spacer(1, 9))

    def _emit_figure(self, line):
        p = self._split(line)
        ref, number, caption = p[1], self._number_for(p[1]), p[2]
        height = 68 * mm
        desc, walk = [], []
        for raw in self._collect_block(
                "@ENDFIGURE", ("@FIGDESC|", "@FIGREAD|", "@FIGHEIGHT|")):
            if raw.startswith("@FIGDESC|"):
                desc.append(raw.split("|", 1)[1])
            elif raw.startswith("@FIGREAD|"):
                walk.append(raw.split("|", 1)[1])
            elif raw.startswith("@FIGHEIGHT|"):
                height = float(raw.split("|", 1)[1]) * mm

        art = image_flowable(ref)
        lead = "<b>Figure note.</b> " if art is not None else "<b>Image description.</b> "
        if art is None:
            art = FigurePlaceholder(height)

        cap, label = self._caption("Figure", number, caption, "Caption")
        # The marker follows the block: it is zero height, so placing it first
        # would satisfy a preceding heading's keepWithNext and strand that
        # heading at the foot of the previous page.
        self.story.append(KeepTogether([Spacer(1, 5), art, cap]))
        self.story.append(TocEntry("LOFEntry", label, self._key("fig")))
        # the walkthrough runs first: it says what is drawn, so that the note
        # after it can be about what the drawing means
        for w in walk:
            self.story.append(
                Paragraph("<b>How to read it.</b> " + rich(w), ST["FigDesc"]))
        for d in desc:
            self.story.append(Paragraph(lead + rich(d), ST["FigDesc"]))

    def _emit_chart(self, line):
        p = self._split(line)
        kind, number, caption = p[1], self._number_for(p[2]), p[3]
        labels, series, ylabel, desc, height = [], [], None, [], 78 * mm
        for raw in self._collect_block(
                "@ENDCHART",
                ("@LABELS|", "@SERIES|", "@YLABEL|", "@FIGDESC|", "@CHARTHEIGHT|")):
            parts = [c.strip() for c in raw.split("|")]
            if raw.startswith("@LABELS|"):
                labels = parts[1:]
            elif raw.startswith("@SERIES|"):
                series.append((parts[1], [float(v) for v in parts[2:]]))
            elif raw.startswith("@YLABEL|"):
                ylabel = parts[1]
            elif raw.startswith("@FIGDESC|"):
                desc.append(raw.split("|", 1)[1])
            elif raw.startswith("@CHARTHEIGHT|"):
                height = float(parts[1]) * mm
        drawing = make_chart(kind, labels, series, ylabel, height=height)
        cap, label = self._caption("Figure", number, caption, "Caption")
        self.story.append(KeepTogether([Spacer(1, 5), drawing, cap]))
        self.story.append(TocEntry("LOFEntry", label, self._key("fig")))
        for d in desc:
            self.story.append(Paragraph("<b>Reading the chart.</b> " + rich(d), ST["FigDesc"]))

    def _emit_title_page(self):
        S = self.story
        f = self.fields
        S.append(Spacer(1, 6 * mm))
        S.append(Paragraph(rich(" ".join(self.title_lines)), ST["TitleMain"]))
        S.append(Spacer(1, 4 * mm))
        S.append(HRule(width=FRAME_W * 0.40, thickness=1.1, color=ACCENT, space=8))
        S.append(Spacer(1, 4 * mm))
        S.append(Paragraph("Final Year Design Project Report", ST["TitleBold"]))
        S.append(Spacer(1, 6 * mm))
        S.append(Paragraph("BY", ST["TitleSmall"]))
        S.append(Spacer(1, 1.5 * mm))
        for name, sid in self.authors:
            S.append(Paragraph(rich(name), ST["TitleBold"]))
            S.append(Paragraph("ID: %s" % rich(sid), ST["TitleSmall"]))
            S.append(Spacer(1, 2.5 * mm))
        S.append(Spacer(1, 2.5 * mm))
        if f.get("degree"):
            S.append(Paragraph(rich(f["degree"]), ST["TitleSmall"]))
        S.append(Spacer(1, 5 * mm))
        for role, key in (("Supervised by", "supervised_by"),
                          ("Co-Supervised by", "cosupervised_by")):
            if f.get(key):
                S.append(Paragraph(role, ST["TitleSmall"]))
                for seg in f[key].split(";"):
                    S.append(Paragraph(rich(seg.strip()), ST["TitleLine"]))
                S.append(Spacer(1, 3.5 * mm))
        S.append(Spacer(1, 2 * mm))
        S.append(HRule(width=FRAME_W * 0.40, thickness=0.8, color=RULE, space=8))
        if f.get("university"):
            S.append(Paragraph(rich(f["university"]).upper(), ST["TitleBold"]))
        if f.get("city"):
            S.append(Paragraph(rich(f["city"]), ST["TitleLine"]))
        if f.get("date"):
            S.append(Paragraph(rich(f["date"]), ST["TitleSmall"]))
        S.append(NextPageTemplate("numbered"))
        S.append(PageBreak())


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def build(src, dst):
    with open(src, encoding="utf-8") as fh:
        text = fh.read()
    story = Parser(text).parse()
    doc = ReportDoc(
        dst,
        title="Selective Intelligence: A Cost-Aware Hybrid NLP–LLM Microservice",
        author="Final Year Design Project",
        subject="Thread-level sentiment, watchlist-driven target stance, and an "
                "MCP-backed agentic RAG insight layer for code-mixed Bangla social media",
    )
    # The index flowables print physical page numbers; hand them the document's
    # roman/arabic labeller so the printed value matches the page footer.
    for flowable in story:
        if isinstance(flowable, Index):
            flowable.formatter = doc.label_for
    doc.multiBuild(story)
    return doc.page


# ---------------------------------------------------------------------------
# Diagram palette
#
# Four hues plus a neutral, one per role, so a reader learns the code once and
# it holds across every figure.  The accents were checked as a categorical set
# (all pairs, not just neighbours): worst colour-vision-deficient separation
# ΔE 9.6 and worst normal-vision ΔE 16.3 in OKLab×100, both clear of the 8/15
# floors, and all four clear 3:1 against the page.  Shape and label carry the
# same distinction anyway — a cylinder is a store whatever the ink does — so
# nothing here depends on colour alone, which is what keeps the figures
# readable in greyscale print.
#
# Fills are held near L 0.95 and inks near L 0.36 in OKLCH, which puts every
# label at ~9.5:1 on its own fill.  That headroom is the point: these diagrams
# are reduced to 6-10pt on the page, and thin type on a tint is the first
# thing to go muddy in print.
# ---------------------------------------------------------------------------

DIAGRAM_ROLES = {
    # role     fill        stroke      ink         stroke width
    "entry": ("#eaecff", "#4a3aa7", "#373660", "1.4px"),   # people and outside systems
    "svc":   ("#ddf0ff", "#2a78d6", "#203d61", "1.4px"),   # services and compute
    "store": ("#d8f7e6", "#008d5a", "#0a4730", "1.4px"),   # datastores
    "obs":   ("#ffe6db", "#d25118", "#5b2d1d", "1.4px"),   # notes and observability
    "tool":  ("#eef1f7", "#747d88", "#3c4249", "1.2px"),   # supporting pieces
}

INK = "#2a2e34"          # body ink
LINE = "#6b7480"         # edges: 4.7:1 on white, still recessive beside a node
SIGNAL = "#4a5462"       # sequence arrows, a step darker than a flowchart edge
PANEL = "#f7f9fb"        # subgraph panel: lighter than every node fill, so the
PANEL_EDGE = "#d8dde3"   # nodes read as cards sitting on it
ACCENT_TITLE = "#2a78d6"


def classdef_lines():
    """The canonical ``classDef`` block shared by every diagram source."""
    return ["classDef %s fill:%s,stroke:%s,stroke-width:%s,color:%s"
            % (role, fill, stroke, width, ink)
            for role, (fill, stroke, ink, width) in DIAGRAM_ROLES.items()]


MERMAID_THEME = {
    "theme": "base",
    "themeVariables": {
        "fontFamily": "Liberation Sans, Helvetica, Arial, sans-serif",
        "fontSize": "18px",
        "primaryColor": DIAGRAM_ROLES["svc"][0],
        "primaryTextColor": DIAGRAM_ROLES["svc"][2],
        "primaryBorderColor": DIAGRAM_ROLES["svc"][1],
        "secondaryColor": DIAGRAM_ROLES["store"][0],
        "secondaryTextColor": DIAGRAM_ROLES["store"][2],
        "secondaryBorderColor": DIAGRAM_ROLES["store"][1],
        "tertiaryColor": DIAGRAM_ROLES["entry"][0],
        "tertiaryTextColor": DIAGRAM_ROLES["entry"][2],
        "tertiaryBorderColor": DIAGRAM_ROLES["entry"][1],
        "mainBkg": DIAGRAM_ROLES["svc"][0],
        "nodeBorder": DIAGRAM_ROLES["svc"][1],
        "nodeTextColor": DIAGRAM_ROLES["svc"][2],
        "lineColor": LINE, "textColor": INK, "titleColor": ACCENT_TITLE,
        "clusterBkg": PANEL, "clusterBorder": PANEL_EDGE,
        "edgeLabelBackground": "#ffffff",
        "actorBkg": DIAGRAM_ROLES["svc"][0],
        "actorBorder": DIAGRAM_ROLES["svc"][1],
        "actorTextColor": DIAGRAM_ROLES["svc"][2],
        "signalColor": SIGNAL, "signalTextColor": INK,
        # mermaid paints lifelines in the actor's border colour, which puts a
        # full-height accent stripe under every message; hold them back
        "actorLineColor": "#9ba4af",
        "labelBoxBkgColor": DIAGRAM_ROLES["entry"][0],
        "labelBoxBorderColor": DIAGRAM_ROLES["entry"][1],
        "labelTextColor": DIAGRAM_ROLES["entry"][2],
        "loopTextColor": INK,
        "noteBkgColor": DIAGRAM_ROLES["obs"][0],
        "noteBorderColor": DIAGRAM_ROLES["obs"][1],
        "noteTextColor": DIAGRAM_ROLES["obs"][2],
        "activationBkgColor": DIAGRAM_ROLES["store"][0],
        "activationBorderColor": DIAGRAM_ROLES["store"][1],
        "sequenceNumberColor": "#ffffff", "altBackground": PANEL,
        "transitionColor": LINE,
        "stateBkg": DIAGRAM_ROLES["svc"][0],
        "stateBorder": DIAGRAM_ROLES["svc"][1],
        "compositeBackground": PANEL, "compositeBorder": PANEL_EDGE,
        # mermaid derives stateLabelColor from stateBkg, which paints state
        # labels in their own box colour: set it explicitly or they vanish.
        "stateLabelColor": DIAGRAM_ROLES["svc"][2],
        "transitionLabelColor": INK,
        "labelBackgroundColor": "#ffffff",
    },
    "flowchart": {"curve": "basis", "padding": 14, "nodeSpacing": 34,
                  "rankSpacing": 40, "htmlLabels": True,
                  # default 200px wraps mid-word and strands single tokens
                  "wrappingWidth": 340},
    "sequence": {"useMaxWidth": True, "mirrorActors": False, "boxMargin": 12,
                 "actorFontSize": 17, "messageFontSize": 16, "noteFontSize": 16},
    "state": {"useMaxWidth": True},
}

# Applied to the rendered SVG rather than the source: mermaid has no config for
# either, and both are what separate a diagram that looks drawn from one that
# looks dumped.  Softened corners on the boxes, and the secondary line inside a
# node held back so the node's name reads first.
DIAGRAM_CSS = (
    "#my-svg .node rect.basic{rx:7px;ry:7px}"
    "#my-svg small{opacity:.82}"
)

CHROME_CANDIDATES = ("/usr/bin/google-chrome-stable", "/usr/bin/google-chrome",
                     "/usr/bin/chromium", "/usr/bin/chromium-browser")


SVG_SCALE = 3.0          # nominal raster scale
SVG_MAX_PX = 14000       # cap the long edge so reportlab stays happy
SVG_MARGIN = 10          # white gutter in SVG units around every diagram:
                         # mermaid's viewBox ends flush with the outermost ink,
                         # which on a sequence diagram is the last arrowhead


def _svg_viewbox(text):
    """Return the (width, height) of an mmdc-produced SVG in user units."""
    import re
    m = re.search(r'viewBox="([\d.eE+-]+) ([\d.eE+-]+) ([\d.eE+-]+) ([\d.eE+-]+)"',
                  text)
    if m is None:
        raise ValueError("no viewBox in SVG")
    return float(m.group(3)), float(m.group(4))


CLUSTER_TITLE_GAP = 4    # clear space between a subgraph title and its contents


def _lift_cluster_titles(text):
    """Move subgraph titles clear of the nodes they sit on top of.

    Mermaid reserves a fixed 20px band at the top of a cluster for its title,
    but lays the title out in a 27px box (18px type at line-height 1.5).  The
    extra 7px land inside the first node in the cluster, so the node's top
    border is drawn straight through the title's descenders.  Every subgraph
    whose first rank starts immediately below the title is affected.

    Nothing in mermaid's config governs that band, so correct it here: shift
    each offending title up by the shortfall and grow its cluster box by the
    same amount, which keeps the title inside its own frame.
    """
    import re
    import xml.etree.ElementTree as ET

    ns = "{http://www.w3.org/2000/svg}"

    def shift(el):
        t = el.get("transform") or ""
        m = re.search(r"translate\(\s*([-\d.eE]+)[,\s]+([-\d.eE]+)\s*\)", t)
        return (float(m.group(1)), float(m.group(2))) if m else (0.0, 0.0)

    def walk(el, dx=0.0, dy=0.0, nodes=None, clusters=None):
        nodes = [] if nodes is None else nodes
        clusters = [] if clusters is None else clusters
        tx, ty = shift(el)
        dx, dy = dx + tx, dy + ty
        cls = (el.get("class") or "").split()
        if "node" in cls:
            rect = el.find(ns + "rect")
            if rect is not None:
                x = dx + float(rect.get("x") or 0)
                nodes.append((x, dy + float(rect.get("y") or 0),
                              x + float(rect.get("width") or 0)))
            else:
                circle = el.find(ns + "circle")
                if circle is not None:
                    r = float(circle.get("r") or 0)
                    nodes.append((dx - r, dy - r, dx + r))
        elif "cluster" in cls and "cluster-label" not in cls:
            rect = el.find(ns + "rect")
            box = el.find("./%sg/%sforeignObject" % (ns, ns))
            if rect is not None and box is not None:
                x = dx + float(rect.get("x") or 0)
                clusters.append((el.get("id"), x, dy + float(rect.get("y") or 0),
                                 x + float(rect.get("width") or 0),
                                 float(box.get("height") or 0)))
        for kid in el:
            walk(kid, dx, dy, nodes, clusters)
        return nodes, clusters

    try:
        nodes, clusters = walk(ET.fromstring(text))
    except ET.ParseError:
        return text

    lift = {}
    for cid, x0, top, x1, label_h in clusters:
        inside = [n for n in nodes if n[0] >= x0 - 2 and n[2] <= x1 + 2 and n[1] > top]
        if not inside:
            continue
        # the title box hangs from the cluster's top edge
        short = (top + label_h + CLUSTER_TITLE_GAP) - min(n[1] for n in inside)
        if short > 0:
            lift[cid] = short

    if not lift:
        return text

    pattern = (r'(<g class="cluster" id="(?P<id>[^"]+)"[^>]*>\s*<rect [^>]*?'
               r'y=")(?P<y>[-\d.eE]+)("[^>]*?width="[-\d.eE]+" height=")'
               r'(?P<h>[-\d.eE]+)("/>\s*<g class="cluster-label" '
               r'transform="translate\([-\d.eE]+,\s*)(?P<ly>[-\d.eE]+)(\))')

    def raise_one(m):
        d = lift.get(m.group("id"), 0.0)
        if not d:
            return m.group(0)
        return "%s%g%s%g%s%g%s" % (
            m.group(1), float(m.group("y")) - d, m.group(4),
            float(m.group("h")) + d, m.group(6),
            float(m.group("ly")) - d, m.group(8))

    text = re.sub(pattern, raise_one, text)

    # a cluster at the very top can now start above the viewBox
    head = re.search(r'viewBox="([-\d.eE]+) ([-\d.eE]+) ([-\d.eE]+) ([-\d.eE]+)"',
                     text)
    nodes, clusters = walk(ET.fromstring(text))
    highest = min([c[2] for c in clusters] + [float(head.group(2))])
    if highest < float(head.group(2)):
        grow = float(head.group(2)) - highest
        text = text.replace(
            head.group(0),
            'viewBox="%s %g %s %g"' % (head.group(1), highest, head.group(3),
                                       float(head.group(4)) + grow), 1)
    return text


def _raise_cluster_titles(text):
    """Repaint subgraph titles above the edges that cross them.

    Mermaid emits the cluster group before the edge paths, so any edge routed
    into a subgraph is drawn straight over its title.  Nothing in the layout
    prevents it, and on this set fourteen titles were struck through.  Draw a
    second copy of each title last, on an opaque patch of the cluster fill, so
    it reads cleanly whatever passes underneath.
    """
    import re
    import xml.etree.ElementTree as ET

    ns = "{http://www.w3.org/2000/svg}"
    fill = MERMAID_THEME["themeVariables"].get("clusterBkg", "#ffffff")

    def shift(el):
        t = el.get("transform") or ""
        m = re.search(r"translate\(\s*([-\d.eE]+)[,\s]+([-\d.eE]+)\s*\)", t)
        return (float(m.group(1)), float(m.group(2))) if m else (0.0, 0.0)

    def walk(el, dx=0.0, dy=0.0, found=None):
        found = [] if found is None else found
        tx, ty = shift(el)
        dx, dy = dx + tx, dy + ty
        if "cluster-label" in (el.get("class") or "").split():
            found.append((dx, dy))
        for kid in el:
            walk(kid, dx, dy, found)
        return found

    try:
        spots = walk(ET.fromstring(text))
    except ET.ParseError:
        return text

    blocks = re.findall(
        r'<g class="cluster-label" transform="translate\([-\d.eE]+,\s*[-\d.eE]+\)">'
        r'(<foreignObject width="([-\d.eE]+)" height="([-\d.eE]+)">.*?</foreignObject>)</g>',
        text, re.S)
    if len(blocks) != len(spots):        # markup is not the shape we expect
        return text

    overlay = []
    for (x, y), (inner, w, h) in zip(spots, blocks):
        overlay.append(
            '<g class="cluster-label" transform="translate(%g, %g)">'
            '<rect x="-3" y="0" width="%g" height="%s" fill="%s" stroke="none"/>'
            '%s</g>' % (x, y, float(w) + 6, h, fill, inner))
    return text.replace("</svg>", "".join(overlay) + "</svg>", 1)


def _rasterise(chrome, svg_path, png_path):
    """Screenshot one SVG at its natural size with headless Chrome.

    mermaid-cli's own ``-o *.png`` path goes through a puppeteer screenshot
    that sizes the page to an 800px viewport and clips to a bounding box it
    computes before layout settles.  On this diagram set that produced blank
    images, right-hand clipping on sequence diagrams and one double-render.
    The SVG output is always correct, so render SVG and rasterise it here:
    the page is pinned to the viewBox and the device scale factor carries the
    resolution, so nothing is ever scaled to fit a viewport.
    """
    import re
    import shutil
    import subprocess
    import tempfile

    text = _lift_cluster_titles(open(svg_path, encoding="utf-8").read())
    text = _raise_cluster_titles(text)
    w, h = _svg_viewbox(text)
    page_w, page_h = w + 2 * SVG_MARGIN, h + 2 * SVG_MARGIN
    scale = min(SVG_SCALE, SVG_MAX_PX / max(page_w, page_h))
    text = text.replace('width="100%"', 'width="%g"' % w, 1)
    text = re.sub(r"max-width:\s*[\d.]+px;", "max-width:none;", text)

    tmp = tempfile.mkdtemp()
    try:
        html = os.path.join(tmp, "diagram.html")
        with open(html, "w", encoding="utf-8") as fh:
            fh.write("<!doctype html><meta charset='utf-8'><style>"
                     "html{background:#fff}"
                     "body{margin:0;padding:%gpx;background:#fff}"
                     "svg{display:block;width:%gpx;height:%gpx}"
                     "%s</style>%s" % (SVG_MARGIN, w, h, DIAGRAM_CSS, text))
        cmd = [chrome, "--headless", "--no-sandbox", "--disable-gpu",
               "--disable-dev-shm-usage", "--hide-scrollbars",
               "--default-background-color=FFFFFFFF",
               "--force-device-scale-factor=%g" % scale,
               "--window-size=%d,%d" % (int(round(page_w)), int(round(page_h))),
               "--virtual-time-budget=8000",
               # without this the screenshot can be taken mid-paint: it lands
               # with a blank rectangle over part of the diagram, and the defect
               # moves from run to run.  It forces every compositor stage to
               # finish before the frame is captured, which also makes the
               # rasterisation reproducible.
               "--run-all-compositor-stages-before-draw",
               "--screenshot=" + os.path.abspath(png_path),
               "file://" + html]
        # headless Chrome occasionally wedges instead of exiting.  Each attempt
        # gets its own profile directory: a wedged process keeps a lock on the
        # one it was using, so a retry that reuses it inherits the hang — which
        # is exactly how a transient stall turned into a failed build.
        last = ""
        for attempt in range(3):
            profile = os.path.join(tmp, "profile-%d" % attempt)
            try:
                res = subprocess.run(cmd + ["--user-data-dir=" + profile],
                                     capture_output=True, text=True, timeout=240)
            except subprocess.TimeoutExpired:
                last = "chrome timed out"
                continue
            if os.path.exists(png_path):
                return int(round(page_w * scale)), int(round(page_h * scale))
            last = res.stderr.strip()[:200] or "no screenshot"
        raise RuntimeError(last)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _looks_blank(png_path, w, h):
    """True when the raster carries no ink.

    This is the failure mode that put a blank page in the report: mermaid-cli
    reported success, the PNG was the right size, and only opening it showed
    that nothing had been drawn.  Checking it here means the renderer fails
    loudly instead.  Pillow gives the real answer; without it, fall back on
    compressed size per megapixel, which separates a flat fill (~4 KB/Mpx on
    the diagram that regressed) from a real diagram (~50 KB/Mpx) with room to
    spare.
    """
    try:
        from PIL import Image, ImageStat
    except ImportError:
        megapixels = max(w * h, 1) / 1e6
        return os.path.getsize(png_path) / megapixels < 10 * 1024
    with Image.open(png_path) as im:
        return max(ImageStat.Stat(im.convert("L")).stddev) < 0.5


def restyle_diagrams():
    """Rewrite the ``classDef`` block of every diagram source from the palette.

    The role names live in the sources — a node is tagged ``store`` because it
    is one — but the colours behind them live here.  Keeping the definitions in
    the ``.mmd`` files means each one still renders standalone in any mermaid
    tool; regenerating them from :data:`DIAGRAM_ROLES` means they cannot drift
    apart, which they had (two files carried a wider stroke than the rest).
    """
    import glob

    canonical = classdef_lines()
    sources = sorted(glob.glob(os.path.join(DIAGRAM_SRC, "*.mmd")))
    if not sources:
        sys.exit("no mermaid sources in %s" % DIAGRAM_SRC)
    changed = 0
    for src in sources:
        with open(src, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        kept = [ln for ln in lines
                if not re.match(r"\s*classDef\s+(%s)\b"
                                % "|".join(DIAGRAM_ROLES), ln)]
        while kept and not kept[-1].strip():
            kept.pop()
        # only the roles the diagram actually assigns
        used = set()
        for ln in kept:
            m = re.match(r"\s*class\s+([\w,]+)\s+(\w+)\s*$", ln)
            if m and m.group(2) in DIAGRAM_ROLES:
                used.add(m.group(2))
        # only roles the diagram assigns: a sequence diagram assigns none, and
        # classDef is not valid syntax there
        block = [ln for ln in canonical if ln.split()[1] in used]
        if not block:
            print("  %-24s no roles, left alone" % os.path.basename(src))
            continue
        out = "\n".join(kept + [""] + ["    " + ln for ln in block]) + "\n"
        if out != "\n".join(lines) + "\n":
            with open(src, "w", encoding="utf-8") as fh:
                fh.write(out)
            changed += 1
        print("  %-24s %d role(s)" % (os.path.basename(src), len(block)))
    print("restyled %d of %d sources" % (changed, len(sources)))


def render_charts(src):
    """Render every ``@CHART`` block in *src* to ``figures/fig-<name>.png``.

    The PDF build draws charts as vector graphics straight into the story and
    never reads these files, so this step is purely for export: it puts the
    charts alongside the diagrams and screenshots, so every figure in the report
    exists as an image on disk rather than only inside the PDF.

    Two hops, because reportlab's raster backend (``renderPM``) needs an
    optional Cairo dependency that the PDF build does not: the Drawing goes to a
    one-page PDF through ``renderPDF``, and ``pdftoppm`` rasterises that at
    ``--chart-dpi``.  Poppler is the only external requirement and it is a
    lighter one than the Node and Chrome that :func:`render_diagrams` needs.
    """
    import shutil
    import subprocess
    import tempfile

    from reportlab.graphics import renderPDF

    if shutil.which("pdftoppm") is None:
        sys.exit("pdftoppm (poppler-utils) not found; cannot rasterise charts")
    os.makedirs(FIGURE_DIR, exist_ok=True)

    with open(src, encoding="utf-8") as fh:
        lines = fh.read().split("\n")

    written, i = [], 0
    while i < len(lines):
        line = lines[i]
        if not line.startswith("@CHART|"):
            i += 1
            continue
        parts = [c.strip() for c in line.split("|")]
        kind, label = parts[1], parts[2]
        labels, series, ylabel, height = [], [], None, 78 * mm
        i += 1
        while i < len(lines) and not lines[i].startswith("@ENDCHART"):
            raw = lines[i]
            cells = [c.strip() for c in raw.split("|")]
            if raw.startswith("@LABELS|"):
                labels = cells[1:]
            elif raw.startswith("@SERIES|"):
                series.append((cells[1], [float(v) for v in cells[2:]]))
            elif raw.startswith("@YLABEL|"):
                ylabel = cells[1]
            elif raw.startswith("@CHARTHEIGHT|"):
                height = float(cells[1]) * mm
            i += 1
        i += 1

        drawing = make_chart(kind, labels, series, ylabel, height=height)
        out = os.path.join(FIGURE_DIR, "fig-%s.png" % label.split(":", 1)[1])
        with tempfile.TemporaryDirectory() as tmp:
            pdf = os.path.join(tmp, "chart.pdf")
            renderPDF.drawToFile(drawing, pdf, "chart")
            stem = os.path.join(tmp, "page")
            subprocess.run(
                ["pdftoppm", "-png", "-r", str(CHART_DPI), "-cropbox",
                 "-singlefile", pdf, stem],
                check=True)
            shutil.move(stem + ".png", out)
        written.append(out)
        print("  wrote %s" % os.path.relpath(out, HERE))

    print("rendered %d charts to %s" % (len(written), os.path.relpath(FIGURE_DIR, HERE)))
    return written


def render_diagrams():
    """Render every ``diagrams/*.mmd`` to ``figures/*.png``.

    Two hops: mermaid-cli produces the SVG, then headless Chrome rasterises it
    (see :func:`_rasterise` for why the one-hop PNG path is not used).

    Kept out of the build path deliberately: it needs Node and a Chrome binary,
    while the PDF build needs only reportlab.  The PNGs are the committed
    artefacts, so a machine without either can still produce the report.
    """
    import glob
    import json
    import shutil
    import subprocess
    import tempfile

    chrome = next((c for c in CHROME_CANDIDATES if os.path.exists(c)), None)
    if chrome is None:
        sys.exit("no Chrome or Chromium binary found; cannot render diagrams")
    os.makedirs(FIGURE_DIR, exist_ok=True)

    tmp = tempfile.mkdtemp()
    try:
        cfg = os.path.join(tmp, "mermaid.json")
        pup = os.path.join(tmp, "puppeteer.json")
        with open(cfg, "w") as fh:
            json.dump(MERMAID_THEME, fh)
        with open(pup, "w") as fh:
            json.dump({"executablePath": chrome,
                       "args": ["--no-sandbox", "--disable-dev-shm-usage"]}, fh)

        sources = sorted(glob.glob(os.path.join(DIAGRAM_SRC, "*.mmd")))
        if not sources:
            sys.exit("no mermaid sources in %s" % DIAGRAM_SRC)
        failed = []
        for src in sources:
            name = os.path.splitext(os.path.basename(src))[0]
            svg = os.path.join(tmp, name + ".svg")
            dst = os.path.join(FIGURE_DIR, name + ".png")
            cmd = ["npx", "-y", "@mermaid-js/mermaid-cli@11", "-i", src,
                   "-o", svg, "-c", cfg, "-p", pup, "-b", "white"]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if res.returncode != 0 or not os.path.exists(svg):
                failed.append(name)
                print("  FAIL %s: %s" % (name, res.stderr.strip()[:160]))
                continue
            try:
                w, h = _rasterise(chrome, svg, dst)
                if _looks_blank(dst, w, h):
                    raise RuntimeError("rasterised blank")
            except Exception as exc:                     # noqa: BLE001
                failed.append(name)
                print("  FAIL %s: %s" % (name, exc))
                continue
            print("  ok   %-22s %dx%d" % (name, w, h))
        print("rendered %d of %d diagrams" % (len(sources) - len(failed), len(sources)))
        if failed:
            sys.exit("failed: %s" % ", ".join(failed))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv):
    if "--restyle-diagrams" in argv:
        restyle_diagrams()
        argv = [a for a in argv if a != "--restyle-diagrams"]
        if len(argv) == 1:
            return 0
    if "--render-diagrams" in argv:
        render_diagrams()
        argv = [a for a in argv if a != "--render-diagrams"]
        if len(argv) == 1:
            return 0
    charts_only = "--render-charts" in argv
    argv = [a for a in argv if a != "--render-charts"]
    src = argv[1] if len(argv) > 1 else os.path.join(HERE, "final_paper.txt")
    if charts_only:
        if not os.path.exists(src):
            sys.exit("source not found: %s" % src)
        render_charts(src)
        return 0
    dst = argv[2] if len(argv) > 2 else os.path.join(HERE, "final_paper.pdf")
    if not os.path.exists(src):
        sys.exit("source not found: %s" % src)
    pages = build(src, dst)
    size = os.path.getsize(dst)
    print("wrote %s  (%d pages, %.1f KB)" % (dst, pages, size / 1024.0))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
