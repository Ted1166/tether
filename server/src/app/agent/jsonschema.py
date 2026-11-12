from typing import Any


def inline_refs(schema: dict[str, Any]) -> dict[str, Any]:
    definitions: dict[str, Any] = schema.get("$defs", {})

    def resolve(node: Any) -> Any:
        if isinstance(node, list):
            return [resolve(item) for item in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            name = str(node["$ref"]).rsplit("/", 1)[-1]
            target = resolve(definitions[name])
            extras = {key: resolve(value) for key, value in node.items() if key != "$ref"}
            return {**target, **extras}
        return {key: resolve(value) for key, value in node.items() if key != "$defs"}

    resolved: dict[str, Any] = resolve(schema)
    return resolved
