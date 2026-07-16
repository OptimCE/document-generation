# Document Generation Service

Generic, **stateless**, **NATS-only** document-generation worker for OptimCE.
Consumes a request over JetStream, fetches a template + manifest from object
storage, validates `data` against the manifest's JSON Schema, renders PDF/XLSX,
writes artifacts to the output bucket, and publishes a result. **No domain
knowledge** — a pure function `(template, data) → artifacts`. All invoice/member/
CWaPE specifics live in templates, never here. Unlike the sibling services there
is **no API and no database**. Full design rationale: `README.md`.

## Commands

```bash
py -3.12 -m venv .venv && . .venv/Scripts/activate   # bash: source .venv/bin/activate
pip install -r requirements/all.txt
# all gates (run from this dir; set ENV=test):
ENV=test ruff check . && ruff format --check . && mypy . && pytest
ENV=local python -m worker.main                      # run worker (needs NATS + MinIO)
python -m scripts.smoke                               # manual end-to-end smoke
docker build -f Dockerfile.worker -t docgen-worker .
```

On Windows prefix test runs with `PYTHONIOENCODING=utf-8 PYTHONUTF8=1` to surface
the real error instead of an INTERNALERROR.

## Architecture (hexagonal / ports & adapters)

```
domain/                 # import-clean core — NO nats/boto/weasyprint imports
  models.py             #   contracts: GenerationRequest/Result, Manifest, Artifact (Pydantic v2)
  ports.py              #   Protocols: TemplateStore, Validator, Renderer, RenderExecutor, ObjectStore, MessageTransport
  errors.py             #   ErrorCode + DocGenError subclasses (.permanent flag drives everything)
  orchestrator.py       #   process_request(): fetch → check formats → validate → render → store → result
  results.py            #   success/failure result builders
adapters/               # concrete port impls — the only place frameworks live
  template_store.py     #   S3 prefix → local-disk bundle (cached by versioned URI)
  object_store.py       #   write artifact + sha256/size + presign
  validator_jsonschema.py #  Draft 2020-12 gate
  nats_transport.py     #   publish results / DLQ
  render_executor.py    #   Inline (tests) + ProcessPool (prod)
  renderers/            #   jinja_html_pdf (WeasyPrint), xlsx (openpyxl), registry, _paths (traversal guard)
core/                   # infra cloned/trimmed from simulation-key
  config.py  queue/{init.py}  storage/client.py  logging.py  tracing.py  metrics.py
worker/
  main.py               #   entrypoint: connect+retry, render pool, subscribe, heartbeat, graceful drain
  dispatcher.py         #   subscription + per-message ack/nak/DLQ matrix
```

**Dependency rule:** `domain/` imports only `domain.*` + stdlib + pydantic.
Adapters depend on domain; `worker/` wires them. Never import an adapter from domain.

## Message contracts

Request and Result are the NATS message **body** (flat JSON, snake_case) — there
is **no `{type,version,data}` Event envelope** (the siblings use one; this
service implements the docgen API exactly). `metadata` is opaque and echoed back;
`request_id` is the correlation/idempotency key. JSON shapes are in `README.md`.

Error codes: `VALIDATION_ERROR`, `TEMPLATE_NOT_FOUND`, `UNSUPPORTED_FORMAT`
(permanent → ack + failed result); `RENDER_ERROR`, `STORAGE_ERROR` (transient →
nak/retry → DLQ). The `permanent` flag on each `DocGenError` is the single source
of truth for both `error.permanent` and the ack/nak decision.

## Engines & manifest

- `jinja-html` → Jinja2 → WeasyPrint PDF (and raw HTML). `base_url` = template dir
  so `./logo.png`/`./invoice.css`/fonts resolve. Templates receive `data` +
  `locale`; `StrictUndefined` makes a missing field a hard error (no silent blanks).
- `xlsx` → openpyxl: workbook **defined names** matching top-level `data` keys,
  plus `data["cells"]` (`"A1"` / `"Sheet!A1"` → value, applied last).
- Manifest has two **optional** fields beyond the spec: `entrypoint` (default
  `template.html` / `template.xlsx`) and `output_basename` (default = last
  dot-segment of `id`, e.g. `billing.invoice` → `invoice.pdf`).

## Reliability (JetStream)

- Durable queue-group push consumer on a work-queue request stream (competing
  replicas), `manual_ack`. Results go to the request's `reply_to` (durable
  results stream).
- `DOCGEN_MAX_DELIVER` is enforced in the handler (primary DLQ path) with a
  server-side `max_deliver = DOCGEN_MAX_DELIVER + 1` backstop on the consumer.
- Permanent → ack + failed result. Transient → nak with backoff; on exhaustion →
  failed result + DLQ + ack. Emit failures (e.g. an un-routable `reply_to`) are
  bounded the same way, so nothing loops forever.

## Gotchas

- **WeasyPrint on Windows:** the native GTK/pango libs aren't installed on the dev
  host, so `import weasyprint` raises **OSError at import** (not ImportError) —
  `importorskip` won't catch it. WeasyPrint is imported lazily in the PDF
  renderer; `tests/renderers/test_jinja_pdf.py` auto-skips the PDF test locally
  and it runs only in the Linux container. Verify PDF: `docker build -f
  Dockerfile.worker` then run pytest inside the image.
- **Stateless:** the only state is the local-disk template cache
  (`TEMPLATE_CACHE_DIR`), keyed by the immutable versioned template URI. No DB.
- **Pure render:** artifact bytes are a function of (template, data) — no `now()`/
  random in a render. XLSX is repacked deterministically (openpyxl stamps `now()`
  at save); `generated_at` lives on the result envelope only.
- **Least privilege:** reads `TEMPLATES_BUCKET`, writes `OUTPUT_BUCKET`, nothing
  else; the `template.uri` bucket must equal `TEMPLATES_BUCKET`.
- **Tests are hand-written and hermetic** — NATS + storage fully faked; `FakeMsg`
  records ack/nak. Not generated.

## Environment

See `.env.exemple`. Key vars: `NATS_URL`, `DOCGEN_REQUEST_SUBJECT/STREAM`,
`DOCGEN_DURABLE`, `DOCGEN_RESULTS_STREAM`, `DOCGEN_DLQ_SUBJECT`,
`DOCGEN_MAX_DELIVER`, `RENDER_POOL_SIZE`, `TEMPLATES_BUCKET`, `OUTPUT_BUCKET`,
`STORAGE_ENDPOINT/ACCESS_KEY/SECRET_KEY/REGION`. The dev stack exposes NATS on
host `8094` and MinIO on `8091`.
