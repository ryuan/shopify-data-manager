from __future__ import annotations

from pathlib import Path
from typing import Iterable

from app.shopify.client import ShopifyClient
from app.utils import iter_jsonl

NODE_CONTEXTS = r"""
query ResourceContexts($ids: [ID!]!) {
  nodes(ids: $ids) {
    __typename
    id
    ... on ProductVariant { product { id } }
  }
}
"""

PRODUCT_MEDIA = r"""
query ProductMedia($ids: [ID!]!) {
  nodes(ids: $ids) {
    __typename
    id
    ... on Product {
      media(first: 250) { nodes { id } }
    }
  }
}
"""


class RelationshipResolver:
    """Resolve and validate Shopify resource relationships used by mutation workflows."""

    def __init__(self, client: ShopifyClient):
        self.client = client

    @staticmethod
    def _unique(ids: Iterable[str]) -> list[str]:
        return list(dict.fromkeys(ids))

    @staticmethod
    def _chunks(ids: list[str], size: int = 250):
        for start in range(0, len(ids), size):
            yield ids[start:start + size]

    def node_contexts(self, ids: Iterable[str]) -> dict[str, dict[str, str | None]]:
        """Return live Shopify type/parent context for Node IDs, transparently chunked."""
        unique_ids = self._unique(ids)
        result: dict[str, dict[str, str | None]] = {}
        for chunk in self._chunks(unique_ids):
            data = self.client.execute(NODE_CONTEXTS, {"ids": chunk})
            for node in data.get("nodes") or []:
                if not node or not node.get("id"):
                    continue
                product = node.get("product") or {}
                result[node["id"]] = {
                    "type": node.get("__typename"),
                    "productId": product.get("id"),
                }
        return result

    def variant_parents(self, variant_ids: Iterable[str]) -> dict[str, str]:
        contexts = self.node_contexts(variant_ids)
        return {
            resource_id: str(context["productId"])
            for resource_id, context in contexts.items()
            if context.get("type") == "ProductVariant" and context.get("productId")
        }

    def product_media_ids(self, product_ids: Iterable[str]) -> dict[str, set[str]]:
        """Return live media membership for candidate Product IDs, transparently chunked."""
        unique_ids = self._unique(product_ids)
        result: dict[str, set[str]] = {}
        for chunk in self._chunks(unique_ids):
            data = self.client.execute(PRODUCT_MEDIA, {"ids": chunk})
            for node in data.get("nodes") or []:
                if not node or node.get("__typename") != "Product" or not node.get("id"):
                    continue
                media = (node.get("media") or {}).get("nodes") or []
                result[node["id"]] = {
                    item["id"] for item in media if item and item.get("id")
                }
        return result

    @staticmethod
    def _parent_sets_from_query(path: Path, target_ids: set[str]) -> dict[str, set[str]]:
        """Collect every __parentId occurrence for target IDs; never use first-match-wins."""
        relationships: dict[str, set[str]] = {}
        if not target_ids:
            return relationships
        for obj in iter_jsonl(path):
            node_id = obj.get("id")
            parent_id = obj.get("__parentId")
            if node_id in target_ids and parent_id:
                relationships.setdefault(node_id, set()).add(parent_id)
        return relationships

    def product_candidates_from_query(
        self,
        path: Path,
        target_ids: set[str],
    ) -> dict[str, set[str]]:
        """Normalize saved-query parent evidence to candidate Product IDs.

        A target may appear more than once in a grouped bulk result. Direct Product parents
        are retained. ProductVariant parents are normalized through the same query when
        possible, with a live Variant -> Product lookup only when the query lacks that
        intermediate Variant relationship.
        """
        immediate = self._parent_sets_from_query(path, target_ids)
        parent_variant_ids = {
            parent_id
            for parents in immediate.values()
            for parent_id in parents
            if parent_id.startswith("gid://shopify/ProductVariant/")
        }

        variant_products: dict[str, set[str]] = {}
        if parent_variant_ids:
            query_variant_parents = self._parent_sets_from_query(path, parent_variant_ids)
            unresolved: list[str] = []
            for variant_id in parent_variant_ids:
                products = {
                    parent_id
                    for parent_id in query_variant_parents.get(variant_id, set())
                    if parent_id.startswith("gid://shopify/Product/")
                }
                if products:
                    variant_products[variant_id] = products
                else:
                    unresolved.append(variant_id)

            if unresolved:
                live = self.variant_parents(unresolved)
                for variant_id, product_id in live.items():
                    variant_products.setdefault(variant_id, set()).add(product_id)

        candidates: dict[str, set[str]] = {}
        for resource_id, parents in immediate.items():
            product_ids: set[str] = set()
            for parent_id in parents:
                if parent_id.startswith("gid://shopify/Product/"):
                    product_ids.add(parent_id)
                elif parent_id.startswith("gid://shopify/ProductVariant/"):
                    product_ids.update(variant_products.get(parent_id, set()))
            if product_ids:
                candidates[resource_id] = product_ids
        return candidates
