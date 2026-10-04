from __future__ import annotations

from typing import Any

from app.source import CellState, SourceData, SourceRow, parse_cell
from app.shopify.relationships import RelationshipResolver
from app.shopify.schema import SchemaInspector, coerce_cell
from app.workflows.helpers import (
    MutationPlan,
    is_metafield_column,
    resource_type,
    root_provenance,
    source_ref,
)


OWNER_TYPE_BY_RESOURCE = {
    "Product": "PRODUCT",
    "ProductVariant": "PRODUCTVARIANT",
    "Collection": "COLLECTION",
}


class UpdateResourcesPlanner:
    def __init__(self, schema: SchemaInspector, relationships: RelationshipResolver):
        self.schema = schema
        self.relationships = relationships

    def relationship_requirements(self, source: SourceData) -> set[str]:
        """Return MediaImage IDs that truly require a saved relationship query."""
        move_fields = self.schema.input_fields("MoveInput")
        media_ids: set[str] = set()
        for row in source.rows:
            kind = resource_type(row.key)
            if kind == "MediaImage":
                if any(
                    column in move_fields and parse_cell(raw).state == CellState.VALUE
                    for column, raw in row.values.items()
                ) and parse_cell(row.values.get("productId")).state == CellState.UNCHANGED:
                    media_ids.add(row.key)
        return media_ids

    def plan(self, source: SourceData, relationship_query=None) -> MutationPlan:
        plan = MutationPlan("update-resources", source, relationship_query=relationship_query)
        product_fields = self.schema.input_fields("ProductUpdateInput")
        variant_fields = self.schema.input_fields("ProductVariantsBulkInput")
        variant_position_fields = self.schema.input_fields("ProductVariantPositionInput")
        file_fields = self.schema.input_fields("FileUpdateInput")
        move_fields = self.schema.input_fields("MoveInput")
        collection_fields = self.schema.input_fields("CollectionInput")

        normal_fields_by_resource = {
            "Product": set(product_fields) - {"id", "metafields"},
            "ProductVariant": (set(variant_fields) | set(variant_position_fields)) - {"id", "metafields"},
            "MediaImage": (set(file_fields) | set(move_fields)) - {"id"},
            "Collection": set(collection_fields) - {"id", "metafields"},
        }
        if "sku" in variant_fields or "inventoryItem" in variant_fields:
            normal_fields_by_resource["ProductVariant"].add("sku")
        field_resources: dict[str, set[str]] = {}
        for resource, fields in normal_fields_by_resource.items():
            for field in fields:
                field_resources.setdefault(field, set()).add(resource)
        globally_valid_fields = set(field_resources)
        metafield_owners = self._resolve_metafield_owners(source, plan)
        supplied_product_ids = self._source_product_ids(source, plan)

        live_contexts: dict[str, dict[str, str | None]] = {}
        supported_rows = [
            row for row in source.rows
            if resource_type(row.key) in {"Product", "ProductVariant", "MediaImage", "Collection"}
        ]
        try:
            live_contexts = self.relationships.node_contexts(row.key for row in supported_rows)
        except Exception as exc:
            plan.errors.append(f"Unable to validate source Shopify resource IDs: {exc}")

        for row in supported_rows:
            expected_type = resource_type(row.key)
            context = live_contexts.get(row.key)
            if not context:
                plan.errors.append(
                    f"Source row {row.row_number}: Shopify resource '{row.key}' does not exist or is not accessible."
                )
            elif context.get("type") != expected_type:
                plan.errors.append(
                    f"Source row {row.row_number}: '{row.key}' resolved as {context.get('type')}, "
                    f"not {expected_type}."
                )

        product_rows: list[dict[str, Any]] = []
        product_prov: list[dict[str, Any]] = []
        collection_rows: list[dict[str, Any]] = []
        collection_prov: list[dict[str, Any]] = []
        file_rows: list[dict[str, Any]] = []
        file_prov: list[dict[str, Any]] = []
        variant_updates: dict[str, dict[str, Any]] = {}
        variant_positions: dict[str, dict[str, Any]] = {}
        media_moves: dict[str, dict[str, Any]] = {}
        metafield_deletes: list[dict[str, Any]] = []
        delete_prov: list[dict[str, Any]] = []
        variant_order: list[str] = []
        media_order: list[str] = []
        row_by_id = {row.key: row for row in source.rows}

        for row in source.rows:
            kind = resource_type(row.key)
            try:
                if kind == "Product":
                    update, deletes = self._build_owner_update(
                        row, product_fields, "Product", globally_valid_fields,
                        field_resources, metafield_owners, plan,
                    )
                    if update:
                        update["id"] = row.key
                        product_rows.append({"product": update})
                        product_prov.append(root_provenance(row))
                    if deletes:
                        metafield_deletes.append({"metafields": deletes})
                        delete_prov.append(root_provenance(row))
                elif kind == "Collection":
                    update, deletes = self._build_owner_update(
                        row, collection_fields, "Collection", globally_valid_fields,
                        field_resources, metafield_owners, plan,
                    )
                    if update:
                        update["id"] = row.key
                        collection_rows.append({"input": update})
                        collection_prov.append(root_provenance(row))
                    if deletes:
                        metafield_deletes.append({"metafields": deletes})
                        delete_prov.append(root_provenance(row))
                elif kind == "ProductVariant":
                    variant_order.append(row.key)
                    update, position, deletes = self._build_variant_update(
                        row, variant_fields, variant_position_fields, globally_valid_fields,
                        field_resources, metafield_owners, plan,
                    )
                    if update:
                        update["id"] = row.key
                        variant_updates[row.key] = update
                    if position:
                        position["id"] = row.key
                        variant_positions[row.key] = position
                    if deletes:
                        metafield_deletes.append({"metafields": deletes})
                        delete_prov.append(root_provenance(row))
                elif kind == "MediaImage":
                    media_order.append(row.key)
                    file_input, move_input = self._build_media_update(
                        row, file_fields, move_fields, globally_valid_fields,
                        field_resources, metafield_owners, plan,
                    )
                    if file_input:
                        file_input["id"] = row.key
                        file_rows.append({"files": [file_input]})
                        file_prov.append(root_provenance(row))
                    if move_input:
                        move_input["id"] = row.key
                        media_moves[row.key] = move_input
                else:
                    plan.errors.append(
                        f"Source row {row.row_number}: unsupported resource ID '{row.key}'. "
                        "Supported update resources are Product, ProductVariant, MediaImage, and Collection."
                    )
            except ValueError as exc:
                plan.errors.append(str(exc))

        variant_ids = [vid for vid in variant_order if vid in variant_updates or vid in variant_positions]
        media_ids = set(media_moves)
        query_candidates: dict[str, set[str]] = {}
        if relationship_query and (variant_order or media_order):
            try:
                query_candidates = self.relationships.product_candidates_from_query(
                    relationship_query, set(variant_order) | set(media_order)
                )
            except Exception as exc:
                plan.errors.append(f"Unable to read resource relationships from query result: {exc}")

        variant_parents = self._variant_product_contexts(
            variant_order,
            variant_ids,
            supplied_product_ids,
            live_contexts,
            query_candidates,
            row_by_id,
            plan,
        )
        media_parents = self._media_product_contexts(
            media_order,
            media_ids,
            supplied_product_ids,
            query_candidates,
            relationship_query,
            row_by_id,
            plan,
        )

        if media_parents:
            try:
                media_by_product = self.relationships.product_media_ids(set(media_parents.values()))
                for media_id, product_id in media_parents.items():
                    if product_id not in media_by_product:
                        row = row_by_id[media_id]
                        plan.errors.append(
                            f"Source row {row.row_number}: parent Product '{product_id}' does not exist or is not accessible."
                        )
                    elif media_id not in media_by_product[product_id]:
                        row = row_by_id[media_id]
                        plan.errors.append(
                            f"Source row {row.row_number}: Shopify does not currently show MediaImage "
                            f"'{media_id}' attached to Product '{product_id}'."
                        )
            except Exception as exc:
                plan.errors.append(f"Unable to validate MediaImage -> Product relationships: {exc}")

        # These limits are defensive only. The workflow groups by one Shopify owner, so
        # normal Froy products should remain far below Shopify's input-array cap.
        for delete_row in metafield_deletes:
            if len(delete_row.get("metafields") or []) > 250:
                plan.errors.append(
                    "A source resource row requests more than Shopify's 250 metafield deletions per call."
                )

        variant_update_rows, variant_update_prov = self._group_variants(
            variant_order, variant_updates, variant_parents, "variants", row_by_id
        )
        variant_reorder_rows, variant_reorder_prov = self._group_variants(
            variant_order, variant_positions, variant_parents, "positions", row_by_id
        )
        media_rows, media_prov = self._group_media(
            media_order, media_moves, media_parents, plan, row_by_id
        )

        plan.add("product_update", product_rows, product_prov)
        plan.add("variant_update", variant_update_rows, variant_update_prov)
        plan.add("variant_reorder", variant_reorder_rows, variant_reorder_prov)
        plan.add("file_update", file_rows, file_prov)
        plan.add("media_reorder", media_rows, media_prov)
        plan.add("collection_update", collection_rows, collection_prov)
        plan.add("metafields_delete", metafield_deletes, delete_prov)
        if not plan.mutations and not plan.errors:
            plan.errors.append("Source contains no recognized nonblank update values.")
        return plan

    def _source_product_ids(self, source: SourceData, plan: MutationPlan) -> dict[str, str]:
        result: dict[str, str] = {}
        for row in source.rows:
            cell = parse_cell(row.values.get("productId"))
            if cell.state == CellState.UNCHANGED:
                continue
            kind = resource_type(row.key)
            if kind not in {"ProductVariant", "MediaImage"}:
                plan.errors.append(
                    f"Source row {row.row_number}: 'productId' is a parent-context helper only for "
                    "ProductVariant and MediaImage rows."
                )
                continue
            if cell.state == CellState.CLEAR:
                plan.errors.append(
                    f"Source row {row.row_number}: 'productId' cannot be explicitly cleared; "
                    "leave it blank to use relationship resolution."
                )
                continue
            product_id = str(cell.value).strip()
            if resource_type(product_id) != "Product":
                plan.errors.append(
                    f"Source row {row.row_number}: invalid productId '{product_id}'."
                )
                continue
            result[row.key] = product_id
        return result

    @staticmethod
    def _variant_product_contexts(
        variant_order: list[str],
        required_variant_ids: list[str],
        supplied_product_ids: dict[str, str],
        live_contexts: dict[str, dict[str, str | None]],
        query_candidates: dict[str, set[str]],
        row_by_id: dict[str, SourceRow],
        plan: MutationPlan,
    ) -> dict[str, str]:
        resolved: dict[str, str] = {}
        required = set(required_variant_ids)
        for variant_id in variant_order:
            row = row_by_id[variant_id]
            supplied = supplied_product_ids.get(variant_id)
            live = (live_contexts.get(variant_id) or {}).get("productId")
            query = query_candidates.get(variant_id, set())

            if supplied and live and supplied != live:
                plan.errors.append(
                    f"Source row {row.row_number}: productId '{supplied}' conflicts with Shopify's current "
                    f"ProductVariant parent '{live}' for '{variant_id}'."
                )
            if len(query) > 1:
                plan.errors.append(
                    f"Source row {row.row_number}: relationship query maps ProductVariant '{variant_id}' "
                    f"to multiple Products: {', '.join(sorted(query))}."
                )
            elif query:
                query_product = next(iter(query))
                if supplied and supplied != query_product:
                    plan.errors.append(
                        f"Source row {row.row_number}: productId '{supplied}' conflicts with relationship query "
                        f"Product '{query_product}' for '{variant_id}'."
                    )
                if live and live != query_product:
                    plan.errors.append(
                        f"Source row {row.row_number}: relationship query Product '{query_product}' conflicts "
                        f"with Shopify's current parent '{live}' for '{variant_id}'."
                    )

            parent = str(live or supplied or (next(iter(query)) if len(query) == 1 else ""))
            if parent:
                resolved[variant_id] = parent
            elif variant_id in required:
                plan.errors.append(
                    f"Source row {row.row_number}: could not resolve parent Product for ProductVariant '{variant_id}'."
                )
        return resolved

    @staticmethod
    def _media_product_contexts(
        media_order: list[str],
        required_media_ids: set[str],
        supplied_product_ids: dict[str, str],
        query_candidates: dict[str, set[str]],
        relationship_query,
        row_by_id: dict[str, SourceRow],
        plan: MutationPlan,
    ) -> dict[str, str]:
        resolved: dict[str, str] = {}
        for media_id in media_order:
            row = row_by_id[media_id]
            supplied = supplied_product_ids.get(media_id)
            query = query_candidates.get(media_id, set())

            if supplied:
                if query and supplied not in query:
                    plan.errors.append(
                        f"Source row {row.row_number}: productId '{supplied}' conflicts with relationship query "
                        f"Product relationship(s) for '{media_id}': {', '.join(sorted(query))}."
                    )
                resolved[media_id] = supplied
                continue

            if len(query) > 1:
                plan.errors.append(
                    f"Source row {row.row_number}: relationship query maps MediaImage '{media_id}' to multiple "
                    f"Products: {', '.join(sorted(query))}. Supply productId to disambiguate."
                )
                continue
            if len(query) == 1:
                resolved[media_id] = next(iter(query))
                continue
            if media_id in required_media_ids:
                if relationship_query:
                    plan.errors.append(
                        f"Source row {row.row_number}: selected relationship query does not resolve MediaImage "
                        f"'{media_id}' to a Product."
                    )
                else:
                    plan.errors.append(
                        f"Source row {row.row_number}: MediaImage '{media_id}' needs productId or a saved "
                        "relationship query for media reordering."
                    )
        return resolved

    def _resolve_metafield_owners(self, source: SourceData, plan: MutationPlan) -> dict[str, set[str]]:
        result: dict[str, set[str]] = {}
        for column in [c for c in source.columns if is_metafield_column(c)]:
            if not any(parse_cell(row.values.get(column)).state != CellState.UNCHANGED for row in source.rows):
                continue
            namespace, key = column.split(".", 1)
            owners: set[str] = set()
            for owner_type in ("PRODUCT", "PRODUCTVARIANT", "COLLECTION"):
                if self.schema.metafield_definition(owner_type, namespace, key):
                    owners.add(owner_type)
            if not owners:
                plan.errors.append(
                    f"Metafield '{column}' has no PRODUCT, PRODUCTVARIANT, or COLLECTION definition."
                )
            result[column] = owners
        return result

    def _build_owner_update(
        self,
        row: SourceRow,
        fields: dict[str, dict[str, Any]],
        resource: str,
        globally_valid_fields: set[str],
        field_resources: dict[str, set[str]],
        metafield_owners: dict[str, set[str]],
        plan: MutationPlan,
    ) -> tuple[dict[str, Any], list[dict[str, str]]]:
        result: dict[str, Any] = {}
        deletes: list[dict[str, str]] = []
        owner_type = OWNER_TYPE_BY_RESOURCE[resource]
        for column, raw in row.values.items():
            cell = parse_cell(raw)
            if cell.state == CellState.UNCHANGED:
                continue
            if column == "productId":
                continue
            if is_metafield_column(column):
                owners = metafield_owners.get(column, set())
                if owner_type not in owners:
                    if owners:
                        self._warn_metafield_mismatch(row, resource, column, owners, plan)
                    continue
                if "metafields" not in fields:
                    raise ValueError(
                        f"Source row {row.row_number}: this resource cannot update metafields through its owner input."
                    )
                namespace, key = column.split(".", 1)
                if cell.state == CellState.CLEAR:
                    deletes.append({"ownerId": row.key, "namespace": namespace, "key": key})
                else:
                    result.setdefault("metafields", []).append(
                        {"namespace": namespace, "key": key, "value": str(cell.value).strip()}
                    )
                continue
            if column not in fields or column in {"id", "metafields"}:
                if column in globally_valid_fields:
                    self._warn_field_mismatch(row, resource, column, field_resources, plan)
                    continue
                raise ValueError(
                    f"Source row {row.row_number}: column '{column}' is not a valid update field for any supported resource."
                )
            try:
                result[column] = coerce_cell(cell, fields[column], column)
            except ValueError as exc:
                raise ValueError(f"Source row {row.row_number}: {exc}") from exc
        return result, deletes

    def _build_variant_update(
        self,
        row: SourceRow,
        update_fields: dict[str, dict[str, Any]],
        reorder_fields: dict[str, dict[str, Any]],
        globally_valid_fields: set[str],
        field_resources: dict[str, set[str]],
        metafield_owners: dict[str, set[str]],
        plan: MutationPlan,
    ) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, str]]]:
        update: dict[str, Any] = {}
        reorder: dict[str, Any] = {}
        deletes: list[dict[str, str]] = []
        for column, raw in row.values.items():
            cell = parse_cell(raw)
            if cell.state == CellState.UNCHANGED:
                continue
            if column == "productId":
                continue
            if is_metafield_column(column):
                owners = metafield_owners.get(column, set())
                if "PRODUCTVARIANT" not in owners:
                    if owners:
                        self._warn_metafield_mismatch(row, "ProductVariant", column, owners, plan)
                    continue
                namespace, key = column.split(".", 1)
                if cell.state == CellState.CLEAR:
                    deletes.append({"ownerId": row.key, "namespace": namespace, "key": key})
                else:
                    update.setdefault("metafields", []).append(
                        {"namespace": namespace, "key": key, "value": str(cell.value).strip()}
                    )
                continue
            if column == "sku":
                if "sku" in update_fields:
                    update["sku"] = coerce_cell(cell, update_fields["sku"], "sku")
                elif "inventoryItem" in update_fields:
                    update.setdefault("inventoryItem", {})["sku"] = (
                        None if cell.state == CellState.CLEAR else str(cell.value).strip()
                    )
                else:
                    raise ValueError(
                        f"Source row {row.row_number}: Shopify exposes no supported SKU field."
                    )
                continue
            if column in reorder_fields and column != "id":
                try:
                    reorder[column] = coerce_cell(cell, reorder_fields[column], column)
                except ValueError as exc:
                    raise ValueError(f"Source row {row.row_number}: {exc}") from exc
                continue
            if column in update_fields and column not in {"id", "metafields"}:
                try:
                    value = coerce_cell(cell, update_fields[column], column)
                except ValueError as exc:
                    raise ValueError(f"Source row {row.row_number}: {exc}") from exc
                if column == "inventoryItem" and isinstance(value, dict) and "inventoryItem" in update:
                    merged = dict(value)
                    merged.update(update["inventoryItem"])
                    update["inventoryItem"] = merged
                else:
                    update[column] = value
                continue
            if column in globally_valid_fields:
                self._warn_field_mismatch(row, "ProductVariant", column, field_resources, plan)
                continue
            raise ValueError(
                f"Source row {row.row_number}: column '{column}' is not a valid update field for any supported resource."
            )
        return update, reorder, deletes

    def _build_media_update(
        self,
        row: SourceRow,
        file_fields: dict[str, dict[str, Any]],
        move_fields: dict[str, dict[str, Any]],
        globally_valid_fields: set[str],
        field_resources: dict[str, set[str]],
        metafield_owners: dict[str, set[str]],
        plan: MutationPlan,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        file_input: dict[str, Any] = {}
        move_input: dict[str, Any] = {}
        for column, raw in row.values.items():
            cell = parse_cell(raw)
            if cell.state == CellState.UNCHANGED:
                continue
            if column == "productId":
                continue
            if is_metafield_column(column):
                owners = metafield_owners.get(column, set())
                if owners:
                    self._warn_metafield_mismatch(row, "MediaImage", column, owners, plan)
                continue
            if column in move_fields and column != "id":
                try:
                    move_input[column] = coerce_cell(cell, move_fields[column], column)
                except ValueError as exc:
                    raise ValueError(f"Source row {row.row_number}: {exc}") from exc
            elif column in file_fields and column != "id":
                try:
                    file_input[column] = coerce_cell(cell, file_fields[column], column)
                except ValueError as exc:
                    raise ValueError(f"Source row {row.row_number}: {exc}") from exc
            elif column in globally_valid_fields:
                self._warn_field_mismatch(row, "MediaImage", column, field_resources, plan)
            else:
                raise ValueError(
                    f"Source row {row.row_number}: column '{column}' is not a valid update field for any supported resource."
                )
        return file_input, move_input

    @staticmethod
    def _warn_field_mismatch(
        row: SourceRow,
        resource: str,
        column: str,
        field_resources: dict[str, set[str]],
        plan: MutationPlan,
    ) -> None:
        owners = ", ".join(sorted(field_resources.get(column, set()))) or "another resource"
        plan.warnings.append(
            f"Source row {row.row_number} is {resource}, but nonblank column '{column}' applies to {owners}; value ignored."
        )

    @staticmethod
    def _warn_metafield_mismatch(
        row: SourceRow,
        resource: str,
        column: str,
        owners: set[str],
        plan: MutationPlan,
    ) -> None:
        owner_text = ", ".join(sorted(owners))
        plan.warnings.append(
            f"Source row {row.row_number} is {resource}, but nonblank metafield '{column}' is defined for {owner_text}; value ignored."
        )

    @staticmethod
    def _group_variants(order, values, parents, field_name, row_by_id):
        grouped: dict[str, list[tuple[dict[str, Any], SourceRow]]] = {}
        for variant_id in order:
            if variant_id in values and variant_id in parents:
                grouped.setdefault(parents[variant_id], []).append((values[variant_id], row_by_id[variant_id]))
        payloads: list[dict[str, Any]] = []
        provenance: list[dict[str, Any]] = []
        for product_id, entries in grouped.items():
            items = [item for item, _ in entries]
            refs = [source_ref(row) for _, row in entries]
            payloads.append({"productId": product_id, field_name: items})
            provenance.append({"sourceRows": refs, "items": {field_name: refs}})
        return payloads, provenance

    @staticmethod
    def _group_media(order, values, parents, plan, row_by_id):
        grouped: dict[str, list[tuple[dict[str, Any], SourceRow]]] = {}
        for media_id in order:
            if media_id in values and media_id in parents:
                grouped.setdefault(parents[media_id], []).append((values[media_id], row_by_id[media_id]))
        payloads: list[dict[str, Any]] = []
        provenance: list[dict[str, Any]] = []
        for product_id, entries in grouped.items():
            if len(entries) > 250:
                plan.errors.append(
                    f"Product '{product_id}' has {len(entries)} media moves; Shopify supports at most 250 per call."
                )
            try:
                entries.sort(key=lambda pair: int(pair[0]["newPosition"]))
            except (KeyError, TypeError, ValueError):
                pass
            moves = [item for item, _ in entries]
            refs = [source_ref(row) for _, row in entries]
            payloads.append({"id": product_id, "moves": moves})
            provenance.append({"sourceRows": refs, "items": {"moves": refs}})
        return payloads, provenance
