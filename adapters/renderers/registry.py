"""Maps a template's (engine, format) to a concrete renderer.

The orchestrator asks the registry for a renderer per requested format; a missing
pair means the format is unsupported for that engine (→ ``UNSUPPORTED_FORMAT``).
The renderer instances are stateless, so they are safely reused across requests
and picklable for the process pool.

Note the deliberate absences: ``pdf-form`` produces only ``pdf`` and ``docx``
only ``docx``. Converting either to another format would need LibreOffice, so a
request for one fails permanently and clearly rather than half-working.
"""

from __future__ import annotations

from adapters.renderers.docx import DocxRenderer
from adapters.renderers.jinja_html_pdf import JinjaHtmlRenderer
from adapters.renderers.pdf_form import PdfFormRenderer
from adapters.renderers.xlsx import XlsxRenderer
from domain.models import Engine, OutputFormat
from domain.ports import Renderer


class DefaultRendererRegistry:
    def __init__(self) -> None:
        jinja = JinjaHtmlRenderer()
        xlsx = XlsxRenderer()
        docx = DocxRenderer()
        pdf_form = PdfFormRenderer()
        self._renderers: dict[tuple[Engine, OutputFormat], Renderer] = {
            (Engine.JINJA_HTML, OutputFormat.PDF): jinja,
            (Engine.JINJA_HTML, OutputFormat.HTML): jinja,
            (Engine.XLSX, OutputFormat.XLSX): xlsx,
            (Engine.DOCX, OutputFormat.DOCX): docx,
            (Engine.PDF_FORM, OutputFormat.PDF): pdf_form,
        }

    def get(self, engine: Engine, fmt: OutputFormat) -> Renderer | None:
        return self._renderers.get((engine, fmt))

    def engines(self) -> set[Engine]:
        """Every engine with at least one renderer. Used by the parity test."""
        return {engine for engine, _fmt in self._renderers}
