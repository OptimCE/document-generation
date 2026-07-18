<p align="center">
  <img src="logo.svg" alt="OptimCE document-generation Logo" width="160">
</p>

# document-generation

[![Website](https://img.shields.io/badge/Website-optimce.be-2e7d32.svg)](https://www.optimce.be/de/)
[![Lizenz](https://img.shields.io/badge/Lizenz-Apache%202.0-blue.svg)](../LICENSE)
[![en](https://img.shields.io/badge/lang-en-lightgrey.svg)](../README.md)
[![fr](https://img.shields.io/badge/lang-fr-lightgrey.svg)](README.fr.md)
[![de](https://img.shields.io/badge/lang-de-43a047.svg)](README.de.md)
[![nl](https://img.shields.io/badge/lang-nl-lightgrey.svg)](README.nl.md)

Ein **generischer**, **zustandsloser**, **ausschließlich über NATS** angebundener
Dokumentgenerierungs-Worker für OptimCE.

Er konsumiert eine Generierungsanfrage über JetStream, holt ein Template +
Manifest aus dem Objektspeicher, validiert die übergebenen `data` gegen das
JSON-Schema des Manifests, rendert nach **PDF, HTML und/oder XLSX**, schreibt die
Artefakte in einen einzigen Ausgabe-Bucket und veröffentlicht ein Ergebnis.

Der Worker enthält **kein Fachwissen** — nichts über Rechnungen, Mitglieder oder
die CWaPE. Alle fachlichen Besonderheiten leben in den Templates und ihren
Manifesten. Er ist eine reine Funktion `(Template, data) → Artefakte`.

## Prinzipien

- **Generisch** — importiert oder referenziert niemals eine Fachdomäne.
- **Zustandslos** — keine Datenbank; der einzige Zustand ist ein Template-Cache
  auf der lokalen Festplatte.
- **Ausschließlich NATS** — kein HTTP-Server, kein Web-Framework, keine
  öffentliche Netzwerkoberfläche.
- **Reines Rendern** — die Artefakt-Bytes sind eine deterministische Funktion von
  `(Template, data)`; niemals `now()`/Zufall innerhalb eines Renderings. Das
  macht die At-least-once-Zustellung idempotent.
- **Geringste Rechte** — liest den Templates-Bucket, schreibt den einzigen
  Ausgabe-Bucket; sonst nichts.

## Architektur (hexagonal / Ports & Adapter)

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

Der Domänenkern orchestriert; die Adapter berühren die Außenwelt. Der Kern
importiert niemals einen Adapter und wird daher mit In-Memory-Fakes
unit-getestet.

## Nachrichtenverträge

Anfrage und Ergebnis sind der **Body der NATS-Nachricht** (JSON), in snake_case.
`metadata` ist opak und wird unverändert zurückgegeben; `request_id` ist der
Korrelationsschlüssel.

**Anfrage** (`docgen.request`):

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

**Ergebnis** (veröffentlicht an `reply_to`):

```json
{
  "request_id": "uuid", "status": "success",
  "artifacts": [{ "format": "pdf", "uri": "s3://optimce-documents/.../invoice.pdf",
                  "presigned_url": "https://...", "size_bytes": 84213, "sha256": "..." }],
  "template_version": "3", "generated_at": "ISO-8601", "metadata": { "invoice_id": "..." }
}
```

Ein fehlgeschlagenes Ergebnis trägt `error: { code, message, permanent }`
anstelle der Artefakte. `error.permanent` sagt dem Aufrufer, ob ein erneuter
Versuch jemals erfolgreich sein könnte.

Fehlercodes: `VALIDATION_ERROR`, `TEMPLATE_NOT_FOUND`, `UNSUPPORTED_FORMAT`
(permanent); `RENDER_ERROR`, `STORAGE_ERROR` (transient).

## Manifest (`manifest.json`, wird mit jedem Template ausgeliefert)

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

- `engine` — `jinja-html` (PDF/HTML über Jinja2 + WeasyPrint) oder `xlsx`
  (openpyxl).
- `entrypoint` *(optional)* — Einstiegsdatei; Standard `template.html` /
  `template.xlsx`.
- `output_basename` *(optional)* — Basisname des Artefakts; Standard ist das
  letzte durch Punkt getrennte Segment von `id` (`billing.invoice` →
  `invoice.pdf`).
- `required_fields` — JSON-Schema; ein leeres Schema validiert alles.

**jinja-html-Templates** erhalten die `data` der Anfrage als `data` und die
Locale als `locale` (z. B. `{{ data.invoice_number }}`). Relative Ressourcen
(`./invoice.css`, `./logo.png`, Schriftarten) werden relativ zum
Template-Verzeichnis aufgelöst. Ein fehlendes Feld ist ein harter Fehler (keine
stillen Leerstellen). Siehe `tests/fixtures/templates/billing_invoice/`.

**xlsx-Templates** werden auf zwei Wegen aus `data` befüllt: über die **defined
names** der Arbeitsmappe, die zu den Top-Level-Schlüsseln von `data` passen, und
über eine explizite `data.cells`-Zuordnung `"A1"` / `"Sheet!A1"` → Wert (zuletzt
angewendet).

## Zuverlässigkeit (JetStream)

- Anfragen liegen auf einem **Work-Queue**-Stream, konsumiert von einem
  **durablen Queue-Group**-Push-Consumer → konkurrierende Replicas skalieren
  horizontal.
- **Permanente** Fehler → ack + fehlgeschlagenes Ergebnis (kein Retry).
  **Transiente** Fehler → nak mit Backoff; nach `DOCGEN_MAX_DELIVER`
  Zustellungen wird die Anfrage mit einem finalen fehlgeschlagenen Ergebnis in
  die **DLQ** geleitet und dann acked.
- Ergebnisse gehen an `reply_to` auf einem durablen Ergebnis-Stream, sodass ein
  kurzzeitig nicht erreichbarer Aufrufer sie dennoch erhält. Deterministische
  Ausgabeschlüssel machen die erneute Zustellung sicher (kein Dedup-Speicher;
  Aufrufer deduplizieren über `request_id`).

## Konfiguration

Über die Umgebung gesteuert (`core/config.py`); kopieren Sie `.env.exemple` →
`.env.local`. Wichtige Variablen: `NATS_URL`, `DOCGEN_REQUEST_SUBJECT/STREAM`,
`DOCGEN_DURABLE`, `DOCGEN_RESULTS_STREAM`, `DOCGEN_DLQ_SUBJECT`,
`DOCGEN_MAX_DELIVER`, `DOCGEN_ACK_WAIT_SECONDS`, `RENDER_POOL_SIZE`,
`TEMPLATES_BUCKET` (Lesen), `OUTPUT_BUCKET` (Schreiben),
`STORAGE_ENDPOINT/ACCESS_KEY/SECRET_KEY/REGION`.

## Entwickeln & testen

```bash
python -m venv .venv && . .venv/Scripts/activate   # or source .venv/bin/activate
pip install -r requirements/all.txt
ruff check . && ruff format --check . && mypy . && pytest
```

Die Tests sind hermetisch — kein NATS, kein Speicher, kein Broker. Der
WeasyPrint-PDF-Test wird dort automatisch übersprungen, wo die nativen
Bibliotheken fehlen (er läuft im Container, siehe unten).

### Den PDF-Renderer in Docker verifizieren (native Linux-Abhängigkeiten)

```bash
docker build -f Dockerfile.worker -t docgen-worker .
docker run --rm --entrypoint sh docgen-worker -c \
  "pip install -r /dev/stdin <<<'pytest==8.3.4 pytest-asyncio==0.24.0 pypdf==5.1.0' \
   && python -m pytest -q"   # add the test sources via a bind mount in practice
```

(Das Image liefert nur den Laufzeitcode aus; mounten Sie `tests/`, um die Suite
auszuführen, oder führen Sie den PDF-Test in der CI unter Linux aus, wo
`import weasyprint` gelingt.)

## Gegen den Dev-Stack ausführen

Die gemeinsame Datei `monorepo/docker-compose.dev.yml` wird von diesem Service
**nicht** verändert. So führen Sie ihn im Dev-Stack aus:

1. Legen Sie die beiden Buckets einmalig an (MinIO auf Host-Port 8091):

   ```bash
   mc alias set local http://localhost:8091 minioadmin minioadmin
   mc mb --ignore-existing local/optimce-templates local/optimce-documents
   ```

2. Führen Sie den Worker entweder lokal aus (`.env.local` zeigt auf den Stack)…

   ```bash
   ENV=local python -m worker.main
   ```

   …oder fügen Sie `docker-compose.dev.yml` einen Service hinzu:

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

   …und ergänzen Sie die Buckets im Entrypoint von `minio-init`:
   `mc mb --ignore-existing local/optimce-templates local/optimce-documents;`

3. Stoßen Sie eine Anfrage End-to-End an:

   ```bash
   python -m scripts.smoke
   ```

## Mitwirken

Beiträge sind willkommen. In [CONTRIBUTING.md](../CONTRIBUTING.md) (auf Englisch)
steht, wie Sie eine Entwicklungsumgebung einrichten, die Qualitätsprüfungen
ausführen und einen Pull Request eröffnen. Mit Ihrer Teilnahme erklären Sie sich
mit unserem [Verhaltenskodex](../CODE_OF_CONDUCT.md) (auf Englisch) einverstanden.

## Sicherheit

Bitte melden Sie Sicherheitslücken verantwortungsvoll — siehe unsere
[Sicherheitsrichtlinie](../SECURITY.md) (auf Englisch). Bitte eröffnen Sie
**keine** öffentlichen Issues für Sicherheitslücken.

## Lizenz

Lizenziert unter der [Apache-Lizenz 2.0](../LICENSE).
