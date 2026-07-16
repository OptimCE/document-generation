"""XLSX renderer tests."""

from __future__ import annotations

import dataclasses
import io

import pytest
from openpyxl import load_workbook

from adapters.renderers.xlsx import XlsxRenderer
from domain.errors import RenderError, TemplateNotFoundError
from domain.models import OutputFormat

_DATA = {
    "company": "ACME Corp",  # written via the workbook's "company" defined name
    "cells": {"B2": 1234.5, "Report!B3": "PAID"},
}


def _render(bundle, data=None, fmt=OutputFormat.XLSX):
    return XlsxRenderer().render(bundle, data or _DATA, fmt, locale=None)


def _sheet(content: bytes):
    return load_workbook(io.BytesIO(content))["Report"]


def test_render_writes_named_and_explicit_cells(xlsx_bundle):
    artifact = _render(xlsx_bundle)

    assert artifact.format is OutputFormat.XLSX
    assert artifact.filename == "report.xlsx"
    sheet = _sheet(artifact.content)
    assert sheet["B1"].value == "ACME Corp"  # defined name "company"
    assert sheet["B2"].value == 1234.5  # explicit active-sheet cell
    assert sheet["B3"].value == "PAID"  # explicit "Report!B3"


def test_render_cell_values_are_deterministic(xlsx_bundle):
    first = _sheet(_render(xlsx_bundle).content)
    second = _sheet(_render(xlsx_bundle).content)
    for coord in ("B1", "B2", "B3"):
        assert first[coord].value == second[coord].value


def test_render_bytes_are_deterministic(xlsx_bundle):
    # Repacking normalises openpyxl's now()-stamped modified time + zip dates, so
    # identical (template, data) yields identical bytes (and sha256).
    assert _render(xlsx_bundle).content == _render(xlsx_bundle).content


def test_entrypoint_traversal_is_rejected(xlsx_bundle):
    evil_manifest = dataclasses.replace(
        xlsx_bundle,
        manifest=xlsx_bundle.manifest.model_copy(update={"entrypoint": "../escape.xlsx"}),
    )
    with pytest.raises(TemplateNotFoundError):
        XlsxRenderer().render(evil_manifest, {}, OutputFormat.XLSX, locale=None)


def test_wrong_format_raises_render_error(xlsx_bundle):
    with pytest.raises(RenderError):
        _render(xlsx_bundle, fmt=OutputFormat.PDF)


def test_missing_sheet_raises_render_error(xlsx_bundle):
    with pytest.raises(RenderError):
        _render(xlsx_bundle, data={"cells": {"NoSuchSheet!A1": 1}})
