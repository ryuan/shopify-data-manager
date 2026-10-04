import json
from pathlib import Path

from app.shopify.relationships import RelationshipResolver


class NoNetwork:
    def execute(self, *args, **kwargs):
        raise AssertionError("network should not be needed")


def test_query_relationships_normalize_media_variant_parent_to_product(tmp_path):
    path = tmp_path / "result.jsonl"
    objects = [
        {"id": "gid://shopify/Product/1"},
        {"id": "gid://shopify/ProductVariant/2", "__parentId": "gid://shopify/Product/1"},
        {"id": "gid://shopify/MediaImage/3", "__parentId": "gid://shopify/ProductVariant/2"},
        {"id": "gid://shopify/MediaImage/3", "__parentId": "gid://shopify/Product/1"},
    ]
    path.write_text("\n".join(json.dumps(x) for x in objects) + "\n")
    resolver = RelationshipResolver(NoNetwork())
    candidates = resolver.product_candidates_from_query(
        path,
        {"gid://shopify/MediaImage/3", "gid://shopify/ProductVariant/2"},
    )
    assert candidates["gid://shopify/MediaImage/3"] == {"gid://shopify/Product/1"}
    assert candidates["gid://shopify/ProductVariant/2"] == {"gid://shopify/Product/1"}


def test_query_relationships_keep_multiple_media_product_candidates(tmp_path):
    path = tmp_path / "result.jsonl"
    objects = [
        {"id": "gid://shopify/MediaImage/3", "__parentId": "gid://shopify/Product/1"},
        {"id": "gid://shopify/MediaImage/3", "__parentId": "gid://shopify/Product/9"},
    ]
    path.write_text("\n".join(json.dumps(x) for x in objects) + "\n")
    resolver = RelationshipResolver(NoNetwork())
    candidates = resolver.product_candidates_from_query(path, {"gid://shopify/MediaImage/3"})
    assert candidates["gid://shopify/MediaImage/3"] == {
        "gid://shopify/Product/1",
        "gid://shopify/Product/9",
    }


class ChunkingClient:
    def __init__(self):
        self.sizes = []

    def execute(self, query, variables=None):
        ids = variables["ids"]
        self.sizes.append(len(ids))
        return {
            "nodes": [
                {"__typename": "Product", "id": gid}
                for gid in ids
            ]
        }


def test_live_node_context_validation_chunks_transparently_at_250():
    client = ChunkingClient()
    resolver = RelationshipResolver(client)
    ids = [f"gid://shopify/Product/{i}" for i in range(600)]
    contexts = resolver.node_contexts(ids)
    assert len(contexts) == 600
    assert client.sizes == [250, 250, 100]
