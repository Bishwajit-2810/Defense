#!/usr/bin/env python3
"""
generate_docx.py — render ``final_paper.txt`` into ``final_paper.docx``.

The report has one source of truth, ``final_paper.txt``.  ``generate_pdf.py``
lays that out for print; this renders the *same* file to Word, so the two
cannot drift: run both after an edit and the DOCX says exactly what the PDF
says.

Why this writes WordprocessingML directly
-----------------------------------------
The FYDP template requires a table of contents, a list of figures and a list of
tables that **update themselves** after an edit.  In Word that means three
things that a Markdown-to-DOCX converter does not produce:

* real ``TOC`` **field codes** rather than a snapshot of the headings, so that
  Update Field re-reads the document;
* heading **numbering owned by Word** - a multilevel list bound to Heading 1-3 -
  so that inserting a section renumbers every later one and the contents entry
  follows;
* captions carrying ``STYLEREF``/``SEQ`` fields, so that "Figure 3.4" is
  computed from the chapter the figure sits in and the count of figures before
  it, and the List of Figures is built from those fields.

Everything else - roman front matter and arabic body pagination in separate
document sections, the criterion-checklist grids, the shaded timeline cells -
is likewise a document-format feature rather than a text feature.  Writing the
XML is therefore the shortest honest path; a converter would only have to be
post-processed into the same shape.

The one consequence worth knowing: **heading numbers are absent from the XML.**
The source writes ``@H2|3.1  Overview``; the numeric prefix is stripped here and
Word regenerates it from the outline.  The rendered numbers are identical to the
PDF's because both derive from the same document order.

Usage
-----
    python3 generate_docx.py                      # final_paper.txt -> final_paper.docx
    python3 generate_docx.py in.txt out.docx

Requires only the standard library plus Pillow (to read image dimensions).
Figures and charts are embedded from ``figures/fig-<label>.png``; that is why
``generate_pdf.py --render-charts`` exists, since the PDF draws its charts as
vector graphics from inline data and never needs a file.  A missing PNG is
reported rather than silently skipped.
"""

from __future__ import annotations

import html
import json
import os
import re
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
FIGURE_DIR = os.path.join(HERE, "figures")

# ---------------------------------------------------------------------------
# Page geometry, in twentieths of a point ("twips").
# ---------------------------------------------------------------------------
PAGE_W, PAGE_H = 11906, 16838            # A4 portrait
MARGIN_L, MARGIN_R = 1699, 1440          # 1.18 in binding edge, 1.00 in outer
MARGIN_T, MARGIN_B = 1440, 1440
CONTENT_H = PAGE_H - MARGIN_T - MARGIN_B          # twips
CONTENT_W = PAGE_W - MARGIN_L - MARGIN_R          # twips
CONTENT_EMU = CONTENT_W * 635                     # 1 twip = 635 EMU

# Diagram scaling — the same rule as generate_pdf.diagram_plan(), so a node
# label is the same size in the DOCX as it is in the PDF.  Mermaid sets a label
# at DIAGRAM_FONT_UNITS SVG units, and stretching every drawing to the content
# width printed that label anywhere between 6 pt and 18 pt depending only on
# how much the drawing contained.
DIAGRAM_FONT_UNITS = 19.0
DIAGRAM_TEXT_PT = 9.5
DIAGRAM_METRICS = "diagram-metrics.json"
SVG_MARGIN = 10                                   # gutter added by the rasteriser
CONTENT_W_PT = CONTENT_W / 20.0
SOLO_FIG_H_PT = CONTENT_H / 20.0 - 52             # a figure alone on its page

# Same rules as generate_pdf.py — see the docstring there.
LABEL_RE = re.compile(r"^@(FIGURE|TABLE|EQ)\|([a-z]+:[a-z0-9_-]+)\|", re.I)
CHART_LABEL_RE = re.compile(r"^@CHART\|[a-z]+\|([a-z]+:[a-z0-9_-]+)\|", re.I)
CHAPTER_RE = re.compile(r"^@CHAPTER\|([^|]+)\|")
XREF_RE = re.compile(r"\{\{([a-z]+:[a-z0-9_-]+)\}\}")
HEADNUM_RE = re.compile(r"^\d+(?:\.\d+)*\s+")

_RE_MONO = re.compile(r"`([^`]+)`")
_RE_BOLD = re.compile(r"\*\*(.+?)\*\*")
_RE_ITAL = re.compile(r"(?<![\*\w])\*(?!\s)(.+?)(?<!\s)\*(?!\*)")
_RE_RANGE = re.compile(r"(?<=\d)-(?=\d)")
_RE_DASH = re.compile(r"(?<=\S)\s-\s(?=\S)")
_RE_SUB = re.compile(r"_\{([^}]*)\}|_(\w)")
_RE_SUP = re.compile(r"\^\{([^}]*)\}|\^(\w)")
_RE_EMPH = re.compile(r"@([A-Za-z][A-Za-z0-9]*)")


def esc(t):
    return html.escape(t, quote=False)


# ---------------------------------------------------------------------------
# Inline runs
# ---------------------------------------------------------------------------

def runs(text, numbers, base=""):
    """Turn one source string into a list of ``<w:r>`` elements.

    The inline markup is tokenised rather than regex-substituted into XML,
    because ``**bold**`` and ``` `mono` ``` change the run *properties*, and a
    run is the smallest thing Word will let carry them.
    """
    text = XREF_RE.sub(lambda m: numbers.get(m.group(1), "??"), text)
    text = _RE_RANGE.sub("–", text)
    text = _RE_DASH.sub(" – ", text)

    tokens, i = [], 0
    pattern = re.compile(r"`[^`]+`|\*\*.+?\*\*|(?<![\*\w])\*(?!\s).+?(?<!\s)\*(?!\*)")
    for m in pattern.finditer(text):
        if m.start() > i:
            tokens.append(("", text[i:m.start()]))
        s = m.group(0)
        if s.startswith("`"):
            tokens.append(("mono", s[1:-1]))
        elif s.startswith("**"):
            tokens.append(("b", s[2:-2]))
        else:
            tokens.append(("i", s[1:-1]))
        i = m.end()
    if i < len(text):
        tokens.append(("", text[i:]))

    out = []
    for kind, chunk in tokens:
        props = []
        if base == "b" or kind == "b":
            props.append("<w:b/>")
        if base == "i" or kind == "i":
            props.append("<w:i/>")
        if kind == "mono":
            props.append('<w:rFonts w:ascii="Consolas" w:hAnsi="Consolas"/>')
            props.append('<w:sz w:val="19"/>')
        rpr = "<w:rPr>%s</w:rPr>" % "".join(props) if props else ""
        out.append('<w:r>%s<w:t xml:space="preserve">%s</w:t></w:r>'
                   % (rpr, esc(chunk)))
    return "".join(out)


def math_runs(text):
    """Runs for an equation body: sub/superscripts and maths italics."""
    out, i = [], 0
    token = re.compile(r"_\{[^}]*\}|_\w|\^\{[^}]*\}|\^\w|@[A-Za-z][A-Za-z0-9]*")

    def run(chunk, script=None, italic=False):
        props = ['<w:rFonts w:ascii="Cambria Math" w:hAnsi="Cambria Math"/>']
        if italic:
            props.append("<w:i/>")
        if script:
            props.append('<w:vertAlign w:val="%s"/>' % script)
        return ('<w:r><w:rPr>%s</w:rPr><w:t xml:space="preserve">%s</w:t></w:r>'
                % ("".join(props), esc(chunk)))

    for m in token.finditer(text):
        if m.start() > i:
            out.append(run(text[i:m.start()]))
        s = m.group(0)
        if s.startswith("_"):
            out.append(run(s[2:-1] if s[1] == "{" else s[1:], script="subscript"))
        elif s.startswith("^"):
            out.append(run(s[2:-1] if s[1] == "{" else s[1:], script="superscript"))
        else:
            out.append(run(s[1:], italic=True))
        i = m.end()
    if i < len(text):
        out.append(run(text[i:]))
    return "".join(out)


def math_prose(text, numbers):
    """``runs`` plus the equation mini-language, for the *where* clauses."""
    text = XREF_RE.sub(lambda m: numbers.get(m.group(1), "??"), text)
    out, i = [], 0
    token = re.compile(r"_\{[^}]*\}|_\w|\^\{[^}]*\}|\^\w|@[A-Za-z][A-Za-z0-9]*")
    for m in token.finditer(text):
        if m.start() > i:
            out.append(runs(text[i:m.start()], numbers))
        s = m.group(0)
        if s.startswith(("_", "^")):
            align = "subscript" if s[0] == "_" else "superscript"
            body = s[2:-1] if s[1] == "{" else s[1:]
            out.append('<w:r><w:rPr><w:vertAlign w:val="%s"/></w:rPr>'
                       '<w:t xml:space="preserve">%s</w:t></w:r>' % (align, esc(body)))
        else:
            out.append('<w:r><w:rPr><w:i/></w:rPr><w:t xml:space="preserve">%s</w:t></w:r>'
                       % esc(s[1:]))
        i = m.end()
    if i < len(text):
        out.append(runs(text[i:], numbers))
    return "".join(out)


# ---------------------------------------------------------------------------
# Fields
# ---------------------------------------------------------------------------

def field(instr, placeholder="Update this field (Ctrl+A, then F9)."):
    """A Word field code, with a placeholder for the cached result.

    The placeholder is what a reader sees before the field is refreshed;
    ``settings.xml`` sets ``updateFields``, so Word offers to refresh on open.
    """
    return ('<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
            '<w:r><w:instrText xml:space="preserve">%s</w:instrText></w:r>'
            '<w:r><w:fldChar w:fldCharType="separate"/></w:r>'
            '<w:r><w:t xml:space="preserve">%s</w:t></w:r>'
            '<w:r><w:fldChar w:fldCharType="end"/></w:r>'
            % (esc(instr), esc(placeholder)))


def para(style, inner, extra_ppr="", jc=None):
    ppr = ['<w:pStyle w:val="%s"/>' % style]
    if jc:
        ppr.append('<w:jc w:val="%s"/>' % jc)
    if extra_ppr:
        ppr.append(extra_ppr)
    return "<w:p><w:pPr>%s</w:pPr>%s</w:p>" % ("".join(ppr), inner)


def caption(kind, text, numbers):
    """A caption whose number is computed by Word.

    ``STYLEREF 1 \\s`` yields the number of the Heading 1 in force - the chapter -
    and ``SEQ <kind> \\* ARABIC \\s 1`` the running count restarted at each
    chapter, which is what makes "Figure 3.4" survive an insertion earlier in
    the chapter.  The List of Figures is built from the SEQ fields, so the two
    can never disagree.
    """
    return para("Caption",
                '<w:r><w:t xml:space="preserve">%s </w:t></w:r>' % kind
                + field("STYLEREF 1 \\n", "0")
                + '<w:r><w:t>.</w:t></w:r>'
                + field("SEQ %s \\* ARABIC \\s 1" % kind, "0")
                + '<w:r><w:t xml:space="preserve">: </w:t></w:r>'
                + runs(text, numbers),
                jc="center")


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------

BORDER = ('<w:tblBorders>'
          + "".join('<w:%s w:val="single" w:sz="6" w:space="0" w:color="B7C0CC"/>' % e
                    for e in ("top", "left", "bottom", "right", "insideH", "insideV"))
          + '</w:tblBorders>')


def build_table(header, rows, numbers, grid=False, fill=None):
    n = len(header) if header else max(len(r) for r in rows)
    if grid and fill is None:
        widths = [int(CONTENT_W / n)] * n
    elif fill is not None:
        first = int(CONTENT_W * 0.33)
        rest = int((CONTENT_W - first) / (n - 1))
        widths = [first] + [rest] * (n - 1)
    else:
        widths = _proportional(header, rows, n)

    out = ['<w:tbl><w:tblPr><w:tblW w:w="%d" w:type="dxa"/>%s'
           '<w:tblLayout w:type="fixed"/></w:tblPr><w:tblGrid>%s</w:tblGrid>'
           % (CONTENT_W, BORDER,
              "".join('<w:gridCol w:w="%d"/>' % w for w in widths))]

    def cell(text, w, style, shade=None, jc=None):
        shading = ('<w:shd w:val="clear" w:color="auto" w:fill="%s"/>' % shade
                   if shade else "")
        body = "" if text is None else runs(text, numbers)
        return ('<w:tc><w:tcPr><w:tcW w:w="%d" w:type="dxa"/>%s'
                '<w:vAlign w:val="center"/></w:tcPr>%s</w:tc>'
                % (w, shading, para(style, body, jc=jc)))

    if header:
        out.append('<w:tr><w:trPr><w:tblHeader/></w:trPr>')
        for w, c in zip(widths, header):
            out.append(cell(c, w, "TableHead", shade="1F4E79",
                            jc="center" if grid else None))
        out.append("</w:tr>")

    for i, r in enumerate(rows):
        out.append("<w:tr>")
        for j, (w, c) in enumerate(zip(widths, r)):
            if fill is not None and c.strip() == fill:
                out.append(cell(None, w, "TableCell", shade="1F4E79"))
            else:
                jc = "center" if grid and not (fill and j == 0) else None
                shade = None
                if not grid and i % 2 == 1:
                    shade = "F2F5F9"
                out.append(cell(c, w, "TableCell", shade=shade, jc=jc))
        out.append("</w:tr>")
    out.append("</w:tbl>")
    out.append(para("Spacer", ""))
    return "".join(out)


def _proportional(header, rows, n):
    """Column widths from the longest content in each column, damped and floored.

    Straight proportionality to the longest cell collapses a short-label column
    next to a prose one - a four-column cost table gave its first column 7 % of
    the width and broke "Development" one character per line.  Damping the
    demand by a square root pulls the extremes together without making every
    column equal, and the floor is the last line of defence.
    """
    demand = [0] * n
    for r in ([header] if header else []) + rows:
        for i, c in enumerate(r[:n]):
            demand[i] = max(demand[i], len(re.sub(r"[*`]", "", c)))
    # A column must also be wide enough for its longest unbreakable word, or
    # Word hyphen-free-wraps "Community" as "Commu / nity".  Roughly 120 twips
    # per character at the body size, plus cell padding, capped so that one
    # long identifier cannot claim the whole table.
    longest = [0] * n
    for r in ([header] if header else []) + rows:
        for i, c in enumerate(r[:n]):
            for w in re.sub(r"[*`]", "", c).split():
                longest[i] = max(longest[i], len(w))
    damped = [max(d, 1) ** 0.5 for d in demand]
    total = sum(damped) or n
    floors = [max(int(CONTENT_W * 0.08),
                  min(int(CONTENT_W * 0.22), lw * 120 + 240)) for lw in longest]
    widths = [max(f, int(CONTENT_W * d / total)) for f, d in zip(floors, damped)]
    scale = CONTENT_W / sum(widths)
    return [int(w * scale) for w in widths]


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------

_DIAGRAM_METRICS = None


def diagram_metrics():
    """SVG user-unit sizes written beside the PNGs by generate_pdf.py."""
    global _DIAGRAM_METRICS
    if _DIAGRAM_METRICS is None:
        try:
            with open(os.path.join(FIGURE_DIR, DIAGRAM_METRICS),
                      encoding="utf-8") as fh:
                _DIAGRAM_METRICS = json.load(fh)
        except Exception:                              # absent or unparseable
            _DIAGRAM_METRICS = {}
    return _DIAGRAM_METRICS


def diagram_plan(path):
    """``(width_emu, height_emu)`` for a diagram, or None if it is not one."""
    m = diagram_metrics().get(os.path.splitext(os.path.basename(path))[0])
    if not m:
        return None
    pw, ph = m[0] + 2 * SVG_MARGIN, m[1] + 2 * SVG_MARGIN
    if pw <= 0 or ph <= 0:
        return None
    scale = min(CONTENT_W_PT / pw, SOLO_FIG_H_PT / ph,
                DIAGRAM_TEXT_PT / DIAGRAM_FONT_UNITS)
    return int(pw * scale * 12700), int(ph * scale * 12700)


def image_para(rel_id, path, max_h_mm=None):
    from PIL import Image
    plan = diagram_plan(path)
    if plan is not None:
        w, h = plan
        return _picture(rel_id, w, h, w, h)
    with Image.open(path) as im:
        px_w, px_h = im.size
    w = CONTENT_EMU
    h = int(w * px_h / px_w)
    cap = int((max_h_mm or 190) * 36000)
    if h > cap:
        h, w = cap, int(cap * px_w / px_h)
    return _picture(rel_id, w, h, w, h)


def _picture(rel_id, frame_w, frame_h, ext_w, ext_h):
    """One inline picture: *frame* is the space it occupies, *ext* its own size."""
    return para(
        "Figure",
        '<w:r><w:drawing><wp:inline distT="0" distB="0" distL="0" distR="0">'
        '<wp:extent cx="%d" cy="%d"/><wp:docPr id="%d" name="Figure %d"/>'
        '<a:graphic xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
        '<a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">'
        '<pic:pic xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture">'
        '<pic:nvPicPr><pic:cNvPr id="%d" name="Figure %d"/><pic:cNvPicPr/></pic:nvPicPr>'
        '<pic:blipFill><a:blip r:embed="%s"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill>'
        '<pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="%d" cy="%d"/></a:xfrm>'
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr>'
        '</pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing></w:r>'
        % (frame_w, frame_h, _next_id(), _next_id(), _next_id(), _next_id(),
           rel_id, ext_w, ext_h),
        jc="center")


_ID = [1000]


def _next_id():
    _ID[0] += 1
    return _ID[0]


# ---------------------------------------------------------------------------
# Static parts
# ---------------------------------------------------------------------------

NS = ('xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
      'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
      'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
      'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"')

CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Default Extension="png" ContentType="image/png"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
<Override PartName="/word/numbering.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"/>
<Override PartName="/word/settings.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml"/>
<Override PartName="/word/footer2.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml"/>
<Override PartName="/word/footer1.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml"/>
<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
</Types>"""

ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
</Relationships>"""

# updateFields makes Word offer to refresh the TOC / LoF / LoT on open, which
# is the difference between a contents page that is live and one that merely
# looks live.
SETTINGS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:settings xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
<w:updateFields w:val="true"/>
<w:defaultTabStop w:val="720"/>
<w:evenAndOddHeaders w:val="false"/>
</w:settings>"""

BLANK_FOOTER = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:ftr %s><w:p><w:pPr><w:pStyle w:val="Footer"/></w:pPr></w:p></w:ftr>""" % NS

FOOTER = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:ftr %s>
<w:p><w:pPr><w:pStyle w:val="Footer"/><w:jc w:val="center"/></w:pPr>
<w:r><w:fldChar w:fldCharType="begin"/></w:r>
<w:r><w:instrText xml:space="preserve"> PAGE </w:instrText></w:r>
<w:r><w:fldChar w:fldCharType="separate"/></w:r><w:r><w:t>1</w:t></w:r>
<w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>
</w:ftr>""" % NS


def numbering_xml():
    """One multilevel list, bound to Heading 1-3.

    Binding the list to the styles rather than to the paragraphs is what makes
    the numbering survive editing: a section pasted in as Heading 2 acquires the
    right number without anything else being touched.
    """
    levels = []
    for i, fmt in enumerate(("%1", "%1.%2", "%1.%2.%3")):
        levels.append(
            '<w:lvl w:ilvl="%d"><w:start w:val="1"/><w:numFmt w:val="decimal"/>'
            '<w:pStyle w:val="Heading%d"/><w:lvlText w:val="%s"/>'
            '<w:lvlJc w:val="left"/><w:pPr><w:ind w:left="0" w:firstLine="0"/></w:pPr>'
            '</w:lvl>' % (i, i + 1, fmt))
    return ("""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:numbering xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
<w:abstractNum w:abstractNumId="0"><w:multiLevelType w:val="multilevel"/>%s</w:abstractNum>
<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>
</w:numbering>""" % "".join(levels))


def _style(sid, name, based, ppr, rpr, extra=""):
    return ('<w:style w:type="paragraph" w:styleId="%s"><w:name w:val="%s"/>'
            '%s%s<w:pPr>%s</w:pPr><w:rPr>%s</w:rPr></w:style>'
            % (sid, name,
               '<w:basedOn w:val="%s"/>' % based if based else "", extra,
               ppr, rpr))


def styles_xml():
    serif = ('<w:rFonts w:ascii="Times New Roman" w:hAnsi="Times New Roman" '
             'w:cs="Times New Roman"/>')
    sans = ('<w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:cs="Calibri"/>')
    head_num = '<w:numPr><w:ilvl w:val="%d"/><w:numId w:val="1"/></w:numPr>'

    s = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
         '<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">',
         '<w:docDefaults><w:rPrDefault><w:rPr>%s<w:sz w:val="23"/>'
         '<w:szCs w:val="24"/></w:rPr></w:rPrDefault>'
         '<w:pPrDefault><w:pPr><w:spacing w:after="160" w:line="276" '
         'w:lineRule="auto"/></w:pPr></w:pPrDefault></w:docDefaults>' % serif,
         _style("Normal", "Normal", None,
                '<w:jc w:val="both"/><w:spacing w:after="150" w:line="276" w:lineRule="auto"/>',
                serif)]

    # Numbered outline headings — these are what the TOC field reads.
    s.append(_style("Heading1", "heading 1", "Normal",
                    head_num % 0 + '<w:spacing w:before="360" w:after="240"/>'
                    '<w:jc w:val="left"/><w:keepNext/><w:outlineLvl w:val="0"/>',
                    sans + '<w:b/><w:sz w:val="40"/><w:color w:val="1F4E79"/>'))
    s.append(_style("Heading2", "heading 2", "Normal",
                    head_num % 1 + '<w:spacing w:before="280" w:after="140"/>'
                    '<w:jc w:val="left"/><w:keepNext/><w:outlineLvl w:val="1"/>',
                    sans + '<w:b/><w:sz w:val="28"/><w:color w:val="1F4E79"/>'))
    s.append(_style("Heading3", "heading 3", "Normal",
                    head_num % 2 + '<w:spacing w:before="220" w:after="120"/>'
                    '<w:jc w:val="left"/><w:keepNext/><w:outlineLvl w:val="2"/>',
                    sans + '<w:b/><w:sz w:val="24"/><w:color w:val="2E5F8A"/>'))
    # Unnumbered front-matter heading, pulled into the contents by \t.
    s.append(_style("FrontHeading", "Front Heading", "Normal",
                    '<w:spacing w:before="0" w:after="280"/><w:jc w:val="center"/>'
                    '<w:keepNext/><w:outlineLvl w:val="0"/>',
                    sans + '<w:b/><w:sz w:val="36"/><w:color w:val="1F4E79"/>'))
    # Same look as FrontHeading, but outside the outline: this is the contents
    # page's own heading, and a contents page does not list itself.
    s.append(_style("FrontHeadingPlain", "Front Heading Plain", "FrontHeading",
                    '<w:outlineLvl w:val="9"/>', ''))
    s.append(_style("RunIn", "Run-in Heading", "Normal",
                    '<w:spacing w:before="200" w:after="80"/><w:jc w:val="left"/><w:keepNext/>',
                    serif + '<w:b/><w:i/>'))
    # The chapter outline: normal face, normal weight, per the template.
    s.append(_style("ChapterIntro", "Chapter Intro", "Normal",
                    '<w:spacing w:after="240"/>', serif))
    s.append(_style("ChapterLabel", "Chapter Label", "Normal",
                    '<w:jc w:val="center"/><w:spacing w:after="0"/>',
                    sans + '<w:sz w:val="28"/><w:color w:val="6B7686"/>'))
    s.append(_style("Caption", "caption", "Normal",
                    '<w:spacing w:before="80" w:after="200"/><w:jc w:val="center"/><w:keepNext/>',
                    sans + '<w:sz w:val="20"/><w:color w:val="44505F"/>'))
    s.append(_style("Figure", "Figure", "Normal",
                    '<w:spacing w:before="200" w:after="40"/><w:jc w:val="center"/><w:keepNext/>',
                    serif))
    s.append(_style("FigDesc", "Figure Description", "Normal",
                    '<w:spacing w:after="140"/><w:ind w:left="340" w:right="340"/>',
                    serif + '<w:sz w:val="20"/><w:color w:val="44505F"/>'))
    s.append(_style("Equation", "Equation", "Normal",
                    '<w:spacing w:before="140" w:after="40"/><w:jc w:val="center"/>'
                    '<w:tabs><w:tab w:val="right" w:pos="%d"/></w:tabs>' % CONTENT_W,
                    serif))
    s.append(_style("EqWhere", "Equation Where", "Normal",
                    '<w:spacing w:after="200"/><w:ind w:left="340"/>',
                    serif + '<w:sz w:val="20"/><w:color w:val="44505F"/>'))
    s.append(_style("TableHead", "Table Head", "Normal",
                    '<w:spacing w:before="40" w:after="40"/><w:jc w:val="left"/>',
                    sans + '<w:b/><w:sz w:val="19"/><w:color w:val="FFFFFF"/>'))
    s.append(_style("TableCell", "Table Cell", "Normal",
                    '<w:spacing w:before="40" w:after="40"/><w:jc w:val="left"/>',
                    serif + '<w:sz w:val="19"/>'))
    s.append(_style("Code", "Code", "Normal",
                    '<w:spacing w:after="40"/><w:jc w:val="left"/>'
                    '<w:ind w:left="340"/>',
                    '<w:rFonts w:ascii="Consolas" w:hAnsi="Consolas"/><w:sz w:val="19"/>'))
    s.append(_style("Note", "Note", "Normal",
                    '<w:ind w:left="340" w:right="340"/><w:spacing w:before="140" w:after="200"/>'
                    '<w:pBdr><w:left w:val="single" w:sz="18" w:space="8" w:color="D25118"/></w:pBdr>',
                    serif + '<w:sz w:val="21"/>'))
    s.append(_style("ListBullet", "List Bullet", "Normal",
                    '<w:ind w:left="454" w:hanging="227"/><w:spacing w:after="80"/>', serif))
    s.append(_style("Reference", "Reference", "Normal",
                    '<w:ind w:left="567" w:hanging="567"/><w:spacing w:after="120"/>'
                    '<w:jc w:val="left"/>', serif + '<w:sz w:val="21"/>'))
    s.append(_style("TitleLine", "Title Line", "Normal",
                    '<w:jc w:val="center"/><w:spacing w:after="120"/>',
                    sans + '<w:b/><w:sz w:val="44"/><w:color w:val="1F4E79"/>'))
    s.append(_style("TitleMeta", "Title Meta", "Normal",
                    '<w:jc w:val="center"/><w:spacing w:after="80"/>', serif))
    s.append(_style("Spacer", "Spacer", "Normal",
                    '<w:spacing w:after="0" w:line="120" w:lineRule="exact"/>', serif))
    s.append(_style("Footer", "footer", "Normal",
                    '<w:jc w:val="center"/><w:spacing w:after="0"/>',
                    serif + '<w:sz w:val="20"/>'))
    for i in range(1, 4):
        s.append(_style("TOC%d" % i, "toc %d" % i, "Normal",
                        '<w:spacing w:after="60"/><w:jc w:val="left"/>'
                        '<w:ind w:left="%d"/><w:tabs>'
                        '<w:tab w:val="right" w:leader="dot" w:pos="%d"/></w:tabs>'
                        % (280 * (i - 1), CONTENT_W), serif))
    s.append('</w:styles>')
    return "".join(s)


# ---------------------------------------------------------------------------
# Document body
# ---------------------------------------------------------------------------

def sect_pr(fmt, start=None, footer_rel="rId4", title_page=False):
    """A section break carrying its own page-number format.

    The front matter is roman and the body restarts at arabic 1, which is a
    property of the *section* in Word rather than of a page, so the report is
    three sections: title page (no number), front matter (i, ii, ...) and body
    (1, 2, ...).
    """
    pg = '<w:pgNumType w:fmt="%s"%s/>' % (
        fmt, ' w:start="%d"' % start if start else "")
    ftr = "" if footer_rel is None else \
        '<w:footerReference w:type="default" r:id="%s"/>' % footer_rel
    return ('<w:sectPr>%s<w:pgSz w:w="%d" w:h="%d"/>'
            '<w:pgMar w:top="%d" w:right="%d" w:bottom="%d" w:left="%d" '
            'w:header="720" w:footer="720" w:gutter="0"/>%s'
            '<w:cols w:space="720"/><w:docGrid w:linePitch="360"/></w:sectPr>'
            % (ftr, PAGE_W, PAGE_H, MARGIN_T, MARGIN_R, MARGIN_B, MARGIN_L, pg))


PAGEBREAK = ('<w:p><w:pPr><w:spacing w:after="0"/></w:pPr>'
             '<w:r><w:br w:type="page"/></w:r></w:p>')


def number_blocks(lines):
    """Assign figure, table and equation numbers in document order, per chapter.

    Word recomputes these from its own SEQ fields; they are still needed here so
    that a ``{{label}}`` cross-reference in the prose resolves to the same string
    the PDF prints.
    """
    numbers, chapter, fig_n, tab_n, eq_n = {}, "0", 0, 0, 0
    for raw in lines:
        line = raw.strip()
        m = CHAPTER_RE.match(line)
        if m:
            chapter, fig_n, tab_n, eq_n = m.group(1).strip(), 0, 0, 0
            continue
        m = LABEL_RE.match(line) or CHART_LABEL_RE.match(line)
        if not m:
            continue
        label = m.group(m.lastindex)
        if label in numbers:
            raise ValueError("duplicate block label %r" % label)
        upper = line.upper()
        if upper.startswith("@TABLE"):
            tab_n += 1
            numbers[label] = "%s.%d" % (chapter, tab_n)
        elif upper.startswith("@EQ"):
            eq_n += 1
            numbers[label] = "%s.%d" % (chapter, eq_n)
        else:
            fig_n += 1
            numbers[label] = "%s.%d" % (chapter, fig_n)
    return numbers


class Builder:
    def __init__(self, src):
        with open(src, encoding="utf-8") as fh:
            raw = fh.read().split("\n")
        self.lines = [l for l in raw if not l.lstrip().startswith("#")]
        self.numbers = number_blocks(self.lines)
        self.out = []
        self.images = {}          # rel id -> absolute path
        self.missing = []
        self.i = 0
        self.title, self.authors, self.fields = [], [], {}
        self.body_started = False

    # -- helpers ---------------------------------------------------------
    def R(self, t, base=""):
        return runs(t, self.numbers, base)

    def add(self, xml):
        self.out.append(xml)

    def rel_for(self, path):
        for rid, p in self.images.items():
            if p == path:
                return rid
        rid = "rIdImg%d" % (len(self.images) + 1)
        self.images[rid] = path
        return rid

    def collect(self, end, allowed):
        got = []
        while self.i < len(self.lines):
            line = self.lines[self.i].rstrip()
            self.i += 1
            if line.startswith(end):
                return got
            if line.strip() and line.startswith(allowed):
                got.append(line)
        raise ValueError("block never closed with %s" % end)

    # -- emitters --------------------------------------------------------
    def front_head(self, text, style="FrontHeading"):
        if not self.out or self.out[-1] != PAGEBREAK:
            self.add(PAGEBREAK)
        self.add(para(style, self.R(text)))

    def chapter(self, num, title):
        if not self.body_started:
            # Close the roman front matter and restart page numbering at 1.
            self.add('<w:p><w:pPr><w:spacing w:after="0"/><w:sectPr>'
                     + sect_pr("lowerRoman", 1)[len("<w:sectPr>"):-len("</w:sectPr>")]
                     + '</w:sectPr></w:pPr></w:p>')
            self.body_started = True
        else:
            self.add(PAGEBREAK)
        self.add(para("ChapterLabel", self.R("Chapter %s" % num)))
        self.add(para("Heading1", self.R(title)))

    def table(self, line):
        p = [c.strip() for c in line.split("|")]
        label, cap = p[1], p[2]
        header, rows, tstyle = None, [], ""
        for raw in self.collect("@ENDTABLE", ("@TH|", "@TR|", "@TSTYLE|")):
            if raw.startswith("@TSTYLE|"):
                tstyle = raw.split("|", 1)[1].strip().lower()
                continue
            cells = [c.strip() for c in raw.split("|")[1:]]
            if raw.startswith("@TH|"):
                header = cells
            else:
                rows.append(cells)
        self.add(caption("Table", cap, self.numbers))
        self.add(build_table(header, rows, self.numbers,
                             grid=tstyle in ("grid", "gantt"),
                             fill="X" if tstyle == "gantt" else None))

    def figure(self, line, chart=False):
        p = [c.strip() for c in line.split("|")]
        label, cap = (p[2], p[3]) if chart else (p[1], p[2])
        desc, read, note, height = [], [], [], None
        end = "@ENDCHART" if chart else "@ENDFIGURE"
        for raw in self.collect(end, ("@FIGDESC|", "@FIGREAD|", "@FIGNOTE|",
                                      "@FIGHEIGHT|",
                                      "@CHARTHEIGHT|", "@LABELS|", "@SERIES|",
                                      "@YLABEL|")):
            key, _, val = raw.partition("|")
            if key == "@FIGDESC":
                desc.append(val)
            elif key == "@FIGREAD":
                read.append(val)
            elif key == "@FIGNOTE":
                note.append(val)
            elif key in ("@FIGHEIGHT", "@CHARTHEIGHT"):
                height = float(val)
        path = os.path.join(FIGURE_DIR, "fig-%s.png" % label.split(":", 1)[1])
        if os.path.exists(path):
            self.add(image_para(self.rel_for(path), path, height))
        else:
            self.missing.append(label)
            self.add(para("Figure", self.R("[missing image: %s]"
                                           % os.path.basename(path)), jc="center"))
        self.add(caption("Figure", cap, self.numbers))
        for t in read:
            self.add(para("FigDesc", self.R("How to read it.", base="b")
                          + self.R(" " + t)))
        # @FIGNOTE opens with its own figure reference, so it takes no bold lead
        for t in note:
            self.add(para("FigDesc", self.R(t)))
        lead = "Reading the chart." if chart else "Figure note."
        for t in desc:
            self.add(para("FigDesc", self.R(lead, base="b") + self.R(" " + t)))

    def equation(self, line):
        _, label, body = line.split("|", 2)
        number = self.numbers[label.strip()]
        where = [l.split("|", 1)[1] for l in self.collect("@ENDEQ", ("@WHERE|",))]
        self.add(para("Equation",
                      math_runs(body.strip())
                      + '<w:r><w:tab/><w:t>(%s)</w:t></w:r>' % esc(number)))
        for w in where:
            self.add(para("EqWhere", math_prose(w, self.numbers)))

    # -- main loop -------------------------------------------------------
    def run(self):
        while self.i < len(self.lines):
            line = self.lines[self.i].rstrip()
            self.i += 1
            if not line.strip():
                continue
            key, _, rest = line.partition("|")
            p = [c.strip() for c in line.split("|")]

            if key == "@TITLE":
                self.title.append(rest)
            elif key == "@AUTHOR":
                self.authors.append((p[1], p[2] if len(p) > 2 else ""))
            elif key == "@FIELD":
                self.fields[p[1]] = p[2] if len(p) > 2 else ""
            elif key == "@FRONT":
                self.front_head(p[2] if len(p) > 2 else p[1].title())
            elif key == "@CHAPTER":
                self.chapter(p[1], p[2])
            elif key == "@INTRO":
                self.add(para("ChapterIntro", self.R(rest)))
            elif key == "@H2":
                self.add(para("Heading2", self.R(HEADNUM_RE.sub("", rest.strip()))))
            elif key == "@H3":
                self.add(para("Heading3", self.R(HEADNUM_RE.sub("", rest.strip()))))
            elif key == "@H4":
                self.add(para("RunIn", self.R(rest)))
            elif key == "@P":
                self.add(para("Normal", self.R(rest)))
            elif key in ("@BULLET", "@NUM"):
                self.add(para("ListBullet",
                              '<w:r><w:t xml:space="preserve">•  </w:t></w:r>'
                              + self.R(rest) if key == "@BULLET" else self.R(rest)))
            elif key == "@NOTE":
                self.add(para("Note", self.R(rest)))
            elif key == "@CODE":
                self.add(para("Code", self.R(rest)))
            elif key == "@SIGN":
                parts = [c for c in p[1:] if c]
                inner = "<w:r><w:br/></w:r>".join(self.R(c) for c in parts)
                self.add(para("TitleMeta", inner, jc="left"))
            elif key == "@SPACE":
                self.add(para("Spacer", ""))
            elif key == "@PAGEBREAK":
                self.add(PAGEBREAK)
            elif key == "@TOC":
                # Styled apart so that the contents page does not list itself.
                self.front_head("Table of Contents", "FrontHeadingPlain")
                self.add(para("Normal", field(
                    ' TOC \\o "1-3" \\h \\z \\u \\t "FrontHeading,1" ',
                    "The table of contents is a field: select all (Ctrl+A) and "
                    "press F9 to build it.")))
            elif key == "@LOF":
                self.front_head("List of Figures")
                self.add(para("Normal", field(
                    ' TOC \\h \\z \\c "Figure" ',
                    "The list of figures is a field: select all (Ctrl+A) and "
                    "press F9 to build it.")))
            elif key == "@LOT":
                self.front_head("List of Tables")
                self.add(para("Normal", field(
                    ' TOC \\h \\z \\c "Table" ',
                    "The list of tables is a field: select all (Ctrl+A) and "
                    "press F9 to build it.")))
            elif key == "@TABLE":
                self.table(line)
            elif key == "@FIGURE":
                self.figure(line)
            elif key == "@CHART":
                self.figure(line, chart=True)
            elif key == "@EQ":
                self.equation(line)
            elif key == "@REFS":
                self.add(PAGEBREAK)
                self.add(para("FrontHeading", self.R("References")))
                self.add(para("Normal", self.R(
                    "References follow the IEEE citation style. Every entry "
                    "carries a Digital Object Identifier that was resolved "
                    "against the Crossref or DataCite registry at the time of "
                    "writing.")))
            elif key == "@REF":
                self.add(para("Reference", self.R(rest)))
        return self

    def title_page(self):
        out = [para("Spacer", "")]
        for t in self.title:
            out.append(para("TitleLine", self.R(t)))
        out.append(para("TitleMeta", self.R("**By**")))
        for n, sid in self.authors:
            out.append(para("TitleMeta", self.R("**%s**" % n)))
            out.append(para("TitleMeta", self.R("ID: %s" % sid)))
        out.append(para("TitleMeta", self.R("**FINAL YEAR DESIGN PROJECT REPORT**")))
        for k in ("degree", "supervised_by", "cosupervised_by"):
            if self.fields.get(k):
                if k != "degree":
                    out.append(para("TitleMeta", self.R(
                        "**%s**" % ("Supervised by" if k == "supervised_by"
                                    else "Co-Supervised by"))))
                for part in self.fields[k].split(";"):
                    out.append(para("TitleMeta", self.R(part.strip())))
        for k in ("university", "city", "date"):
            if self.fields.get(k):
                out.append(para("TitleMeta", self.R("**%s**" % self.fields[k])))
        # Title page carries no page number; the front matter that follows is roman.
        out.append('<w:p><w:pPr><w:spacing w:after="0"/><w:sectPr>'
                   + sect_pr("decimal", footer_rel="rId5")[len("<w:sectPr>"):-len("</w:sectPr>")]
                   + '</w:sectPr></w:pPr></w:p>')
        return "".join(out)


def document_xml(b):
    body = b.title_page() + "".join(b.out) + sect_pr("decimal", 1)
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<w:document %s><w:body>%s</w:body></w:document>' % (NS, body))


def main(argv):
    src = argv[1] if len(argv) > 1 else os.path.join(HERE, "final_paper.txt")
    dst = argv[2] if len(argv) > 2 else os.path.join(HERE, "final_paper.docx")
    if not os.path.exists(src):
        sys.exit("source not found: %s" % src)

    b = Builder(src).run()

    rels = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">',
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>',
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/numbering" Target="numbering.xml"/>',
            '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/settings" Target="settings.xml"/>',
            '<Relationship Id="rId4" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/footer" Target="footer1.xml"/>',
            '<Relationship Id="rId5" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/footer" Target="footer2.xml"/>']
    media = {}
    for n, (rid, path) in enumerate(sorted(b.images.items()), 1):
        name = "media/image%d.png" % n
        media[name] = path
        rels.append('<Relationship Id="%s" Type="http://schemas.openxmlformats.org/'
                    'officeDocument/2006/relationships/image" Target="%s"/>' % (rid, name))
    rels.append("</Relationships>")

    core = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/">'
            '<dc:title>%s</dc:title></cp:coreProperties>'
            % esc(" ".join(b.title)))

    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", CONTENT_TYPES)
        z.writestr("_rels/.rels", ROOT_RELS)
        z.writestr("docProps/core.xml", core)
        z.writestr("word/document.xml", document_xml(b))
        z.writestr("word/styles.xml", styles_xml())
        z.writestr("word/numbering.xml", numbering_xml())
        z.writestr("word/settings.xml", SETTINGS)
        z.writestr("word/footer1.xml", FOOTER)
        z.writestr("word/footer2.xml", BLANK_FOOTER)
        z.writestr("word/_rels/document.xml.rels", "".join(rels))
        for name, path in media.items():
            z.write(path, "word/" + name)

    if b.missing:
        print("  ! %d figure PNG(s) missing — run generate_pdf.py --render-charts"
              % len(b.missing))
        for m in b.missing:
            print("      %s" % m)
    print("wrote %s  (%.1f KB)" % (dst, os.path.getsize(dst) / 1024.0))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
