"""NATS subscription + per-message handler for document-generation requests.

A single durable push subscription on the request subject within a queue group of
the same name; multiple worker replicas share work via JetStream (competing
consumers).

Outcome matrix — the request message body is the contract (no envelope):

* **Undecodable** — cannot build a result (no ``reply_to``); best-effort route to
  the DLQ for inspection, then ack/drop.
* **Success / permanent failure** (validation, template-not-found,
  unsupported-format) — publish the result to ``reply_to`` and ack. No retry
  could change the outcome.
* **Transient failure** (storage, render, transport, or an unexpected bug) — nak
  for redelivery while ``num_delivered < DOCGEN_MAX_DELIVER``; on the final
  delivery, publish a failed result *and* route the original message to the DLQ,
  then ack (terminate).
* **Emit failure** (the result/DLQ publish or ack itself fails — e.g. an
  un-routable ``reply_to`` that raises on every attempt, or a broker hiccup) —
  bounded exactly like a transient failure: nak under the bound, then DLQ + ack
  on exhaustion, so a permanently un-emittable request can never loop forever.

``DOCGEN_MAX_DELIVER`` is enforced in the handler (the primary DLQ path), with a
server-side ``max_deliver`` backstop on the consumer (``+ 1``) so even a handler
that never acks cannot cause truly unbounded redelivery.

The orchestrator (``domain.orchestrator.process_request``) builds success and
classified-failure results; this module only decides ack/nak/DLQ and owns the
NATS-facing concerns.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from nats.aio.msg import Msg
from nats.js import JetStreamContext
from nats.js.api import ConsumerConfig
from pydantic import ValidationError

from core import metrics as app_metrics
from core.config import settings
from core.context_vars import current_request_id
from domain.errors import ErrorCode
from domain.models import GenerationRequest, GenerationResult, GenerationStatus
from domain.orchestrator import process_request
from domain.ports import (
    MessageTransport,
    ObjectStore,
    RendererRegistry,
    RenderExecutor,
    TemplateStore,
    Validator,
)
from domain.results import failure_result
from shared.const import HEADER_DLQ_REASON, HEADER_REQUEST_ID

logger = logging.getLogger(__name__)

Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclasses.dataclass(frozen=True)
class HandlerDeps:
    """Everything the per-message handler needs, wired once at startup."""

    template_store: TemplateStore
    validator: Validator
    registry: RendererRegistry
    render_executor: RenderExecutor
    object_store: ObjectStore
    transport: MessageTransport
    max_deliver: int = settings.DOCGEN_MAX_DELIVER
    nak_delay: int = settings.DOCGEN_NAK_RETRY_DELAY_SECONDS
    clock: Clock = _utc_now


async def subscribe(
    js: JetStreamContext,
    deps: HandlerDeps,
    *,
    inflight: set[asyncio.Task] | None = None,
    max_ack_pending: int | None = None,
) -> JetStreamContext.PushSubscription:
    """Subscribe the worker to the request subject. Returns the subscription.

    ``inflight`` enables concurrent handling: the callback spawns each request as
    a tracked background task and returns, so the next message is delivered while
    this one renders. Left ``None`` the callback handles each message inline to
    completion — the shape the handler unit tests rely on.

    ``max_ack_pending`` caps how many messages JetStream delivers before we ack,
    so the broker never hands us more than the render pool can work.
    """
    config_kwargs: dict = {
        "ack_wait": settings.DOCGEN_ACK_WAIT_SECONDS,
        # Server-side backstop, one above the handler's own DLQ bound: the
        # in-handler DLQ (at DOCGEN_MAX_DELIVER) is the primary path and fires
        # first, but if the handler somehow never acks, JetStream still stops
        # redelivering. (nats-py reuses an existing durable's config, so changing
        # this on a live durable requires recreating it, not just a restart.)
        "max_deliver": settings.DOCGEN_MAX_DELIVER + 1,
    }
    if max_ack_pending is not None:
        config_kwargs["max_ack_pending"] = max_ack_pending
    sub = await js.subscribe(
        subject=settings.DOCGEN_REQUEST_SUBJECT,
        stream=settings.DOCGEN_REQUEST_STREAM,
        durable=settings.DOCGEN_DURABLE,
        queue=settings.DOCGEN_DURABLE,
        manual_ack=True,
        cb=_make_handler(deps, inflight=inflight),
        config=ConsumerConfig(**config_kwargs),
    )
    logger.info(
        "Subscribed to %s (stream=%s, durable=%s)",
        settings.DOCGEN_REQUEST_SUBJECT,
        settings.DOCGEN_REQUEST_STREAM,
        settings.DOCGEN_DURABLE,
    )
    return sub


def _make_handler(
    deps: HandlerDeps,
    *,
    inflight: set[asyncio.Task] | None = None,
) -> Callable[[Msg], Awaitable[None]]:
    """Build the per-message callback.

    nats-py serialises a subscription's callback — it awaits one message before
    pulling the next. To handle several requests concurrently we spawn the work
    as a background task and return; ``inflight`` tracks those tasks for a
    graceful drain on shutdown. With ``inflight`` None the message is handled
    inline to completion (unit-test shape, single-process fallback).
    """

    def _on_task_done(task: asyncio.Task) -> None:
        if inflight is not None:
            inflight.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.error("Docgen handler task crashed: %r", task.exception())

    async def handle(msg: Msg) -> None:
        if inflight is None:
            await _handle_message(msg, deps)
            return
        task = asyncio.create_task(_handle_message(msg, deps))
        inflight.add(task)
        task.add_done_callback(_on_task_done)

    return handle


async def _handle_message(msg: Msg, deps: HandlerDeps) -> None:
    request = _decode(msg.data)
    if request is None:
        await _drop_undecodable(msg, deps)
        return

    token = current_request_id.set(request.request_id)
    started = time.perf_counter()
    status = "unknown"
    try:
        result = await _build_result(request, deps)
        status = result.status.value
        outcome = await _emit(msg, request, result, deps)
        app_metrics.worker_messages.add(1, {"outcome": outcome})
        if result.status is GenerationStatus.SUCCESS:
            for artifact in result.artifacts:
                app_metrics.documents_rendered.add(
                    1, {"format": artifact.format.value, "status": "success"}
                )
    except Exception:
        # An ack/nak/publish failure during emit (broker hiccup, or a permanently
        # un-routable reply_to that raises every time). Bounded like a transient
        # failure so it can't loop forever. Never let the subscription die.
        logger.exception("Failed to emit outcome for request %s", request.request_id)
        outcome = await _handle_emit_failure(msg, request, deps)
        app_metrics.worker_messages.add(1, {"outcome": outcome})
    finally:
        app_metrics.render_duration.record(time.perf_counter() - started, {"status": status})
        current_request_id.reset(token)


def _decode(raw: bytes) -> GenerationRequest | None:
    try:
        return GenerationRequest.model_validate_json(raw)
    except (ValidationError, ValueError) as exc:
        logger.error("Undecodable docgen request; will drop: %s", exc)
        return None


async def _build_result(request: GenerationRequest, deps: HandlerDeps) -> GenerationResult:
    """Run the pipeline, mapping an unexpected (non-classified) bug to a
    transient failure so it is redelivered (bounded) rather than acked as done."""
    try:
        return await process_request(
            request,
            template_store=deps.template_store,
            validator=deps.validator,
            registry=deps.registry,
            render_executor=deps.render_executor,
            object_store=deps.object_store,
            generated_at=deps.clock().isoformat(),
        )
    except Exception:
        logger.exception("Unhandled error processing request %s", request.request_id)
        return failure_result(
            request,
            code=str(ErrorCode.RENDER_ERROR),
            message="unhandled_worker_error",
            permanent=False,
            generated_at=deps.clock().isoformat(),
        )


async def _emit(
    msg: Msg, request: GenerationRequest, result: GenerationResult, deps: HandlerDeps
) -> str:
    if _is_terminal(result):
        await deps.transport.publish_result(request.reply_to, result)
        await msg.ack()
        return "ack" if result.status is GenerationStatus.SUCCESS else "ack_permanent_fail"

    # Transient failure: redeliver until the bound, then dead-letter.
    if _num_delivered(msg) < deps.max_deliver:
        await msg.nak(delay=deps.nak_delay)
        return "nak"

    logger.warning(
        "Request %s exhausted %d deliveries; dead-lettering", request.request_id, deps.max_deliver
    )
    await deps.transport.publish_result(request.reply_to, result)
    await deps.transport.publish_dlq(msg.data, _dlq_headers(request, result))
    await msg.ack()
    return "dlq"


async def _handle_emit_failure(msg: Msg, request: GenerationRequest, deps: HandlerDeps) -> str:
    """Bound a publish/ack failure the same way as a transient failure.

    Redeliver under the bound; on exhaustion, route the raw message to the DLQ
    (best-effort) and ack so a request whose result can never be published (e.g.
    an un-routable ``reply_to``) cannot loop forever.
    """
    if _num_delivered(msg) < deps.max_deliver:
        with contextlib.suppress(Exception):
            await msg.nak(delay=deps.nak_delay)
        return "emit_error_nak"

    logger.warning(
        "Request %s un-emittable after %d deliveries; dead-lettering and acking",
        request.request_id,
        deps.max_deliver,
    )
    headers = {HEADER_REQUEST_ID: request.request_id, HEADER_DLQ_REASON: "emit_failure"}
    with contextlib.suppress(Exception):
        await deps.transport.publish_dlq(msg.data, headers)
    with contextlib.suppress(Exception):
        await msg.ack()
    return "emit_error_dlq"


def _is_terminal(result: GenerationResult) -> bool:
    """A success, or a permanent failure — no retry could change it."""
    return result.status is GenerationStatus.SUCCESS or (
        result.error is not None and result.error.permanent
    )


def _dlq_headers(request: GenerationRequest, result: GenerationResult) -> dict[str, str]:
    reason = result.error.code if result.error is not None else "unknown"
    return {HEADER_REQUEST_ID: request.request_id, HEADER_DLQ_REASON: reason}


def _num_delivered(msg: Msg) -> int:
    """JetStream delivery count for this message (1 on first delivery)."""
    try:
        return int(msg.metadata.num_delivered)
    except Exception:
        return 1


async def _drop_undecodable(msg: Msg, deps: HandlerDeps) -> None:
    """Best-effort route a poison (undecodable) message to the DLQ, then ack.

    We cannot build a result (no ``reply_to``); acking stops an un-parseable
    message from looping forever. A DLQ publish failure is swallowed — dropping a
    poison message is preferable to an infinite redelivery loop.
    """
    with contextlib.suppress(Exception):
        await deps.transport.publish_dlq(msg.data, {HEADER_DLQ_REASON: "decode"})
    await msg.ack()
    app_metrics.worker_messages.add(1, {"outcome": "drop_decode"})
