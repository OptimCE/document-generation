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
schéma JSON du manifeste, effectue le rendu en **PDF, HTML, DOCX et/ou XLSX**,
écrit les artefacts dans un unique bucket de sortie et publie un résultat.

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
  renderers/               jinja-html→PDF (WeasyPrint), xlsx (openpyxl),
                           docx (docxtpl), pdf-form (pypdf), registry, _ooxml
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
  "options": { "locale": "fr-BE", "presign_ttl": 3600, "watermark": "BROUILLON" },
  "metadata": { "invoice_id": "..." }
}
```

**Résultat** (publié vers `reply_to`) :

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

`options.watermark` (facultatif, 1 à 40 caractères) marque un brouillon : un texte en
diagonale sur chaque page PDF, le filigrane texte de Word dans chaque en-tête de
.docx, l'en-tête et le pied d'impression de chaque feuille .xlsx (Excel n'a pas de
filigrane à l'écran sans modifier les cellules, et les classeurs des régulateurs sont
des formats imposés), un élément fixe en HTML. Sans cette option, la sortie est
exactement le document non marqué. Le résultat renvoie le filigrane appliqué : un
appelant peut donc refuser un brouillon qu'un worker plus ancien, qui ignore les
options inconnues, aurait rendu sans marque.

`renderer` nomme ce qui a produit les octets (Python, zlib, les bibliothèques de
rendu). Un rendu est une fonction pure de (modèle, data, filigrane) pour un moteur
donné ; un appelant qui garde le sha256 d'un artefact comme preuve et le régénère plus
tard en a besoin pour distinguer une garantie perdue d'une bibliothèque mise à jour.

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

- `engine` — l'un de :

  | moteur | produit | bibliothèque | usage |
  |---|---|---|---|
  | `jinja-html` | `pdf`, `html` | Jinja2 + WeasyPrint | les documents que nous rédigeons |
  | `xlsx` | `xlsx` | openpyxl | les formulaires tableur |
  | `docx` | `docx` | docxtpl | les documents dont le format imposé est Word |
  | `pdf-form` | `pdf` | pypdf | remplir le PDF remplissable d'une autorité |

- `entrypoint` *(optionnel)* — fichier d'entrée ; par défaut `template.html` /
  `template.xlsx` / `template.docx` / `template.pdf`.
- `output_basename` *(optionnel)* — nom de base de l'artefact ; par défaut le
  dernier segment (séparé par un point) de `id` (`billing.invoice` →
  `invoice.pdf`).
- `required_fields` — schéma JSON ; un schéma vide valide tout.
- `blocks` *(xlsx seulement)* — zones répétées, voir ci-dessous.
- `fields` *(pdf-form seulement)* — clé de data → nom du champ AcroForm.

**Les modèles jinja-html** reçoivent les `data` de la requête sous le nom `data`
et la locale sous le nom `locale` (p. ex. `{{ data.invoice_number }}`). Les
ressources relatives (`./invoice.css`, `./logo.png`, polices) sont résolues par
rapport au répertoire du modèle. Un champ manquant est une erreur bloquante (pas
de blanc silencieux). Voir `tests/fixtures/templates/billing_invoice/`.

**Les modèles docx** fonctionnent de la même façon — `{{ data.x }}` écrit dans Word.

**Les modèles xlsx** sont remplis à partir de `data` de trois façons : les **noms
définis** du classeur qui correspondent aux clés de premier niveau de `data`, les
**blocs répétés**, et une table `data.cells` explicite associant `"A1"` /
`"Sheet!A1"` → valeur (appliquée en dernier).

Un bloc transforme une liste de longueur variable en lignes de feuille. La
disposition est déclarée par le *manifeste*, jamais par l'appelant — la cellule
d'ancrage est une propriété du classeur :

```json
"blocks": [{
  "source": "participants",
  "anchor": "Membres et actionnaires!A4",
  "columns": ["categorie", "nom", "rue", "code_postal"],
  "hide_unused_rows": true
}]
```

`data.participants` est alors une liste de dictionnaires, et la ligne *i* colonne *j*
vaut `rows[i][columns[j]]`. Les lignes sont écrites **sur place** dans une bande que le
modèle prévoit déjà — jamais insérées, car openpyxl ne corrige ni les formules, ni les
plages fusionnées, ni les noms définis, ni les validations de données, ni les zones
d'impression quand les lignes se décalent. La capacité vient de
`required_fields.properties.<source>.maxItems` (ou d'un `max_rows` explicite), et la
dépasser est une `VALIDATION_ERROR` **permanente**, levée avant tout rendu. Les lignes
inutilisées sont masquées, pas supprimées.

**Les modèles pdf-form** sont le PDF remplissable de l'autorité elle-même ; seules les
valeurs des champs changent. Les formulaires officiels nomment leurs champs
automatiquement, le manifeste les associe donc :
`"fields": {"community_name": "Champ de texte 68"}`. Les états d'export des cases à
cocher sont lus dans le PDF : `/Oui`, `/Yes` et `/1` fonctionnent tous sans
configuration.

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
