"""
Schema validator for the defense system.

Validates upstream PostWithDetails payloads (input) and canonical
AnalysisResult documents (output) against their JSON Schema (draft-07)
definitions.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft7Validator
from jsonschema.exceptions import SchemaError

_SCHEMA_DIR = Path(__file__).parent

# ---------------------------------------------------------------------------
# Schema loading
# ---------------------------------------------------------------------------

def _load_schema(filename: str) -> dict:
    schema_path = _SCHEMA_DIR / filename
    with schema_path.open("r", encoding="utf-8") as fh:
        schema = json.load(fh)
    # Eagerly check the schema itself is valid so misconfiguration fails fast.
    try:
        Draft7Validator.check_schema(schema)
    except SchemaError as exc:
        raise RuntimeError(
            f"Schema file {filename!r} is not a valid JSON Schema: {exc.message}"
        ) from exc
    return schema


_INPUT_SCHEMA: dict = _load_schema("input_schema.json")
_OUTPUT_SCHEMA: dict = _load_schema("output_schema.json")

_input_validator = Draft7Validator(_INPUT_SCHEMA)
_output_validator = Draft7Validator(_OUTPUT_SCHEMA)

# ---------------------------------------------------------------------------
# Internal helper
# ---------------------------------------------------------------------------

def _collect_errors(validator: Draft7Validator, data: Any) -> list[str]:
    """Return all validation errors as human-readable strings."""
    errors: list[str] = []
    for error in sorted(validator.iter_errors(data), key=lambda e: list(e.absolute_path)):
        path = " -> ".join(str(p) for p in error.absolute_path)
        location = f"[{path}] " if path else ""
        errors.append(f"{location}{error.message}")
    return errors

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def validate_input(data: dict) -> tuple[bool, list[str]]:
    """Validate *data* against the PostWithDetails (input) schema.

    Returns:
        (True, [])           — data is valid.
        (False, [str, ...])  — data is invalid; the list contains one
                               human-readable string per validation error.
    """
    errors = _collect_errors(_input_validator, data)
    return (len(errors) == 0, errors)


def validate_output(data: dict) -> tuple[bool, list[str]]:
    """Validate *data* against the AnalysisResult (output) schema.

    Returns:
        (True, [])           — data is valid.
        (False, [str, ...])  — data is invalid; the list contains one
                               human-readable string per validation error.
    """
    errors = _collect_errors(_output_validator, data)
    return (len(errors) == 0, errors)


def assert_valid_input(data: dict) -> None:
    """Raise ValueError (with all errors) if *data* fails input schema validation."""
    valid, errors = validate_input(data)
    if not valid:
        raise ValueError(
            "Input payload failed schema validation:\n"
            + "\n".join(f"  - {e}" for e in errors)
        )


def assert_valid_output(data: dict) -> None:
    """Raise ValueError (with all errors) if *data* fails output schema validation."""
    valid, errors = validate_output(data)
    if not valid:
        raise ValueError(
            "Output payload failed schema validation:\n"
            + "\n".join(f"  - {e}" for e in errors)
        )
