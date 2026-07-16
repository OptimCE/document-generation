"""MessageTransport adapter over NATS JetStream.

Publishes results to the request's ``reply_to`` subject (durable results stream)
and routes exhausted/poison requests to the DLQ subject. Consumption itself is
driven by ``worker.dispatcher`` (it owns the subscription + ack/nak), so this
adapter only handles the publish side of the port.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping

from nats.js import JetStreamContext

from domain.models import GenerationResult

logger = logging.getLogger(__name__)

# Hard cap on a publish round-trip so a stalled broker fails fast instead of
# blocking on the client's long default request timeout.
_PUBLISH_TIMEOUT_SECONDS = 5.0


class NatsTransport:
    def __init__(
        self,
        js: JetStreamContext,
        *,
        dlq_subject: str,
        publish_timeout: float = _PUBLISH_TIMEOUT_SECONDS,
    ) -> None:
        self._js = js
        self._dlq_subject = dlq_subject
        self._publish_timeout = publish_timeout

    async def publish_result(self, subject: str, result: GenerationResult) -> None:
        ack = await self._js.publish(
            subject,
            result.to_json_bytes(),
            headers={"Event-Type": "docgen.result", "Request-Id": result.request_id},
            timeout=self._publish_timeout,
        )
        logger.debug(
            "Published result request_id=%s status=%s to %s (stream=%s, seq=%d)",
            result.request_id,
            result.status,
            subject,
            ack.stream,
            ack.seq,
        )

    async def publish_dlq(self, payload: bytes, headers: Mapping[str, str]) -> None:
        ack = await self._js.publish(
            self._dlq_subject,
            payload,
            headers=dict(headers),
            timeout=self._publish_timeout,
        )
        logger.warning(
            "Routed message to DLQ %s (stream=%s, seq=%d)", self._dlq_subject, ack.stream, ack.seq
        )
