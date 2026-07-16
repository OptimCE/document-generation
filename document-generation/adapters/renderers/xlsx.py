"""Renderer for the ``xlsx`` engine: openpyxl over a template workbook.

Loads the template workbook and writes ``data`` into it via two generic
mechanisms (no domain knowledge):

* **Defined names** — for every workbook defined name that matches a top-level
  ``data`` key, the named cell(s) are set to that value.
* **Explicit cells** — ``data["cells"]`` maps ``"A1"`` (active sheet) or
  ``"Sheet!A1"`` to a value, applied last so it wins.

Determinism: the output bytes are a pure function of (template, data). openpyxl
rewrites ``docProps/core.xml``'s ``<dcterms:modified>`` to ``now()`` inside
``save()`` and stamps each zip member with the current time, so the workbook is
repacked deterministically (fixed member dates + fixed core timestamps) before
hashing — identical input yields identical bytes and sha256.
"""

from __future__ import annotations

import io
import re
import zipfile
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from openpyxl import load_workbook

from adapters.renderers._paths import resolve_template_file
from domain.errors import DocGenError, RenderError
from domain.models import OutputFormat, RenderedArtifact
from domain.ports import TemplateBundle

# Fixed (not wall-clock) so normalised workbooks are reproducible. Naive on
# purpose — openpyxl stores naive datetimes in workbook core properties.
_FIXED_TIMESTAMP = datetime(2000, 1, 1)
_NORMALISED_AUTHOR = "document-generation"

# Deterministic zip repack: a fixed member date and forced core timestamps.
_FIXED_ZIP_DATE = (1980, 1, 1, 0, 0, 0)
_FIXED_ISO = b"2000-01-01T00:00:00Z"
_CREATED_RE = re.compile(rb"(<dcterms:created[^>]*>)[^<]*(</dcterms:created>)")
_MODIFIED_RE = re.compile(rb"(<dcterms:modified[^>]*>)[^<]*(</dcterms:modified>)")


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
        entry = resolve_template_file(bundle.root, bundle.manifest.resolve_entrypoint())
        workbook = load_workbook(entry)
        cls._apply_defined_names(workbook, data)
        cls._apply_cells(workbook, data.get("cells", {}))
        cls._normalise_properties(workbook)
        buffer = io.BytesIO()
        workbook.save(buffer)
        return _normalise_xlsx_bytes(buffer.getvalue())

    @staticmethod
    def _apply_defined_names(workbook: Any, data: dict[str, Any]) -> None:
        for name, defined in workbook.defined_names.items():
            if name not in data:
                continue
            for sheet_name, coordinate in defined.destinations:
                # destinations yields absolute refs ("$B$1"); only single cells
                # can take a scalar — skip multi-cell ranges.
                coord = coordinate.replace("$", "")
                if ":" in coord:
                    continue
                workbook[sheet_name][coord] = data[name]

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


def _normalise_xlsx_bytes(raw: bytes) -> bytes:
    """Repack the saved workbook so identical input yields identical bytes.

    openpyxl's ``save()`` overwrites ``<dcterms:modified>`` with ``now()`` and the
    zip members carry the current time. Rebuild the archive with sorted members,
    a fixed member date, and fixed created/modified timestamps.
    """
    with zipfile.ZipFile(io.BytesIO(raw)) as src:
        names = sorted(src.namelist())
        members = {name: src.read(name) for name in names}

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for name in names:
            data = members[name]
            if name == "docProps/core.xml":
                data = _CREATED_RE.sub(rb"\g<1>" + _FIXED_ISO + rb"\g<2>", data)
                data = _MODIFIED_RE.sub(rb"\g<1>" + _FIXED_ISO + rb"\g<2>", data)
            info = zipfile.ZipInfo(name, date_time=_FIXED_ZIP_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            dst.writestr(info, data)
    return out.getvalue()
