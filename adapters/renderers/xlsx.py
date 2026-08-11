"""Renderer for the ``xlsx`` engine: openpyxl over a template workbook.

Loads the template workbook and writes ``data`` into it via three generic
mechanisms (no domain knowledge):

* **Defined names** — for every workbook defined name that matches a top-level
  ``data`` key, the named cell(s) are set to that value.
* **Repeating blocks** — the *manifest* declares where a variable-length list
  lands (``Manifest.blocks``); ``data[block.source]`` supplies the rows. Layout
  belongs to the template, never to the caller.
* **Explicit cells** — ``data["cells"]`` maps ``"A1"`` (active sheet) or
  ``"Sheet!A1"`` to a value, applied last so it wins.

Blocks write **in place** into a band the template already provisions and never
call ``insert_rows``: openpyxl does not fix up formulas, merged ranges, defined
names, conditional formatting, data validations, print areas or row heights when
rows shift, and official forms are full of all of those. Unused rows are hidden
rather than deleted, for the same reason.

Determinism: the output bytes are a pure function of (template, data). openpyxl
rewrites ``docProps/core.xml``'s ``<dcterms:modified>`` to ``now()`` inside
``save()`` and stamps each zip member with the current time, so the workbook is
repacked deterministically before hashing (see ``_ooxml``).
"""

from __future__ import annotations

import io
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from openpyxl import load_workbook
from openpyxl.cell.cell import MergedCell
from openpyxl.utils import coordinate_to_tuple

from adapters.renderers._ooxml import normalise_ooxml_bytes
from adapters.renderers._paths import resolve_template_file
from domain.errors import DocGenError, RenderError, SchemaValidationError
from domain.models import BlockSpec, Manifest, OutputFormat, RenderedArtifact
from domain.ports import TemplateBundle

# Fixed (not wall-clock) so normalised workbooks are reproducible. Naive on
# purpose — openpyxl stores naive datetimes in workbook core properties.
_FIXED_TIMESTAMP = datetime(2000, 1, 1)
_NORMALISED_AUTHOR = "document-generation"


class XlsxRenderer:
    """Stateless (hence picklable for the process pool) xlsx renderer."""

    def render(
        self,
        bundle: TemplateBundle,
        data: Mapping[str, Any],
        fmt: OutputFormat,
        *,
        locale: str | None,
    ) -> RenderedArtifact:
        if fmt is not OutputFormat.XLSX:
            raise RenderError(f"xlsx engine cannot produce {fmt.value!r}")
        try:
            content = self._render(bundle, dict(data))
        except DocGenError:
            raise
        except Exception as exc:  # any openpyxl failure → transient render error
            raise RenderError(f"xlsx render failed: {exc}") from exc

        return RenderedArtifact(
            format=fmt,
            filename=bundle.manifest.filename_for(fmt),
            content=content,
        )

    @classmethod
    def _render(cls, bundle: TemplateBundle, data: dict[str, Any]) -> bytes:
        manifest = bundle.manifest
        entry = resolve_template_file(bundle.root, manifest.resolve_entrypoint())
        workbook = load_workbook(entry)
        # A block source is a list; a defined name pointing at one would make
        # openpyxl raise on "cannot convert". Blocks own those keys.
        cls._apply_defined_names(workbook, data, skip={b.source for b in manifest.blocks})
        cls._apply_blocks(workbook, manifest, data)
        cls._apply_cells(workbook, data.get("cells", {}))
        cls._normalise_properties(workbook)
        buffer = io.BytesIO()
        workbook.save(buffer)
        return normalise_ooxml_bytes(buffer.getvalue())

    @staticmethod
    def _apply_defined_names(workbook: Any, data: dict[str, Any], skip: Iterable[str] = ()) -> None:
        skipped = set(skip)
        for name, defined in workbook.defined_names.items():
            if name not in data or name in skipped:
                continue
            for sheet_name, coordinate in defined.destinations:
                # destinations yields absolute refs ("$B$1"); only single cells
                # can take a scalar — skip multi-cell ranges.
                coord = coordinate.replace("$", "")
                if ":" in coord:
                    continue
                workbook[sheet_name][coord] = data[name]

    # -- repeating blocks ---------------------------------------------------

    @classmethod
    def _apply_blocks(cls, workbook: Any, manifest: Manifest, data: dict[str, Any]) -> None:
        for block in manifest.blocks:
            rows = data.get(block.source) or []
            if not isinstance(rows, list):
                raise SchemaValidationError(
                    f"block source {block.source!r} must be a list, got {type(rows).__name__}"
                )
            capacity = manifest.block_capacity(block)
            if len(rows) > capacity:
                # Permanent: no retry of the same over-long list can ever fit.
                raise SchemaValidationError(
                    f"{len(rows)} rows supplied for block {block.source!r} but the "
                    f"template reserves only {capacity}"
                )
            sheet, first_row, first_col = cls._resolve_anchor(workbook, block)
            for offset, row in enumerate(rows):
                for column_offset, key in enumerate(block.columns):
                    cls._set_cell(
                        sheet,
                        first_row + offset,
                        first_col + column_offset,
                        cls._normalise_value(row.get(key)),
                    )
            if block.hide_unused_rows:
                for row_index in range(first_row + len(rows), first_row + capacity):
                    sheet.row_dimensions[row_index].hidden = True

    @staticmethod
    def _resolve_anchor(workbook: Any, block: BlockSpec) -> tuple[Any, int, int]:
        """Resolve a block's anchor to (worksheet, row, column) 1-based indices."""
        if block.anchor_name is not None:
            defined = workbook.defined_names.get(block.anchor_name)
            if defined is None:
                raise SchemaValidationError(
                    f"block {block.source!r} anchors on defined name "
                    f"{block.anchor_name!r}, which the workbook does not declare"
                )
            sheet_name, coordinate = next(iter(defined.destinations))
            reference = f"{sheet_name}!{coordinate}"
        else:
            reference = block.anchor or ""

        sheet_name, separator, coordinate = reference.rpartition("!")
        sheet = workbook[sheet_name] if separator else workbook.active
        row, column = coordinate_to_tuple(coordinate.replace("$", ""))
        return sheet, row, column

    @staticmethod
    def _set_cell(sheet: Any, row: int, column: int, value: Any) -> None:
        """Write one cell, redirecting a merged cell to its range's top-left.

        Writing to a ``MergedCell`` raises (it is a read-only view); the value
        belongs on the anchor cell of the range that covers it.
        """
        cell = sheet.cell(row=row, column=column)
        if isinstance(cell, MergedCell):
            for merged in sheet.merged_cells.ranges:
                if (row, column) in merged.cells:
                    sheet.cell(row=merged.min_row, column=merged.min_col).value = value
                    return
            return  # merged view with no range (corrupt template) — skip silently
        cell.value = value

    @staticmethod
    def _normalise_value(value: Any) -> Any:
        """Canonicalise 'absent' to None in exactly one place.

        ``None`` clears a cell; ``""`` writes an inline-string element. Different
        XML, different sha256 — so the two must never both reach a cell.
        """
        return None if value == "" else value

    # -- explicit cells -----------------------------------------------------

    @staticmethod
    def _apply_cells(workbook: Any, cells: Any) -> None:
        for ref, value in dict(cells).items():
            sheet_name, separator, coordinate = str(ref).partition("!")
            if separator:
                workbook[sheet_name][coordinate] = value
            else:
                workbook.active[ref] = value

    @staticmethod
    def _normalise_properties(workbook: Any) -> None:
        props = workbook.properties
        props.created = _FIXED_TIMESTAMP
        props.modified = _FIXED_TIMESTAMP
        props.creator = _NORMALISED_AUTHOR
        props.lastModifiedBy = _NORMALISED_AUTHOR
