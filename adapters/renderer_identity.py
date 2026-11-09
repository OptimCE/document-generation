"""What produces the bytes: Python, zlib and the rendering libraries' versions.

Echoed on every success (``GenerationResult.renderer``). Renders are pure
functions of (template, data, watermark) for a GIVEN renderer, but the OOXML
formats are zip archives whose compressed bytes depend on the zlib build, and a
library upgrade can change how a document is serialised. A caller that keeps a
render as evidence and later regenerates it to check it needs both facts: the
inputs were the same, and so was (or was not) the renderer.

Read once at worker start-up; nothing here changes while the process runs.
"""

from __future__ import annotations

import platform
import zlib
from importlib import metadata

# Every distribution whose version can change an artifact's bytes.
_LIBRARIES = (
    "pypdf",
    "openpyxl",
    "docxtpl",
    "python-docx",
    "lxml",
    "jinja2",
    "weasyprint",
    "pydyf",
    "fonttools",
    "pillow",
)

ABSENT = "absent"


def renderer_identity() -> dict[str, str]:
    identity = {"python": platform.python_version(), "zlib": zlib.ZLIB_RUNTIME_VERSION}
    for name in _LIBRARIES:
        try:
            identity[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            identity[name] = ABSENT
    return identity
