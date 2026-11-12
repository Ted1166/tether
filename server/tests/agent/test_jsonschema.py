from typing import Any

from app.agent.jsonschema import inline_refs
from app.agent.tools import CreateInvoiceInput


def has_key(node: Any, key: str) -> bool:
    if isinstance(node, dict):
        return key in node or any(has_key(value, key) for value in node.values())
    if isinstance(node, list):
        return any(has_key(item, key) for item in node)
    return False


def test_references_are_replaced_by_their_definitions() -> None:
    schema = {
        "$defs": {"Item": {"type": "object", "properties": {"name": {"type": "string"}}}},
        "type": "object",
        "properties": {"items": {"type": "array", "items": {"$ref": "#/$defs/Item"}}},
    }
    resolved = inline_refs(schema)
    assert resolved["properties"]["items"]["items"] == {
        "type": "object",
        "properties": {"name": {"type": "string"}},
    }
    assert "$defs" not in resolved


def test_sibling_keys_next_to_a_reference_are_kept() -> None:
    schema = {
        "$defs": {"Item": {"type": "string"}},
        "properties": {"field": {"$ref": "#/$defs/Item", "description": "A field"}},
    }
    assert inline_refs(schema)["properties"]["field"] == {
        "type": "string",
        "description": "A field",
    }


def test_references_inside_lists_are_resolved() -> None:
    schema = {
        "$defs": {"Text": {"type": "string"}},
        "properties": {"value": {"anyOf": [{"$ref": "#/$defs/Text"}, {"type": "null"}]}},
    }
    assert inline_refs(schema)["properties"]["value"]["anyOf"] == [
        {"type": "string"},
        {"type": "null"},
    ]


def test_schemas_without_references_are_unchanged() -> None:
    schema = {"type": "object", "properties": {"a": {"type": "integer"}}}
    assert inline_refs(schema) == schema


def test_the_real_tool_schema_has_no_references_left() -> None:
    resolved = inline_refs(CreateInvoiceInput.model_json_schema())
    assert not has_key(resolved, "$ref")
    assert not has_key(resolved, "$defs")
    item = resolved["properties"]["items"]["items"]
    assert set(item["properties"]) == {"name", "quantity", "unit_amount"}
