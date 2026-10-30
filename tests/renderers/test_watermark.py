"""Draft watermarks: stamped on every format, deterministic, and absent unless asked.

A caller keeps a render's sha256 as evidence and regenerates it later to check
it, so a stamped draft must be as reproducible as an unstamped final — and an
unstamped render must not change because the feature exists.
"""

from __future__ import annotations

import io
import zipfile

import pytest
from pypdf import PdfReader

from adapters.renderers._watermark import pdf_stamp_content, stamp_html
from adapters.renderers.docx import DocxRenderer
from adapters.renderers.jinja_html_pdf import JinjaHtmlRenderer
from adapters.renderers.pdf_form import PdfFormRenderer
from adapters.renderers.xlsx import XlsxRenderer
from domain.models import OutputFormat

_MARK = "BROUILLON"


# -- pdf-form ----------------------------------------------------------------

_PDF_DATA = {"community_name": "Communauté ACME", "agreed": True}


def _pdf(bundle, watermark=None):
    return (
        PdfFormRenderer()
        .render(bundle, _PDF_DATA, OutputFormat.PDF, locale=None, watermark=watermark)
        .content
    )


def test_pdf_form_draft_shows_the_watermark_on_every_page(pdf_form_bundle):
    reader = PdfReader(io.BytesIO(_pdf(pdf_form_bundle, _MARK)))
    assert all(_MARK in page.extract_text() for page in reader.pages)


def test_pdf_form_draft_keeps_its_fields_filled_and_fillable(pdf_form_bundle):
    fields = PdfReader(io.BytesIO(_pdf(pdf_form_bundle, _MARK))).get_fields() or {}
    assert fields["Champ de texte 1"]["/V"] == "Communauté ACME"
    assert fields["Case a cocher 1"]["/V"] == "/Oui"


def test_pdf_form_draft_is_deterministic(pdf_form_bundle):
    assert _pdf(pdf_form_bundle, _MARK) == _pdf(pdf_form_bundle, _MARK)


def test_pdf_form_without_watermark_is_the_plain_render(pdf_form_bundle):
    plain = _pdf(pdf_form_bundle)
    assert _MARK not in PdfReader(io.BytesIO(plain)).pages[0].extract_text()
    assert plain != _pdf(pdf_form_bundle, _MARK)


def test_pdf_stamp_escapes_the_literal_and_pins_number_formats():
    content = pdf_stamp_content(595, 842, 0, 0, "BROUILLON (v2) \\")
    assert b"(BROUILLON \\(v2\\) \\\\) Tj" in content
    # Fixed precision: the operators never depend on float repr.
    assert content == pdf_stamp_content(595.0, 842.0, 0.0, 0.0, "BROUILLON (v2) \\")


# -- docx --------------------------------------------------------------------

_DOCX_DATA = {"community_name": "Communauté ACME", "representative": "Alice Dupont"}


def _docx(bundle, watermark=None):
    return (
        DocxRenderer()
        .render(bundle, _DOCX_DATA, OutputFormat.DOCX, locale=None, watermark=watermark)
        .content
    )


def _parts(content: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def test_docx_draft_carries_words_watermark_in_a_header(docx_bundle):
    parts = _parts(_docx(docx_bundle, _MARK))
    headers = [body for name, body in parts.items() if name.startswith("word/header")]
    assert headers, "the template has no header: the renderer must add one"
    assert all(b'string="BROUILLON"' in body for body in headers)
    assert b"<cp:contentStatus>BROUILLON</cp:contentStatus>" in parts["docProps/core.xml"]


def test_docx_draft_leaves_the_body_untouched(docx_bundle):
    plain, draft = _parts(_docx(docx_bundle)), _parts(_docx(docx_bundle, _MARK))
    assert (
        draft["word/document.xml"].split(b"<w:sectPr")[0]
        == (plain["word/document.xml"].split(b"<w:sectPr")[0])
    )


def test_docx_draft_is_deterministic(docx_bundle):
    assert _docx(docx_bundle, _MARK) == _docx(docx_bundle, _MARK)


def test_docx_watermark_text_is_xml_escaped(docx_bundle):
    parts = _parts(_docx(docx_bundle, 'A & "B"'))
    header = next(body for name, body in parts.items() if name.startswith("word/header"))
    assert b'string="A &amp; &quot;B&quot;"' in header


# -- xlsx --------------------------------------------------------------------

_ROWS = [{"nom": "Dupont", "ean": "541234567890123456", "localite": "Liège"}]


def _xlsx(bundle, watermark=None):
    return (
        XlsxRenderer()
        .render(
            bundle, {"participants": _ROWS}, OutputFormat.XLSX, locale=None, watermark=watermark
        )
        .content
    )


def test_xlsx_draft_marks_every_sheets_header_footer_and_status(xlsx_block_bundle):
    from openpyxl import load_workbook

    workbook = load_workbook(io.BytesIO(_xlsx(xlsx_block_bundle, _MARK)))
    for sheet in workbook.worksheets:
        assert _MARK in sheet.oddHeader.center.text
        assert _MARK in sheet.oddFooter.center.text
    assert workbook.properties.contentStatus == _MARK


def test_xlsx_draft_leaves_the_cells_alone(xlsx_block_bundle):
    from openpyxl import load_workbook

    def cells(content: bytes) -> list[tuple]:
        sheet = load_workbook(io.BytesIO(content)).active
        return [tuple(cell.value for cell in row) for row in sheet.iter_rows()]

    assert cells(_xlsx(xlsx_block_bundle, _MARK)) == cells(_xlsx(xlsx_block_bundle))


def test_xlsx_draft_is_deterministic(xlsx_block_bundle):
    assert _xlsx(xlsx_block_bundle, _MARK) == _xlsx(xlsx_block_bundle, _MARK)


def test_xlsx_ampersand_is_not_read_as_a_control_code(xlsx_block_bundle):
    from openpyxl import load_workbook

    workbook = load_workbook(io.BytesIO(_xlsx(xlsx_block_bundle, "A & B")))
    assert workbook.active.oddHeader.center.text == "A && B"


# -- jinja-html --------------------------------------------------------------

_HTML_DATA = {
    "invoice_number": "INV-001",
    "customer_name": "ACME Corp",
    "total": 1234.5,
    "currency": "EUR",
}


def test_html_draft_carries_the_mark_before_the_body_ends(jinja_bundle):
    html = (
        JinjaHtmlRenderer()
        .render(jinja_bundle, _HTML_DATA, OutputFormat.HTML, locale=None, watermark=_MARK)
        .content.decode("utf-8")
    )
    assert html.index(_MARK) < html.lower().rindex("</body>")


def test_html_watermark_is_escaped():
    assert "&lt;b&gt;" in stamp_html("<html><body></body></html>", "<b>")
    assert stamp_html("no body", "X").startswith("no body")


def _weasyprint_available() -> bool:
    import importlib

    try:
        importlib.import_module("weasyprint")
    except Exception:
        return False
    return True


@pytest.mark.skipif(not _weasyprint_available(), reason="WeasyPrint native libs absent")
def test_jinja_pdf_draft_shows_the_watermark(jinja_bundle):
    def render(watermark):
        return (
            JinjaHtmlRenderer()
            .render(jinja_bundle, _HTML_DATA, OutputFormat.PDF, locale=None, watermark=watermark)
            .content
        )

    draft = render(_MARK)
    assert _MARK in PdfReader(io.BytesIO(draft)).pages[0].extract_text()
    assert draft == render(_MARK)


# -- the OOXML repack ----------------------------------------------------------


@pytest.mark.parametrize("fixture", ["xlsx_block_bundle", "docx_bundle"])
def test_ooxml_members_are_written_as_unix_on_every_host(fixture, request):
    bundle = request.getfixturevalue(fixture)
    renderer = XlsxRenderer() if fixture.startswith("xlsx") else DocxRenderer()
    data = {"participants": _ROWS} if fixture.startswith("xlsx") else _DOCX_DATA
    fmt = OutputFormat.XLSX if fixture.startswith("xlsx") else OutputFormat.DOCX
    content = renderer.render(bundle, data, fmt, locale=None).content
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        assert {info.create_system for info in archive.infolist()} == {3}
