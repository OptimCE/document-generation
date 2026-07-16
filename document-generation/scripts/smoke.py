"""Manual end-to-end smoke test against a running stack + worker.

Not part of the automated suite. It uploads a sample template, publishes one
request, waits for the result on the reply subject, and saves the artifact.

Prerequisites:
  1. The dev stack is up (NATS on :8094, MinIO on :8091) — see the repo README.
  2. The worker is running (``python -m worker.main``) so the JetStream streams
     exist and requests get processed.
  3. ``.env.local`` points NATS_URL / STORAGE_* at the stack.

Run:  python -m scripts.smoke
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import nats
from aiobotocore.session import get_session

from core.config import settings
from core.storage import client

logger = logging.getLogger("smoke")

_PREFIX = "billing/invoice/v1/"
_ROOT = Path(__file__).resolve().parent.parent
_FIXTURES = _ROOT / "tests" / "fixtures" / "templates" / "billing_invoice"
_REPLY_SUBJECT = "docgen.result.smoke"
_RESULT_TIMEOUT_SECONDS = 60.0


async def _ensure_bucket(name: str) -> None:
    session = get_session()
    async with session.create_client(
        "s3",
        endpoint_url=settings.STORAGE_ENDPOINT or None,
        region_name=settings.STORAGE_REGION,
        aws_access_key_id=settings.STORAGE_ACCESS_KEY,
        aws_secret_access_key=settings.STORAGE_SECRET_KEY,
    ) as s3:
        try:
            await s3.create_bucket(Bucket=name)
            logger.info("created bucket %s", name)
        except Exception as exc:
            # Already-exists (or similar) is fine for a smoke run.
            logger.info("bucket %s not created (likely exists): %s", name, exc)


async def _upload_template() -> None:
    for path in _FIXTURES.iterdir():
        if path.is_file():
            key = f"{_PREFIX}{path.name}"
            await client.put_bytes(settings.TEMPLATES_BUCKET, key, path.read_bytes())
            logger.info("uploaded %s", path.name)


def _build_request() -> dict[str, Any]:
    return {
        "request_id": "smoke-0001",
        "tenant_id": "tenant-smoke",
        "requested_by": "billing",
        "template": {"uri": f"s3://{settings.TEMPLATES_BUCKET}/{_PREFIX}"},
        "outputs": [{"format": "pdf"}, {"format": "html"}],
        "data": {
            "invoice_number": "INV-SMOKE-1",
            "customer_name": "Smoke Test SA",
            "total": 4242.0,
            "currency": "EUR",
        },
        "key_prefix": "billing/invoices/smoke/inv-0001/",
        "reply_to": _REPLY_SUBJECT,
        "options": {"locale": "fr-BE", "presign_ttl": 3600},
        "metadata": {"invoice_id": "smoke-invoice-1"},
    }


async def _download_artifacts(result: dict[str, Any], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for artifact in result.get("artifacts", []):
        uri = artifact["uri"]
        # s3://bucket/key → key
        key = uri.split("/", 3)[3]
        data = await client.get_bytes(settings.OUTPUT_BUCKET, key)
        target = out_dir / Path(key).name
        target.write_bytes(data)
        logger.info("saved %s (%d bytes)", target, len(data))


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    await _ensure_bucket(settings.TEMPLATES_BUCKET)
    await _ensure_bucket(settings.OUTPUT_BUCKET)
    await _upload_template()

    nc = await nats.connect(settings.NATS_URL)
    js = nc.jetstream()
    sub = await nc.subscribe(_REPLY_SUBJECT)

    request = _build_request()
    await js.publish(settings.DOCGEN_REQUEST_SUBJECT, json.dumps(request).encode())
    logger.info("published request %s; waiting for result...", request["request_id"])

    try:
        msg = await sub.next_msg(timeout=_RESULT_TIMEOUT_SECONDS)
    except TimeoutError:
        logger.error("no result within %.0fs — is the worker running?", _RESULT_TIMEOUT_SECONDS)
        await nc.drain()
        return

    result = json.loads(msg.data)
    logger.info("result:\n%s", json.dumps(result, indent=2))
    if result.get("status") == "success":
        await _download_artifacts(result, Path(__file__).resolve().parent.parent / "output")

    await nc.drain()


if __name__ == "__main__":
    asyncio.run(main())
