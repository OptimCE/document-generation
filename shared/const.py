"""Cross-module constants for the document-generation worker.

Stream / subject / durable-consumer *names* are configuration (see
``core.config.settings``); these are the fixed wire constants that are not worth
making configurable.
"""

from __future__ import annotations

import pathlib

# NATS message header keys.
HEADER_EVENT_TYPE = "Event-Type"
HEADER_REQUEST_ID = "Request-Id"
HEADER_DLQ_REASON = "X-DocGen-Error"

# Container liveness file: the worker's queue-depth poller touches it on every
# successful consumer_info round-trip; the Docker HEALTHCHECK reads its mtime.
HEARTBEAT_PATH = pathlib.Path("/tmp/worker.alive")  # noqa: S108 — dedicated container, non-root user
