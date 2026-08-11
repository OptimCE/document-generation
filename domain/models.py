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

from pydantic import BaseModel, ConfigDict, Field, model_validator

# ---------------------------------------------------------------------------
# Content types per output format. Kept here (domain) because the format set is
# a contract concept; adapters import these constants rather than re-deriving.
# ---------------------------------------------------------------------------
_CONTENT_TYPES: dict[str, str] = {
    "pdf": "application/pdf",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "html": "text/html; charset=utf-8",
}


class OutputFormat(StrEnum):
    PDF = "pdf"
    XLSX = "xlsx"
    DOCX = "docx"
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
    DOCX = "docx"
    # Fills an existing fillable (AcroForm) PDF rather than authoring one. Used
    # for official regulator forms, where the filed document must BE the
    # authority's own file with only its field values set.
    PDF_FORM = "pdf-form"


class GenerationStatus(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"


# Default entry-file name per engine when a manifest omits ``entrypoint``.
_DEFAULT_ENTRYPOINT: dict[Engine, str] = {
    Engine.JINJA_HTML: "template.html",
    Engine.XLSX: "template.xlsx",
    Engine.DOCX: "template.docx",
    Engine.PDF_FORM: "template.pdf",
}

# A missing entry would raise a bare KeyError from resolve_entrypoint() — and a
# KeyError is not a DocGenError, so the orchestrator would not catch it and the
# dispatcher would misclassify a template-config bug as a transient failure,
# burning every retry before the DLQ. Fail at import instead, in every
# environment including tests. Not an `assert`: `python -O` strips those, and
# this guard is most needed in exactly the optimised container image.
if set(_DEFAULT_ENTRYPOINT) != set(Engine):
    raise RuntimeError(
        "every Engine needs a default entrypoint; missing "
        f"{sorted(set(Engine) - set(_DEFAULT_ENTRYPOINT))}"
    )


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
class BlockSpec(BaseModel):
    """A repeating region of a spreadsheet template, declared by the template.

    Layout (which sheet, which cell, which column order, how many rows fit) is a
    fact about the workbook, so it lives here — in the bundle — and never in the
    caller's ``data``. A caller supplies only the list: ``data[source]`` is a
    list of dicts, and row *i* column *j* is ``rows[i][columns[j]]``.
    """

    model_config = ConfigDict(extra="ignore")

    source: str = Field(min_length=1)
    columns: list[str] = Field(min_length=1)
    # Exactly one of the two. ``anchor`` is the primary form because real
    # regulator workbooks rarely carry usable defined names (and where they do,
    # they are data-validation list sources, not layout markers).
    anchor: str | None = None  # "Sheet name!A4" or "A4" (active sheet)
    anchor_name: str | None = None  # a workbook defined name
    # Capacity of the pre-provisioned band. ``None`` derives it from the
    # ``maxItems`` of this source in ``required_fields`` (single source of truth).
    max_rows: int | None = Field(default=None, ge=1)
    # Blank out-of-use rows in a bordered/banded template look like empty table
    # rows; hiding them is deterministic and preserves every style and range,
    # unlike delete_rows().
    hide_unused_rows: bool = True

    @model_validator(mode="after")
    def _exactly_one_anchor(self) -> BlockSpec:
        if bool(self.anchor) == bool(self.anchor_name):
            raise ValueError(
                f"block {self.source!r} must declare exactly one of " f"'anchor' or 'anchor_name'"
            )
        return self


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

    # engine="xlsx": repeating regions this workbook provisions.
    blocks: list[BlockSpec] = Field(default_factory=list)
    # engine="pdf-form": semantic data key → AcroForm field name. Official forms
    # name their fields "Champ de texte 68", so the bundle owns the translation.
    # A list of names is a comb: the value is spelled one character per box.
    fields: dict[str, str | list[str]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _blocks_have_resolvable_capacity(self) -> Manifest:
        """Resolve every block's capacity now, so a render can never fail on it.

        ``template_store._load_manifest`` turns a ValueError here into a
        permanent ``TemplateNotFoundError`` at fetch time — which is what a
        template-authoring mistake deserves.
        """
        for block in self.blocks:
            self.block_capacity(block)
        return self

    def block_capacity(self, block: BlockSpec) -> int:
        """Rows the template reserves for ``block`` — explicit, or from the schema."""
        if block.max_rows is not None:
            return block.max_rows
        properties = self.required_fields.get("properties")
        schema = properties.get(block.source) if isinstance(properties, dict) else None
        max_items = schema.get("maxItems") if isinstance(schema, dict) else None
        if isinstance(max_items, int) and max_items >= 1:
            return max_items
        raise ValueError(
            f"block {block.source!r} has no capacity: set 'max_rows' on the block, or "
            f"'maxItems' on required_fields.properties.{block.source}"
        )

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
