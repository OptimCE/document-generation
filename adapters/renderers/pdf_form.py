"""Renderer for the ``pdf-form`` engine: fill an existing AcroForm PDF.

Unlike ``jinja-html``, which *authors* a PDF, this engine takes the authority's
own fillable form and sets its field values. Everything else — layout, legal
text, logos, page count — stays exactly as the regulator published it, which is
the whole point when the filed artifact must be the official document rather
than a reproduction of it.

Official forms name their fields automatically (``Champ de texte 68``,
``Case à cocher 81``), so the manifest carries the translation:
``fields: {"<data key>": "<AcroForm field name>"}``. That map is a fact about the
template, so it lives in the bundle.

A value may also map to a **list** of field names, for the comb boxes these forms
use to spell a code out one character per box (an 18-digit EAN across 16 boxes,
say). The string is then spread left-aligned across them and any leftover box is
cleared.

Checkbox fields are detected from the PDF's own ``/FT`` and export states — a
truthy value selects the non-``/Off`` state, a falsy one selects ``/Off`` — so a
form using ``/Oui``, ``/Yes`` or ``/1`` needs no manifest change.

A field the manifest does not map is left untouched, and the artifact stays a
fillable AcroForm — so anything we cannot derive remains editable by hand in the
user's PDF reader rather than being lost.

Determinism: pypdf derives the trailer ``/ID`` from content rather than
randomness, and ``/Info`` dates are inherited from the source document (which is
itself fixed), so the output is a pure function of (template, data). The
``/ID`` is pinned defensively anyway, and ``tests/renderers/test_pdf_form.py``
asserts byte-equality so a dependency upgrade cannot regress it silently.

pypdf is imported lazily, matching the WeasyPrint convention — though unlike
WeasyPrint it is pure Python and needs no native libraries.
"""

from __future__ import annotations

import hashlib
import io
from collections.abc import Mapping
from typing import Any

from adapters.renderers._paths import resolve_template_file
from domain.errors import DocGenError, RenderError, TemplateNotFoundError
from domain.models import OutputFormat, RenderedArtifact
from domain.ports import TemplateBundle

_FIELD_TYPE = "/FT"
_BUTTON = "/Btn"
_STATES = "/_States_"
_OFF = "/Off"


class PdfFormRenderer:
    """Stateless (hence picklable for the process pool) AcroForm filler."""

    def render(
        self,
        bundle: TemplateBundle,
        data: Mapping[str, Any],
        fmt: OutputFormat,
        *,
        locale: str | None,
    ) -> RenderedArtifact:
        if fmt is not OutputFormat.PDF:
            raise RenderError(f"pdf-form engine cannot produce {fmt.value!r}")
        try:
            content = self._render(bundle, dict(data))
        except DocGenError:
            raise
        except Exception as exc:  # any pypdf failure → transient render error
            raise RenderError(f"pdf-form render failed: {exc}") from exc

        return RenderedArtifact(
            format=fmt,
            filename=bundle.manifest.filename_for(fmt),
            content=content,
        )

    @classmethod
    def _render(cls, bundle: TemplateBundle, data: dict[str, Any]) -> bytes:
        # Lazy import: keeps the process-pool import cost and the test import
        # graph unchanged, matching the jinja-html renderer's convention.
        from pypdf import PdfReader, PdfWriter

        manifest = bundle.manifest
        entry = resolve_template_file(bundle.root, manifest.resolve_entrypoint())
        reader = PdfReader(str(entry))
        declared = reader.get_fields() or {}
        if not declared:
            raise TemplateNotFoundError(
                f"template {manifest.id!r} declares engine 'pdf-form' but "
                f"{manifest.resolve_entrypoint()!r} has no AcroForm fields"
            )

        values = cls._resolve_values(manifest.fields, declared, data)

        writer = PdfWriter(clone_from=reader)
        # Without this, viewers that do not generate appearance streams
        # themselves render a filled field as blank.
        writer.set_need_appearances_writer(True)
        if values:
            for page in writer.pages:
                writer.update_page_form_field_values(page, values)

        cls._pin_document_id(writer, manifest.id, manifest.version)
        buffer = io.BytesIO()
        writer.write(buffer)
        return buffer.getvalue()

    @staticmethod
    def _resolve_values(
        mapping: Mapping[str, str | list[str]],
        declared: Mapping[str, Any],
        data: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Translate ``data`` keys to AcroForm field names, coercing per field type.

        A mapped field that the PDF does not declare is a template-authoring
        error and permanent — the same class as a bad entrypoint, so it raises
        the same ``TemplateNotFoundError``.
        """
        values: dict[str, Any] = {}
        for key, target in mapping.items():
            names = [target] if isinstance(target, str) else list(target)
            for field_name in names:
                if field_name not in declared:
                    raise TemplateNotFoundError(
                        f"manifest maps {key!r} to AcroForm field {field_name!r}, "
                        f"which this PDF does not declare"
                    )
            if key not in data:
                continue  # unsupplied optional field — leave the form's own value
            if isinstance(target, str):
                values[target] = _coerce(declared[target], data[key])
            else:
                # Comb boxes: one character per field, left-aligned, the rest
                # cleared so a shorter value never leaves stale digits behind.
                characters = "" if data[key] is None else str(data[key])
                for position, field_name in enumerate(names):
                    values[field_name] = characters[position] if position < len(characters) else ""
        return values

    @staticmethod
    def _pin_document_id(writer: Any, template_id: str, version: str) -> None:
        """Pin the trailer /ID so reproducibility cannot depend on pypdf internals."""
        from pypdf.generic import ArrayObject, ByteStringObject

        digest = hashlib.sha256(f"{template_id}:{version}".encode()).digest()[:16]
        if hasattr(writer, "_ID"):
            writer._ID = ArrayObject([ByteStringObject(digest), ByteStringObject(digest)])


def _coerce(field: Mapping[str, Any], value: Any) -> Any:
    """Coerce a data value to what the AcroForm field expects."""
    if field.get(_FIELD_TYPE) == _BUTTON:
        states = [s for s in (field.get(_STATES) or []) if s != _OFF]
        on_state = states[0] if states else "/Yes"
        return on_state if _is_checked(value) else _OFF
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Oui" if value else "Non"
    return str(value)


def _is_checked(value: Any) -> bool:
    """Interpret a checkbox value, tolerating the string forms JSON tends to carry."""
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "oui", "on", "1", "x"}
    return bool(value)
