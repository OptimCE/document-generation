"""Shared test fixtures and in-memory fakes.

Tests are hermetic: no NATS, no object storage, no real broker. The fakes below
structurally satisfy the domain ports (duck typing), and the dispatcher tests
drive the handler directly with a ``FakeMsg``. Template fixtures are either the
committed jinja-html invoice or an openpyxl workbook built at test time.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, cast

# core.config reads ENV at import to choose .env.<env>; set it before any project
# module is imported.
os.environ.setdefault("ENV", "test")

import pytest
from openpyxl import Workbook
from openpyxl.workbook.defined_name import DefinedName

from domain.errors import DocGenError
from domain.models import (
    BlockSpec,
    Engine,
    GenerationRequest,
    Manifest,
    OutputFormat,
    RenderedArtifact,
)
from domain.ports import StoredArtifact, TemplateBundle

FIXED_GENERATED_AT = "2026-06-18T00:00:00+00:00"
_FIXTURES = Path(__file__).parent / "fixtures" / "templates"


# ---------------------------------------------------------------------------
# Request builder
# ---------------------------------------------------------------------------
def make_request(**overrides: Any) -> GenerationRequest:
    payload: dict[str, Any] = {
        "request_id": "req-1",
        "tenant_id": "tenant-1",
        "requested_by": "billing",
        "template": {"uri": "s3://optimce-templates/billing/invoice/v1/"},
        "outputs": [{"format": "pdf"}],
        "data": {
            "invoice_number": "INV-001",
            "customer_name": "ACME Corp",
            "total": 1234.5,
            "currency": "EUR",
        },
        "key_prefix": "billing/invoices/2026/06/inv-00042/",
        "reply_to": "docgen.result.billing",
        "options": {"locale": "fr-BE"},
        "metadata": {"invoice_id": "abc-123"},
    }
    payload.update(overrides)
    return GenerationRequest.model_validate(payload)


# ---------------------------------------------------------------------------
# Template fixtures
# ---------------------------------------------------------------------------
def _load_manifest(directory: Path) -> Manifest:
    return Manifest.model_validate_json((directory / "manifest.json").read_bytes())


@pytest.fixture
def jinja_bundle() -> TemplateBundle:
    directory = _FIXTURES / "billing_invoice"
    return TemplateBundle(manifest=_load_manifest(directory), root=directory)


@pytest.fixture
def xlsx_bundle(tmp_path: Path) -> TemplateBundle:
    """Build an xlsx template workbook + manifest in a temp dir."""
    directory = tmp_path / "xlsx_template"
    directory.mkdir()

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Report"
    sheet["A1"] = "Company"
    sheet["A2"] = "Amount"
    sheet["A3"] = "Status"
    # A named cell the renderer can target from a top-level data key.
    workbook.defined_names.add(DefinedName("company", attr_text="Report!$B$1"))
    workbook.save(directory / "template.xlsx")

    manifest = Manifest(
        id="billing.report",
        version="2",
        engine=Engine.XLSX,
        supported_formats=[OutputFormat.XLSX],
        required_fields={},
        entrypoint="template.xlsx",
        output_basename="report",
    )
    (directory / "manifest.json").write_text(manifest.model_dump_json())
    return TemplateBundle(manifest=manifest, root=directory)


@pytest.fixture
def xlsx_block_bundle(tmp_path: Path) -> TemplateBundle:
    """A workbook with a header row and a 4-row participant band at A4.

    Mirrors the real CWaPE annexes: a titled sheet, a header row, then a
    pre-provisioned band the renderer writes into without shifting anything.
    """
    directory = tmp_path / "xlsx_block_template"
    directory.mkdir()

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Participants"
    sheet["A1"] = "PARTICIPANTS"
    sheet["A3"], sheet["B3"], sheet["C3"] = "Nom", "EAN", "Localite"
    workbook.save(directory / "template.xlsx")

    manifest = Manifest(
        id="admin.participants",
        version="1",
        engine=Engine.XLSX,
        supported_formats=[OutputFormat.XLSX],
        required_fields={
            "type": "object",
            "properties": {
                "participants": {"type": "array", "maxItems": 4},
            },
        },
        entrypoint="template.xlsx",
        output_basename="participants",
        blocks=[
            BlockSpec(
                source="participants",
                anchor="Participants!A4",
                columns=["nom", "ean", "localite"],
            )
        ],
    )
    (directory / "manifest.json").write_text(manifest.model_dump_json())
    return TemplateBundle(manifest=manifest, root=directory)


@pytest.fixture
def docx_bundle(tmp_path: Path) -> TemplateBundle:
    """A .docx carrying docxtpl Jinja placeholders, built at test time."""
    from docx import Document

    directory = tmp_path / "docx_template"
    directory.mkdir()

    document = Document()
    document.add_heading("Convention", level=1)
    document.add_paragraph("Communauté : {{ data.community_name }}")
    document.add_paragraph("Représentant : {{ data.representative }}")
    document.save(str(directory / "template.docx"))

    manifest = Manifest(
        id="admin.agreement",
        version="1",
        engine=Engine.DOCX,
        supported_formats=[OutputFormat.DOCX],
        required_fields={},
        entrypoint="template.docx",
        output_basename="agreement",
    )
    (directory / "manifest.json").write_text(manifest.model_dump_json())
    return TemplateBundle(manifest=manifest, root=directory)


@pytest.fixture
def pdf_form_bundle(tmp_path: Path) -> TemplateBundle:
    """A two-field AcroForm PDF (one text field, one checkbox) built at test time.

    Built rather than committed so the fixture carries no third-party document
    and the field names are meaningful in assertions.
    """
    directory = tmp_path / "pdf_form_template"
    directory.mkdir()
    _write_acroform_pdf(directory / "template.pdf")

    manifest = Manifest(
        id="admin.declaration",
        version="1",
        engine=Engine.PDF_FORM,
        supported_formats=[OutputFormat.PDF],
        required_fields={},
        entrypoint="template.pdf",
        output_basename="declaration",
        fields={"community_name": "Champ de texte 1", "agreed": "Case a cocher 1"},
    )
    (directory / "manifest.json").write_text(manifest.model_dump_json())
    return TemplateBundle(manifest=manifest, root=directory)


def _write_acroform_pdf(path: Path) -> None:
    """Write a minimal one-page PDF with a text field and a checkbox.

    A well-formed AcroForm carries a ``/DR`` (default resources, with a font) and
    a ``/DA`` (default appearance) — pypdf needs both to regenerate a field's
    appearance stream. Real regulator forms have them; a fixture without them
    fails in a way that looks like a renderer bug, so they are set here.
    """
    from pypdf import PdfWriter
    from pypdf.generic import (
        ArrayObject,
        BooleanObject,
        DictionaryObject,
        NameObject,
        NumberObject,
        TextStringObject,
    )

    writer = PdfWriter()
    page = writer.add_blank_page(width=595, height=842)

    helvetica = writer._add_object(
        DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
                NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
            }
        )
    )
    default_resources = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/Helv"): helvetica})}
    )
    default_appearance = TextStringObject("/Helv 0 Tf 0 g")

    def _field(name: str, rect: list[int], extra: dict[str, Any]) -> Any:
        field = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Annot"),
                NameObject("/Subtype"): NameObject("/Widget"),
                NameObject("/T"): TextStringObject(name),
                NameObject("/Rect"): ArrayObject([NumberObject(n) for n in rect]),
                NameObject("/F"): NumberObject(4),
            }
        )
        field.update({NameObject(k): v for k, v in extra.items()})
        return writer._add_object(field)

    text_ref = _field(
        "Champ de texte 1",
        [50, 700, 300, 720],
        {
            "/FT": NameObject("/Tx"),
            "/V": TextStringObject(""),
            "/DA": default_appearance,
        },
    )
    checkbox_ref = _field(
        "Case a cocher 1",
        [50, 650, 70, 670],
        {
            "/FT": NameObject("/Btn"),
            "/V": NameObject("/Off"),
            "/AS": NameObject("/Off"),
            "/AP": DictionaryObject(
                {
                    NameObject("/N"): DictionaryObject(
                        {
                            NameObject("/Oui"): writer._add_object(DictionaryObject()),
                            NameObject("/Off"): writer._add_object(DictionaryObject()),
                        }
                    )
                }
            ),
        },
    )

    annots = ArrayObject([text_ref, checkbox_ref])
    page[NameObject("/Annots")] = annots
    writer._root_object[NameObject("/AcroForm")] = DictionaryObject(
        {
            NameObject("/Fields"): annots,
            NameObject("/NeedAppearances"): BooleanObject(True),
            NameObject("/DR"): default_resources,
            NameObject("/DA"): default_appearance,
        }
    )
    with open(path, "wb") as handle:
        writer.write(handle)


# ---------------------------------------------------------------------------
# In-memory fakes (duck-typed against domain.ports)
# ---------------------------------------------------------------------------
class FakeTemplateStore:
    def __init__(
        self, bundle: TemplateBundle | None = None, error: Exception | None = None
    ) -> None:
        self._bundle = bundle
        self._error = error
        self.calls: list[str] = []

    async def fetch(self, uri: str) -> TemplateBundle:
        self.calls.append(uri)
        if self._error is not None:
            raise self._error
        assert self._bundle is not None
        return self._bundle


class RecordingValidator:
    def __init__(self, error: Exception | None = None) -> None:
        self._error = error
        self.calls: list[tuple[dict, dict]] = []

    def validate(self, data: Any, schema: Any) -> None:
        self.calls.append((dict(data), dict(schema)))
        if self._error is not None:
            raise self._error


class FakeRenderer:
    def __init__(self, content: bytes = b"%PDF-1.4 fake", error: Exception | None = None) -> None:
        self.content = content
        self.error = error
        self.calls: list[tuple[OutputFormat, dict, str | None]] = []

    def render(
        self, bundle: TemplateBundle, data: Any, fmt: OutputFormat, *, locale: str | None
    ) -> RenderedArtifact:
        self.calls.append((fmt, dict(data), locale))
        if self.error is not None:
            raise self.error
        return RenderedArtifact(
            format=fmt, filename=bundle.manifest.filename_for(fmt), content=self.content
        )


class SingleRendererRegistry:
    def __init__(
        self, renderer: Any, *, supports: set[tuple[Engine, OutputFormat]] | None = None
    ) -> None:
        self._renderer = renderer
        self._supports = supports

    def get(self, engine: Engine, fmt: OutputFormat) -> Any:
        if self._supports is not None and (engine, fmt) not in self._supports:
            return None
        return self._renderer


class FakeObjectStore:
    def __init__(self, error: Exception | None = None, presign: str | None = None) -> None:
        self.error = error
        self.presign = presign
        self.puts: list[tuple[str, bytes, str, int | None]] = []

    async def put(
        self,
        key: str,
        content: bytes,
        content_type: str,
        *,
        presign_ttl: int | None = None,
    ) -> StoredArtifact:
        self.puts.append((key, content, content_type, presign_ttl))
        if self.error is not None:
            raise self.error
        return StoredArtifact(
            uri=f"s3://optimce-documents/{key}",
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            presigned_url=self.presign,
        )


class InlineExecutor:
    """Inline RenderExecutor that wraps non-DocGenError into the caller's care.

    Mirrors adapters.render_executor.InlineRenderExecutor without importing it,
    so domain tests stay independent of the adapter layer.
    """

    async def run(
        self,
        renderer: Any,
        bundle: TemplateBundle,
        data: Any,
        fmt: OutputFormat,
        *,
        locale: str | None,
    ) -> RenderedArtifact:
        from domain.errors import RenderError

        try:
            return cast(RenderedArtifact, renderer.render(bundle, data, fmt, locale=locale))
        except DocGenError:
            raise
        except Exception as exc:
            raise RenderError(f"render failed: {exc}") from exc


class FakeTransport:
    def __init__(self, fail_result: bool = False) -> None:
        self.fail_result = fail_result
        self.results: list[tuple[str, Any]] = []
        self.dlqs: list[tuple[bytes, dict[str, str]]] = []

    async def publish_result(self, subject: str, result: Any) -> None:
        if self.fail_result:
            raise RuntimeError("broker down")
        self.results.append((subject, result))

    async def publish_dlq(self, payload: bytes, headers: Any) -> None:
        self.dlqs.append((payload, dict(headers)))


class FakeMsg:
    """Stands in for a nats-py JetStream message in handler tests."""

    def __init__(self, data: bytes, *, num_delivered: int = 1) -> None:
        self.data = data
        self.metadata = _Meta(num_delivered)
        self.acked = False
        self.naked = False
        self.nak_delay: int | None = None

    async def ack(self) -> None:
        self.acked = True

    async def nak(self, delay: int | None = None) -> None:
        self.naked = True
        self.nak_delay = delay


class _Meta:
    def __init__(self, num_delivered: int) -> None:
        self.num_delivered = num_delivered


def request_bytes(**overrides: Any) -> bytes:
    return make_request(**overrides).model_dump_json().encode()


def raw_request_dict(**overrides: Any) -> bytes:
    """A raw request payload (dict → JSON) for malformed-input tests."""
    base = json.loads(make_request().model_dump_json())
    base.update(overrides)
    return json.dumps(base).encode()
