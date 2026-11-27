<p align="center">
  <img src="logo.svg" alt="OptimCE document-generation-logo" width="160">
</p>

# document-generation

[![Website](https://img.shields.io/badge/Website-optimce.be-2e7d32.svg)](https://www.optimce.be/nl/)
[![Licentie](https://img.shields.io/badge/Licentie-Apache%202.0-blue.svg)](../LICENSE)
[![en](https://img.shields.io/badge/lang-en-lightgrey.svg)](../README.md)
[![fr](https://img.shields.io/badge/lang-fr-lightgrey.svg)](README.fr.md)
[![de](https://img.shields.io/badge/lang-de-lightgrey.svg)](README.de.md)
[![nl](https://img.shields.io/badge/lang-nl-43a047.svg)](README.nl.md)

Een **generieke**, **stateless**, **uitsluitend op NATS** werkende
documentgeneratie-worker voor OptimCE.

Hij consumeert een generatieverzoek via JetStream, haalt een template + manifest
op uit de objectopslag, valideert de aangeleverde `data` tegen het JSON-schema
van het manifest, rendert naar **PDF, HTML, DOCX en/of XLSX**, schrijft de
artefacten naar één enkele output-bucket en publiceert een resultaat.

De worker bevat **geen domeinkennis** — niets over facturen, leden of de CWaPE.
Alle domeinspecifieke zaken leven in de templates en hun manifesten. Het is een
pure functie `(template, data) → artefacten`.

## Principes

- **Generiek** — importeert of verwijst nooit naar een businessdomein.
- **Stateless** — geen database; de enige state is een template-cache op de
  lokale schijf.
- **Uitsluitend NATS** — geen HTTP-server, geen webframework, geen publiek
  netwerkoppervlak.
- **Pure rendering** — de artefact-bytes zijn een deterministische functie van
  `(template, data)`; nooit `now()`/willekeur binnen een rendering. Dat maakt de
  at-least-once-levering idempotent.
- **Minimale rechten** — leest de templates-bucket, schrijft de enige
  output-bucket; verder niets.

## Architectuur (hexagonaal / ports & adapters)

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
  renderers/               jinja-html→PDF (WeasyPrint), xlsx (openpyxl),
                           docx (docxtpl), pdf-form (pypdf), registry, _ooxml
core/          # reused infra (settings, logging, tracing, metrics, queue, storage client)
worker/        # main.py entrypoint + dispatcher (subscription, ack/nak/DLQ)
```

De domeinkern orchestreert; de adapters raken de buitenwereld. De kern importeert
nooit een adapter en wordt daarom met in-memory fakes unit-getest.

## Berichtcontracten

Verzoek en resultaat vormen de **body van het NATS-bericht** (JSON), in
snake_case. `metadata` is ondoorzichtig en wordt ongewijzigd teruggegeven;
`request_id` is de correlatiesleutel.

**Verzoek** (`docgen.request`):

```json
{
  "request_id": "uuid", "tenant_id": "uuid", "requested_by": "billing",
  "template": { "uri": "s3://optimce-templates/billing/invoice/v3/" },
  "outputs": [{ "format": "pdf" }],
  "data": { },
  "key_prefix": "billing/invoices/2026/06/inv-00042/",
  "reply_to": "docgen.result.billing",
  "options": { "locale": "fr-BE", "presign_ttl": 3600, "watermark": "BROUILLON" },
  "metadata": { "invoice_id": "..." }
}
```

**Resultaat** (gepubliceerd naar `reply_to`):

```json
{
  "request_id": "uuid", "status": "success",
  "artifacts": [{ "format": "pdf", "uri": "s3://optimce-documents/.../invoice.pdf",
                  "presigned_url": "https://...", "size_bytes": 84213, "sha256": "..." }],
  "template_version": "3", "watermark": "BROUILLON",
  "renderer": { "python": "3.12.13", "zlib": "1.3.1", "pypdf": "5.1.0", "...": "..." },
  "generated_at": "ISO-8601", "metadata": { "invoice_id": "..." }
}
```

`options.watermark` (optioneel, 1 tot 40 tekens) markeert een concept: een diagonale
tekst op elke pdf-pagina, het tekstwatermerk van Word in elke .docx-koptekst, de kop-
en voettekst bij het afdrukken van elk .xlsx-blad (Excel heeft geen watermerk op het
scherm zonder de cellen te wijzigen, en de werkmappen van regulatoren zijn opgelegde
formaten), een vast element in HTML. Zonder deze optie is de uitvoer exact het
ongemarkeerde document. Het resultaat geeft het toegepaste watermerk terug, zodat een
aanroeper een concept kan weigeren dat een oudere worker, die onbekende opties negeert,
zonder markering heeft weergegeven.

`renderer` noemt wat de bytes heeft geproduceerd (Python, zlib, de
renderbibliotheken). Een rendering is voor een gegeven renderer een pure functie van
(template, data, watermerk); een aanroeper die de sha256 van een artefact als bewijs
bewaart en het later opnieuw genereert, heeft dit nodig om een verloren garantie te
onderscheiden van een bijgewerkte bibliotheek.

Een mislukt resultaat draagt `error: { code, message, permanent }` in plaats van
artefacten. `error.permanent` vertelt de aanroeper of een nieuwe poging ooit kan
slagen.

Foutcodes: `VALIDATION_ERROR`, `TEMPLATE_NOT_FOUND`, `UNSUPPORTED_FORMAT`
(permanent); `RENDER_ERROR`, `STORAGE_ERROR` (tijdelijk).

## Manifest (`manifest.json`, meegeleverd met elk template)

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

- `engine` — een van:

  | engine | produceert | bibliotheek | gebruik |
  |---|---|---|---|
  | `jinja-html` | `pdf`, `html` | Jinja2 + WeasyPrint | documenten die we zelf opstellen |
  | `xlsx` | `xlsx` | openpyxl | spreadsheetformulieren |
  | `docx` | `docx` | docxtpl | documenten waarvan Word het opgelegde formaat is |
  | `pdf-form` | `pdf` | pypdf | de invulbare pdf van een overheid invullen |

- `entrypoint` *(optioneel)* — invoerbestand; standaard `template.html` /
  `template.xlsx` / `template.docx` / `template.pdf`.
- `output_basename` *(optioneel)* — basisnaam van het artefact; standaard het
  laatste met een punt gescheiden segment van `id` (`billing.invoice` →
  `invoice.pdf`).
- `required_fields` — JSON-schema; een leeg schema valideert alles.
- `blocks` *(alleen xlsx)* — herhaalde zones, zie hieronder.
- `fields` *(alleen pdf-form)* — sleutel van data → naam van het AcroForm-veld.

**jinja-html-templates** ontvangen de `data` van het verzoek als `data` en de
locale als `locale` (bijv. `{{ data.invoice_number }}`). Relatieve bronnen
(`./invoice.css`, `./logo.png`, fonts) worden opgelost ten opzichte van de
template-map. Een ontbrekend veld is een harde fout (geen stille lege waarden).
Zie `tests/fixtures/templates/billing_invoice/`.

**docx-templates** werken op dezelfde manier — `{{ data.x }}`, geschreven in Word.

**xlsx-templates** worden op drie manieren uit `data` gevuld: via de **defined
names** van de werkmap die overeenkomen met de top-level sleutels van `data`, via
**herhaalde blokken**, en via een expliciete `data.cells`-map `"A1"` /
`"Sheet!A1"` → waarde (als laatste toegepast).

Een blok zet een lijst van variabele lengte om in rijen van een werkblad. De indeling
wordt vastgelegd door het *manifest*, nooit door de aanroeper — de ankercel is een
eigenschap van de werkmap:

```json
"blocks": [{
  "source": "participants",
  "anchor": "Membres et actionnaires!A4",
  "columns": ["categorie", "nom", "rue", "code_postal"],
  "hide_unused_rows": true
}]
```

`data.participants` is dan een lijst van dictionaries, en rij *i* kolom *j* is
`rows[i][columns[j]]`. De rijen worden **ter plaatse** geschreven in een strook die het
template al voorziet — nooit ingevoegd, omdat openpyxl formules, samengevoegde bereiken,
defined names, gegevensvalidaties en afdrukbereiken niet aanpast wanneer rijen
verschuiven. De capaciteit komt uit `required_fields.properties.<source>.maxItems` (of
een expliciete `max_rows`), en ze overschrijden is een **permanente**
`VALIDATION_ERROR`, opgeworpen vóór elke rendering. Ongebruikte rijen worden verborgen,
niet verwijderd.

**pdf-form-templates** zijn de invulbare pdf van de overheid zelf; alleen de
veldwaarden veranderen. Officiële formulieren benoemen hun velden automatisch, dus
koppelt het manifest ze: `"fields": {"community_name": "Champ de texte 68"}`. De
exportwaarden van selectievakjes worden uit de pdf zelf gelezen, zodat `/Oui`, `/Yes`
en `/1` allemaal werken zonder configuratie.

## Betrouwbaarheid (JetStream)

- Verzoeken staan op een **work-queue**-stream die wordt geconsumeerd door een
  **durable, queue-group** push-consumer → concurrerende replica's schalen
  horizontaal.
- **Permanente** fouten → ack + mislukt resultaat (geen nieuwe poging).
  **Tijdelijke** fouten → nak met backoff; na `DOCGEN_MAX_DELIVER` leveringen
  wordt het verzoek naar de **DLQ** geleid met een definitief mislukt resultaat
  en vervolgens geackt.
- Resultaten gaan naar `reply_to` op een durable resultaten-stream, zodat een
  kort niet-beschikbare aanroeper ze alsnog ontvangt. Deterministische
  outputsleutels maken herlevering veilig (geen dedup-opslag; aanroepers
  dedupliceren op `request_id`).

## Configuratie

Aangestuurd via de omgeving (`core/config.py`); kopieer `.env.exemple` →
`.env.local`. Belangrijke variabelen: `NATS_URL`, `DOCGEN_REQUEST_SUBJECT/STREAM`,
`DOCGEN_DURABLE`, `DOCGEN_RESULTS_STREAM`, `DOCGEN_DLQ_SUBJECT`,
`DOCGEN_MAX_DELIVER`, `DOCGEN_ACK_WAIT_SECONDS`, `RENDER_POOL_SIZE`,
`TEMPLATES_BUCKET` (lezen), `OUTPUT_BUCKET` (schrijven),
`STORAGE_ENDPOINT/ACCESS_KEY/SECRET_KEY/REGION`.

## Ontwikkelen & testen

```bash
python -m venv .venv && . .venv/Scripts/activate   # or source .venv/bin/activate
pip install -r requirements/all.txt
ruff check . && ruff format --check . && mypy . && pytest
```

De tests zijn hermetisch — geen NATS, geen opslag, geen broker. De
WeasyPrint-PDF-test wordt automatisch overgeslagen waar de native bibliotheken
ontbreken (hij draait in de container, hieronder).

### De PDF-renderer verifiëren in Docker (native Linux-afhankelijkheden)

```bash
docker build -f Dockerfile.worker -t docgen-worker .
docker run --rm --entrypoint sh docgen-worker -c \
  "pip install -r /dev/stdin <<<'pytest==8.3.4 pytest-asyncio==0.24.0 pypdf==5.1.0' \
   && python -m pytest -q"   # add the test sources via a bind mount in practice
```

(De image bevat alleen de runtime-code; mount `tests/` om de suite te draaien, of
draai de PDF-test in CI op Linux waar `import weasyprint` slaagt.)

## Draaien tegen de dev-stack

Het gedeelde bestand `monorepo/docker-compose.dev.yml` wordt door deze service
**niet** gewijzigd. Zo draai je hem in de dev-stack:

1. Maak de twee buckets eenmalig aan (MinIO op hostpoort 8091):

   ```bash
   mc alias set local http://localhost:8091 minioadmin minioadmin
   mc mb --ignore-existing local/optimce-templates local/optimce-documents
   ```

2. Draai de worker ofwel lokaal (`.env.local` wijst naar de stack)…

   ```bash
   ENV=local python -m worker.main
   ```

   …of voeg een service toe aan `docker-compose.dev.yml`:

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

   …en voeg de buckets toe aan de entrypoint van `minio-init`:
   `mc mb --ignore-existing local/optimce-templates local/optimce-documents;`

3. Stuur één verzoek end-to-end door:

   ```bash
   python -m scripts.smoke
   ```

## Bijdragen

Bijdragen zijn welkom. Zie [CONTRIBUTING.md](../CONTRIBUTING.md) (in het Engels)
voor hoe je een ontwikkelomgeving opzet, de kwaliteitscontroles draait en een
pull request opent. Door deel te nemen ga je akkoord met onze
[Gedragscode](../CODE_OF_CONDUCT.md) (in het Engels).

## Beveiliging

Meld beveiligingskwetsbaarheden op een verantwoorde manier — zie ons
[beveiligingsbeleid](../SECURITY.md) (in het Engels). Open **geen** publieke
issues voor kwetsbaarheden.

## Licentie

Gelicentieerd onder de [Apache-licentie 2.0](../LICENSE).
