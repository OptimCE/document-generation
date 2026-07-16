"""Worker process entry point.

Bootstraps logging + tracing, connects to NATS JetStream, and subscribes the
dispatcher to the durable docgen consumer. Renders run in a process pool so the
CPU-bound work never blocks the event loop. Runs until SIGINT/SIGTERM, then
drains in-flight work and disposes resources cleanly.

Run locally:

    python -m worker.main
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import logging
import math
import os
import pathlib
import signal
import sys
from collections.abc import Callable
from concurrent.futures import Executor, ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool

from adapters.nats_transport import NatsTransport
from adapters.object_store import ObjectStorageObjectStore
from adapters.render_executor import ProcessPoolRenderExecutor
from adapters.renderers.registry import DefaultRendererRegistry
from adapters.template_store import ObjectStorageTemplateStore
from adapters.validator_jsonschema import JsonSchemaValidator
from core import metrics as app_metrics
from core.config import settings
from core.logging import configure_logging
from core.queue.init import close_nats, get_jetstream, init_nats
from core.tracing import setup_tracer_provider
from shared.const import HEARTBEAT_PATH
from worker import dispatcher
from worker.dispatcher import HandlerDeps

logger = logging.getLogger(__name__)

# Bounded retry on NATS connect so a slow-to-start broker doesn't crash the
# container immediately (~10 minutes of backoff, then give up and let the
# orchestrator recreate it).
_NATS_CONNECT_MAX_ATTEMPTS = 10
_NATS_CONNECT_BASE_DELAY_SECONDS = 2
_NATS_CONNECT_MAX_DELAY_SECONDS = 30

_QUEUE_DEPTH_POLL_INTERVAL_SECONDS = 15

# How long shutdown waits for in-flight renders to finish + ack before cancelling
# them (they redeliver after ack_wait; deterministic keys make that safe). Kept
# under a typical SIGTERM grace period so we aren't SIGKILL'd mid-drain.
_INFLIGHT_DRAIN_TIMEOUT_SECONDS = 25


def _detect_available_cpus() -> int:
    """Best-effort count of CPUs this process may actually use.

    ``os.cpu_count()`` reports host cores and ignores a container CPU quota
    (Docker ``cpus:`` / k8s limits). Prefer the cgroup quota, then CPU affinity,
    then the host count.
    """
    try:
        parts = pathlib.Path("/sys/fs/cgroup/cpu.max").read_text().split()
        if parts and parts[0] != "max":
            quota = int(parts[0])
            period = int(parts[1]) if len(parts) > 1 else 100_000
            if quota > 0 and period > 0:
                return max(1, math.floor(quota / period))
    except (OSError, ValueError):
        pass
    try:
        quota = int(pathlib.Path("/sys/fs/cgroup/cpu/cpu.cfs_quota_us").read_text())
        period = int(pathlib.Path("/sys/fs/cgroup/cpu/cpu.cfs_period_us").read_text())
        if quota > 0 and period > 0:
            return max(1, math.floor(quota / period))
    except (OSError, ValueError):
        pass
    if hasattr(os, "sched_getaffinity"):
        try:
            return max(1, len(os.sched_getaffinity(0)))
        except OSError:
            pass
    return max(1, os.cpu_count() or 1)


def _render_pool_size() -> int:
    """Number of render processes.

    ``RENDER_POOL_SIZE`` overrides; 0 means "all available CPUs bar one", leaving
    a core of scheduling headroom for the event loop / NATS client.
    """
    if settings.RENDER_POOL_SIZE > 0:
        return settings.RENDER_POOL_SIZE
    return max(1, _detect_available_cpus() - 1)


def _init_render_process() -> None:
    """Pool-worker initializer — runs once per render process at spawn.

    Pays WeasyPrint's native-library load cost once per process rather than on the
    first PDF. Suppressed so a host without WeasyPrint's native deps (e.g. a bare
    Windows dev box) can still render XLSX-only templates.
    """
    with contextlib.suppress(Exception):
        importlib.import_module("weasyprint")


class _RenderPool(Executor):
    """ProcessPoolExecutor that rebuilds itself if a worker dies abnormally.

    A native crash or OOM kill in one pool worker poisons the whole
    ProcessPoolExecutor: every subsequent ``submit`` raises ``BrokenProcessPool``.
    Rebuilding on the next submit restores capacity; the render that tripped the
    break is redelivered (the executor maps the break to a transient render
    error). ``submit`` is only called from the event-loop thread, so the rebuild
    needs no extra locking.
    """

    def __init__(self, max_workers: int, initializer: Callable[[], None]) -> None:
        self._max_workers = max_workers
        self._initializer = initializer
        self._pool = self._new_pool()

    def _new_pool(self) -> ProcessPoolExecutor:
        return ProcessPoolExecutor(max_workers=self._max_workers, initializer=self._initializer)

    def submit(self, fn, /, *args, **kwargs):
        try:
            return self._pool.submit(fn, *args, **kwargs)
        except BrokenProcessPool:
            logger.error(
                "Render pool broken (worker died); rebuilding with %d worker(s)",
                self._max_workers,
            )
            self._pool = self._new_pool()
            return self._pool.submit(fn, *args, **kwargs)

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        self._pool.shutdown(wait=wait, cancel_futures=cancel_futures)


async def _connect_nats_with_retry() -> None:
    for attempt in range(1, _NATS_CONNECT_MAX_ATTEMPTS + 1):
        try:
            await init_nats()
            return
        except Exception as exc:
            if attempt == _NATS_CONNECT_MAX_ATTEMPTS:
                logger.error(
                    "NATS connect attempt %d/%d failed: %s; aborting worker startup",
                    attempt,
                    _NATS_CONNECT_MAX_ATTEMPTS,
                    exc,
                )
                raise
            delay = min(
                _NATS_CONNECT_BASE_DELAY_SECONDS * (2 ** (attempt - 1)),
                _NATS_CONNECT_MAX_DELAY_SECONDS,
            )
            logger.warning(
                "NATS connect attempt %d/%d failed: %s; retrying in %.1fs",
                attempt,
                _NATS_CONNECT_MAX_ATTEMPTS,
                exc,
                delay,
            )
            await asyncio.sleep(delay)


async def _poll_queue_depth(js, shutdown_event: asyncio.Event) -> None:
    """Refresh the queue-depth metric snapshot and the liveness heartbeat."""
    while not shutdown_event.is_set():
        ok = False
        try:
            info = await js.consumer_info(settings.DOCGEN_REQUEST_STREAM, settings.DOCGEN_DURABLE)
            subject = settings.DOCGEN_REQUEST_SUBJECT
            app_metrics.queue_depth_snapshot[subject] = int(info.num_pending)
            ok = True
        except Exception as exc:
            logger.debug("queue depth poll failed: %s", exc)
        if ok:
            try:
                await asyncio.to_thread(HEARTBEAT_PATH.touch)
            except OSError as exc:
                logger.debug("heartbeat touch failed: %s", exc)
        try:
            await asyncio.wait_for(
                shutdown_event.wait(), timeout=_QUEUE_DEPTH_POLL_INTERVAL_SECONDS
            )
        except TimeoutError:
            continue


def _build_deps(js, render_executor: ProcessPoolRenderExecutor) -> HandlerDeps:
    return HandlerDeps(
        template_store=ObjectStorageTemplateStore(
            bucket=settings.TEMPLATES_BUCKET,
            cache_dir=pathlib.Path(settings.TEMPLATE_CACHE_DIR),
        ),
        validator=JsonSchemaValidator(),
        registry=DefaultRendererRegistry(),
        render_executor=render_executor,
        object_store=ObjectStorageObjectStore(bucket=settings.OUTPUT_BUCKET),
        transport=NatsTransport(js, dlq_subject=settings.DOCGEN_DLQ_SUBJECT),
    )


async def main() -> None:
    configure_logging()
    setup_tracer_provider()

    await _connect_nats_with_retry()
    js = get_jetstream()

    shutdown_event = asyncio.Event()
    _install_signal_handlers(shutdown_event)

    pool_size = _render_pool_size()
    render_pool = _RenderPool(pool_size, _init_render_process)
    render_semaphore = asyncio.Semaphore(pool_size)
    render_executor = ProcessPoolRenderExecutor(render_pool, semaphore=render_semaphore)
    inflight: set[asyncio.Task] = set()
    logger.info("Render process pool ready: %d worker(s)", pool_size)

    deps = _build_deps(js, render_executor)

    sub = None
    queue_depth_task: asyncio.Task | None = None
    try:
        sub = await dispatcher.subscribe(js, deps, inflight=inflight, max_ack_pending=pool_size)
        queue_depth_task = asyncio.create_task(
            _poll_queue_depth(js, shutdown_event), name="queue-depth-poller"
        )

        logger.info("Worker ready — listening on the docgen request queue")
        await shutdown_event.wait()
        logger.info("Shutdown signal received; draining subscription...")
    finally:
        if queue_depth_task is not None:
            queue_depth_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await queue_depth_task

        # Stop NEW deliveries first: draining the subscription flushes its
        # buffered messages through the callback (which only spawns tasks, so this
        # returns quickly) and unsubscribes. Only then snapshot in-flight, so a
        # message buffered at shutdown still gets its task awaited below rather
        # than being orphaned by a wait taken before drain.
        if sub is not None:
            try:
                await sub.drain()
            except Exception:
                logger.exception("Error draining subscription")

        if inflight:
            logger.info("Waiting for %d in-flight request(s) to finish...", len(inflight))
            _done, pending = await asyncio.wait(
                set(inflight), timeout=_INFLIGHT_DRAIN_TIMEOUT_SECONDS
            )
            if pending:
                logger.warning(
                    "%d request(s) still running after %ds; cancelling — they will be "
                    "redelivered after ack_wait",
                    len(pending),
                    _INFLIGHT_DRAIN_TIMEOUT_SECONDS,
                )
                for task in pending:
                    task.cancel()

        try:
            render_pool.shutdown(wait=False, cancel_futures=True)
        except Exception:
            logger.exception("Error shutting down render pool")

        try:
            await close_nats()
        except Exception:
            logger.exception("Error closing NATS connection")

        logger.info("Worker shutdown complete")


def _install_signal_handlers(shutdown_event: asyncio.Event) -> None:
    """Wire SIGINT/SIGTERM to set ``shutdown_event`` (POSIX + Windows)."""
    loop = asyncio.get_running_loop()

    def _set_event() -> None:
        if not shutdown_event.is_set():
            shutdown_event.set()

    if sys.platform == "win32":
        signal.signal(signal.SIGINT, lambda *_: _set_event())
        signal.signal(signal.SIGTERM, lambda *_: _set_event())
        return

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _set_event)
        except NotImplementedError:
            signal.signal(sig, lambda *_: _set_event())


if __name__ == "__main__":
    asyncio.run(main())
