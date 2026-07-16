"""Validator adapter: JSON Schema (Draft 2020-12) document-integrity gate.

This refuses to emit an official document built from incomplete ``data`` (e.g. a
NULL national-registry field). It is not input sanitization — its job is to fail
*before* rendering when the data does not satisfy the manifest's
``required_fields`` schema. An empty schema validates everything.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, best_match

from domain.errors import SchemaValidationError


class JsonSchemaValidator:
    def validate(self, data: Mapping[str, Any], schema: Mapping[str, Any]) -> None:
        if not schema:
            return  # schema-less manifest: nothing to enforce

        schema_dict = dict(schema)
        try:
            Draft202012Validator.check_schema(schema_dict)
        except SchemaError as exc:
            # A broken required_fields schema is a template authoring error;
            # surfaced as a (permanent) validation failure so the caller sees it.
            raise SchemaValidationError(f"invalid required_fields schema: {exc.message}") from exc

        validator = Draft202012Validator(schema_dict)
        error = best_match(validator.iter_errors(dict(data)))
        if error is not None:
            location = "/".join(str(p) for p in error.absolute_path) or "<root>"
            raise SchemaValidationError(f"data invalid at {location}: {error.message}")
