"""The pure document-generation pipeline.

``process_request`` is a deterministic-keyed orchestration of:

    fetch template + manifest → check formats → validate data → render → store
    → build result.

It depends only on ports (``domain.ports``) and domain models, so it is unit
tested with in-memory fakes — no NATS, object storage or rendering libraries.

Failure handling: any classified failure (``DocGenError``) is caught and turned
into a *failed* ``GenerationResult`` carrying the right error code, the
``permanent`` flag, and the ``template_version`` known so far. Unexpected (non
``DocGenError``) exceptions propagate to the caller (the worker dispatcher),
which logs them and treats them as transient. The result envelope's
``generated_at`` is supplied by the caller — it is metadata, not part of any
artifact's bytes, so it does not compromise render determinism.
"""

from __future__ import annotations

from domain.errors import DocGenError, UnsupportedFormatError
from domain.models import Artifact, GenerationRequest, GenerationResult, RenderedArtifact
from domain.ports import (
    ObjectStore,
    Renderer,
    RendererRegistry,
    RenderExecutor,
    TemplateStore,
    Validator,
)
from domain.results import failure_from_error, success_result


def build_object_key(key_prefix: str, filename: str) -> str:
    """Join the caller's ``key_prefix`` with a per-format filename.

    The caller supplies only a prefix (never a bucket). Normalised to exactly one
    separator so ``billing/.../inv-00042/`` + ``invoice.pdf`` →
    ``billing/.../inv-00042/invoice.pdf`` regardless of a trailing slash.
    """
    return f"{key_prefix.rstrip('/')}/{filename.lstrip('/')}"


async def process_request(
    request: GenerationRequest,
    *,
    template_store: TemplateStore,
    validator: Validator,
    registry: RendererRegistry,
    render_executor: RenderExecutor,
    object_store: ObjectStore,
    generated_at: str,
) -> GenerationResult:
    template_version: str | None = None
    try:
        # ---- Fetch template + manifest -------------------------------------
        bundle = await template_store.fetch(request.template.uri)
        manifest = bundle.manifest
        template_version = manifest.version

        # ---- Format support (permanent) ------------------------------------
        # Every requested format must be declared by the template AND have a
        # renderer for the template's engine. Checked up front so an
        # unsupported format fails before we validate or render anything.
        renderers: dict = {}
        for fmt in request.requested_formats:
            if fmt not in manifest.supported_formats:
                raise UnsupportedFormatError(
                    f"format {fmt.value!r} not in template supported_formats "
                    f"{[f.value for f in manifest.supported_formats]}"
                )
            renderer = registry.get(manifest.engine, fmt)
            if renderer is None:
                raise UnsupportedFormatError(
                    f"no renderer for engine {manifest.engine.value!r} + format {fmt.value!r}"
                )
            renderers[fmt] = renderer

        # ---- Validation gate (permanent) — BEFORE any render ---------------
        validator.validate(request.data, manifest.required_fields)

        # ---- Render every requested format (transient on failure) ----------
        rendered: list[RenderedArtifact] = []
        for fmt in request.requested_formats:
            renderer_for_fmt: Renderer = renderers[fmt]
            art = await render_executor.run(
                renderer_for_fmt,
                bundle,
                request.data,
                fmt,
                locale=request.options.locale,
            )
            rendered.append(art)

        # ---- Store every artifact (transient on failure) -------------------
        # Deterministic keys → redelivery overwrites the same object, so storing
        # after a partial earlier attempt is idempotent.
        artifacts: list[Artifact] = []
        for art in rendered:
            key = build_object_key(request.key_prefix, art.filename)
            stored = await object_store.put(
                key,
                art.content,
                art.content_type,
                presign_ttl=request.options.presign_ttl,
            )
            artifacts.append(
                Artifact(
                    format=art.format,
                    uri=stored.uri,
                    presigned_url=stored.presigned_url,
                    size_bytes=stored.size_bytes,
                    sha256=stored.sha256,
                )
            )

        return success_result(
            request,
            artifacts,
            template_version=template_version,
            generated_at=generated_at,
        )

    except DocGenError as exc:
        return failure_from_error(
            request,
            exc,
            generated_at=generated_at,
            template_version=template_version,
        )
