# Contributing to OptimCE — document-generation

Thank you for your interest in contributing! Issues and pull requests are
welcome from everyone. By participating in this project, you agree to abide by
our [Code of Conduct](CODE_OF_CONDUCT.md).

## Where to Contribute

This repository holds the **document-generation** worker — a generic, stateless,
NATS-only service that renders templates to PDF, HTML, and XLSX. It is one of
several repositories under the
[OptimCE organization](https://github.com/OptimCE), and is included in the
[OptimCE monorepo](https://github.com/OptimCE/monorepo) as a git submodule.

- **Changes to the worker itself** (rendering engines, adapters, the domain
  core, message contracts, tests) belong here.
- **Changes to the development environment and orchestration** — Docker Compose,
  the API gateway (KrakenD), authentication (Keycloak), shared reference data —
  belong in the [monorepo](https://github.com/OptimCE/monorepo) instead.

## Setting Up a Development Environment

The worker targets **Python 3.12**.

```bash
git clone https://github.com/OptimCE/document-generation.git
cd document-generation
py -3.12 -m venv .venv && . .venv/Scripts/activate   # bash: source .venv/bin/activate
pip install -r requirements/all.txt
```

Run the quality gates before opening a pull request:

```bash
ENV=test ruff check . && ruff format --check . && mypy . && pytest
```

The tests are hermetic — no NATS, no object storage, no broker. The WeasyPrint
PDF test is skipped automatically where the native GTK/Pango libraries are absent
(e.g. Windows dev hosts) and is verified in the Docker image:

```bash
docker build -f Dockerfile.worker -t docgen-worker .
```

On Windows, prefix test runs with `PYTHONIOENCODING=utf-8 PYTHONUTF8=1` so the
real error surfaces instead of an `INTERNALERROR`. See the [README](README.md)
for the full architecture, message contracts, and how to run the worker against
the dev stack.

## Reporting Bugs and Suggesting Features

Open a [GitHub issue](https://github.com/OptimCE/document-generation/issues).
For bugs, include what you did, what you expected, and what happened instead —
logs and reproduction steps help a lot.

For security vulnerabilities, **do not open a public issue**; follow the
[security policy](SECURITY.md) instead.

## Submitting Pull Requests

1. Fork the repository and create a feature branch from `main`.
2. Make your changes. Keep each pull request focused on a single topic.
3. Make sure the quality gates pass
   (`ENV=test ruff check . && ruff format --check . && mypy . && pytest`).
4. Open a pull request against `main`, describing **what** you changed and
   **why**.

Notes:

- The worker stays **generic** — it must never import or reference a business
  domain (invoices, members, CWaPE). Those specifics live in templates and their
  manifests, not in this code.
- Small documentation fixes are welcome as direct pull requests; for larger
  changes, opening an issue first to discuss the approach can save you time.

## Commit Messages

Use short, imperative commit messages, preferably following the
[Conventional Commits](https://www.conventionalcommits.org/) style used in this
repository:

```
feat: add xlsx defined-name binding
fix: resolve template asset base_url
chore: bump weasyprint to 63.1
docs: document the manifest output_basename default
```

## License

document-generation is licensed under the [Apache License 2.0](LICENSE). By
contributing, you agree that your contributions will be licensed under the same
license.
