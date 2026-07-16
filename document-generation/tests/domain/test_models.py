"""Contract model tests: manifest defaults, result serialization, request parsing."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from domain.models import (
    Engine,
    GenerationResult,
    GenerationStatus,
    Manifest,
    OutputFormat,
    ResultError,
)
from tests.conftest import make_request


def _manifest(**overrides):
    base = {
        "id": "billing.invoice",
        "version": "3",
        "engine": "jinja-html",
        "supported_formats": ["pdf"],
        "required_fields": {},
    }
    base.update(overrides)
    return Manifest.model_validate(base)


def test_manifest_entrypoint_defaults_by_engine():
    assert _manifest(engine="jinja-html").resolve_entrypoint() == "template.html"
    xlsx_manifest = _manifest(engine="xlsx", supported_formats=["xlsx"])
    assert xlsx_manifest.resolve_entrypoint() == "template.xlsx"
    assert _manifest(entrypoint="invoice.html").resolve_entrypoint() == "invoice.html"


def test_manifest_output_basename_defaults_to_last_id_segment():
    assert _manifest(id="billing.invoice").resolve_output_basename() == "invoice"
    assert _manifest(output_basename="facture").resolve_output_basename() == "facture"


def test_manifest_filename_for():
    manifest = _manifest(id="billing.invoice")
    assert manifest.filename_for(OutputFormat.PDF) == "invoice.pdf"


def test_manifest_ignores_unknown_fields():
    manifest = _manifest(future_field={"x": 1})
    assert manifest.engine is Engine.JINJA_HTML


def test_success_result_json_omits_null_fields():
    result = GenerationResult(
        request_id="req-1",
        tenant_id="tenant-1",
        status=GenerationStatus.SUCCESS,
        artifacts=[],
        error=None,
        template_version="3",
        generated_at="2026-06-18T00:00:00+00:00",
        metadata={"invoice_id": "x"},
    )
    payload = json.loads(result.to_json_bytes())
    assert "error" not in payload
    assert payload["status"] == "success"
    assert payload["tenant_id"] == "tenant-1"
    assert payload["metadata"] == {"invoice_id": "x"}


def test_failed_result_json_includes_error():
    result = GenerationResult(
        request_id="req-1",
        tenant_id="tenant-1",
        status=GenerationStatus.FAILED,
        error=ResultError(code="VALIDATION_ERROR", message="bad", permanent=True),
        generated_at="2026-06-18T00:00:00+00:00",
    )
    payload = json.loads(result.to_json_bytes())
    assert payload["error"] == {"code": "VALIDATION_ERROR", "message": "bad", "permanent": True}
    assert payload["artifacts"] == []


def test_requested_formats_dedupes_preserving_order():
    request = make_request(outputs=[{"format": "pdf"}, {"format": "html"}, {"format": "pdf"}])
    assert [f.value for f in request.requested_formats] == ["pdf", "html"]


def test_request_ignores_unknown_top_level_fields():
    request = make_request(unexpected_field="ignored")
    assert request.request_id == "req-1"


def test_request_requires_at_least_one_output():
    with pytest.raises(ValidationError):
        make_request(outputs=[])


def test_output_format_content_types():
    assert OutputFormat.PDF.content_type == "application/pdf"
    assert OutputFormat.HTML.content_type == "text/html; charset=utf-8"
    assert "spreadsheetml" in OutputFormat.XLSX.content_type
