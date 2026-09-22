"""The strict-schema transform changes exactly what strict mode documents."""

import pytest
from pydantic import BaseModel, ConfigDict, Field

from src.agents.orbit.models import OrbitAssessment
from src.evaluation.codex.models import SchemaUnsupportedError
from src.evaluation.codex.schema import strict_schema


class Inner(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str
    optional_count: int = 3


class Outer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    inner: Inner
    tags: list[str] = Field(default_factory=list)


class FreeForm(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bag: dict[str, object]


def test_every_property_becomes_required() -> None:
    schema = strict_schema(OrbitAssessment)
    assert set(schema["required"]) == set(schema["properties"])
    assert "schema_version" in schema["required"]
    assert "data_gaps" in schema["required"]
    assert schema["additionalProperties"] is False


def test_existing_constraints_survive() -> None:
    schema = strict_schema(OrbitAssessment)
    assert schema["properties"]["pair_id"]["pattern"] == r"^\S(?:.*\S)?$"
    assert schema["properties"]["summary"]["maxLength"] == 400
    assert schema["properties"]["reason_codes"]["minItems"] == 1
    assert schema["properties"]["cited_observation_ids"]["items"]["format"] == "uuid"


def test_defaults_are_required_not_nullable() -> None:
    schema = strict_schema(Outer)
    counted = schema["$defs"]["Inner"]["properties"]["optional_count"]
    assert "optional_count" in schema["$defs"]["Inner"]["required"]
    # A default is a reason to demand the key, never a reason to allow null.
    assert counted["type"] == "integer"
    assert counted["default"] == 3
    gaps = strict_schema(OrbitAssessment)["properties"]["data_gaps"]
    assert gaps["type"] == "array"
    assert "anyOf" not in gaps


def test_nested_definitions_are_closed_recursively() -> None:
    schema = strict_schema(Outer)
    inner = schema["$defs"]["Inner"]
    assert inner["additionalProperties"] is False
    assert set(inner["required"]) == {"label", "optional_count"}


def test_original_schema_is_never_mutated() -> None:
    before = OrbitAssessment.model_json_schema()
    snapshot = dict(before)
    strict_schema(OrbitAssessment)
    after = OrbitAssessment.model_json_schema()
    assert after["required"] == snapshot["required"]
    assert "additionalProperties" not in after or after["additionalProperties"] is False


def test_free_form_mapping_is_refused_instead_of_closed() -> None:
    with pytest.raises(SchemaUnsupportedError) as caught:
        strict_schema(FreeForm)
    assert caught.value.reason_code == "OPEN_ADDITIONAL_PROPERTIES"
