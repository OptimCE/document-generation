"""Deterministic repacking for OOXML (Office Open XML) documents.

``.xlsx`` and ``.docx`` are both OPC packages: a zip whose members include
``docProps/core.xml`` carrying ``<dcterms:created>``/``<dcterms:modified>``.
Both writers we use stamp wall-clock time — openpyxl rewrites ``core.xml``'s
modified date inside ``save()``, and both libraries let ``zipfile`` stamp each
member with ``time.localtime()``. Neither is a property of (template, data), so
neither may reach the bytes we hash.

Shared by the xlsx and docx renderers so the two cannot drift.
"""

from __future__ import annotations

import io
import re
import zipfile

# Fixed (not wall-clock) so normalised packages are reproducible.
_FIXED_ZIP_DATE = (1980, 1, 1, 0, 0, 0)
_FIXED_ISO = b"2000-01-01T00:00:00Z"
_CREATED_RE = re.compile(rb"(<dcterms:created[^>]*>)[^<]*(</dcterms:created>)")
_MODIFIED_RE = re.compile(rb"(<dcterms:modified[^>]*>)[^<]*(</dcterms:modified>)")

_CORE_PROPS = "docProps/core.xml"
# OPC requires the content-types stream to be the first part in the package.
# Sorted order happens to put it first today ('[' is 0x5B, below every other
# part's initial letter), but relying on that is a latent corruption bug — so
# emit it explicitly first instead.
_CONTENT_TYPES = "[Content_Types].xml"


def normalise_ooxml_bytes(raw: bytes) -> bytes:
    """Repack an OOXML package so identical input yields identical bytes."""
    with zipfile.ZipFile(io.BytesIO(raw)) as src:
        members = {name: src.read(name) for name in src.namelist()}

    ordered = [name for name in (_CONTENT_TYPES,) if name in members]
    ordered += sorted(name for name in members if name != _CONTENT_TYPES)

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for name in ordered:
            data = members[name]
            if name == _CORE_PROPS:
                data = _CREATED_RE.sub(rb"\g<1>" + _FIXED_ISO + rb"\g<2>", data)
                data = _MODIFIED_RE.sub(rb"\g<1>" + _FIXED_ISO + rb"\g<2>", data)
            info = zipfile.ZipInfo(name, date_time=_FIXED_ZIP_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            dst.writestr(info, data)
    return out.getvalue()
