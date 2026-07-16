"""TemplateStore adapter: object storage → local template bundle.

Resolves a ``s3://templates-bucket/prefix/`` URI to a local directory holding the
manifest, the entry template and its relative assets, plus the parsed manifest.

The downloaded prefix is cached on local disk keyed by the (immutable, versioned)
URI — the only state this otherwise-stateless worker keeps. A fresh prefix is
downloaded into a temp dir and atomically renamed into place, so a partial
download is never observed as a complete cache entry.

Least privilege: the URI's bucket must equal the configured templates bucket;
any other bucket is rejected.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from pathlib import Path, PurePosixPath

from core.storage import client
from domain.errors import StorageError, TemplateNotFoundError
from domain.models import Manifest
from domain.ports import TemplateBundle

logger = logging.getLogger(__name__)

_MANIFEST_NAME = "manifest.json"


def parse_s3_uri(uri: str) -> tuple[str, str]:
    """Split ``s3://bucket/prefix/...`` into ``(bucket, prefix)``.

    The prefix is normalised to end with a single ``/`` (directory semantics).
    Raises ``ValueError`` if the scheme is not ``s3://`` or the bucket is empty.
    """
    if not uri.startswith("s3://"):
        raise ValueError(f"template uri must use the s3:// scheme: {uri!r}")
    remainder = uri[len("s3://") :]
    bucket, _, prefix = remainder.partition("/")
    if not bucket:
        raise ValueError(f"template uri has no bucket: {uri!r}")
    prefix = prefix.strip("/")
    return bucket, f"{prefix}/" if prefix else ""


class ObjectStorageTemplateStore:
    """Fetches template prefixes from the configured read-only templates bucket."""

    def __init__(self, *, bucket: str, cache_dir: Path) -> None:
        self._bucket = bucket
        self._cache_dir = cache_dir

    async def fetch(self, uri: str) -> TemplateBundle:
        try:
            bucket, prefix = parse_s3_uri(uri)
        except ValueError as exc:
            raise TemplateNotFoundError(str(exc)) from exc

        if bucket != self._bucket:
            raise TemplateNotFoundError(
                f"template uri bucket {bucket!r} is not the templates bucket {self._bucket!r}"
            )
        if not prefix:
            raise TemplateNotFoundError(f"template uri has no prefix: {uri!r}")

        local_dir = self._local_dir_for(prefix)
        manifest_path = local_dir / _MANIFEST_NAME
        if not manifest_path.is_file():
            await self._download_prefix(prefix, local_dir)

        return TemplateBundle(manifest=self._load_manifest(manifest_path, uri), root=local_dir)

    # -- internals ----------------------------------------------------------

    def _local_dir_for(self, prefix: str) -> Path:
        """Map a prefix to a cache directory, guarding against path traversal."""
        cache_root = self._cache_dir.resolve()
        candidate = (cache_root / prefix).resolve()
        if cache_root != candidate and cache_root not in candidate.parents:
            raise TemplateNotFoundError(f"template prefix escapes the cache root: {prefix!r}")
        return candidate

    async def _download_prefix(self, prefix: str, local_dir: Path) -> None:
        try:
            keys = await client.list_prefix(self._bucket, prefix)
        except client.TransientStorageError as exc:
            raise StorageError(f"list template prefix {prefix!r}: {exc}") from exc

        if not any(PurePosixPath(k).name == _MANIFEST_NAME for k in keys):
            raise TemplateNotFoundError(f"no {_MANIFEST_NAME} under template prefix {prefix!r}")

        self._cache_dir.mkdir(parents=True, exist_ok=True)
        tmp_dir = Path(tempfile.mkdtemp(dir=self._cache_dir))
        try:
            for key in keys:
                rel = key[len(prefix) :]
                if not rel or key.endswith("/"):
                    continue  # the prefix "directory" placeholder itself
                target = (tmp_dir / rel).resolve()
                if tmp_dir.resolve() not in target.parents:
                    raise TemplateNotFoundError(f"template object escapes its prefix: {key!r}")
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    target.write_bytes(await client.get_bytes(self._bucket, key))
                except client.ObjectNotFound as exc:
                    # Listed-then-deleted mid-fetch — treat the template as gone.
                    raise TemplateNotFoundError(f"template object vanished: {key!r}") from exc
                except client.TransientStorageError as exc:
                    raise StorageError(f"download template object {key!r}: {exc}") from exc
            self._publish(tmp_dir, local_dir)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    @staticmethod
    def _publish(tmp_dir: Path, local_dir: Path) -> None:
        """Atomically move the freshly-downloaded prefix into the cache.

        If another in-flight fetch already populated ``local_dir`` (same
        immutable URI), keep theirs and drop ours — the content is identical.
        """
        if local_dir.exists():
            return
        local_dir.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(tmp_dir, local_dir)
        except OSError:
            # A concurrent winner created local_dir between the check and the
            # rename; their copy is authoritative.
            if not local_dir.exists():
                raise

    @staticmethod
    def _load_manifest(manifest_path: Path, uri: str) -> Manifest:
        try:
            return Manifest.model_validate_json(manifest_path.read_bytes())
        except OSError as exc:
            raise TemplateNotFoundError(f"manifest unreadable for {uri!r}: {exc}") from exc
        except ValueError as exc:
            # Malformed manifest — a template authoring error, permanent.
            raise TemplateNotFoundError(f"invalid manifest for {uri!r}: {exc}") from exc
