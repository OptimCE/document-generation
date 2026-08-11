"""pdf-form renderer tests: filling an official AcroForm rather than authoring one."""

from __future__ import annotations

import dataclasses
import io

import pytest
from pypdf import PdfReader

from adapters.renderers.pdf_form import PdfFormRenderer
from domain.errors import RenderError, TemplateNotFoundError
from domain.models import OutputFormat

_DATA = {"community_name": "Communauté ACME", "agreed": True}


def _render(bundle, data=None, fmt=OutputFormat.PDF):
    return PdfFormRenderer().render(bundle, _DATA if data is None else data, fmt, locale=None)


def _fields(content: bytes) -> dict:
    return PdfReader(io.BytesIO(content)).get_fields() or {}


def test_render_fills_text_and_checkbox(pdf_form_bundle):
    artifact = _render(pdf_form_bundle)

    assert artifact.format is OutputFormat.PDF
    assert artifact.filename == "declaration.pdf"
    fields = _fields(artifact.content)
    assert fields["Champ de texte 1"]["/V"] == "Communauté ACME"
    # The export state comes from the PDF itself, not from the manifest.
    assert fields["Case a cocher 1"]["/V"] == "/Oui"


def test_falsy_checkbox_selects_off(pdf_form_bundle):
    artifact = _render(pdf_form_bundle, {"community_name": "X", "agreed": False})
    assert _fields(artifact.content)["Case a cocher 1"]["/V"] == "/Off"


@pytest.mark.parametrize("raw", ["oui", "TRUE", "x", "1", "on"])
def test_string_checkbox_values_are_understood(pdf_form_bundle, raw):
    """JSON payloads routinely carry booleans as strings; a wrong read is silent."""
    artifact = _render(pdf_form_bundle, {"community_name": "X", "agreed": raw})
    assert _fields(artifact.content)["Case a cocher 1"]["/V"] == "/Oui"


def test_unsupplied_field_is_left_alone(pdf_form_bundle):
    """An absent key must not blank the form's own default."""
    artifact = _render(pdf_form_bundle, {"community_name": "Only this"})
    assert _fields(artifact.content)["Champ de texte 1"]["/V"] == "Only this"


def test_need_appearances_is_set(pdf_form_bundle):
    """Without it, Chrome/Preview render a filled field as blank."""
    reader = PdfReader(io.BytesIO(_render(pdf_form_bundle).content))
    # pypdf returns a BooleanObject, not the True singleton.
    assert bool(reader.trailer["/Root"]["/AcroForm"]["/NeedAppearances"]) is True


def test_render_bytes_are_deterministic(pdf_form_bundle):
    # The artifact sha256 is the document's identity in document_version, so a
    # wall-clock date or a random /ID leaking into the bytes would be a defect.
    assert _render(pdf_form_bundle).content == _render(pdf_form_bundle).content


def test_unknown_acroform_field_is_permanent(pdf_form_bundle):
    """A manifest naming a field the PDF lacks is a template bug, not a retry."""
    broken = dataclasses.replace(
        pdf_form_bundle,
        manifest=pdf_form_bundle.manifest.model_copy(
            update={"fields": {"community_name": "No Such Field"}}
        ),
    )
    with pytest.raises(TemplateNotFoundError):
        PdfFormRenderer().render(broken, _DATA, OutputFormat.PDF, locale=None)


def test_pdf_without_acroform_is_permanent(pdf_form_bundle, tmp_path):
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    with open(pdf_form_bundle.root / "flat.pdf", "wb") as handle:
        writer.write(handle)

    flat = dataclasses.replace(
        pdf_form_bundle,
        manifest=pdf_form_bundle.manifest.model_copy(update={"entrypoint": "flat.pdf"}),
    )
    with pytest.raises(TemplateNotFoundError):
        PdfFormRenderer().render(flat, {}, OutputFormat.PDF, locale=None)


def test_entrypoint_traversal_is_rejected(pdf_form_bundle):
    evil = dataclasses.replace(
        pdf_form_bundle,
        manifest=pdf_form_bundle.manifest.model_copy(update={"entrypoint": "../escape.pdf"}),
    )
    with pytest.raises(TemplateNotFoundError):
        PdfFormRenderer().render(evil, {}, OutputFormat.PDF, locale=None)


def test_wrong_format_raises_render_error(pdf_form_bundle):
    with pytest.raises(RenderError):
        _render(pdf_form_bundle, fmt=OutputFormat.XLSX)
