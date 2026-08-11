"""XLSX renderer tests."""

from __future__ import annotations

import dataclasses
import io

import pytest
from openpyxl import load_workbook

from adapters.renderers.xlsx import XlsxRenderer
from domain.errors import RenderError, SchemaValidationError, TemplateNotFoundError
from domain.models import BlockSpec, Manifest, OutputFormat

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


# ---------------------------------------------------------------------------
# Repeating blocks — the variable-length participant/installation lists that
# official annexes are made of. Layout is manifest-owned; data supplies rows.
# ---------------------------------------------------------------------------

_ROWS = [
    {"nom": "Alice Dupont", "ean": "541448000000000001", "localite": "Namur"},
    {"nom": "ACME SRL", "ean": "541448000000000002", "localite": "Liège"},
]


def _participants(content: bytes):
    return load_workbook(io.BytesIO(content))["Participants"]


def test_block_writes_one_row_per_item(xlsx_block_bundle):
    artifact = XlsxRenderer().render(
        xlsx_block_bundle, {"participants": _ROWS}, OutputFormat.XLSX, locale=None
    )

    sheet = _participants(artifact.content)
    assert (sheet["A4"].value, sheet["B4"].value, sheet["C4"].value) == (
        "Alice Dupont",
        "541448000000000001",
        "Namur",
    )
    assert sheet["A5"].value == "ACME SRL"
    assert sheet["A3"].value == "Nom"  # header untouched


def test_block_hides_unused_rows_instead_of_deleting_them(xlsx_block_bundle):
    """Deleting would break merged ranges, formulas and print areas below."""
    artifact = XlsxRenderer().render(
        xlsx_block_bundle, {"participants": _ROWS}, OutputFormat.XLSX, locale=None
    )

    sheet = _participants(artifact.content)
    assert sheet.row_dimensions[6].hidden is True  # capacity is 4, rows 4-5 used
    assert sheet.row_dimensions[7].hidden is True
    assert sheet.row_dimensions[4].hidden is False


def test_block_overflow_is_permanent(xlsx_block_bundle):
    """maxItems is the template's capacity; no retry of the same list can fit."""
    with pytest.raises(SchemaValidationError, match="reserves only 4"):
        XlsxRenderer().render(
            xlsx_block_bundle, {"participants": _ROWS * 3}, OutputFormat.XLSX, locale=None
        )


def test_block_empty_list_renders_cleanly(xlsx_block_bundle):
    artifact = XlsxRenderer().render(
        xlsx_block_bundle, {"participants": []}, OutputFormat.XLSX, locale=None
    )
    assert _participants(artifact.content)["A4"].value is None


def test_block_missing_column_key_clears_the_cell(xlsx_block_bundle):
    """None and "" must not both reach a cell — they produce different bytes."""
    artifact = XlsxRenderer().render(
        xlsx_block_bundle,
        {"participants": [{"nom": "Solo", "localite": ""}]},
        OutputFormat.XLSX,
        locale=None,
    )
    sheet = _participants(artifact.content)
    assert sheet["B4"].value is None  # key absent
    assert sheet["C4"].value is None  # empty string canonicalised to None


def test_block_bytes_are_deterministic(xlsx_block_bundle):
    render = lambda: XlsxRenderer().render(  # noqa: E731
        xlsx_block_bundle, {"participants": _ROWS}, OutputFormat.XLSX, locale=None
    )
    assert render().content == render().content


def test_block_source_is_not_written_through_a_defined_name(xlsx_block_bundle):
    """A defined name colliding with a block source would make openpyxl raise.

    Real CWaPE workbooks name their data-validation list sources, so a collision
    is a live possibility rather than a hypothetical.
    """
    from openpyxl import load_workbook as _load
    from openpyxl.workbook.defined_name import DefinedName

    workbook = _load(xlsx_block_bundle.root / "template.xlsx")
    workbook.defined_names.add(DefinedName("participants", attr_text="Participants!$E$1"))
    workbook.save(xlsx_block_bundle.root / "template.xlsx")

    artifact = XlsxRenderer().render(
        xlsx_block_bundle, {"participants": _ROWS}, OutputFormat.XLSX, locale=None
    )
    sheet = _participants(artifact.content)
    assert sheet["E1"].value is None  # the list was NOT written into the named cell
    assert sheet["A4"].value == "Alice Dupont"


def test_block_without_resolvable_capacity_is_rejected_at_manifest_load():
    """Fails when the bundle is fetched, not mid-render."""
    with pytest.raises(ValueError, match="no capacity"):
        Manifest(
            id="x.y",
            version="1",
            engine="xlsx",
            supported_formats=["xlsx"],
            required_fields={},
            blocks=[BlockSpec(source="rows", anchor="A1", columns=["a"])],
        )


def test_block_needs_exactly_one_anchor():
    with pytest.raises(ValueError, match="exactly one"):
        BlockSpec(source="rows", columns=["a"])
    with pytest.raises(ValueError, match="exactly one"):
        BlockSpec(source="rows", columns=["a"], anchor="A1", anchor_name="rows_start")
