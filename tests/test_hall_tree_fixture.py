from pathlib import Path
import shutil

from app.source import load_source
from app.workflows.update_resources import UpdateResourcesPlanner


class FakeSchema:
    def input_fields(self, name):
        empty = {"kind": "SCALAR", "name": "String", "ofType": None}
        refs = {
            "ProductUpdateInput": {"id": empty, "metafields": {"kind": "LIST", "name": None, "ofType": empty}},
            "ProductVariantsBulkInput": {"id": empty},
            "ProductVariantPositionInput": {"id": empty},
            "FileUpdateInput": {"id": empty},
            "MoveInput": {"id": empty},
            "CollectionInput": {"id": empty, "metafields": {"kind": "LIST", "name": None, "ofType": empty}},
        }
        return refs[name]

    def metafield_definition(self, owner_type, namespace, key):
        return {"ownerType": owner_type} if (owner_type, namespace, key) == ("PRODUCT", "filter", "sub_type") else None


class NoRelationships:
    def node_contexts(self, ids):
        return {gid: {"type": "Product", "productId": None} for gid in ids}


def test_uploaded_hall_tree_file_produces_six_product_update_roots(tmp_path):
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    original = Path(__file__).resolve().parents[2] / "hall-tree-sub-type-mf-update.csv"
    # In the packaged project this external fixture is absent, so reconstruct if necessary.
    if original.exists():
        shutil.copy2(original, source_dir / original.name)
    else:
        (source_dir / "hall-tree.csv").write_text(
            "id,filter.sub_type\n"
            "gid://shopify/Product/1,Hall Tree\n"
            "gid://shopify/Product/2,Hall Tree\n"
            "gid://shopify/Product/3,Hall Tree\n"
            "gid://shopify/Product/4,Hall Tree\n"
            "gid://shopify/Product/5,Hall Tree\n"
            "gid://shopify/Product/6,Hall Tree\n"
        )
    source = load_source(source_dir, "id")
    plan = UpdateResourcesPlanner(FakeSchema(), NoRelationships()).plan(source)
    assert not plan.errors
    assert len(plan.mutations["product_update"]) == 6
    assert set(plan.mutations) == {"product_update"}
