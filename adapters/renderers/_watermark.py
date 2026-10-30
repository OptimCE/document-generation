"""Stamping a draft watermark onto each output format.

A caller asks for one through ``RequestOptions.watermark`` (an administrative
draft is stamped "BROUILLON", say). The stamp is added by the renderer, never by
the template, so a template cannot forget it and a regulator's own file needs no
edit to carry it. Each helper is a pure function of (document, text): no
wall-clock and no randomness, so a stamped render is as reproducible as an
unstamped one. With no watermark the renderers do not call these at all, and the
output is exactly the unstamped document.

What "visible" can mean differs per format:

* **PDF** (``pdf-form``): a translucent diagonal text drawn over every page's
  content. Form widgets are annotations, which viewers draw above page content,
  so the fields stay readable and fillable.
* **HTML / PDF** (``jinja-html``): a fixed-position element, which WeasyPrint
  repeats on every page.
* **DOCX**: Word's own text watermark (a VML WordArt shape in the page header),
  added to every header the sections use. Word and LibreOffice both show it.
* **XLSX**: Excel has no on-screen watermark short of editing the workbook's
  cells, and the regulator's workbooks are imposed formats. So the stamp goes in
  every sheet's print header and footer, plus the document's status property.
  The caller is expected to mark the file name too.
"""

from __future__ import annotations

import html as html_lib
import math
from typing import Any

# -- PDF ---------------------------------------------------------------------

# Names chosen to be unlikely to collide with a page's own resources.
_PDF_FONT = "/OptimCEWatermarkFont"
_PDF_GSTATE = "/OptimCEWatermarkGS"
_PDF_OPACITY = 0.18
# Helvetica-Bold upper case averages ~0.7 em per character; close enough to
# centre the text, which is all the estimate is for.
_PDF_EM_PER_CHAR = 0.7
_PDF_MAX_FONT_SIZE = 110.0


def _pdf_literal(text: str) -> bytes:
    """``text`` as a PDF literal string in WinAnsi (cp1252), escaped."""
    raw = text.encode("cp1252", errors="replace")
    escaped = raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")
    return b"(" + escaped + b")"


def _pdf_number(value: float) -> bytes:
    """Fixed precision, so the content stream's bytes never depend on float repr."""
    return f"{value:.4f}".encode()


def pdf_stamp_content(width: float, height: float, left: float, bottom: float, text: str) -> bytes:
    """The content-stream operators drawing ``text`` across one page's diagonal."""
    angle = math.atan2(height, width)
    diagonal = math.hypot(width, height)
    size = min(_PDF_MAX_FONT_SIZE, 0.7 * diagonal / (_PDF_EM_PER_CHAR * max(len(text), 1)))
    text_width = _PDF_EM_PER_CHAR * size * len(text)
    cos, sin = math.cos(angle), math.sin(angle)
    centre_x, centre_y = left + width / 2, bottom + height / 2
    # Start so the baseline's midpoint (raised by ~a third of the size, for the
    # cap height) sits on the page centre.
    start_x = centre_x - (text_width / 2) * cos + (size * 0.35) * sin
    start_y = centre_y - (text_width / 2) * sin - (size * 0.35) * cos
    matrix = b" ".join(_pdf_number(v) for v in (cos, sin, -sin, cos, start_x, start_y))
    return b"".join(
        [
            b"q\n",
            _PDF_GSTATE.encode() + b" gs\n",
            b"0.6 0 0 rg\n",
            b"BT\n",
            _PDF_FONT.encode() + b" " + _pdf_number(size) + b" Tf\n",
            matrix + b" Tm\n",
            _pdf_literal(text) + b" Tj\n",
            b"ET\n",
            b"Q\n",
        ]
    )


def stamp_pdf_writer(writer: Any, text: str) -> None:
    """Draw ``text`` over every page of a pypdf ``PdfWriter``, in place.

    The page's own content is wrapped in ``q``/``Q`` first, so a page that
    leaves its graphics state changed (a stray transform, say) cannot move or
    hide the stamp. Streams must be indirect objects in a PDF, hence the
    writer's ``_add_object`` (pypdf has no public equivalent; the pinned /ID
    already reaches into the writer the same way).
    """
    from pypdf.generic import (
        ArrayObject,
        DecodedStreamObject,
        DictionaryObject,
        FloatObject,
        IndirectObject,
        NameObject,
    )

    def _stream(data: bytes) -> Any:
        stream = DecodedStreamObject()
        stream.set_data(data)
        return writer._add_object(stream)

    font = writer._add_object(
        DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica-Bold"),
                NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
            }
        )
    )
    gstate = writer._add_object(
        DictionaryObject(
            {
                NameObject("/Type"): NameObject("/ExtGState"),
                NameObject("/ca"): FloatObject(_PDF_OPACITY),
                NameObject("/CA"): FloatObject(_PDF_OPACITY),
            }
        )
    )

    for page in writer.pages:
        # Indexing a pypdf dictionary resolves indirect references, so these
        # writes reach a resource dictionary the pages share, once, by name.
        if "/Resources" not in page:
            page[NameObject("/Resources")] = DictionaryObject()
        resources = page["/Resources"]
        for category, name, ref in (
            ("/Font", _PDF_FONT, font),
            ("/ExtGState", _PDF_GSTATE, gstate),
        ):
            if category not in resources:
                resources[NameObject(category)] = DictionaryObject()
            resources[category][NameObject(name)] = ref

        box = page.mediabox
        stamp = pdf_stamp_content(
            float(box.width), float(box.height), float(box.left), float(box.bottom), text
        )
        parts: list[Any] = []
        if "/Contents" in page:
            raw = page.raw_get("/Contents")
            resolved = raw.get_object()
            if isinstance(resolved, ArrayObject):
                parts = list(resolved)
            elif isinstance(raw, IndirectObject):
                parts = [raw]
            else:  # a direct stream: legal on a page, not inside an array
                parts = [writer._add_object(resolved)]
        page[NameObject("/Contents")] = ArrayObject(
            [_stream(b"q\n"), *parts, _stream(b"\nQ\n" + stamp)]
        )


# -- HTML --------------------------------------------------------------------

_HTML_MARK = (
    '<div aria-hidden="true" style="position:fixed;top:42%;left:-15%;width:130%;'
    "text-align:center;transform:rotate(-35deg);font:bold 96px sans-serif;"
    'color:rgba(170,0,0,0.15);z-index:2147483647;pointer-events:none">{text}</div>'
)


def stamp_html(document: str, text: str) -> str:
    """Insert a fixed watermark element just before ``</body>`` (or at the end)."""
    mark = _HTML_MARK.format(text=html_lib.escape(text))
    position = document.lower().rfind("</body>")
    if position < 0:
        return document + mark
    return document[:position] + mark + document[position:]


# -- DOCX --------------------------------------------------------------------

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_V = "urn:schemas-microsoft-com:vml"
_O = "urn:schemas-microsoft-com:office:office"

# Word's own WordArt "plain text" shape type (o:spt 136), as Word writes it for
# Design > Watermark. Kept verbatim: Word draws the textpath from these formulas.
_DOCX_SHAPETYPE = (
    '<v:shapetype id="_x0000_t136" coordsize="21600,21600" o:spt="136" adj="10800" '
    'path="m@7,l@8,m@5,21600l@6,21600e">'
    "<v:formulas>"
    '<v:f eqn="sum #0 0 10800"/><v:f eqn="prod #0 2 1"/><v:f eqn="sum 21600 0 @1"/>'
    '<v:f eqn="sum 0 0 @2"/><v:f eqn="sum 21600 0 @3"/><v:f eqn="if @0 @3 0"/>'
    '<v:f eqn="if @0 21600 @1"/><v:f eqn="if @0 0 @2"/><v:f eqn="if @0 @4 21600"/>'
    '<v:f eqn="mid @5 @6"/><v:f eqn="mid @8 @5"/><v:f eqn="mid @7 @8"/>'
    '<v:f eqn="mid @6 @7"/><v:f eqn="sum @6 0 @5"/>'
    "</v:formulas>"
    '<v:path textpathok="t" o:connecttype="custom" '
    'o:connectlocs="@9,0;@10,10800;@11,21600;@12,10800" o:connectangles="270,180,90,0"/>'
    '<v:textpath on="t" fitshape="t"/>'
    '<v:handles><v:h position="#0,bottomRight" xrange="6629,14971"/></v:handles>'
    '<o:lock v:ext="edit" text="t" shapetype="t"/>'
    "</v:shapetype>"
)

_DOCX_PARAGRAPH = (
    f'<w:p xmlns:w="{_W}" xmlns:v="{_V}" xmlns:o="{_O}">'
    "<w:r><w:rPr><w:noProof/></w:rPr><w:pict>"
    "{shapetype}"
    '<v:shape id="OptimCEWatermark{index}" o:spid="_x0000_s{spid}" type="#_x0000_t136" '
    'style="position:absolute;margin-left:0;margin-top:0;width:468pt;height:117pt;'
    "rotation:315;z-index:-251654144;mso-position-horizontal:center;"
    "mso-position-horizontal-relative:margin;mso-position-vertical:center;"
    'mso-position-vertical-relative:margin" o:allowincell="f" fillcolor="#c00000" '
    'stroked="f"><v:fill opacity=".2"/>'
    '<v:textpath style="font-family:&quot;Arial&quot;;font-size:1pt" string="{text}"/>'
    "</v:shape></w:pict></w:r></w:p>"
)


def stamp_docx(document: Any, text: str) -> None:
    """Add Word's text watermark to every header a python-docx ``Document`` shows.

    A section without a header of its own gets one (the regulator's contracts
    have none). The first-page and even-page headers are stamped too when the
    section or the document uses them, or those pages would print unmarked.
    """
    from docx.oxml import parse_xml

    even_pages = bool(document.settings.odd_and_even_pages_header_footer)
    headers: list[Any] = []
    for section in document.sections:
        candidates = [section.header]
        if section.different_first_page_header_footer:
            candidates.append(section.first_page_header)
        if even_pages:
            candidates.append(section.even_page_header)
        for header in candidates:
            if header.is_linked_to_previous:
                header.is_linked_to_previous = False
            if all(header._element is not seen._element for seen in headers):
                headers.append(header)

    escaped = html_lib.escape(text, quote=True)
    for index, header in enumerate(headers):
        paragraph = _DOCX_PARAGRAPH.format(
            # The shape type is declared once per header part, as Word does.
            shapetype=_DOCX_SHAPETYPE,
            index=index + 1,
            spid=2049 + index,
            text=escaped,
        )
        header._element.append(parse_xml(paragraph))
    document.core_properties.content_status = text


# -- XLSX --------------------------------------------------------------------

_XLSX_COLOR = "C00000"
_XLSX_FONT = "Arial,Bold"
_XLSX_SIZE = 20


def stamp_workbook(workbook: Any, text: str) -> None:
    """Put ``text`` in every sheet's print header and footer, and the status property.

    The cells are left alone: the workbook is the regulator's imposed format.
    A sheet's own header text, if any, is kept after the stamp.
    """
    # "&" starts a header/footer control code in Excel; a literal one is "&&".
    code = text.replace("&", "&&")
    for sheet in workbook.worksheets:
        settings = sheet.HeaderFooter
        parts = [sheet.oddHeader, sheet.oddFooter]
        if settings.differentOddEven:
            parts += [sheet.evenHeader, sheet.evenFooter]
        if settings.differentFirst:
            parts += [sheet.firstHeader, sheet.firstFooter]
        for part in parts:
            centre = part.center
            centre.text = code if not centre.text else f"{code} {centre.text}"
            centre.font = _XLSX_FONT
            centre.size = _XLSX_SIZE
            centre.color = _XLSX_COLOR
    workbook.properties.contentStatus = text
