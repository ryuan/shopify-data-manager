from pathlib import Path

from app.source import SourceData, SourceRow
from app.workflows.create_products import CreateProductsPlanner
from app.workflows.create_variants import CreateVariantsPlanner


def scalar(name="String"):
    return {"kind": "SCALAR", "name": name, "ofType": None}


def list_of_input(name):
    return {"kind": "LIST", "name": None, "ofType": {"kind": "INPUT_OBJECT", "name": name, "ofType": None}}


class FakeSchema:
    def input_fields(self, name):
        return {
            "ProductVariantsBulkInput": {"price": scalar("Decimal"), "inventoryItem": {"kind": "INPUT_OBJECT", "name": "InventoryItemInput", "ofType": None}, "metafields": list_of_input("MetafieldInput")},
            "ProductSetInput": {"title": scalar(), "vendor": scalar(), "files": list_of_input("FileSetInput"), "variants": list_of_input("ProductVariantSetInput"), "productOptions": list_of_input("ProductOptionSetInput"), "metafields": list_of_input("MetafieldInput")},
            "ProductVariantSetInput": {"price": scalar("Decimal"), "optionValues": list_of_input("VariantOptionValueInput"), "inventoryItem": {"kind": "INPUT_OBJECT", "name": "InventoryItemInput", "ofType": None}, "metafields": list_of_input("MetafieldInput")},
        }[name]

    def metafield_definition(self, owner_type, namespace, key):
        if (owner_type, namespace, key) == ("PRODUCT", "specs", "foo"):
            return {"ownerType": owner_type}
        return None


class FakeRelationships:
    def node_contexts(self, ids):
        return {
            gid: {"type": "Product", "productId": None}
            for gid in ids
        }


def test_create_variants_groups_by_product_id_and_preserves_source_sku():
    source = SourceData(
        Path("source.csv"), "sku", ["productId", "price"],
        [
            SourceRow(2, "A", {"productId": "gid://shopify/Product/1", "price": "10"}),
            SourceRow(3, "B", {"productId": "gid://shopify/Product/1", "price": "11"}),
        ],
    )
    plan = CreateVariantsPlanner(FakeSchema(), FakeRelationships()).plan(source)
    assert not plan.errors
    row = plan.mutations["create_variants"][0]
    assert row["productId"] == "gid://shopify/Product/1"
    assert row["strategy"] == "PRESERVE_STANDALONE_VARIANT"
    assert [v["inventoryItem"]["sku"] for v in row["variants"]] == ["A", "B"]


def test_create_variants_requires_destination_product_to_exist():
    class MissingRelationships(FakeRelationships):
        def node_contexts(self, ids):
            return {}

    source = SourceData(
        Path("source.csv"), "sku", ["productId", "price"],
        [SourceRow(2, "A", {"productId": "gid://shopify/Product/999", "price": "10"})],
    )
    plan = CreateVariantsPlanner(FakeSchema(), MissingRelationships()).plan(source)
    assert any("does not exist or is not accessible" in error for error in plan.errors)


def test_create_products_groups_by_dummy_title_and_warns_on_product_conflict():
    title = "Meilin Mei-A-S-0004"
    source = SourceData(
        Path("source.csv"), "sku", ["title", "vendor", "price", "optionValues"],
        [
            SourceRow(2, "A", {"title": title, "vendor": "Vendor A", "price": "10", "optionValues": '[{"optionName":"Size","name":"Small"}]'}),
            SourceRow(3, "B", {"title": title, "vendor": "Vendor B", "price": "11", "optionValues": '[{"optionName":"Size","name":"Large"}]'}),
        ],
    )
    plan = CreateProductsPlanner(FakeSchema()).plan(source)
    assert not plan.errors
    assert len(plan.mutations["create_products"]) == 1
    product = plan.mutations["create_products"][0]["input"]
    assert product["title"] == title
    assert product["vendor"] == "Vendor A"
    assert [v["inventoryItem"]["sku"] for v in product["variants"]] == ["A", "B"]
    assert [x["name"] for x in product["productOptions"][0]["values"]] == ["Small", "Large"]
    assert len(plan.warnings) == 1
