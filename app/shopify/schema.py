from __future__ import annotations

import json
from typing import Any

from app.source import CellState, ParsedCell
from app.shopify.client import ShopifyClient

INTROSPECT_INPUT = r"""
query IntrospectInput($name: String!) {
  __type(name: $name) {
    inputFields {
      name
      type {
        kind name
        ofType { kind name ofType { kind name ofType { kind name ofType { kind name } } } }
      }
    }
  }
}
"""

METAFIELD_DEFINITION = r"""
query MetafieldDefinition($ownerType: MetafieldOwnerType!, $namespace: String!, $key: String!) {
  metafieldDefinition(identifier: {ownerType: $ownerType, namespace: $namespace, key: $key}) {
    id namespace key ownerType type { name }
  }
}
"""


class SchemaInspector:
    def __init__(self, client: ShopifyClient):
        self.client = client
        self._inputs: dict[str, dict[str, dict[str, Any]]] = {}
        self._metafields: dict[tuple[str, str, str], dict[str, Any] | None] = {}

    def input_fields(self, input_name: str) -> dict[str, dict[str, Any]]:
        if input_name not in self._inputs:
            data = self.client.execute(INTROSPECT_INPUT, {"name": input_name})
            gql_type = data.get("__type")
            if not gql_type:
                raise RuntimeError(f"Shopify input type '{input_name}' was not found.")
            self._inputs[input_name] = {
                field["name"]: field["type"] for field in gql_type.get("inputFields") or []
            }
        return self._inputs[input_name]

    def metafield_definition(self, owner_type: str, namespace: str, key: str) -> dict[str, Any] | None:
        cache_key = (owner_type, namespace, key)
        if cache_key not in self._metafields:
            data = self.client.execute(
                METAFIELD_DEFINITION,
                {"ownerType": owner_type, "namespace": namespace, "key": key},
            )
            self._metafields[cache_key] = data.get("metafieldDefinition")
        return self._metafields[cache_key]


def unwrap_non_null(type_ref: dict[str, Any]) -> dict[str, Any]:
    return type_ref.get("ofType") or {} if type_ref.get("kind") == "NON_NULL" else type_ref


def base_type(type_ref: dict[str, Any]) -> tuple[str | None, str | None]:
    current = type_ref
    while current and current.get("kind") in {"NON_NULL", "LIST"}:
        current = current.get("ofType") or {}
    return current.get("kind"), current.get("name")


def is_list_type(type_ref: dict[str, Any]) -> bool:
    return unwrap_non_null(type_ref).get("kind") == "LIST"


def clear_value(type_ref: dict[str, Any], field: str) -> Any:
    if is_list_type(type_ref):
        return []
    if type_ref.get("kind") != "NON_NULL":
        return None
    raise ValueError(
        f"Field '{field}' is non-null in Shopify's input schema and cannot be cleared generically."
    )


def coerce_cell(cell: ParsedCell, type_ref: dict[str, Any], field: str) -> Any:
    if cell.state == CellState.CLEAR:
        return clear_value(type_ref, field)
    if cell.state != CellState.VALUE:
        raise ValueError("coerce_cell should only receive VALUE or CLEAR cells.")

    value = cell.value
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    kind, name = base_type(type_ref)

    if is_list_type(type_ref):
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Field '{field}' expects a JSON array: {stripped}") from exc
        if not isinstance(parsed, list):
            raise ValueError(f"Field '{field}' expects a JSON array: {stripped}")
        return parsed

    if kind == "INPUT_OBJECT":
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Field '{field}' expects a JSON object: {stripped}") from exc
        if not isinstance(parsed, dict):
            raise ValueError(f"Field '{field}' expects a JSON object: {stripped}")
        return parsed

    if kind == "SCALAR":
        if name == "Int":
            return int(stripped)
        if name in {"Float", "Decimal"}:
            return float(stripped)
        if name == "Boolean":
            lowered = stripped.casefold()
            if lowered == "true":
                return True
            if lowered == "false":
                return False
            raise ValueError(f"Field '{field}' expects TRUE or FALSE, got '{stripped}'.")
        if name == "JSON":
            try:
                return json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Field '{field}' expects valid JSON: {stripped}") from exc
    return stripped
