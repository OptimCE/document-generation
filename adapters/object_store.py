"""ObjectStore adapter: write artifacts to the configured output bucket.

Computes ``size_bytes`` and ``sha256`` over the exact bytes written, returns the
canonical ``s3://output-bucket/key`` URI, and (when a presign TTL is supplied)
attaches a presigned GET URL. Least privilege: only the output bucket is ever
written; nothing is read.
"""

from __future__ import annotations

import hashlib
import logging

from core.storage import client
from domain.errors import StorageError
from domain.ports import StoredArtifact

logger = logging.getLogger(__name__)


class ObjectStorageObjectStore:
    def __init__(self, *, bucket: str) -> None:
        self._bucket = bucket

    async def put(
        self,
        key: str,
        content: bytes,
        content_type: str,
        *,
        presign_ttl: int | None = None,
    ) -> StoredArtifact:
        try:
            await client.put_bytes(self._bucket, key, content, content_type)
        except client.TransientStorageError as exc:
            raise StorageError(f"upload {key!r}: {exc}") from exc

        presigned: str | None = None
        if presign_ttl is not None:
            try:
                presigned = await client.presign_get(self._bucket, key, presign_ttl)
            except Exception:
                # Presigning is best-effort: the canonical URI is always returned,
                # so a signing hiccup must not fail an otherwise-successful write.
                logger.warning("presign failed for key=%s; returning canonical uri only", key)

        return StoredArtifact(
            uri=f"s3://{self._bucket}/{key}",
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            presigned_url=presigned,
        )
