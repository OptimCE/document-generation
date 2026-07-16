"""Renderer for the ``jinja-html`` engine: Jinja2 → HTML → WeasyPrint PDF.

Produces ``html`` (the rendered HTML) or ``pdf`` (that HTML through WeasyPrint).
WeasyPrint's ``base_url`` is the downloaded template directory, so relative asset
references in the template (``./logo.png``, ``./invoice.css``, fonts) resolve.

Determinism: the output is a pure function of (template, data). ``StrictUndefined``
makes a missing field a hard error rather than a silent blank — fitting for an
official document, and complementary to the upstream validation gate.

WeasyPrint is imported lazily inside ``_to_pdf`` because it needs native
libraries (pango, cairo, gdk-pixbuf) that are present in the container but not on
a bare developer host. Importing it at module load would break ``html``-only use
and the rest of the test suite on such hosts.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from adapters.renderers._paths import resolve_template_file
from domain.errors import DocGenError, RenderError
from domain.models import OutputFormat, RenderedArtifact
from domain.ports import TemplateBundle


class JinjaHtmlRenderer:
    """Stateless (hence picklable for the process pool) jinja-html renderer."""

    def render(
        self,
        bundle: TemplateBundle,
        data: Mapping[str, Any],
        fmt: OutputFormat,
        *,
        locale: str | None,
    ) -> RenderedArtifact:
        try:
            html = self._render_html(bundle, dict(data), locale)
            if fmt is OutputFormat.HTML:
                content = html.encode("utf-8")
            elif fmt is OutputFormat.PDF:
                content = self._to_pdf(html, base_url=str(bundle.root))
            else:
                raise RenderError(f"jinja-html engine cannot produce {fmt.value!r}")
        except DocGenError:
            raise
        except Exception as exc:  # any Jinja/WeasyPrint failure → transient render error
            raise RenderError(f"jinja-html render failed: {exc}") from exc

        return RenderedArtifact(
            format=fmt,
            filename=bundle.manifest.filename_for(fmt),
            content=content,
        )

    @staticmethod
    def _render_html(bundle: TemplateBundle, data: dict[str, Any], locale: str | None) -> str:
        entrypoint = bundle.manifest.resolve_entrypoint()
        # Confine the entrypoint (traversal/missing → permanent TEMPLATE_NOT_FOUND)
        # before handing the name to Jinja's loader.
        resolve_template_file(bundle.root, entrypoint)
        env = Environment(
            loader=FileSystemLoader(str(bundle.root)),
            autoescape=select_autoescape(default=True, default_for_string=True),
            undefined=StrictUndefined,
        )
        template = env.get_template(entrypoint)
        return template.render(data=data, locale=locale)

    @staticmethod
    def _to_pdf(html: str, base_url: str) -> bytes:
        # Lazy import: WeasyPrint's native deps are only needed for PDF.
        import weasyprint

        return cast(bytes, weasyprint.HTML(string=html, base_url=base_url).write_pdf())
