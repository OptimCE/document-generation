"""Storage error-classification tests."""

from __future__ import annotations

from botocore.exceptions import ClientError

from core.storage.client import (
    ObjectNotFound,
    TransientStorageError,
    _classify_client_error,
)


def _client_error(code: str, status: int) -> ClientError:
    return ClientError(
        {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}},
        "GetObject",
    )


def test_missing_key_is_object_not_found():
    result = _classify_client_error(_client_error("NoSuchKey", 404), "k")
    assert isinstance(result, ObjectNotFound)


def test_missing_bucket_is_transient_not_permanent():
    # A missing bucket is operational — retry, don't permanently ack the request.
    result = _classify_client_error(_client_error("NoSuchBucket", 404), "k")
    assert isinstance(result, TransientStorageError)


def test_server_5xx_is_transient():
    result = _classify_client_error(_client_error("InternalError", 503), "k")
    assert isinstance(result, TransientStorageError)


def test_other_client_error_propagates_unchanged():
    original = _client_error("AccessDenied", 403)
    assert _classify_client_error(original, "k") is original
