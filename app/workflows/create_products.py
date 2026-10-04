from __future__ import annotations

import json
from typing import Any

from app.source import CellState, SourceData, SourceRow, parse_cell
from app.shopify.schema import SchemaInspector, coerce_cell
from app.workflows.helpers import (
    MutationPlan,
    is_metafield_column,
    merge_product_value,
    metafield_input,
    read_existing_inventory_item,
    resolve_creation_metafield_owners,
    source_ref,
)


class CreateProductsPlanner:
    def __init__(self, schema: SchemaInspector):
        self.schema = schema

    def plan(self, source: SourceData) -> MutationPlan:
        plan = MutationPlan("create-products", source)
        product_fields = self.schema.input_fields("ProductSetInput")
        variant_fields = self.schema.input_fields("ProductVariantSetInput")
        metafield_owners = resolve_creation_metafield_owners(self.schema, source, plan)

        groups: dict[str, list[SourceRow]] = {}
        for row in source.rows:
            title_cell = parse_cell(row.values.get("title"))
            if title_cell.state != CellState.VALUE:
                plan.errors.append(f"Source row {row.row_number}: 'title' is required.")
                continue
            title = str(title_cell.value).strip()
            groups.setdefault(title, []).append(row)

        output_rows: list[dict[str, Any]] = []
        provenance: list[dict[str, Any]] = []
        for title, rows in groups.items():
            product_input: dict[str, Any] = {}
            product_values: dict[str, tuple[Any, int]] = {}
            product_metafields: dict[str, tuple[str, int]] = {}
            variants: list[dict[str, Any]] = []
            files: list[Any] = []
            seen_files: set[str] = set()
            option_map: dict[str, list[str]] = {}

            for row in rows:
                variant: dict[str, Any] = {}
                if "sku" in variant_fields:
                    variant["sku"] = row.key
                elif "inventoryItem" in variant_fields:
                    inventory_item = read_existing_inventory_item(row, variant_fields, plan)
                    inventory_item["sku"] = row.key
                    variant["inventoryItem"] = inventory_item
                else:
                    plan.errors.append(
                        f"Source row {row.row_number}: Shopify's ProductVariantSetInput exposes no supported SKU field."
                    )

                for column, raw in row.values.items():
                    cell = parse_cell(raw)
                    if cell.state == CellState.UNCHANGED:
                        continue

                    if column == "files":
                        if cell.state == CellState.VALUE:
                            try:
                                parsed = (
                                    json.loads(str(cell.value))
                                    if isinstance(cell.value, str)
                                    else cell.value
                                )
                                if not isinstance(parsed, list):
                                    raise ValueError("files must be a JSON array")
                                for item in parsed:
                                    signature = json.dumps(item, sort_keys=True, ensure_ascii=False)
                                    if signature not in seen_files:
                                        seen_files.add(signature)
                                        files.append(item)
                            except (json.JSONDecodeError, ValueError) as exc:
                                plan.errors.append(
                                    f"Source row {row.row_number}: invalid files value: {exc}"
                                )
                        continue

                    if column == "id":
                        plan.errors.append(
                            f"Source row {row.row_number}: Create products does not accept an existing Product ID."
                        )
                        continue

                    if is_metafield_column(column):
                        owner = metafield_owners.get(column)
                        if owner == "PRODUCT" and cell.state == CellState.VALUE:
                            merge_product_value(
                                title,
                                column,
                                str(cell.value).strip(),
                                row.row_number,
                                product_metafields,
                                plan,
                            )
                        elif owner == "PRODUCTVARIANT" and cell.state == CellState.VALUE:
                            mf = metafield_input(
                                self.schema, owner, row, column, cell, plan
                            )
                            if mf:
                                variant.setdefault("metafields", []).append(mf)
                        continue

                    is_product_field = column in product_fields and column not in {
                        "id", "variants", "productOptions", "files"
                    }
                    is_variant_field = column in variant_fields and column not in {
                        "id", "metafields", "sku"
                    }

                    if is_product_field and cell.state == CellState.VALUE:
                        try:
                            merge_product_value(
                                title,
                                column,
                                coerce_cell(cell, product_fields[column], column),
                                row.row_number,
                                product_values,
                                plan,
                            )
                        except ValueError as exc:
                            plan.errors.append(f"Source row {row.row_number}: {exc}")

                    if is_variant_field and cell.state == CellState.VALUE:
                        try:
                            value = coerce_cell(cell, variant_fields[column], column)
                            if column == "inventoryItem" and isinstance(value, dict) and "inventoryItem" in variant:
                                merged = dict(value)
                                merged.update(variant["inventoryItem"])
                                variant["inventoryItem"] = merged
                            else:
                                variant[column] = value
                        except ValueError as exc:
                            plan.errors.append(f"Source row {row.row_number}: {exc}")

                    if not is_product_field and not is_variant_field and column != "inventoryItem":
                        plan.errors.append(
                            f"Source row {row.row_number}: column '{column}' is not valid for ProductSetInput or ProductVariantSetInput."
                        )

                option_values = variant.get("optionValues")
                if isinstance(option_values, list):
                    for option in option_values:
                        if not isinstance(option, dict):
                            continue
                        option_name = option.get("optionName")
                        value_name = option.get("name")
                        if option_name and value_name:
                            option_map.setdefault(option_name, [])
                            if value_name not in option_map[option_name]:
                                option_map[option_name].append(value_name)

                variants.append(variant)

            for field_name, (value, _) in product_values.items():
                product_input[field_name] = value

            if product_metafields:
                product_input["metafields"] = []
                for column, (value, row_number) in product_metafields.items():
                    namespace, key = column.split(".", 1)
                    if not self.schema.metafield_definition("PRODUCT", namespace, key):
                        plan.errors.append(
                            f"Source row {row_number}: metafield '{column}' has no PRODUCT definition."
                        )
                        continue
                    product_input["metafields"].append(
                        {"namespace": namespace, "key": key, "value": value}
                    )

            if variants:
                product_input["variants"] = variants
            if files:
                product_input["files"] = files
            if option_map:
                product_input["productOptions"] = [
                    {"name": name, "values": [{"name": value} for value in values]}
                    for name, values in option_map.items()
                ]
            if product_input:
                output_rows.append({"input": product_input})
                refs = [source_ref(row) for row in rows]
                provenance.append({"sourceRows": refs, "items": {"variants": refs}})

        plan.add("create_products", output_rows, provenance)
        if not plan.mutations and not plan.errors:
            plan.errors.append("Source contains no products to create.")
        return plan
