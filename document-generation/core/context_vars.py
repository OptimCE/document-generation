"""Request-scoped context for log enrichment.

The worker has no users or communities (it is a generic, stateless renderer), so
the only context worth carrying is the ``request_id`` of the document-generation
request currently being handled. ``core.logging.RequestIdFilter`` stamps it onto
every log record; ``worker.dispatcher`` sets it per message.
"""

from __future__ import annotations

from contextvars import ContextVar

current_request_id: ContextVar[str | None] = ContextVar("current_request_id", default=None)
