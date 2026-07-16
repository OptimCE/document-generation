# document-generation

A generic, **stateless**, **NATS-only** document-generation worker for OptimCE.

It consumes a generation request over JetStream, fetches a template + manifest
from object storage, validates the supplied `data` against the manifest's JSON
Schema, renders to **PDF and/or XLSX**, writes the artifacts to a single output
bucket, and publishes a result.

The worker contains **no domain knowledge** — nothing about invoices, members or
CWaPE. All domain specifics live in templates and their manifests. It is a pure
function `(template, data) → artifacts`.

## Principles

- **Generic** — never imports or references a business domain.
- **Stateless** — no database; the only state is a local-disk template cache.
- **NATS-only** — no HTTP server, no web framework, no public network surface.
- **Pure render** — artifact bytes are a deterministic function of
  `(template, data)`; never `now()`/random inside a render. This is what makes
  at-least-once delivery idempotent.
- **Least privilege** — reads the templates bucket, writes the single output
  bucket; nothing else.

## Architecture (hexagonal / ports & adapters)

```
domain/        # import-clean core — no NATS/boto/render imports
  models.py        request/result/manifest contracts (Pydantic v2)
  ports.py         Protocols: TemplateStore, Validator, Renderer*, ObjectStore, MessageTransport
  errors.py        ErrorCode + permanent/transient classification
  orchestrator.py  fetch → check formats → validate → render → store → result
adapters/      # concrete implementations of the ports
  template_store.py        object-storage read → local bundle (cached)
  object_store.py          write artifact + sha256/size + presign
  validator_jsonschema.py  Draft 2020-12 validation gate
  nats_transport.py        publish results / DLQ
  render_executor.py       inline + process-pool execution
  renderers/               jinja-html→PDF (WeasyPrint), xlsx (openpyxl), registry
core/          # reused infra (settings, logging, tracing, metrics, queue, storage client)
worker/        # main.py entrypoint + dispatcher (subscription, ack/nak/DLQ)
```

The domain core orchestrates; adapters touch the outside world. The core never
imports an adapter, so it is unit-tested with in-memory fakes.

## Message contracts

Request and result are the **NATS message body** (JSON), snake_case. `metadata`
is opaque and echoed back untouched; `request_id` is the correlation key.

**Request** (`docgen.request`):

```json
{
  "request_id": "uuid", "tenant_id": "uuid", "requested_by": "billing",
  "template": { "uri": "s3://optimce-templates/billing/invoice/v3/" },
  "outputs": [{ "format": "pdf" }],
  "data": { },
  "key_prefix": "billing/invoices/2026/06/inv-00042/",
  "reply_to": "docgen.result.billing",
  "options": { "locale": "fr-BE", "presign_ttl": 3600 },
  "metadata": { "invoice_id": "..." }
}
```

**Result** (published to `reply_to`):

```json
{
  "request_id": "uuid", "status": "success",
  "artifacts": [{ "format": "pdf", "uri": "s3://optimce-documents/.../invoice.pdf",
                  "presigned_url": "https://...", "size_bytes": 84213, "sha256": "..." }],
  "template_version": "3", "generated_at": "ISO-8601", "metadata": { "invoice_id": "..." }
}
```

A failed result carries `error: { code, message, permanent }` instead of
artifacts. `error.permanent` tells the caller whether a retry could ever succeed.

Error codes: `VALIDATION_ERROR`, `TEMPLATE_NOT_FOUND`, `UNSUPPORTED_FORMAT`
(permanent); `RENDER_ERROR`, `STORAGE_ERROR` (transient).

## Manifest (`manifest.json`, ships with every template)

```json
{
  "id": "billing.invoice",
  "version": "3",
  "engine": "jinja-html",
  "supported_formats": ["pdf", "html"],
  "entrypoint": "invoice.html",
  "output_basename": "invoice",
  "required_fields": { "$schema": "...2020-12...", "type": "object", "required": ["..."] }
}
```

- `engine` — `jinja-html` (PDF/HTML via Jinja2 + WeasyPrint) or `xlsx` (openpyxl).
- `entrypoint` *(optional)* — entry file; defaults `template.html` / `template.xlsx`.
- `output_basename` *(optional)* — artifact base name; defaults to the last
  dot-segment of `id` (`billing.invoice` → `invoice.pdf`).
- `required_fields` — JSON Schema; an empty schema validates everything.

**jinja-html templates** receive the request `data` as `data` and the locale as
`locale` (e.g. `{{ data.invoice_number }}`). Relative assets (`./invoice.css`,
`./logo.png`, fonts) resolve against the template directory. Missing fields are a
hard error (no silent blanks). See `tests/fixtures/templates/billing_invoice/`.

**xlsx templates** are filled from `data` two ways: workbook **defined names**
matching top-level `data` keys, and an explicit `data.cells` map of
`"A1"` / `"Sheet!A1"` → value (applied last).

## Reliability (JetStream)

- Requests on a **work-queue** stream consumed by a **durable, queue-group**
  push consumer → competing replicas scale horizontally.
- **Permanent** failures → ack + failed result (no retry). **Transient** failures
  → nak with backoff; after `DOCGEN_MAX_DELIVER` deliveries the request is routed
  to the **DLQ** with a final failed result, then acked.
- Results go to `reply_to` on a durable results stream so a briefly-down caller
  still receives them. Deterministic output keys make redelivery safe (no dedupe
  store; callers dedupe on `request_id`).

## Configuration

Env-driven (`core/config.py`); copy `.env.exemple` → `.env.local`. Key vars:
`NATS_URL`, `DOCGEN_REQUEST_SUBJECT/STREAM`, `DOCGEN_DURABLE`,
`DOCGEN_RESULTS_STREAM`, `DOCGEN_DLQ_SUBJECT`, `DOCGEN_MAX_DELIVER`,
`DOCGEN_ACK_WAIT_SECONDS`, `RENDER_POOL_SIZE`, `TEMPLATES_BUCKET` (read),
`OUTPUT_BUCKET` (write), `STORAGE_ENDPOINT/ACCESS_KEY/SECRET_KEY/REGION`.

## Develop & test

```bash
python -m venv .venv && . .venv/Scripts/activate   # or source .venv/bin/activate
pip install -r requirements/all.txt
ruff check . && ruff format --check . && mypy . && pytest
```

Tests are hermetic — no NATS, no storage, no broker. The WeasyPrint PDF test is
skipped automatically where the native libraries are absent (it runs in the
container, below).

### Verify the PDF renderer in Docker (Linux native deps)

```bash
docker build -f Dockerfile.worker -t docgen-worker .
docker run --rm --entrypoint sh docgen-worker -c \
  "pip install -r /dev/stdin <<<'pytest==8.3.4 pytest-asyncio==0.24.0 pypdf==5.1.0' \
   && python -m pytest -q"   # add the test sources via a bind mount in practice
```

(The image ships only runtime code; mount `tests/` to run the suite, or run the
PDF test in CI on Linux where `import weasyprint` succeeds.)

## Run against the dev stack

The shared `monorepo/docker-compose.dev.yml` is **not** modified by this service.
To run it in the dev stack:

1. Create the two buckets once (MinIO on host port 8091):

   ```bash
   mc alias set local http://localhost:8091 minioadmin minioadmin
   mc mb --ignore-existing local/optimce-templates local/optimce-documents
   ```

2. Either run the worker locally (`.env.local` points at the stack)…

   ```bash
   ENV=local python -m worker.main
   ```

   …or add a service to `docker-compose.dev.yml`:

   ```yaml
   document-generation-worker:
     profiles: ["dev"]
     build:
       context: document-generation
       dockerfile: Dockerfile.worker
     restart: unless-stopped
     cpus: "2.0"
     environment:
       - ENV=local
       - NATS_URL=nats://nats:4222
       - STORAGE_ENDPOINT=${STORAGE_API_URL}
       - TEMPLATES_BUCKET=optimce-templates
       - OUTPUT_BUCKET=optimce-documents
       - STORAGE_ACCESS_KEY=${MINIO_ROOT_USER:-minioadmin}
       - STORAGE_SECRET_KEY=${MINIO_ROOT_PASSWORD:-minioadmin}
       - STORAGE_REGION=us-east-1
     networks: [backend]
     depends_on:
       nats: { condition: service_healthy }
       minio-init: { condition: service_completed_successfully }
   ```

   …and add the buckets to the `minio-init` entrypoint:
   `mc mb --ignore-existing local/optimce-templates local/optimce-documents;`

3. Drive one request end-to-end:

   ```bash
   python -m scripts.smoke
   ```
```
