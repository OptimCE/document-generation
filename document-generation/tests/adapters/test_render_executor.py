"""Render-executor tests, including the real process-pool path.

The process-pool test proves the renderer, the template bundle and the rendered
artifact all cross a process boundary cleanly (picklable) and that CPU work runs
off the event loop — the path worker.main uses in production but that the inline
executor used elsewhere does not exercise.
"""

from __future__ import annotations

import io
from concurrent.futures import ProcessPoolExecutor

import pytest
from openpyxl import load_workbook

from adapters.render_executor import InlineRenderExecutor, ProcessPoolRenderExecutor
from adapters.renderers.xlsx import XlsxRenderer
from domain.errors import RenderError
from domain.models import OutputFormat


async def test_process_pool_renders_in_a_subprocess(xlsx_bundle):
    data = {"company": "ACME", "cells": {"B2": 7, "Report!B3": "OK"}}
    with ProcessPoolExecutor(max_workers=1) as pool:
        executor = ProcessPoolRenderExecutor(pool)
        artifact = await executor.run(
            XlsxRenderer(), xlsx_bundle, data, OutputFormat.XLSX, locale=None
        )

    assert artifact.filename == "report.xlsx"
    sheet = load_workbook(io.BytesIO(artifact.content))["Report"]
    assert sheet["B1"].value == "ACME"
    assert sheet["B2"].value == 7
    assert sheet["B3"].value == "OK"


async def test_inline_executor_wraps_unexpected_error(xlsx_bundle):
    class Boom:
        def render(self, *args, **kwargs):
            raise ValueError("kaboom")

    with pytest.raises(RenderError):
        await InlineRenderExecutor().run(Boom(), xlsx_bundle, {}, OutputFormat.XLSX, locale=None)


async def test_inline_executor_passes_through_domain_error(xlsx_bundle):
    class Boom:
        def render(self, *args, **kwargs):
            raise RenderError("already classified")

    with pytest.raises(RenderError, match="already classified"):
        await InlineRenderExecutor().run(Boom(), xlsx_bundle, {}, OutputFormat.XLSX, locale=None)
