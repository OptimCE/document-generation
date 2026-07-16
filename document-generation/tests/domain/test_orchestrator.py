"""Orchestrator pipeline tests — hermetic, with in-memory fakes."""

from __future__ import annotations

import pytest

from domain.errors import (
    ErrorCode,
    RenderError,
    SchemaValidationError,
    StorageError,
    TemplateNotFoundError,
)
from domain.models import GenerationStatus
from domain.orchestrator import build_object_key, process_request
from tests.conftest import (
    FIXED_GENERATED_AT,
    FakeObjectStore,
    FakeRenderer,
    FakeTemplateStore,
    InlineExecutor,
    RecordingValidator,
    SingleRendererRegistry,
    make_request,
)


async def _run(request, *, template_store, validator, registry, object_store):
    return await process_request(
        request,
        template_store=template_store,
        validator=validator,
        registry=registry,
        render_executor=InlineExecutor(),
        object_store=object_store,
        generated_at=FIXED_GENERATED_AT,
    )


def _deps(jinja_bundle, *, renderer=None, validator=None, object_store=None, supports=None):
    renderer = renderer or FakeRenderer()
    return {
        "template_store": FakeTemplateStore(jinja_bundle),
        "validator": validator or RecordingValidator(),
        "registry": SingleRendererRegistry(renderer, supports=supports),
        "object_store": object_store or FakeObjectStore(),
    }


async def test_happy_path_success(jinja_bundle):
    renderer = FakeRenderer(content=b"%PDF-1.4 hello")
    store = FakeObjectStore()
    deps = _deps(jinja_bundle, renderer=renderer, object_store=store)

    result = await _run(make_request(), **deps)

    assert result.status is GenerationStatus.SUCCESS
    assert result.error is None
    assert result.template_version == "1"
    assert result.generated_at == FIXED_GENERATED_AT
    assert result.tenant_id == "tenant-1"
    assert result.metadata == {"invoice_id": "abc-123"}
    assert len(result.artifacts) == 1
    artifact = result.artifacts[0]
    assert artifact.format.value == "pdf"
    assert artifact.uri == "s3://optimce-documents/billing/invoices/2026/06/inv-00042/invoice.pdf"
    assert artifact.size_bytes == len(b"%PDF-1.4 hello")
    assert store.puts[0][0] == "billing/invoices/2026/06/inv-00042/invoice.pdf"
    assert renderer.calls  # renderer was invoked


async def test_validation_gate_blocks_render(jinja_bundle):
    renderer = FakeRenderer()
    store = FakeObjectStore()
    validator = RecordingValidator(error=SchemaValidationError("missing customer_name"))
    deps = _deps(jinja_bundle, renderer=renderer, validator=validator, object_store=store)

    result = await _run(make_request(), **deps)

    assert result.status is GenerationStatus.FAILED
    assert result.error is not None
    assert result.error.code == ErrorCode.VALIDATION_ERROR
    assert result.error.permanent is True
    # The whole point: no render, no write.
    assert renderer.calls == []
    assert store.puts == []


async def test_unsupported_format_not_in_manifest(jinja_bundle):
    renderer = FakeRenderer()
    validator = RecordingValidator()
    deps = _deps(jinja_bundle, renderer=renderer, validator=validator)
    request = make_request(outputs=[{"format": "xlsx"}])

    result = await _run(request, **deps)

    assert result.status is GenerationStatus.FAILED
    assert result.error.code == ErrorCode.UNSUPPORTED_FORMAT
    assert result.error.permanent is True
    # Format support is checked before validation and before render.
    assert validator.calls == []
    assert renderer.calls == []


async def test_unsupported_when_no_renderer_for_pair(jinja_bundle):
    deps = _deps(jinja_bundle, supports=set())  # registry returns None for any pair
    result = await _run(make_request(), **deps)
    assert result.status is GenerationStatus.FAILED
    assert result.error.code == ErrorCode.UNSUPPORTED_FORMAT


async def test_template_not_found_is_permanent(jinja_bundle):
    deps = _deps(jinja_bundle)
    deps["template_store"] = FakeTemplateStore(error=TemplateNotFoundError("gone"))

    result = await _run(make_request(), **deps)

    assert result.status is GenerationStatus.FAILED
    assert result.error.code == ErrorCode.TEMPLATE_NOT_FOUND
    assert result.error.permanent is True
    assert result.template_version is None  # never learned the version


async def test_storage_failure_is_transient(jinja_bundle):
    store = FakeObjectStore(error=StorageError("storage 503"))
    deps = _deps(jinja_bundle, object_store=store)

    result = await _run(make_request(), **deps)

    assert result.status is GenerationStatus.FAILED
    assert result.error.code == ErrorCode.STORAGE_ERROR
    assert result.error.permanent is False
    assert result.template_version == "1"


async def test_render_failure_is_transient(jinja_bundle):
    renderer = FakeRenderer(error=RenderError("weasyprint boom"))
    deps = _deps(jinja_bundle, renderer=renderer)

    result = await _run(make_request(), **deps)

    assert result.status is GenerationStatus.FAILED
    assert result.error.code == ErrorCode.RENDER_ERROR
    assert result.error.permanent is False


async def test_presigned_url_attached_when_ttl_set(jinja_bundle):
    store = FakeObjectStore(presign="https://signed.example/inv")
    deps = _deps(jinja_bundle, object_store=store)
    request = make_request(options={"presign_ttl": 3600})

    result = await _run(request, **deps)

    assert result.artifacts[0].presigned_url == "https://signed.example/inv"
    assert store.puts[0][3] == 3600  # presign_ttl forwarded


async def test_multiple_formats_produce_multiple_artifacts(jinja_bundle):
    store = FakeObjectStore()
    deps = _deps(jinja_bundle, object_store=store)
    request = make_request(outputs=[{"format": "pdf"}, {"format": "html"}])

    result = await _run(request, **deps)

    assert [a.format.value for a in result.artifacts] == ["pdf", "html"]
    assert [p[0].rsplit("/", 1)[-1] for p in store.puts] == ["invoice.pdf", "invoice.html"]


async def test_deterministic_output_key_and_bytes(jinja_bundle):
    request = make_request()
    keys = []
    digests = []
    for _ in range(2):
        store = FakeObjectStore()
        result = await _run(request, **_deps(jinja_bundle, object_store=store))
        keys.append(store.puts[0][0])
        digests.append(result.artifacts[0].sha256)

    assert keys[0] == keys[1]
    assert digests[0] == digests[1]


@pytest.mark.parametrize(
    ("prefix", "filename", "expected"),
    [
        ("a/b/", "x.pdf", "a/b/x.pdf"),
        ("a/b", "x.pdf", "a/b/x.pdf"),
        ("a/b/", "/x.pdf", "a/b/x.pdf"),
    ],
)
def test_build_object_key_normalises_slashes(prefix, filename, expected):
    assert build_object_key(prefix, filename) == expected
