"""Domain error taxonomy and the permanent/transient classification.

Every failure the orchestrator can produce maps to one ``ErrorCode`` and a
``permanent`` flag. The flag is the single source of truth for two things:

* what goes into the result message's ``error.permanent`` field, and
* how the worker treats the message — permanent → ack (no retry could ever
  succeed); transient → nak and let JetStream redeliver.

Classification (per the spec):

* **Permanent** — ``VALIDATION_ERROR``, ``TEMPLATE_NOT_FOUND``,
  ``UNSUPPORTED_FORMAT``.
* **Transient** — ``RENDER_ERROR``, ``STORAGE_ERROR`` (and transport failures,
  which surface as ``STORAGE_ERROR``/handled at the transport layer).

Part of the import-clean domain core: stdlib only.
"""

from __future__ import annotations

from enum import StrEnum
from typing import ClassVar


class ErrorCode(StrEnum):
    VALIDATION_ERROR = "VALIDATION_ERROR"
    TEMPLATE_NOT_FOUND = "TEMPLATE_NOT_FOUND"
    UNSUPPORTED_FORMAT = "UNSUPPORTED_FORMAT"
    RENDER_ERROR = "RENDER_ERROR"
    STORAGE_ERROR = "STORAGE_ERROR"


class DocGenError(Exception):
    """Base class for every classified document-generation failure.

    ``code`` / ``permanent`` are class attributes so a caught exception can be
    turned into a result error and an ack/nak decision without a lookup table.
    """

    code: ClassVar[ErrorCode]
    permanent: ClassVar[bool]


class SchemaValidationError(DocGenError):
    """``data`` did not satisfy the manifest's JSON Schema. Refuse to render."""

    code = ErrorCode.VALIDATION_ERROR
    permanent = True


class TemplateNotFoundError(DocGenError):
    """The template prefix or its manifest is absent from the templates bucket."""

    code = ErrorCode.TEMPLATE_NOT_FOUND
    permanent = True


class UnsupportedFormatError(DocGenError):
    """A requested format is not in the template's ``supported_formats`` (or no
    renderer exists for the template's engine + that format)."""

    code = ErrorCode.UNSUPPORTED_FORMAT
    permanent = True


class RenderError(DocGenError):
    """A renderer failed. Transient per the spec — redeliver up to max_deliver."""

    code = ErrorCode.RENDER_ERROR
    permanent = False


class StorageError(DocGenError):
    """A read/write against object storage failed transiently (5xx/timeout)."""

    code = ErrorCode.STORAGE_ERROR
    permanent = False
