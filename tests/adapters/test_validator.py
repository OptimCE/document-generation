"""JSON Schema validator adapter tests."""

from __future__ import annotations

import pytest

from adapters.validator_jsonschema import JsonSchemaValidator
from domain.errors import SchemaValidationError

_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["invoice_number", "total"],
    "properties": {
        "invoice_number": {"type": "string"},
        "total": {"type": "number"},
    },
}


def test_valid_data_passes():
    JsonSchemaValidator().validate({"invoice_number": "X", "total": 1.5}, _SCHEMA)


def test_missing_required_field_raises():
    with pytest.raises(SchemaValidationError):
        JsonSchemaValidator().validate({"invoice_number": "X"}, _SCHEMA)


def test_wrong_type_raises():
    with pytest.raises(SchemaValidationError):
        JsonSchemaValidator().validate({"invoice_number": "X", "total": "NaN"}, _SCHEMA)


def test_empty_schema_validates_everything():
    JsonSchemaValidator().validate({"anything": [1, 2, 3]}, {})


def test_invalid_schema_raises_validation_error():
    with pytest.raises(SchemaValidationError, match="invalid required_fields schema"):
        JsonSchemaValidator().validate({}, {"type": 123})
