"""Message contracts and the manifest, as framework-free Pydantic v2 models.

These mirror the document-generation API exactly (snake_case fields). The
request/result models *are* the NATS message body — there is no outer envelope.
``metadata`` is opaque: typed ``dict[str, Any]`` and echoed back untouched on the
result.

This module is part of the import-clean domain core: it imports only stdlib and
pydantic — never NATS, object-storage, or rendering libraries.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Content types per output format. Kept here (domain) because the format set is
# a contract concept; adapters import these constants rather than re-deriving.
# ---------------------------------------------------------------------------
_CONTENT_TYPES: dict[str, str] = {
    "pdf": "application/pdf",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "html": "text/html; charset=utf-8",
}


class OutputFormat(StrEnum):
    PDF = "pdf"
    XLSX = "xlsx"
    HTML = "html"

    @property
    def content_type(self) -> str:
        return _CONTENT_TYPES[self.value]

    @property
    def extension(self) -> str:
        return self.value


class Engine(StrEnum):
    """Render engine declared by a template's manifest.

    One template → one engine. The engine plus the requested format selects a
    concrete renderer (see ``adapters.renderers.registry``).
    """

    JINJA_HTML = "jinja-html"
    XLSX = "xlsx"


class GenerationStatus(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"


# Default entry-file name per engine when a manifest omits ``entrypoint``.
_DEFAULT_ENTRYPOINT: dict[Engine, str] = {
    Engine.JINJA_HTML: "template.html",
    Engine.XLSX: "template.xlsx",
}


# ---------------------------------------------------------------------------
# Request
# ---------------------------------------------------------------------------
class TemplateRef(BaseModel):
    model_config = ConfigDict(extra="ignore")
    uri: str = Field(min_length=1)


class OutputSpec(BaseModel):
    model_config = ConfigDict(extra="ignore")
    format: OutputFormat


class RequestOptions(BaseModel):
    # Forward-compatible: unknown option keys are ignored, not rejected.
    model_config = ConfigDict(extra="ignore")
    locale: str | None = None
    presign_ttl: int | None = Field(default=None, ge=1)


class GenerationRequest(BaseModel):
    # Forward-compatible envelope: a newer caller adding top-level fields must
    # not break an older worker. Missing *required* fields still fail
    # validation, which is what catches a genuinely malformed request.
    model_config = ConfigDict(extra="ignore")

    request_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    requested_by: str = Field(min_length=1)
    template: TemplateRef
    outputs: list[OutputSpec] = Field(min_length=1)
    data: dict[str, Any] = Field(default_factory=dict)
    key_prefix: str = Field(min_length=1)
    reply_to: str = Field(min_length=1)
    options: RequestOptions = Field(default_factory=RequestOptions)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def requested_formats(self) -> list[OutputFormat]:
        # De-duplicate while preserving first-seen order.
        seen: dict[OutputFormat, None] = {}
        for out in self.outputs:
            seen.setdefault(out.format, None)
        return list(seen)


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------
class Artifact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    format: OutputFormat
    uri: str
    presigned_url: str | None = None
    size_bytes: int
    sha256: str


class ResultError(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str
    message: str
    permanent: bool


class GenerationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str
    # Echoed straight from the request so a multi-tenant caller can route/scope
    # the result without re-deriving it. Tenant-agnostic (no domain knowledge).
    tenant_id: str = Field(min_length=1)
    status: GenerationStatus
    artifacts: list[Artifact] = Field(default_factory=list)
    error: ResultError | None = None
    template_version: str | None = None
    generated_at: str
    metadata: dict[str, Any] = Field(default_factory=dict)

    def to_json_bytes(self) -> bytes:
        """Serialize for the wire, dropping null optional fields.

        ``error`` is absent on success and ``presigned_url`` is absent when no
        presign was requested — ``exclude_none`` keeps the payload faithful to
        the contract examples while leaving the (possibly empty) artifacts list
        in place.
        """
        return self.model_dump_json(exclude_none=True).encode()


# ---------------------------------------------------------------------------
# Manifest (ships with every template)
# ---------------------------------------------------------------------------
class Manifest(BaseModel):
    # ``extra="ignore"`` so a manifest authored against a future, richer schema
    # still loads here.
    model_config = ConfigDict(extra="ignore")

    id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    engine: Engine
    supported_formats: list[OutputFormat] = Field(min_length=1)
    required_fields: dict[str, Any] = Field(default_factory=dict)

    # Optional extensions (see plan): default-derived when omitted.
    entrypoint: str | None = None
    output_basename: str | None = None

    def resolve_entrypoint(self) -> str:
        return self.entrypoint or _DEFAULT_ENTRYPOINT[self.engine]

    def resolve_output_basename(self) -> str:
        if self.output_basename:
            return self.output_basename
        # "billing.invoice" → "invoice"
        return self.id.rsplit(".", 1)[-1]

    def filename_for(self, fmt: OutputFormat) -> str:
        return f"{self.resolve_output_basename()}.{fmt.extension}"


# ---------------------------------------------------------------------------
# Renderer output (internal — never serialized to the wire)
# ---------------------------------------------------------------------------
class RenderedArtifact(BaseModel):
    """A renderer's product: the bytes plus how to store them.

    ``content`` is kept out of any logging/serialization path; this model only
    travels between a renderer and the object store inside one process.
    """

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)
    format: OutputFormat
    filename: str
    content: bytes

    @property
    def content_type(self) -> str:
        return self.format.content_type
