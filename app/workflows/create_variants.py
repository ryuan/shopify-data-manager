from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

from app.source import CellState, SourceData, parse_cell
from app.shopify.relationships import RelationshipResolver
from app.shopify.schema import SchemaInspector, coerce_cell
from app.workflows.helpers import (
    MutationPlan, is_metafield_column, metafield_input, read_existing_inventory_item, source_ref
)


class CreateVariantsPlanner:
    def __init__(self, schema: SchemaInspector, relationships: RelationshipResolver):
        self.schema = schema
        self.relationships = relationships

    def plan(self, source: SourceData) -> MutationPlan:
        plan = MutationPlan("create-variants", source)
        fields = self.schema.input_fields("ProductVariantsBulkInput")

        groups: dict[str, list[dict[str, Any]]] = {}
        group_rows: dict[str, list[Any]] = defaultdict(list)
        media_by_product: dict[str, list[Any]] = defaultdict(list)
        seen_media: dict[str, set[str]] = defaultdict(set)

        for row in source.rows:
            product_cell = parse_cell(row.values.get("productId"))
            if product_cell.state != CellState.VALUE:
                plan.errors.append(
                    f"Source row {row.row_number}: 'productId' is required when creating variants."
                )
                continue
            product_id = str(product_cell.value).strip()
            if not product_id.startswith("gid://shopify/Product/"):
                plan.errors.append(
                    f"Source row {row.row_number}: invalid productId '{product_id}'."
                )
                continue

            variant: dict[str, Any] = {}
            if "sku" in fields:
                variant["sku"] = row.key
            elif "inventoryItem" in fields:
                inventory_item = read_existing_inventory_item(row, fields, plan)
                inventory_item["sku"] = row.key
                variant["inventoryItem"] = inventory_item
            else:
                plan.errors.append(
                    f"Source row {row.row_number}: Shopify's ProductVariantsBulkInput exposes no supported SKU field."
                )

            for column, raw in row.values.items():
                if column in {"productId", "media", "inventoryItem"}:
                    continue
                cell = parse_cell(raw)
                if cell.state == CellState.UNCHANGED:
                    continue
                if is_metafield_column(column):
                    if cell.state == CellState.CLEAR:
                        continue
                    mf = metafield_input(
                        self.schema, "PRODUCTVARIANT", row, column, cell, plan
                    )
                    if mf:
                        variant.setdefault("metafields", []).append(mf)
                elif column in fields and column not in {"id", "metafields"}:
                    if cell.state == CellState.CLEAR:
                        continue
                    try:
                        value = coerce_cell(cell, fields[column], column)
                        if column == "inventoryItem" and isinstance(value, dict) and "inventoryItem" in variant:
                            merged = dict(value)
                            merged.update(variant["inventoryItem"])
                            variant["inventoryItem"] = merged
                        else:
                            variant[column] = value
                    except ValueError as exc:
                        plan.errors.append(f"Source row {row.row_number}: {exc}")
                else:
                    plan.errors.append(
                        f"Source row {row.row_number}: column '{column}' is not valid for ProductVariantsBulkInput."
                    )

            media_cell = parse_cell(row.values.get("media"))
            if media_cell.state == CellState.VALUE:
                try:
                    media_items = (
                        json.loads(str(media_cell.value))
                        if isinstance(media_cell.value, str)
                        else media_cell.value
                    )
                    if not isinstance(media_items, list):
                        raise ValueError("media must be a JSON array")
                    for item in media_items:
                        signature = json.dumps(item, sort_keys=True, ensure_ascii=False)
                        if signature not in seen_media[product_id]:
                            seen_media[product_id].add(signature)
                            media_by_product[product_id].append(item)
                except (json.JSONDecodeError, ValueError) as exc:
                    plan.errors.append(f"Source row {row.row_number}: invalid media value: {exc}")

            groups.setdefault(product_id, []).append(variant)
            group_rows[product_id].append(row)

        if groups:
            try:
                contexts = self.relationships.node_contexts(groups.keys())
                for product_id, rows_for_product in group_rows.items():
                    context = contexts.get(product_id)
                    if not context:
                        first_row = rows_for_product[0]
                        plan.errors.append(
                            f"Source row {first_row.row_number}: Product '{product_id}' does not exist or is not accessible."
                        )
                    elif context.get("type") != "Product":
                        first_row = rows_for_product[0]
                        plan.errors.append(
                            f"Source row {first_row.row_number}: productId '{product_id}' resolved as "
                            f"{context.get('type')}, not Product."
                        )
            except Exception as exc:
                plan.errors.append(f"Unable to validate destination Product IDs: {exc}")

        rows: list[dict[str, Any]] = []
        provenance: list[dict[str, Any]] = []
        for product_id, variants in groups.items():
            payload: dict[str, Any] = {
                "productId": product_id,
                "variants": variants,
                "strategy": "PRESERVE_STANDALONE_VARIANT",
            }
            if media_by_product.get(product_id):
                payload["media"] = media_by_product[product_id]
            rows.append(payload)
            refs = [source_ref(row) for row in group_rows[product_id]]
            provenance.append({"sourceRows": refs, "items": {"variants": refs}})

        plan.add("create_variants", rows, provenance)
        if not plan.mutations and not plan.errors:
            plan.errors.append("Source contains no variants to create.")
        return plan
