from __future__ import annotations

from typing import Any

from app.source import CellState, SourceData, SourceRow, parse_cell
from app.shopify.relationships import RelationshipResolver
from app.shopify.schema import SchemaInspector
from app.workflows.helpers import (
    MutationPlan,
    resource_type,
    root_provenance,
    source_ref,
)


REDIRECT_QUERY = r"""
query UrlRedirect($id: ID!) {
  urlRedirect(id: $id) {
    id
    path
    target
  }
}
"""


class DeleteResourcesPlanner:
    def __init__(self, schema: SchemaInspector, relationships: RelationshipResolver):
        self.schema = schema
        self.relationships = relationships

    def plan(self, source: SourceData) -> MutationPlan:
        plan = MutationPlan("delete-resources", source)
        supported_types = {"Product", "ProductVariant", "MediaImage", "Collection", "UrlRedirect"}
        supported_rows = [row for row in source.rows if resource_type(row.key) in supported_types]

        live_contexts: dict[str, dict[str, str | None]] = {}
        try:
            live_contexts = self.relationships.node_contexts(
                row.key for row in supported_rows if resource_type(row.key) != "UrlRedirect"
            )
        except Exception as exc:
            plan.errors.append(f"Unable to validate source Shopify resource IDs: {exc}")

        for row in supported_rows:
            kind = resource_type(row.key)
            if kind == "UrlRedirect":
                try:
                    data = self.relationships.client.execute(REDIRECT_QUERY, {"id": row.key})
                    if (data.get("urlRedirect") or {}).get("id") != row.key:
                        plan.errors.append(
                            f"Source row {row.row_number}: Shopify resource '{row.key}' does not exist or is not accessible."
                        )
                except Exception as exc:
                    plan.errors.append(f"Unable to validate Shopify UrlRedirect '{row.key}': {exc}")
                continue
            context = live_contexts.get(row.key)
            if not context:
                plan.errors.append(
                    f"Source row {row.row_number}: Shopify resource '{row.key}' does not exist or is not accessible."
                )
            elif context.get("type") != kind:
                plan.errors.append(
                    f"Source row {row.row_number}: '{row.key}' resolved as {context.get('type')}, "
                    f"not {kind}."
                )

        product_rows: list[dict[str, Any]] = []
        product_prov: list[dict[str, Any]] = []
        collection_rows: list[dict[str, Any]] = []
        collection_prov: list[dict[str, Any]] = []
        redirect_rows: list[dict[str, Any]] = []
        redirect_prov: list[dict[str, Any]] = []
        variant_order: list[str] = []
        media_order: list[str] = []
        row_by_id = {row.key: row for row in source.rows}
        seen_ids: set[str] = set()

        for row in source.rows:
            kind = resource_type(row.key)
            try:
                if kind not in supported_types:
                    raise ValueError(
                        f"Source row {row.row_number}: unsupported resource ID '{row.key}'. "
                        "Supported delete resources are Product, ProductVariant, MediaImage, Collection, and UrlRedirect."
                    )
                if row.key in seen_ids:
                    raise ValueError(f"Source row {row.row_number}: duplicate resource ID '{row.key}'.")
                seen_ids.add(row.key)
                for column, raw in row.values.items():
                    if parse_cell(raw).state != CellState.UNCHANGED:
                        raise ValueError(
                            f"Source row {row.row_number}: delete resources only requires an id; "
                            f"nonblank column '{column}' is not supported."
                        )
                if kind == "Product":
                    product_rows.append({"input": {"id": row.key}})
                    product_prov.append(root_provenance(row))
                elif kind == "Collection":
                    collection_rows.append({"input": {"id": row.key}})
                    collection_prov.append(root_provenance(row))
                elif kind == "UrlRedirect":
                    redirect_rows.append({"id": row.key})
                    redirect_prov.append(root_provenance(row))
                elif kind == "ProductVariant":
                    variant_order.append(row.key)
                elif kind == "MediaImage":
                    media_order.append(row.key)
            except ValueError as exc:
                plan.errors.append(str(exc))

        variant_parents: dict[str, str] = {}
        for variant_id in variant_order:
            row = row_by_id[variant_id]
            parent = (live_contexts.get(variant_id) or {}).get("productId")
            if not parent:
                plan.errors.append(
                    f"Source row {row.row_number}: could not resolve parent Product for ProductVariant '{variant_id}'."
                )
                continue
            if parent in seen_ids:
                plan.errors.append(
                    f"Source row {row.row_number}: ProductVariant '{variant_id}' is selected along with "
                    f"its parent Product '{parent}'; delete the Product alone."
                )
                continue
            variant_parents[variant_id] = parent

        variant_rows, variant_prov = self._group_variants(variant_order, variant_parents, row_by_id)
        file_rows, file_prov = self._group_files(media_order, row_by_id)

        plan.add("product_delete", product_rows, product_prov)
        plan.add("variant_delete", variant_rows, variant_prov)
        plan.add("file_delete", file_rows, file_prov)
        plan.add("collection_delete", collection_rows, collection_prov)
        plan.add("redirect_delete", redirect_rows, redirect_prov)
        if not plan.mutations and not plan.errors:
            plan.errors.append("Source contains no supported resource IDs to delete.")
        return plan

    @staticmethod
    def _group_variants(order, parents, row_by_id):
        grouped: dict[str, list[SourceRow]] = {}
        for variant_id in order:
            if variant_id in parents:
                grouped.setdefault(parents[variant_id], []).append(row_by_id[variant_id])
        payloads: list[dict[str, Any]] = []
        provenance: list[dict[str, Any]] = []
        for product_id, entries in grouped.items():
            for index in range(0, len(entries), 250):
                batch = entries[index:index + 250]
                refs = [source_ref(row) for row in batch]
                payloads.append({"productId": product_id, "variantsIds": [row.key for row in batch]})
                provenance.append({"sourceRows": refs, "items": {"variantsIds": refs}})
        return payloads, provenance

    @staticmethod
    def _group_files(order, row_by_id):
        payloads: list[dict[str, Any]] = []
        provenance: list[dict[str, Any]] = []
        for index in range(0, len(order), 250):
            batch = [row_by_id[media_id] for media_id in order[index:index + 250]]
            refs = [source_ref(row) for row in batch]
            payloads.append({"fileIds": [row.key for row in batch]})
            provenance.append({"sourceRows": refs, "items": {"fileIds": refs}})
        return payloads, provenance
