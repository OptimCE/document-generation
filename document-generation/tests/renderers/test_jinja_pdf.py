"""Jinja-HTML / WeasyPrint renderer tests.

The HTML path runs everywhere. The PDF path needs WeasyPrint's native libraries
(pango/cairo/gdk-pixbuf); where they are absent (e.g. a bare Windows dev host)
the PDF test skips via ``importorskip`` — it still runs in the Linux container.
"""

from __future__ import annotations

import io

import pytest

from adapters.renderers.jinja_html_pdf import JinjaHtmlRenderer
from domain.errors import RenderError
from domain.models import OutputFormat


def _weasyprint_available() -> bool:
    # WeasyPrint raises OSError (not ImportError) at import when its native libs
    # are missing, so importorskip can't gate it — probe explicitly.
    import importlib

    try:
        importlib.import_module("weasyprint")
    except Exception:
        return False
    return True


_DATA = {
    "invoice_number": "INV-001",
    "customer_name": "ACME Corp",
    "total": 1234.5,
    "currency": "EUR",
}


def _render(bundle, fmt, data=None, locale="fr-BE"):
    return JinjaHtmlRenderer().render(bundle, data or _DATA, fmt, locale=locale)


def test_html_render_contains_data(jinja_bundle):
    artifact = _render(jinja_bundle, OutputFormat.HTML)
    html = artifact.content.decode("utf-8")

    assert artifact.filename == "invoice.html"
    assert "Invoice INV-001" in html
    assert "ACME Corp" in html
    assert "1234.5 EUR" in html
    assert 'lang="fr-BE"' in html


def test_missing_field_raises_render_error(jinja_bundle):
    # StrictUndefined: a missing field is a hard error, not a silent blank.
    incomplete = {"customer_name": "ACME Corp", "total": 1, "currency": "EUR"}
    with pytest.raises(RenderError):
        _render(jinja_bundle, OutputFormat.HTML, data=incomplete)


def test_wrong_format_raises_render_error(jinja_bundle):
    with pytest.raises(RenderError):
        _render(jinja_bundle, OutputFormat.XLSX)


@pytest.mark.skipif(
    not _weasyprint_available(),
    reason="WeasyPrint native deps not installed (e.g. bare Windows host)",
)
def test_pdf_render_produces_pdf_with_expected_text(jinja_bundle):
    pypdf = pytest.importorskip("pypdf")

    artifact = _render(jinja_bundle, OutputFormat.PDF)

    assert artifact.filename == "invoice.pdf"
    assert artifact.content[:5] == b"%PDF-"  # valid PDF header
    reader = pypdf.PdfReader(io.BytesIO(artifact.content))
    text = "".join(page.extract_text() for page in reader.pages)
    assert "INV-001" in text
    assert "ACME" in text
