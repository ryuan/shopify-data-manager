from __future__ import annotations

import json
from pathlib import Path

from app.source import SourceData, SourceRow
from app.workflows.update_resources import UpdateResourcesPlanner


def scalar(name="String", non_null=False):
    ref = {"kind": "SCALAR", "name": name, "ofType": None}
    return {"kind": "NON_NULL", "name": None, "ofType": ref} if non_null else ref


def list_of(name="String"):
    return {"kind": "LIST", "name": None, "ofType": {"kind": "SCALAR", "name": name, "ofType": None}}


class FakeSchema:
    def input_fields(self, name):
        return {
            "ProductUpdateInput": {"id": scalar("ID"), "title": scalar(), "metafields": list_of("MetafieldInput")},
            "ProductVariantsBulkInput": {"id": scalar("ID"), "price": scalar("Decimal"), "metafields": list_of("MetafieldInput")},
            "ProductVariantPositionInput": {"id": scalar("ID"), "position": scalar("Int")},
            "FileUpdateInput": {"id": scalar("ID"), "alt": scalar()},
            "MoveInput": {"id": scalar("ID"), "newPosition": scalar("Int")},
            "CollectionInput": {"id": scalar("ID"), "templateSuffix": scalar(), "metafields": list_of("MetafieldInput")},
        }[name]

    def metafield_definition(self, owner_type, namespace, key):
        allowed = {
            ("PRODUCT", "filter", "sub_type"),
            ("PRODUCTVARIANT", "specs", "foo"),
            ("COLLECTION", "specs", "foo"),
        }
        return {"ownerType": owner_type} if (owner_type, namespace, key) in allowed else None


class FakeRelationships:
    def node_contexts(self, ids):
        contexts = {}
        for gid in ids:
            if "/ProductVariant/" in gid:
                contexts[gid] = {"type": "ProductVariant", "productId": "gid://shopify/Product/10"}
            elif "/MediaImage/" in gid:
                contexts[gid] = {"type": "MediaImage", "productId": None}
            elif "/Collection/" in gid:
                contexts[gid] = {"type": "Collection", "productId": None}
            else:
                contexts[gid] = {"type": "Product", "productId": None}
        return contexts

    def product_candidates_from_query(self, path, target_ids):
        return {gid: {"gid://shopify/Product/10"} for gid in target_ids}

    def product_media_ids(self, product_ids):
        return {
            gid: {"gid://shopify/MediaImage/30"}
            for gid in product_ids
        }


def make_source(rows, columns):
    return SourceData(Path("source.csv"), "id", columns, rows)


def test_product_metafield_updates_stay_inside_product_update():
    rows = [
        SourceRow(i + 2, f"gid://shopify/Product/{100+i}", {"filter.sub_type": "Hall Tree"})
        for i in range(6)
    ]
    plan = UpdateResourcesPlanner(FakeSchema(), FakeRelationships()).plan(
        make_source(rows, ["filter.sub_type"])
    )
    assert not plan.errors
    assert set(plan.mutations) == {"product_update"}
    assert len(plan.mutations["product_update"]) == 6
    assert all(
        row["product"]["metafields"] == [
            {"namespace": "filter", "key": "sub_type", "value": "Hall Tree"}
        ]
        for row in plan.mutations["product_update"]
    )
    assert "metafields_set" not in plan.mutations


def test_explicit_clear_creates_delete_stream_but_blank_does_not():
    rows = [
        SourceRow(2, "gid://shopify/Product/100", {"filter.sub_type": "null"}),
        SourceRow(3, "gid://shopify/Product/101", {"filter.sub_type": "   "}),
    ]
    plan = UpdateResourcesPlanner(FakeSchema(), FakeRelationships()).plan(
        make_source(rows, ["filter.sub_type"])
    )
    assert not plan.errors
    assert plan.mutations["metafields_delete"] == [
        {"metafields": [{"ownerId": "gid://shopify/Product/100", "namespace": "filter", "key": "sub_type"}]}
    ]
    assert "product_update" not in plan.mutations


def test_mixed_resources_group_only_when_shopify_requires_parent():
    rows = [
        SourceRow(2, "gid://shopify/Product/10", {"title": "New", "price": "", "alt": "", "newPosition": ""}),
        SourceRow(3, "gid://shopify/ProductVariant/20", {"title": "", "price": "12.5", "position": "0", "alt": "", "newPosition": ""}),
        SourceRow(4, "gid://shopify/MediaImage/30", {"title": "", "price": "", "alt": "Front", "newPosition": "1"}),
        SourceRow(5, "gid://shopify/Collection/40", {"templateSuffix": "custom"}),
    ]
    source = make_source(rows, ["title", "price", "position", "alt", "newPosition", "templateSuffix"])
    plan = UpdateResourcesPlanner(FakeSchema(), FakeRelationships()).plan(
        source, relationship_query=Path("query.jsonl")
    )
    assert not plan.errors
    assert plan.mutations["product_update"] == [{"product": {"title": "New", "id": "gid://shopify/Product/10"}}]
    assert plan.mutations["variant_update"][0]["productId"] == "gid://shopify/Product/10"
    assert plan.mutations["variant_update"][0]["variants"][0]["id"] == "gid://shopify/ProductVariant/20"
    assert plan.mutations["file_update"] == [{"files": [{"alt": "Front", "id": "gid://shopify/MediaImage/30"}]}]
    assert plan.mutations["media_reorder"][0]["id"] == "gid://shopify/Product/10"
    assert plan.mutations["collection_update"] == [{"input": {"templateSuffix": "custom", "id": "gid://shopify/Collection/40"}}]


def test_variant_flat_sku_routes_into_inventory_item_when_shopify_nests_sku():
    class NestedSkuSchema(FakeSchema):
        def input_fields(self, name):
            if name == "ProductVariantsBulkInput":
                return {
                    "id": scalar("ID"),
                    "inventoryItem": {"kind": "INPUT_OBJECT", "name": "InventoryItemInput", "ofType": None},
                    "metafields": list_of("MetafieldInput"),
                }
            return super().input_fields(name)

    source = make_source(
        [SourceRow(2, "gid://shopify/ProductVariant/20", {"sku": "NEW-SKU"})],
        ["sku"],
    )
    plan = UpdateResourcesPlanner(NestedSkuSchema(), FakeRelationships()).plan(source)
    assert not plan.errors
    variant = plan.mutations["variant_update"][0]["variants"][0]
    assert variant["inventoryItem"]["sku"] == "NEW-SKU"


def test_nonblank_field_for_wrong_resource_warns_instead_of_silent_ignore():
    source = make_source(
        [SourceRow(2, "gid://shopify/Product/10", {"title": "Desk", "price": "99.00"})],
        ["title", "price"],
    )
    plan = UpdateResourcesPlanner(FakeSchema(), FakeRelationships()).plan(source)
    assert not plan.errors
    assert plan.mutations["product_update"][0]["product"]["title"] == "Desk"
    assert any("row 2" in warning.lower() and "price" in warning for warning in plan.warnings)


def test_grouped_variant_provenance_preserves_source_row_order():
    source = make_source(
        [
            SourceRow(7, "gid://shopify/ProductVariant/20", {"price": "12.5"}),
            SourceRow(11, "gid://shopify/ProductVariant/21", {"price": "13.5"}),
        ],
        ["price"],
    )
    plan = UpdateResourcesPlanner(FakeSchema(), FakeRelationships()).plan(source)
    prov = plan.provenance["variant_update"][0]
    assert prov["items"]["variants"] == [
        {"row": 7, "id": "gid://shopify/ProductVariant/20"},
        {"row": 11, "id": "gid://shopify/ProductVariant/21"},
    ]


def test_media_product_id_helper_avoids_query_and_is_live_validated():
    source = make_source(
        [
            SourceRow(
                2,
                "gid://shopify/MediaImage/30",
                {"newPosition": "0", "productId": "gid://shopify/Product/10"},
            )
        ],
        ["newPosition", "productId"],
    )
    planner = UpdateResourcesPlanner(FakeSchema(), FakeRelationships())
    assert planner.relationship_requirements(source) == set()
    plan = planner.plan(source)
    assert not plan.errors
    assert plan.mutations["media_reorder"] == [
        {
            "id": "gid://shopify/Product/10",
            "moves": [{"newPosition": 0, "id": "gid://shopify/MediaImage/30"}],
        }
    ]


def test_variant_product_id_mismatch_with_live_shopify_is_preflight_error():
    source = make_source(
        [
            SourceRow(
                2,
                "gid://shopify/ProductVariant/20",
                {"price": "12.5", "productId": "gid://shopify/Product/999"},
            )
        ],
        ["price", "productId"],
    )
    plan = UpdateResourcesPlanner(FakeSchema(), FakeRelationships()).plan(source)
    assert any("conflicts with Shopify's current" in error for error in plan.errors)


def test_media_query_conflict_with_supplied_product_id_is_preflight_error():
    class ConflictingQueryRelationships(FakeRelationships):
        def product_candidates_from_query(self, path, target_ids):
            return {gid: {"gid://shopify/Product/999"} for gid in target_ids}

    source = make_source(
        [
            SourceRow(
                2,
                "gid://shopify/MediaImage/30",
                {"newPosition": "0", "productId": "gid://shopify/Product/10"},
            )
        ],
        ["newPosition", "productId"],
    )
    plan = UpdateResourcesPlanner(FakeSchema(), ConflictingQueryRelationships()).plan(
        source, relationship_query=Path("query.jsonl")
    )
    assert any("conflicts with relationship query" in error for error in plan.errors)


def test_media_product_id_must_match_live_product_media_membership():
    class WrongMembershipRelationships(FakeRelationships):
        def product_media_ids(self, product_ids):
            return {gid: set() for gid in product_ids}

    source = make_source(
        [
            SourceRow(
                2,
                "gid://shopify/MediaImage/30",
                {"newPosition": "0", "productId": "gid://shopify/Product/10"},
            )
        ],
        ["newPosition", "productId"],
    )
    plan = UpdateResourcesPlanner(FakeSchema(), WrongMembershipRelationships()).plan(source)
    assert any("does not currently show MediaImage" in error for error in plan.errors)


def test_missing_live_key_id_is_preflight_error():
    class MissingRelationships(FakeRelationships):
        def node_contexts(self, ids):
            return {}

    source = make_source(
        [SourceRow(2, "gid://shopify/Product/10", {"title": "Desk"})],
        ["title"],
    )
    plan = UpdateResourcesPlanner(FakeSchema(), MissingRelationships()).plan(source)
    assert any("does not exist or is not accessible" in error for error in plan.errors)
