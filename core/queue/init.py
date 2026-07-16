"""NATS connection + JetStream stream declarations for the docgen worker.

Three streams, declared from settings so the operational subject/stream names and
the declared topology never drift:

* ``DOCGEN_REQUESTS`` — work-queue retention; the request subject. Competing
  worker replicas share it via a durable queue-group consumer.
* ``DOCGEN_RESULTS`` — limits retention; ``docgen.result.>``. Results survive a
  briefly-down caller.
* ``DOCGEN_DLQ`` — limits retention; the dead-letter subject for exhausted /
  poison requests, kept for inspection.

The connection uses infinite reconnect + async callbacks so a broker outage
produces structured WARNING logs rather than failing silently — the same shape
as the sibling services' ``core/queue/init.py``.
"""

from __future__ import annotations

import logging

import nats
from nats.aio.client import Client as NATSClient
from nats.js import JetStreamContext
from nats.js.api import RetentionPolicy, StorageType

from core.config import settings

logger = logging.getLogger(__name__)

_nats_client: NATSClient | None = None
_jetstream: JetStreamContext | None = None

# Connection knobs. The worker is long-running and must ride out broker
# failovers without the orchestrator restarting it; max_reconnect_attempts=-1
# means "keep trying forever". The 2 s reconnect wait keeps the retry loop tight;
# the 5 s connect timeout caps the initial handshake so a misconfigured URL fails
# fast at startup.
_RECONNECT_TIME_WAIT_SECONDS = 2
_MAX_RECONNECT_ATTEMPTS = -1
_CONNECT_TIMEOUT_SECONDS = 5


def _desired_streams() -> list[dict]:
    return [
        {
            "name": settings.DOCGEN_REQUEST_STREAM,
            "subjects": [settings.DOCGEN_REQUEST_SUBJECT],
            "retention": RetentionPolicy.WORK_QUEUE,
            "storage": StorageType.FILE,
        },
        {
            "name": settings.DOCGEN_RESULTS_STREAM,
            "subjects": [settings.DOCGEN_RESULTS_SUBJECT_FILTER],
            "retention": RetentionPolicy.LIMITS,
            "storage": StorageType.FILE,
        },
        {
            "name": settings.DOCGEN_DLQ_STREAM,
            "subjects": [settings.DOCGEN_DLQ_SUBJECT],
            "retention": RetentionPolicy.LIMITS,
            "storage": StorageType.FILE,
        },
    ]


async def _on_error(exc: Exception) -> None:
    logger.warning("NATS async error: %s", exc)


async def _on_disconnected() -> None:
    logger.warning("NATS disconnected; client will attempt to reconnect")


async def _on_reconnected() -> None:
    if _nats_client and _nats_client.connected_url:
        server = _nats_client.connected_url.netloc
    else:
        server = "<unknown>"
    logger.warning("NATS reconnected to %s", server)


async def _on_closed() -> None:
    logger.warning("NATS connection closed; no further reconnect attempts will be made")


async def init_nats() -> None:
    """Connect to NATS, build JetStream, and ensure the docgen streams exist."""
    global _nats_client, _jetstream
    servers = [u.strip() for u in settings.NATS_URL.split(",") if u.strip()]
    _nats_client = await nats.connect(
        servers=servers or None,  # type: ignore[arg-type]  # nats stubs reject None, but library accepts it
        reconnect_time_wait=_RECONNECT_TIME_WAIT_SECONDS,
        max_reconnect_attempts=_MAX_RECONNECT_ATTEMPTS,
        connect_timeout=_CONNECT_TIMEOUT_SECONDS,
        error_cb=_on_error,
        disconnected_cb=_on_disconnected,
        reconnected_cb=_on_reconnected,
        closed_cb=_on_closed,
    )
    _jetstream = _nats_client.jetstream()

    for stream in _desired_streams():
        await _jetstream.add_stream(**stream)


async def close_nats() -> None:
    global _nats_client, _jetstream
    if _nats_client:
        await _nats_client.drain()
        _nats_client = None
        _jetstream = None


def get_nats() -> NATSClient:
    if _nats_client is None:
        raise RuntimeError("NATS client not initialized")
    return _nats_client


def get_jetstream() -> JetStreamContext:
    if _jetstream is None:
        raise RuntimeError("NATS JetStream not initialized")
    return _jetstream
