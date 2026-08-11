"""Registry parity: adding an engine must not be possible to half-do.

Together with the module-level assert in ``domain.models`` (every Engine has a
default entrypoint), this makes "add an engine" a fully-guarded change: forget
either half and the suite fails immediately, rather than a template failing at
render time and being misclassified as a transient error.
"""

from __future__ import annotations

import pytest

from adapters.renderers.registry import DefaultRendererRegistry
from domain.models import Engine, OutputFormat


def test_every_engine_has_at_least_one_renderer():
    assert DefaultRendererRegistry().engines() == set(Engine)


@pytest.mark.parametrize(
    ("engine", "fmt"),
    [
        (Engine.JINJA_HTML, OutputFormat.PDF),
        (Engine.JINJA_HTML, OutputFormat.HTML),
        (Engine.XLSX, OutputFormat.XLSX),
        (Engine.DOCX, OutputFormat.DOCX),
        (Engine.PDF_FORM, OutputFormat.PDF),
    ],
)
def test_supported_pairs_resolve(engine, fmt):
    assert DefaultRendererRegistry().get(engine, fmt) is not None


@pytest.mark.parametrize(
    ("engine", "fmt"),
    [
        # Converting either to another format would need LibreOffice; failing
        # permanently via UNSUPPORTED_FORMAT is the intended behaviour.
        (Engine.PDF_FORM, OutputFormat.HTML),
        (Engine.DOCX, OutputFormat.PDF),
        (Engine.XLSX, OutputFormat.PDF),
    ],
)
def test_deliberately_unsupported_pairs_return_none(engine, fmt):
    assert DefaultRendererRegistry().get(engine, fmt) is None
