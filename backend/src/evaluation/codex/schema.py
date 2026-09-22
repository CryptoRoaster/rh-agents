"""Turn a Pydantic JSON schema into one Codex will accept, and nothing more.

Codex 0.153.4 sends `--output-schema` with strict validation always on:
`output_schema_strict` defaults to `true` in `core/src/client_common.rs` and no
CLI flag or config key turns it off. Strict structured output documents exactly
two structural requirements, and this module implements exactly those two:

  * every property of an object must appear in `required`;
  * objects must be closed with `additionalProperties: false`.

Everything else is left alone. `pattern`, `minLength`, `maxLength`, `minItems`,
`maxItems` and `format` stay in the schema, because no reachable document shows
that strict mode rejects them. Stripping them on suspicion would weaken the
request for no demonstrated reason; if a real run rejects one, the rejection
message is evidence and the rule can be added then.

Two things this module refuses to do quietly. It never rewrites a field that has
a default into a nullable field -- strict mode wants the key present, which is a
different demand from allowing null, and guessing here would change the domain
meaning. And it never closes a free-form object: `dict[str, object]` means "any
keys", so stamping `additionalProperties: false` onto it would silently turn a
permissive contract into an impossible one. Such a schema is rejected instead.

The input schema is never mutated; callers keep the object they passed in.
"""

from copy import deepcopy
from typing import Any

from pydantic import BaseModel

from src.evaluation.codex.models import SchemaUnsupportedError

# Keys whose values are a single nested schema.
_NESTED_SCHEMA_KEYS = ("items", "not", "additionalItems", "contains", "propertyNames")
# Keys whose values are a list of schemas.
_NESTED_SCHEMA_LISTS = ("anyOf", "oneOf", "allOf", "prefixItems")
# Keys whose values are a mapping of name -> schema.
_NESTED_SCHEMA_MAPS = ("properties", "$defs", "definitions", "patternProperties")


def strict_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Produce a strict-mode schema for `model`, or refuse to.

    Raises `SchemaUnsupportedError` when the model cannot be expressed without
    changing what it means.
    """
    transformed = _transform(deepcopy(model.model_json_schema()), path="$")
    if not isinstance(transformed, dict):  # pragma: no cover - a model schema is an object
        raise SchemaUnsupportedError("ROOT_NOT_OBJECT")
    return transformed


def _transform(node: Any, *, path: str) -> Any:
    if isinstance(node, list):
        return [_transform(item, path=f"{path}[{index}]") for index, item in enumerate(node)]
    if not isinstance(node, dict):
        return node

    schema: dict[str, Any] = dict(node)

    if "patternProperties" in schema:
        # Pattern-keyed objects have no strict-mode equivalent, and approximating
        # one would invent a key set the model never declared.
        raise SchemaUnsupportedError("PATTERN_PROPERTIES")

    if _is_object(schema):
        schema = _close_object(schema, path=path)

    for key in _NESTED_SCHEMA_KEYS:
        if key in schema:
            schema[key] = _transform(schema[key], path=f"{path}.{key}")
    for key in _NESTED_SCHEMA_LISTS:
        if key in schema:
            schema[key] = _transform(schema[key], path=f"{path}.{key}")
    for key in _NESTED_SCHEMA_MAPS:
        if key in schema and isinstance(schema[key], dict):
            schema[key] = {
                name: _transform(child, path=f"{path}.{key}.{name}")
                for name, child in schema[key].items()
            }

    return schema


def _is_object(schema: dict[str, Any]) -> bool:
    if "properties" in schema:
        return True
    declared = schema.get("type")
    if declared == "object":
        return True
    return isinstance(declared, list) and "object" in declared


def _close_object(schema: dict[str, Any], *, path: str) -> dict[str, Any]:
    additional = schema.get("additionalProperties")
    properties = schema.get("properties")

    if additional is True or isinstance(additional, dict):
        # A free-form or typed-value mapping. Closing it would change the
        # contract from "any keys" to "no keys".
        raise SchemaUnsupportedError("OPEN_ADDITIONAL_PROPERTIES")

    if not isinstance(properties, dict):
        if additional is False:
            # Explicitly closed and empty: already strict, nothing to add.
            return schema
        raise SchemaUnsupportedError("UNCONSTRAINED_OBJECT")

    closed = dict(schema)
    closed["additionalProperties"] = False
    # Strict mode wants every declared property present. Declaration order is
    # kept so the emitted schema stays stable across runs.
    closed["required"] = list(properties.keys())
    return closed
