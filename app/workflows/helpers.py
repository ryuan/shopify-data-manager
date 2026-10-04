from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.source import CellState, ParsedCell, SourceData, SourceRow, parse_cell
from app.shopify.schema import SchemaInspector, coerce_cell


@dataclass
class MutationPlan:
    action: str
    source: SourceData
    mutations: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    provenance: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    relationship_query: Path | None = None

    def add(
        self,
        name: str,
        rows: list[dict[str, Any]],
        provenance: list[dict[str, Any]] | None = None,
    ) -> None:
        if not rows:
            return
        self.mutations[name] = rows
        if provenance is not None:
            if len(provenance) != len(rows):
                raise ValueError(f"Provenance length mismatch for '{name}'.")
            self.provenance[name] = provenance


def source_ref(row: SourceRow) -> dict[str, Any]:
    return {"row": row.row_number, "id": row.key}


def root_provenance(row: SourceRow) -> dict[str, Any]:
    return {"sourceRows": [source_ref(row)]}


def grouped_provenance(rows: list[SourceRow], item_name: str) -> dict[str, Any]:
    refs = [source_ref(row) for row in rows]
    return {"sourceRows": refs, "items": {item_name: refs}}


def resource_type(gid: str) -> str | None:
    if not gid.startswith("gid://shopify/"):
        return None
    rest = gid[len("gid://shopify/"):]
    return rest.split("/", 1)[0] if "/" in rest else None


def is_metafield_column(column: str) -> bool:
    return column.count(".") == 1 and all(column.split(".", 1))


def canonical(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, str):
        return value.strip()
    return repr(value)


def metafield_input(
    schema: SchemaInspector,
    owner_type: str,
    row: SourceRow,
    column: str,
    cell: ParsedCell,
    plan: MutationPlan,
) -> dict[str, str] | None:
    namespace, key = column.split(".", 1)
    if not schema.metafield_definition(owner_type, namespace, key):
        plan.errors.append(
            f"Source row {row.row_number}: metafield '{column}' has no {owner_type} definition."
        )
        return None
    return {"namespace": namespace, "key": key, "value": str(cell.value).strip()}


def read_existing_inventory_item(
    row: SourceRow,
    fields: dict[str, dict[str, Any]],
    plan: MutationPlan,
) -> dict[str, Any]:
    cell = parse_cell(row.values.get("inventoryItem"))
    if cell.state == CellState.UNCHANGED:
        return {}
    if cell.state == CellState.CLEAR:
        plan.errors.append(
            f"Source row {row.row_number}: inventoryItem cannot be explicitly cleared while creating a variant."
        )
        return {}
    if "inventoryItem" not in fields:
        plan.errors.append(
            f"Source row {row.row_number}: Shopify input does not expose inventoryItem."
        )
        return {}
    try:
        value = coerce_cell(cell, fields["inventoryItem"], "inventoryItem")
    except ValueError as exc:
        plan.errors.append(f"Source row {row.row_number}: {exc}")
        return {}
    if not isinstance(value, dict):
        plan.errors.append(
            f"Source row {row.row_number}: inventoryItem must be a JSON object."
        )
        return {}
    return dict(value)


def merge_product_value(
    title: str,
    field_name: str,
    value: Any,
    row_number: int,
    store: dict[str, tuple[Any, int]],
    plan: MutationPlan,
) -> None:
    if field_name not in store:
        store[field_name] = (value, row_number)
        return
    existing, existing_row = store[field_name]
    if canonical(existing) != canonical(value):
        plan.warnings.append(
            f"Product '{title}' has conflicting nonblank values for '{field_name}' "
            f"on rows {existing_row} and {row_number}. Using row {existing_row}."
        )


def resolve_creation_metafield_owners(
    schema: SchemaInspector,
    source: SourceData,
    plan: MutationPlan,
) -> dict[str, str]:
    result: dict[str, str] = {}
    for column in [c for c in source.columns if is_metafield_column(c)]:
        if not any(parse_cell(row.values.get(column)).state == CellState.VALUE for row in source.rows):
            continue
        namespace, key = column.split(".", 1)
        product = schema.metafield_definition("PRODUCT", namespace, key)
        variant = schema.metafield_definition("PRODUCTVARIANT", namespace, key)
        if product and variant:
            plan.errors.append(
                f"Metafield '{column}' exists for both PRODUCT and PRODUCTVARIANT. "
                "The create-products source column is ambiguous."
            )
        elif product:
            result[column] = "PRODUCT"
        elif variant:
            result[column] = "PRODUCTVARIANT"
        else:
            plan.errors.append(
                f"Metafield '{column}' has neither a PRODUCT nor PRODUCTVARIANT definition."
            )
    return result
