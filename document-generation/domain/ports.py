"""Ports: the framework-free interfaces the orchestrator depends on.

Concrete adapters (NATS, object storage, WeasyPrint, openpyxl, jsonschema) live
under ``adapters/`` and implement these Protocols. The orchestrator imports only
this module and ``domain.*`` — never an adapter — so the core stays testable with
in-memory fakes.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from domain.models import Engine, GenerationResult, Manifest, OutputFormat, RenderedArtifact


@dataclass(frozen=True)
class TemplateBundle:
    """A fetched template: its parsed manifest plus the local directory holding
    the entry file and relative assets (logo, CSS, fonts)."""

    manifest: Manifest
    root: Path


@dataclass(frozen=True)
class StoredArtifact:
    """The outcome of writing one artifact to the output bucket."""

    uri: str
    size_bytes: int
    sha256: str
    presigned_url: str | None = None


@runtime_checkable
class TemplateStore(Protocol):
    async def fetch(self, uri: str) -> TemplateBundle:
        """Resolve ``uri`` (``s3://templates-bucket/prefix/``) to a local bundle.

        Raises ``domain.errors.TemplateNotFoundError`` if the prefix or its
        ``manifest.json`` is absent, ``domain.errors.StorageError`` on a
        transient storage failure.
        """
        ...


@runtime_checkable
class Validator(Protocol):
    def validate(self, data: Mapping[str, Any], schema: Mapping[str, Any]) -> None:
        """Validate ``data`` against a JSON Schema.

        Returns ``None`` on success; raises ``domain.errors.SchemaValidationError``
        otherwise. A schema-less manifest (empty schema) validates everything.
        """
        ...


@runtime_checkable
class Renderer(Protocol):
    def render(
        self,
        bundle: TemplateBundle,
        data: Mapping[str, Any],
        fmt: OutputFormat,
        *,
        locale: str | None,
    ) -> RenderedArtifact:
        """Render ``data`` into one artifact. Synchronous and CPU-bound — runs
        inside a process pool via ``RenderExecutor``.

        Must be a pure function of (template, data): no wall-clock, no randomness.
        Raises ``domain.errors.RenderError`` on failure.
        """
        ...


@runtime_checkable
class RendererRegistry(Protocol):
    def get(self, engine: Engine, fmt: OutputFormat) -> Renderer | None:
        """Return the renderer for an (engine, format) pair, or ``None`` if no
        renderer can produce ``fmt`` from ``engine``."""
        ...


@runtime_checkable
class RenderExecutor(Protocol):
    async def run(
        self,
        renderer: Renderer,
        bundle: TemplateBundle,
        data: Mapping[str, Any],
        fmt: OutputFormat,
        *,
        locale: str | None,
    ) -> RenderedArtifact:
        """Execute ``renderer.render(...)``, keeping CPU work off the event loop.

        Production runs it in a process pool; tests run it inline. Translates a
        dead pool worker into ``domain.errors.RenderError`` (transient)."""
        ...


@runtime_checkable
class ObjectStore(Protocol):
    async def put(
        self,
        key: str,
        content: bytes,
        content_type: str,
        *,
        presign_ttl: int | None = None,
    ) -> StoredArtifact:
        """Write ``content`` to ``key`` in the output bucket and return its URI,
        size, sha256 and (when ``presign_ttl`` is set) a presigned GET URL.

        Raises ``domain.errors.StorageError`` on a transient failure."""
        ...


@runtime_checkable
class MessageTransport(Protocol):
    async def publish_result(self, subject: str, result: GenerationResult) -> None:
        """Publish a result to the request's ``reply_to`` subject (durable
        results stream)."""
        ...

    async def publish_dlq(self, payload: bytes, headers: Mapping[str, str]) -> None:
        """Route an exhausted/poison request to the dead-letter subject."""
        ...
