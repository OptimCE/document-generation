"""Builders that turn a request + outcome into a ``GenerationResult``.

Centralised so the success path (orchestrator) and the failure paths (worker
dispatcher) construct results identically — same ``request_id`` correlation and
the same opaque ``metadata`` echo.
"""

from __future__ import annotations

from domain.errors import DocGenError
from domain.models import (
    Artifact,
    GenerationRequest,
    GenerationResult,
    GenerationStatus,
    ResultError,
)


def success_result(
    request: GenerationRequest,
    artifacts: list[Artifact],
    *,
    template_version: str,
    generated_at: str,
) -> GenerationResult:
    return GenerationResult(
        request_id=request.request_id,
        status=GenerationStatus.SUCCESS,
        artifacts=artifacts,
        error=None,
        template_version=template_version,
        generated_at=generated_at,
        metadata=request.metadata,
    )


def failure_result(
    request: GenerationRequest,
    *,
    code: str,
    message: str,
    permanent: bool,
    generated_at: str,
    template_version: str | None = None,
) -> GenerationResult:
    return GenerationResult(
        request_id=request.request_id,
        status=GenerationStatus.FAILED,
        artifacts=[],
        error=ResultError(code=code, message=message, permanent=permanent),
        template_version=template_version,
        generated_at=generated_at,
        metadata=request.metadata,
    )


def failure_from_error(
    request: GenerationRequest,
    exc: DocGenError,
    *,
    generated_at: str,
    template_version: str | None = None,
) -> GenerationResult:
    return failure_result(
        request,
        code=str(exc.code),
        message=str(exc) or exc.__class__.__name__,
        permanent=exc.permanent,
        generated_at=generated_at,
        template_version=template_version,
    )
