"""Renderer for the ``docx`` engine: Jinja inside a real ``.docx`` via docxtpl.

For documents whose mandated format is Word — the CWaPE standard DSO agreements,
for instance — where a PDF would not be accepted. Authoring matches the
``jinja-html`` bundles: the same ``{{ data.x }}`` expressions, but written inside
Word rather than in HTML.

``StrictUndefined`` mirrors the jinja-html renderer: a missing field is a hard
error, never a silent blank, which is the right default for an official document.

Determinism: docxtpl introduces nothing wall-clock of its own, but python-docx
lets ``zipfile`` stamp members with the current time, so the package is repacked
through ``_ooxml.normalise_ooxml_bytes`` — the same pass the xlsx renderer uses,
since both are OPC packages.

Unlike WeasyPrint, docxtpl is pure Python (its only compiled dependency is lxml,
whose wheels bundle libxml2/libxslt), so this renderer imports and its tests run
on a bare developer host. It is still imported lazily, matching the convention.
"""

from __future__ import annotations

import datetime
import io
from collections.abc import Mapping
from typing import Any

from adapters.renderers._ooxml import normalise_ooxml_bytes
from adapters.renderers._paths import resolve_template_file
from domain.errors import DocGenError, RenderError
from domain.models import OutputFormat, RenderedArtifact
from domain.ports import TemplateBundle

_FIXED_TIMESTAMP = datetime.datetime(2000, 1, 1)
_NORMALISED_AUTHOR = "document-generation"


class DocxRenderer:
    """Stateless (hence picklable for the process pool) docx renderer."""

    def render(
        self,
        bundle: TemplateBundle,
        data: Mapping[str, Any],
        fmt: OutputFormat,
        *,
        locale: str | None,
    ) -> RenderedArtifact:
        if fmt is not OutputFormat.DOCX:
            raise RenderError(f"docx engine cannot produce {fmt.value!r}")
        try:
            content = self._render(bundle, dict(data), locale)
        except DocGenError:
            raise
        except Exception as exc:  # any docxtpl/python-docx failure → transient
            raise RenderError(f"docx render failed: {exc}") from exc

        return RenderedArtifact(
            format=fmt,
            filename=bundle.manifest.filename_for(fmt),
            content=content,
        )

    @classmethod
    def _render(cls, bundle: TemplateBundle, data: dict[str, Any], locale: str | None) -> bytes:
        # Lazy import, and the Jinja Environment is built here rather than held
        # on the instance: an Environment is not reliably picklable and this
        # renderer crosses the process-pool boundary.
        from docxtpl import DocxTemplate  # type: ignore[import-untyped]
        from jinja2 import Environment, StrictUndefined

        entry = resolve_template_file(bundle.root, bundle.manifest.resolve_entrypoint())
        template = DocxTemplate(str(entry))
        # autoescape is mandatory here, not a lint appeasement: a .docx body is
        # XML, so an unescaped "&" or "<" in a value (think "Smith & Co")
        # produces a corrupt package Word refuses to open.
        #
        # finalize maps None to an empty string. Jinja's default is to print the
        # literal "None", which in a contract reads as a filled-in value and is
        # far worse than a visible blank. StrictUndefined still catches a field
        # the caller forgot entirely — the two guard different mistakes.
        environment = Environment(
            undefined=StrictUndefined,
            autoescape=True,
            finalize=lambda value: "" if value is None else value,
        )
        template.render({"data": data, "locale": locale}, environment)
        cls._normalise_properties(template.docx)

        buffer = io.BytesIO()
        template.save(buffer)
        return normalise_ooxml_bytes(buffer.getvalue())

    @staticmethod
    def _normalise_properties(document: Any) -> None:
        """Clear author/revision metadata so the template author does not leak."""
        props = document.core_properties
        props.created = _FIXED_TIMESTAMP
        props.modified = _FIXED_TIMESTAMP
        props.last_modified_by = _NORMALISED_AUTHOR
        props.author = _NORMALISED_AUTHOR
        props.revision = 1
