"""Worker dispatcher tests: the ack / nak / DLQ matrix, driven with FakeMsg.

No live NATS or storage — the handler is invoked directly and collaborators are
in-memory fakes. Asserts the failure-class behaviour: success/permanent → ack +
result; transient → nak until exhausted, then result + DLQ + ack.
"""

from __future__ import annotations

from domain.errors import RenderError, SchemaValidationError, StorageError
from domain.models import GenerationStatus
from tests.conftest import (
    FakeMsg,
    FakeObjectStore,
    FakeRenderer,
    FakeTemplateStore,
    FakeTransport,
    InlineExecutor,
    RecordingValidator,
    SingleRendererRegistry,
    request_bytes,
)
from worker import dispatcher
from worker.dispatcher import HandlerDeps


def _deps(
    jinja_bundle,
    *,
    validator=None,
    object_store=None,
    transport=None,
    max_deliver=4,
) -> HandlerDeps:
    return HandlerDeps(
        template_store=FakeTemplateStore(jinja_bundle),
        validator=validator or RecordingValidator(),
        registry=SingleRendererRegistry(FakeRenderer()),
        render_executor=InlineExecutor(),
        object_store=object_store or FakeObjectStore(),
        transport=transport or FakeTransport(),
        max_deliver=max_deliver,
        nak_delay=30,
    )


async def _handle(msg, deps):
    await dispatcher._make_handler(deps)(msg)  # inflight=None → inline


async def test_success_acks_and_publishes_result(jinja_bundle):
    transport = FakeTransport()
    deps = _deps(jinja_bundle, transport=transport)
    msg = FakeMsg(request_bytes())

    await _handle(msg, deps)

    assert msg.acked and not msg.naked
    assert len(transport.results) == 1
    subject, result = transport.results[0]
    assert subject == "docgen.result.billing"
    assert result.status is GenerationStatus.SUCCESS
    assert transport.dlqs == []


async def test_permanent_failure_acks_with_failed_result(jinja_bundle):
    transport = FakeTransport()
    deps = _deps(
        jinja_bundle,
        validator=RecordingValidator(error=SchemaValidationError("missing field")),
        transport=transport,
    )
    msg = FakeMsg(request_bytes())

    await _handle(msg, deps)

    assert msg.acked and not msg.naked
    result = transport.results[0][1]
    assert result.status is GenerationStatus.FAILED
    assert result.error.permanent is True
    assert transport.dlqs == []  # permanent → no dead-letter


async def test_transient_failure_naks_before_exhaustion(jinja_bundle):
    transport = FakeTransport()
    deps = _deps(
        jinja_bundle,
        object_store=FakeObjectStore(error=StorageError("503")),
        transport=transport,
        max_deliver=4,
    )
    msg = FakeMsg(request_bytes(), num_delivered=1)

    await _handle(msg, deps)

    assert msg.naked and not msg.acked
    assert msg.nak_delay == 30
    assert transport.results == []
    assert transport.dlqs == []


async def test_transient_failure_dead_letters_on_exhaustion(jinja_bundle):
    transport = FakeTransport()
    deps = _deps(
        jinja_bundle,
        object_store=FakeObjectStore(error=StorageError("503")),
        transport=transport,
        max_deliver=4,
    )
    msg = FakeMsg(request_bytes(), num_delivered=4)  # final allowed delivery

    await _handle(msg, deps)

    assert msg.acked and not msg.naked
    # A failed result is still published to the caller…
    result = transport.results[0][1]
    assert result.status is GenerationStatus.FAILED
    assert result.error.permanent is False  # it was transient; retries just ran out
    # …and the original message is routed to the DLQ.
    assert len(transport.dlqs) == 1
    payload, headers = transport.dlqs[0]
    assert payload == msg.data
    assert headers["Request-Id"] == "req-1"


async def test_render_failure_then_exhaustion_dead_letters(jinja_bundle):
    transport = FakeTransport()
    deps = _deps(jinja_bundle, transport=transport, max_deliver=2)
    deps = HandlerDeps(
        template_store=deps.template_store,
        validator=deps.validator,
        registry=SingleRendererRegistry(FakeRenderer(error=RenderError("boom"))),
        render_executor=deps.render_executor,
        object_store=deps.object_store,
        transport=transport,
        max_deliver=2,
    )
    msg = FakeMsg(request_bytes(), num_delivered=2)

    await _handle(msg, deps)

    assert msg.acked
    assert transport.dlqs[0][1]["X-DocGen-Error"] == "RENDER_ERROR"


async def test_undecodable_message_acks_and_dead_letters(jinja_bundle):
    transport = FakeTransport()
    deps = _deps(jinja_bundle, transport=transport)
    msg = FakeMsg(b"not even json")

    await _handle(msg, deps)

    assert msg.acked and not msg.naked
    assert transport.results == []  # cannot build a result (no reply_to)
    assert transport.dlqs[0][1]["X-DocGen-Error"] == "decode"


async def test_emit_transport_failure_naks_before_exhaustion(jinja_bundle):
    # publish_result raises (broker down) → handler naks so the result retries.
    transport = FakeTransport(fail_result=True)
    deps = _deps(jinja_bundle, transport=transport, max_deliver=4)
    msg = FakeMsg(request_bytes(), num_delivered=1)

    await _handle(msg, deps)

    assert msg.naked and not msg.acked


async def test_emit_transport_failure_dead_letters_on_exhaustion(jinja_bundle):
    # A reply_to that can never be published (raises every time) must not loop
    # forever: on exhaustion it is dead-lettered and acked.
    transport = FakeTransport(fail_result=True)
    deps = _deps(jinja_bundle, transport=transport, max_deliver=2)
    msg = FakeMsg(request_bytes(), num_delivered=2)

    await _handle(msg, deps)

    assert msg.acked and not msg.naked
    assert transport.dlqs[0][1]["X-DocGen-Error"] == "emit_failure"


async def test_unexpected_bug_treated_as_transient(jinja_bundle):
    # A non-DocGenError (plain bug) from a collaborator → transient, redelivered.
    transport = FakeTransport()
    deps = _deps(
        jinja_bundle,
        object_store=FakeObjectStore(error=RuntimeError("unexpected")),
        transport=transport,
        max_deliver=4,
    )
    msg = FakeMsg(request_bytes(), num_delivered=1)

    await _handle(msg, deps)

    assert msg.naked and not msg.acked
    assert transport.results == []
