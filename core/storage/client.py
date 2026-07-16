"""aiobotocore-backed S3 primitives, parameterised by bucket.

The worker reads template prefixes from the templates bucket and writes
artifacts to the output bucket. Unlike the sibling services (single bucket, no
listing, no presign) this client takes the bucket per call and adds
``list_prefix`` and ``presign_get`` — but it stays a thin, function-based facade
over aiobotocore. The *adapters* decide which bucket each operation may touch
(least privilege); this module enforces nothing about bucket identity.

Errors are classified into the two buckets callers care about: ``ObjectNotFound``
(deterministic — the object is gone) and ``TransientStorageError`` (5xx /
timeout / transport — retryable).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import cast

from aiobotocore.session import get_session
from botocore.exceptions import (
    ClientError,
    EndpointConnectionError,
    ReadTimeoutError,
)
from botocore.exceptions import (
    ConnectionError as BotoConnectionError,
)

from core.config import settings

logger = logging.getLogger(__name__)


class ObjectNotFound(Exception):  # noqa: N818 — public exception, classified by callers
    """The requested key is absent from the bucket. Deterministic — no retry."""


class TransientStorageError(Exception):
    """A retryable storage error (5xx, timeout, transport). Let the worker retry."""


# Object-level "the key isn't there" — a deterministic miss.
_NOT_FOUND_CODES = {"NoSuchKey", "404"}
# A missing *bucket* is an operational/config problem, not a poison message —
# classified transient so it is retried rather than permanently acked.
_MISSING_BUCKET_CODES = {"NoSuchBucket"}


@asynccontextmanager
async def _client() -> AsyncIterator:
    """Yield a fresh aiobotocore S3 client bound to the configured endpoint.

    aiobotocore clients are async context managers; opening one per operation is
    cheap because the underlying aiohttp connector pool is shared across the loop.
    """
    session = get_session()
    async with session.create_client(
        "s3",
        endpoint_url=settings.STORAGE_ENDPOINT or None,
        region_name=settings.STORAGE_REGION,
        aws_access_key_id=settings.STORAGE_ACCESS_KEY,
        aws_secret_access_key=settings.STORAGE_SECRET_KEY,
    ) as client:
        yield client


def _classify_client_error(exc: ClientError, key: str) -> Exception:
    error_code = exc.response.get("Error", {}).get("Code", "")
    status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    if error_code in _MISSING_BUCKET_CODES:
        return TransientStorageError(f"missing bucket for {key}")
    if error_code in _NOT_FOUND_CODES or status == 404:
        return ObjectNotFound(key)
    if status is not None and 500 <= status < 600:
        return TransientStorageError(f"storage {status} for {key}")
    # botocore's ClientError is untyped (Any) under ignore_missing_imports.
    return cast(Exception, exc)


async def get_bytes(bucket: str, key: str) -> bytes:
    """Fetch the object at ``bucket/key`` and return its bytes.

    Raises ``ObjectNotFound`` for missing keys, ``TransientStorageError`` for
    network/server hiccups; other errors propagate.
    """
    try:
        async with _client() as s3:
            response = await s3.get_object(Bucket=bucket, Key=key)
            async with response["Body"] as stream:
                return cast(bytes, await stream.read())
    except ClientError as exc:
        raise _classify_client_error(exc, key) from exc
    except (TimeoutError, EndpointConnectionError, BotoConnectionError, ReadTimeoutError) as exc:
        raise TransientStorageError(f"storage transport: {exc}") from exc


async def list_prefix(bucket: str, prefix: str) -> list[str]:
    """Return every object key under ``bucket/prefix`` (paginated).

    Raises ``TransientStorageError`` on a network/server hiccup; other errors
    propagate. An absent prefix simply yields an empty list (S3 has no
    directories), which callers treat as ``TemplateNotFound``.
    """
    keys: list[str] = []
    try:
        async with _client() as s3:
            paginator = s3.get_paginator("list_objects_v2")
            async for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
                for obj in page.get("Contents", []):
                    keys.append(obj["Key"])
        return keys
    except ClientError as exc:
        raise _classify_client_error(exc, prefix) from exc
    except (TimeoutError, EndpointConnectionError, BotoConnectionError, ReadTimeoutError) as exc:
        raise TransientStorageError(f"storage transport: {exc}") from exc


async def put_bytes(bucket: str, key: str, content: bytes, content_type: str | None = None) -> None:
    """Upload ``content`` as the object at ``bucket/key``.

    Raises ``TransientStorageError`` on a network/server hiccup; other errors
    propagate so an unexpected failure is not silently swallowed.
    """
    extra: dict[str, str] = {}
    if content_type:
        extra["ContentType"] = content_type
    try:
        async with _client() as s3:
            await s3.put_object(Bucket=bucket, Key=key, Body=content, **extra)
    except ClientError as exc:
        raise _classify_client_error(exc, key) from exc
    except (TimeoutError, EndpointConnectionError, BotoConnectionError, ReadTimeoutError) as exc:
        raise TransientStorageError(f"storage transport: {exc}") from exc


async def presign_get(bucket: str, key: str, expires_in: int) -> str:
    """Generate a presigned GET URL for ``bucket/key`` valid for ``expires_in`` s.

    Note: the URL is signed against ``STORAGE_ENDPOINT``; behind an internal
    MinIO hostname it is only reachable from inside the network. Turning it into
    an externally-resolvable link is the calling domain's concern.
    """
    async with _client() as s3:
        return cast(
            str,
            await s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": bucket, "Key": key},
                ExpiresIn=expires_in,
            ),
        )


async def delete(bucket: str, key: str) -> None:
    """Delete ``bucket/key``. Idempotent and best-effort — never raises."""
    try:
        async with _client() as s3:
            await s3.delete_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "")
        if error_code in _NOT_FOUND_CODES:
            logger.debug("delete: object already gone key=%s", key)
            return
        logger.warning("delete failed for key=%s code=%s; leaking object", key, error_code)
    except Exception:
        logger.warning("delete failed for key=%s; leaking object", key, exc_info=True)
