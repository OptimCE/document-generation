"""TemplateStore adapter tests — storage client mocked, real local filesystem."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import core.storage.client as client_mod
from adapters.template_store import ObjectStorageTemplateStore, parse_s3_uri
from domain.errors import TemplateNotFoundError

_BUCKET = "optimce-templates"
_PREFIX = "billing/invoice/v1/"
_URI = f"s3://{_BUCKET}/{_PREFIX}"

_MANIFEST = json.dumps(
    {
        "id": "billing.invoice",
        "version": "1",
        "engine": "jinja-html",
        "supported_formats": ["pdf", "html"],
        "entrypoint": "invoice.html",
        "required_fields": {},
    }
).encode()

_OBJECTS = {
    f"{_PREFIX}manifest.json": _MANIFEST,
    f"{_PREFIX}invoice.html": b"<h1>{{ data.x }}</h1>",
    f"{_PREFIX}invoice.css": b"h1 { color: red; }",
}


@pytest.fixture
def patched_client(monkeypatch):
    counters = {"list": 0, "get": 0}

    async def fake_list(bucket, prefix):
        counters["list"] += 1
        return [k for k in _OBJECTS if k.startswith(prefix)]

    async def fake_get(bucket, key):
        counters["get"] += 1
        return _OBJECTS[key]

    monkeypatch.setattr(client_mod, "list_prefix", fake_list)
    monkeypatch.setattr(client_mod, "get_bytes", fake_get)
    return counters


def _store(tmp_path: Path) -> ObjectStorageTemplateStore:
    return ObjectStorageTemplateStore(bucket=_BUCKET, cache_dir=tmp_path / "cache")


@pytest.mark.parametrize(
    ("uri", "expected"),
    [
        ("s3://b/p/q/", ("b", "p/q/")),
        ("s3://b/p/q", ("b", "p/q/")),
        ("s3://b/", ("b", "")),
    ],
)
def test_parse_s3_uri(uri, expected):
    assert parse_s3_uri(uri) == expected


def test_parse_s3_uri_rejects_non_s3():
    with pytest.raises(ValueError, match="s3://"):
        parse_s3_uri("https://example.com/x")


async def test_fetch_downloads_and_parses_manifest(tmp_path, patched_client):
    bundle = await _store(tmp_path).fetch(_URI)

    assert bundle.manifest.id == "billing.invoice"
    assert bundle.manifest.version == "1"
    assert (bundle.root / "manifest.json").is_file()
    assert (bundle.root / "invoice.html").read_bytes() == _OBJECTS[f"{_PREFIX}invoice.html"]
    assert (bundle.root / "invoice.css").is_file()


async def test_fetch_is_cached_on_second_call(tmp_path, patched_client):
    store = _store(tmp_path)
    await store.fetch(_URI)
    assert patched_client["list"] == 1
    await store.fetch(_URI)
    # Already on disk → no second download.
    assert patched_client["list"] == 1


async def test_wrong_bucket_is_template_not_found(tmp_path, patched_client):
    store = ObjectStorageTemplateStore(bucket=_BUCKET, cache_dir=tmp_path)
    with pytest.raises(TemplateNotFoundError, match="not the templates bucket"):
        await store.fetch("s3://some-other-bucket/billing/invoice/v1/")


async def test_missing_manifest_is_template_not_found(tmp_path, monkeypatch):
    async def fake_list(bucket, prefix):
        return [f"{_PREFIX}invoice.html"]  # no manifest.json

    async def fake_get(bucket, key):
        return b"x"

    monkeypatch.setattr(client_mod, "list_prefix", fake_list)
    monkeypatch.setattr(client_mod, "get_bytes", fake_get)

    with pytest.raises(TemplateNotFoundError, match="no manifest.json"):
        await _store(tmp_path).fetch(_URI)


async def test_non_s3_uri_is_template_not_found(tmp_path, patched_client):
    with pytest.raises(TemplateNotFoundError):
        await _store(tmp_path).fetch("https://example.com/x")


async def test_path_traversal_is_rejected(tmp_path, patched_client):
    with pytest.raises(TemplateNotFoundError):
        await _store(tmp_path).fetch(f"s3://{_BUCKET}/../escape/")
