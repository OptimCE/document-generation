"""Maps a template's (engine, format) to a concrete renderer.

The orchestrator asks the registry for a renderer per requested format; a missing
pair means the format is unsupported for that engine (→ ``UNSUPPORTED_FORMAT``).
The renderer instances are stateless, so they are safely reused across requests
and picklable for the process pool.
"""

from __future__ import annotations

from adapters.renderers.jinja_html_pdf import JinjaHtmlRenderer
from adapters.renderers.xlsx import XlsxRenderer
from domain.models import Engine, OutputFormat
from domain.ports import Renderer


class DefaultRendererRegistry:
    def __init__(self) -> None:
        jinja = JinjaHtmlRenderer()
        xlsx = XlsxRenderer()
        self._renderers: dict[tuple[Engine, OutputFormat], Renderer] = {
            (Engine.JINJA_HTML, OutputFormat.PDF): jinja,
            (Engine.JINJA_HTML, OutputFormat.HTML): jinja,
            (Engine.XLSX, OutputFormat.XLSX): xlsx,
        }

    def get(self, engine: Engine, fmt: OutputFormat) -> Renderer | None:
        return self._renderers.get((engine, fmt))
