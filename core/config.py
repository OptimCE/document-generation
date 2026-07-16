"""Environment-driven settings (pydantic-settings), worker-only.

No database, no CORS, no HTTP — this service has none. The settings cover NATS
JetStream topology, the two object-storage buckets, the render pool, and
observability. ``ENV`` selects the ``.env.<env>`` file at import time; a module
``model_validator`` enforces the variables that must be present per environment.
"""

from __future__ import annotations

import os
from enum import StrEnum

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    LOCAL = "local"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


def _get_env_file() -> str:
    env = os.getenv("ENV", "local").strip()
    return f".env.{env}"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_get_env_file(),
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # ---- Core ----
    ENV: Environment = Environment.LOCAL

    # ---- NATS ----
    NATS_URL: str = ""

    # ---- JetStream topology (docgen.*) ----
    # Defaults match core/queue/init.py's stream declarations. Override via env
    # only if you also keep the declared stream subjects consistent.
    DOCGEN_REQUEST_STREAM: str = "DOCGEN_REQUESTS"
    DOCGEN_REQUEST_SUBJECT: str = "docgen.request"
    DOCGEN_DURABLE: str = "worker-docgen"
    DOCGEN_RESULTS_STREAM: str = "DOCGEN_RESULTS"
    DOCGEN_RESULTS_SUBJECT_FILTER: str = "docgen.result.>"
    DOCGEN_DLQ_STREAM: str = "DOCGEN_DLQ"
    DOCGEN_DLQ_SUBJECT: str = "docgen.dlq"
    # Bounded redelivery: after this many attempts a transient failure is routed
    # to the DLQ with a final failed result. Must be >= 1.
    DOCGEN_MAX_DELIVER: int = 4
    # ack_wait comfortably above max render wall-clock so a slow render is not
    # redelivered while still running.
    DOCGEN_ACK_WAIT_SECONDS: int = 600
    DOCGEN_NAK_RETRY_DELAY_SECONDS: int = 30

    # ---- Rendering ----
    # 0 → size the process pool automatically (available CPUs - 1).
    RENDER_POOL_SIZE: int = 0
    # Local-disk cache for downloaded template prefixes (the only permitted state).
    TEMPLATE_CACHE_DIR: str = "/tmp/docgen-templates"  # noqa: S108 — dedicated container path

    # ---- Storage (S3-compatible, MinIO in dev) ----
    # Endpoint URL of the S3-compatible storage server (e.g. http://minio:9000).
    # Empty in local/test where the storage module is mocked.
    STORAGE_ENDPOINT: str = ""
    # Two dedicated buckets: read-only templates, write-only output. The worker
    # never touches any other bucket.
    TEMPLATES_BUCKET: str = "optimce-templates"
    OUTPUT_BUCKET: str = "optimce-documents"
    STORAGE_ACCESS_KEY: str = ""
    STORAGE_SECRET_KEY: str = ""
    # MinIO ignores region but botocore still requires it to sign requests.
    STORAGE_REGION: str = "us-east-1"

    # ---- Observability ----
    LOGGING_TOKEN: str = ""
    LOGGING_TRACES_URL: str = ""
    LOGGING_LOGS_URL: str = ""
    LOGGING_METRICS_URL: str = ""

    @model_validator(mode="after")
    def validate_env_config(self) -> Settings:
        if self.DOCGEN_MAX_DELIVER < 1:
            raise ValueError("DOCGEN_MAX_DELIVER must be >= 1")
        if self.ENV in (Environment.STAGING, Environment.PRODUCTION):
            if not self.NATS_URL.strip():
                raise ValueError("NATS_URL is required in staging/production")
            if not self.STORAGE_ENDPOINT.strip():
                raise ValueError("STORAGE_ENDPOINT is required in staging/production")
            if not self.STORAGE_ACCESS_KEY.strip():
                raise ValueError("STORAGE_ACCESS_KEY is required in staging/production")
            if not self.STORAGE_SECRET_KEY.strip():
                raise ValueError("STORAGE_SECRET_KEY is required in staging/production")
            if not self.TEMPLATES_BUCKET.strip() or not self.OUTPUT_BUCKET.strip():
                raise ValueError(
                    "TEMPLATES_BUCKET and OUTPUT_BUCKET are required in staging/production"
                )
        if self.ENV == Environment.PRODUCTION:
            if not self.LOGGING_TOKEN:
                raise ValueError("LOGGING_TOKEN required for production")
            if not self.LOGGING_LOGS_URL:
                raise ValueError("LOGGING_LOGS_URL required for production")
            if not self.LOGGING_METRICS_URL:
                raise ValueError("LOGGING_METRICS_URL required for production")
        return self


settings = Settings()
