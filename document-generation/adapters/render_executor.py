"""RenderExecutor implementations.

Renders are CPU-bound and synchronous. ``ProcessPoolRenderExecutor`` runs them in
a process pool so the event loop stays responsive (NATS PING/PONG, heartbeat) and
several renders can use several cores; ``InlineRenderExecutor`` runs them on the
calling thread for the single-process fallback and unit tests.

Both translate failures to ``RenderError`` (transient): a renderer's own
``DocGenError`` passes through unchanged, a dead pool worker or any other
exception becomes a transient ``RenderError`` so the message is redelivered
rather than wrongly marked permanent.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from concurrent.futures import BrokenExecutor, Executor
from typing import Any

from domain.errors import DocGenError, RenderError
from domain.models import OutputFormat, RenderedArtifact
from domain.ports import Renderer, TemplateBundle


def _invoke(
    renderer: Renderer,
    bundle: TemplateBundle,
    data: dict[str, Any],
    fmt: OutputFormat,
    locale: str | None,
) -> RenderedArtifact:
    """Top-level, picklable entry executed inside a pool process."""
    return renderer.render(bundle, data, fmt, locale=locale)


class InlineRenderExecutor:
    async def run(
        self,
        renderer: Renderer,
        bundle: TemplateBundle,
        data: Mapping[str, Any],
        fmt: OutputFormat,
        *,
        locale: str | None,
    ) -> RenderedArtifact:
        try:
            return renderer.render(bundle, data, fmt, locale=locale)
        except DocGenError:
            raise
        except Exception as exc:
            raise RenderError(f"render failed: {exc}") from exc


class ProcessPoolRenderExecutor:
    def __init__(self, executor: Executor, *, semaphore: asyncio.Semaphore | None = None) -> None:
        self._executor = executor
        self._semaphore = semaphore

    async def run(
        self,
        renderer: Renderer,
        bundle: TemplateBundle,
        data: Mapping[str, Any],
        fmt: OutputFormat,
        *,
        locale: str | None,
    ) -> RenderedArtifact:
        loop = asyncio.get_running_loop()
        try:
            if self._semaphore is None:
                return await loop.run_in_executor(
                    self._executor, _invoke, renderer, bundle, dict(data), fmt, locale
                )
            async with self._semaphore:
                return await loop.run_in_executor(
                    self._executor, _invoke, renderer, bundle, dict(data), fmt, locale
                )
        except DocGenError:
            raise
        except BrokenExecutor as exc:
            # A pool worker died abnormally (OOM, native crash). Transient: the
            # pool rebuilds on the next submit (see worker.main), so a redelivery
            # lands on a healthy worker.
            raise RenderError(f"render pool broken: {exc}") from exc
        except Exception as exc:
            raise RenderError(f"render failed: {exc}") from exc
