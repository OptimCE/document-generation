<p align="center">
  <img src="logo.svg" alt="Logo document-generation OptimCE" width="160">
</p>

# document-generation

[![Site web](https://img.shields.io/badge/Site%20web-optimce.be-2e7d32.svg)](https://www.optimce.be)
[![Licence](https://img.shields.io/badge/Licence-Apache%202.0-blue.svg)](../LICENSE)
[![en](https://img.shields.io/badge/lang-en-lightgrey.svg)](../README.md)
[![fr](https://img.shields.io/badge/lang-fr-43a047.svg)](README.fr.md)
[![de](https://img.shields.io/badge/lang-de-lightgrey.svg)](README.de.md)
[![nl](https://img.shields.io/badge/lang-nl-lightgrey.svg)](README.nl.md)

Un worker de génération de documents **générique**, **sans état** et
**exclusivement NATS** pour OptimCE.

Il consomme une requête de génération via JetStream, récupère un modèle + un
manifeste depuis le stockage objet, valide les `data` fournies au regard du
schéma JSON du manifeste, effectue le rendu en **PDF, HTML et/ou XLSX**, écrit
les artefacts dans un unique bucket de sortie et publie un résultat.

Le worker ne contient **aucune connaissance métier** — rien sur les factures,
les membres ou la CWaPE. Toutes les spécificités métier vivent dans les modèles
et leurs manifestes. C'est une fonction pure `(modèle, data) → artefacts`.

## Principes

- **Générique** — n'importe et ne référence jamais un domaine métier.
- **Sans état** — pas de base de données ; le seul état est un cache de modèles
  sur disque local.
- **Exclusivement NATS** — pas de serveur HTTP, pas de framework web, aucune
  surface réseau publique.
- **Rendu pur** — les octets d'un artefact sont une fonction déterministe de
  `(modèle, data)` ; jamais de `now()`/aléatoire dans un rendu. C'est ce qui
  rend la livraison au-moins-une-fois idempotente.
- **Moindre privilège** — lit le bucket des modèles, écrit l'unique bucket de
  sortie ; rien d'autre.

## Architecture (hexagonale / ports & adaptateurs)

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

Le cœur du domaine orchestre ; les adaptateurs touchent le monde extérieur. Le
cœur n'importe jamais un adaptateur, il est donc testé unitairement avec des
faux (fakes) en mémoire.

## Contrats de messages

La requête et le résultat sont le **corps du message NATS** (JSON), en
snake_case. `metadata` est opaque et renvoyé tel quel ; `request_id` est la clé
de corrélation.

**Requête** (`docgen.request`) :

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

**Résultat** (publié vers `reply_to`) :

```json
{
  "request_id": "uuid", "status": "success",
  "artifacts": [{ "format": "pdf", "uri": "s3://optimce-documents/.../invoice.pdf",
                  "presigned_url": "https://...", "size_bytes": 84213, "sha256": "..." }],
  "template_version": "3", "generated_at": "ISO-8601", "metadata": { "invoice_id": "..." }
}
```

Un résultat en échec porte `error: { code, message, permanent }` au lieu des
artefacts. `error.permanent` indique à l'appelant si une nouvelle tentative
pourrait un jour réussir.

Codes d'erreur : `VALIDATION_ERROR`, `TEMPLATE_NOT_FOUND`, `UNSUPPORTED_FORMAT`
(permanents) ; `RENDER_ERROR`, `STORAGE_ERROR` (transitoires).

## Manifeste (`manifest.json`, livré avec chaque modèle)

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

- `engine` — `jinja-html` (PDF/HTML via Jinja2 + WeasyPrint) ou `xlsx`
  (openpyxl).
- `entrypoint` *(optionnel)* — fichier d'entrée ; par défaut `template.html` /
  `template.xlsx`.
- `output_basename` *(optionnel)* — nom de base de l'artefact ; par défaut le
  dernier segment (séparé par un point) de `id` (`billing.invoice` →
  `invoice.pdf`).
- `required_fields` — schéma JSON ; un schéma vide valide tout.

**Les modèles jinja-html** reçoivent les `data` de la requête sous le nom `data`
et la locale sous le nom `locale` (p. ex. `{{ data.invoice_number }}`). Les
ressources relatives (`./invoice.css`, `./logo.png`, polices) sont résolues par
rapport au répertoire du modèle. Un champ manquant est une erreur bloquante (pas
de blanc silencieux). Voir `tests/fixtures/templates/billing_invoice/`.

**Les modèles xlsx** sont remplis à partir de `data` de deux façons : les **noms
définis** du classeur qui correspondent aux clés de premier niveau de `data`, et
une table `data.cells` explicite associant `"A1"` / `"Sheet!A1"` → valeur
(appliquée en dernier).

## Fiabilité (JetStream)

- Les requêtes sont sur un stream **work-queue** consommé par un consommateur
  push **durable et en queue-group** → des réplicas concurrents montent en
  charge horizontalement.
- Échecs **permanents** → ack + résultat en échec (pas de nouvelle tentative).
  Échecs **transitoires** → nak avec backoff ; après `DOCGEN_MAX_DELIVER`
  livraisons, la requête est routée vers la **DLQ** avec un résultat en échec
  final, puis acquittée.
- Les résultats vont vers `reply_to` sur un stream de résultats durable, afin
  qu'un appelant momentanément indisponible les reçoive quand même. Des clés de
  sortie déterministes rendent la re-livraison sûre (pas de magasin de
  déduplication ; les appelants dédupliquent sur `request_id`).

## Configuration

Piloté par l'environnement (`core/config.py`) ; copiez `.env.exemple` →
`.env.local`. Variables clés : `NATS_URL`, `DOCGEN_REQUEST_SUBJECT/STREAM`,
`DOCGEN_DURABLE`, `DOCGEN_RESULTS_STREAM`, `DOCGEN_DLQ_SUBJECT`,
`DOCGEN_MAX_DELIVER`, `DOCGEN_ACK_WAIT_SECONDS`, `RENDER_POOL_SIZE`,
`TEMPLATES_BUCKET` (lecture), `OUTPUT_BUCKET` (écriture),
`STORAGE_ENDPOINT/ACCESS_KEY/SECRET_KEY/REGION`.

## Développer et tester

```bash
python -m venv .venv && . .venv/Scripts/activate   # or source .venv/bin/activate
pip install -r requirements/all.txt
ruff check . && ruff format --check . && mypy . && pytest
```

Les tests sont hermétiques — pas de NATS, pas de stockage, pas de broker. Le
test PDF WeasyPrint est ignoré automatiquement là où les bibliothèques natives
sont absentes (il s'exécute dans le conteneur, ci-dessous).

### Vérifier le moteur de rendu PDF dans Docker (dépendances natives Linux)

```bash
docker build -f Dockerfile.worker -t docgen-worker .
docker run --rm --entrypoint sh docgen-worker -c \
  "pip install -r /dev/stdin <<<'pytest==8.3.4 pytest-asyncio==0.24.0 pypdf==5.1.0' \
   && python -m pytest -q"   # add the test sources via a bind mount in practice
```

(L'image n'embarque que le code d'exécution ; montez `tests/` pour lancer la
suite, ou exécutez le test PDF en CI sous Linux, là où `import weasyprint`
réussit.)

## Exécuter avec la stack de développement

Le fichier partagé `monorepo/docker-compose.dev.yml` n'est **pas** modifié par
ce service. Pour l'exécuter dans la stack de développement :

1. Créez les deux buckets une seule fois (MinIO sur le port hôte 8091) :

   ```bash
   mc alias set local http://localhost:8091 minioadmin minioadmin
   mc mb --ignore-existing local/optimce-templates local/optimce-documents
   ```

2. Soit exécutez le worker localement (`.env.local` pointe vers la stack)…

   ```bash
   ENV=local python -m worker.main
   ```

   …soit ajoutez un service à `docker-compose.dev.yml` :

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

   …et ajoutez les buckets à l'entrypoint de `minio-init` :
   `mc mb --ignore-existing local/optimce-templates local/optimce-documents;`

3. Déclenchez une requête de bout en bout :

   ```bash
   python -m scripts.smoke
   ```

## Contribuer

Les contributions sont les bienvenues. Consultez
[CONTRIBUTING.md](../CONTRIBUTING.md) (en anglais) pour savoir comment mettre en
place un environnement de développement, exécuter les contrôles qualité et
ouvrir une pull request. En participant, vous acceptez de respecter notre
[Code de conduite](../CODE_OF_CONDUCT.md) (en anglais).

## Sécurité

Merci de signaler les vulnérabilités de sécurité de manière responsable — voir
notre [politique de sécurité](../SECURITY.md) (en anglais). Merci de **ne pas**
ouvrir d'issues publiques pour les vulnérabilités.

## Licence

Distribué sous [licence Apache 2.0](../LICENSE).
