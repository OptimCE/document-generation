"""Application metrics for the document-generation worker.

Instruments are created at import against the OTel proxy provider and rebind to
the real provider once ``core.tracing.setup_tracer_provider`` installs it (in
staging/production). In local/test the proxy stays a no-op, so every
``.add()``/``.record()`` is a cheap dispatch with no side effects.

Naming follows OTel semantic conventions: dotted lowercase, ``.total`` on
counters, seconds for duration histograms.
"""

from __future__ import annotations

from collections.abc import Iterable

from opentelemetry import metrics
from opentelemetry.metrics import CallbackOptions, Observation

_meter = metrics.get_meter("document-generation")

worker_messages = _meter.create_counter(
    name="worker.messages.total",
    description="NATS message handler outcomes, labelled by outcome",
    unit="1",
)

documents_rendered = _meter.create_counter(
    name="documents.rendered.total",
    description="Artifacts rendered by the worker, labelled by format and status",
    unit="1",
)

render_duration = _meter.create_histogram(
    name="document.render.seconds",
    description="Wall-clock time spent rendering one request (all formats)",
    unit="s",
)

# Latest JetStream consumer pending count. Mutated by the worker's background
# poller (worker.main._poll_queue_depth); read on each collection cycle.
queue_depth_snapshot: dict[str, int] = {}


def _queue_depth_callback(options: CallbackOptions) -> Iterable[Observation]:
    return [
        Observation(value, {"subject": subject}) for subject, value in queue_depth_snapshot.items()
    ]


_meter.create_observable_gauge(
    name="nats.queue.depth",
    callbacks=[_queue_depth_callback],
    description="JetStream consumer num_pending for the docgen request subject",
    unit="1",
)
