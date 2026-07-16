"""ObjectStore adapter tests — storage client mocked."""

from __future__ import annotations

import hashlib

import pytest

import core.storage.client as client_mod
from adapters.object_store import ObjectStorageObjectStore
from domain.errors import StorageError

_BUCKET = "optimce-documents"


def _store() -> ObjectStorageObjectStore:
    return ObjectStorageObjectStore(bucket=_BUCKET)


async def test_put_returns_uri_size_and_sha256(monkeypatch):
    captured = []

    async def fake_put(bucket, key, content, content_type=None):
        captured.append((bucket, key, content, content_type))

    monkeypatch.setattr(client_mod, "put_bytes", fake_put)

    art = await _store().put("k/x.pdf", b"hello", "application/pdf")

    assert art.uri == "s3://optimce-documents/k/x.pdf"
    assert art.size_bytes == 5
    assert art.sha256 == hashlib.sha256(b"hello").hexdigest()
    assert art.presigned_url is None
    assert captured[0] == (_BUCKET, "k/x.pdf", b"hello", "application/pdf")


async def test_presigned_url_attached_when_ttl_set(monkeypatch):
    async def fake_put(bucket, key, content, content_type=None):
        return None

    async def fake_presign(bucket, key, ttl):
        return f"https://signed/{key}?ttl={ttl}"

    monkeypatch.setattr(client_mod, "put_bytes", fake_put)
    monkeypatch.setattr(client_mod, "presign_get", fake_presign)

    art = await _store().put("k/x.pdf", b"hi", "application/pdf", presign_ttl=60)
    assert art.presigned_url == "https://signed/k/x.pdf?ttl=60"


async def test_presign_failure_is_swallowed(monkeypatch):
    async def fake_put(bucket, key, content, content_type=None):
        return None

    async def fake_presign(bucket, key, ttl):
        raise RuntimeError("signing boom")

    monkeypatch.setattr(client_mod, "put_bytes", fake_put)
    monkeypatch.setattr(client_mod, "presign_get", fake_presign)

    art = await _store().put("k/x.pdf", b"hi", "application/pdf", presign_ttl=60)
    assert art.presigned_url is None  # write still succeeded
    assert art.uri == "s3://optimce-documents/k/x.pdf"


async def test_transient_put_failure_raises_storage_error(monkeypatch):
    async def fake_put(bucket, key, content, content_type=None):
        raise client_mod.TransientStorageError("503")

    monkeypatch.setattr(client_mod, "put_bytes", fake_put)

    with pytest.raises(StorageError):
        await _store().put("k/x.pdf", b"hi", "application/pdf")
