from __future__ import annotations

import json
from pathlib import Path

from config import Settings
import app.shopify.client as client_module
from app.shopify.bulk import BulkManager
from app.shopify.client import ShopifyClient


class FakeGraphqlResult:
    ok = True
    data = {"shop": {"id": "gid://shopify/Shop/1"}}
    log = type("Log", (), {"code": "ok", "detail": "ok"})()


class FakeShopifyApp:
    def __init__(self, client_id, client_secret):
        self.client_id = client_id
        self.client_secret = client_secret
        self.calls = []

    def admin_graphql_request(self, query, **kwargs):
        self.calls.append((query, kwargs))
        return FakeGraphqlResult()


def settings(tmp_path):
    return Settings(
        root_dir=tmp_path,
        shop="test.myshopify.com",
        api_version="2026-04",
        access_token="token",
        api_key="key",
        api_secret="secret",
    )


def test_client_uses_shopifyapp_background_graphql(monkeypatch, tmp_path):
    monkeypatch.setattr(client_module, "ShopifyApp", FakeShopifyApp)
    client = ShopifyClient(settings(tmp_path))
    data = client.execute("query { shop { id } }", {"x": 1})
    assert data["shop"]["id"].endswith("/1")
    _, kwargs = client._app.calls[0]
    assert kwargs["shop"] == "test"
    assert kwargs["access_token"] == "token"
    assert kwargs["api_version"] == "2026-04"
    assert kwargs["invalid_token_response"] is None


class FakeBulkClient:
    def __init__(self):
        self.count = 0

    def execute(self, query, variables=None):
        self.count += 1
        if "bulkOperationRunQuery" in query:
            return {"bulkOperationRunQuery": {"bulkOperation": {"id": "gid://shopify/BulkOperation/99", "status": "CREATED"}, "userErrors": []}}
        raise AssertionError("unexpected call")


def test_query_manifest_has_operation_id_before_poll(monkeypatch, tmp_path):
    s = settings(tmp_path)
    s.ensure_directories()
    query_path = s.query_definitions_dir / "query.graphql"
    query_path.write_text("{ products { edges { node { id } } } }")
    bulk = BulkManager(FakeBulkClient(), s)

    def fake_poll(operation_id, interval):
        query_dir = next(s.queries_dir.iterdir())
        manifest = json.loads(next(query_dir.glob("manifest_*.json")).read_text())
        assert manifest["bulkOperationId"] == operation_id
        return {
            "id": operation_id,
            "status": "COMPLETED",
            "objectCount": "0",
            "rootObjectCount": "0",
            "url": None,
            "partialDataUrl": None,
        }

    monkeypatch.setattr(bulk, "poll", fake_poll)
    query_dir = bulk.run_query(query_path)
    result = next(query_dir.glob("result_*.jsonl"))
    xlsx = next(query_dir.glob("result_*.xlsx"))
    manifest = next(query_dir.glob("manifest_*.json"))
    saved_query = next(query_dir.glob("query_*.graphql"))
    ts = query_dir.name[:19]
    assert result.name == f"result_{ts}.jsonl"
    assert xlsx.name == f"result_{ts}.xlsx"
    assert manifest.name == f"manifest_{ts}.json"
    assert saved_query.name == f"query_{ts}.graphql"
    bulk.close()


def test_interrupted_query_manifest_can_resume(monkeypatch, tmp_path):
    s = settings(tmp_path)
    s.ensure_directories()
    query_path = s.query_definitions_dir / "query.graphql"
    query_path.write_text("{ products { edges { node { id } } } }")
    bulk = BulkManager(FakeBulkClient(), s)

    def interrupted_poll(operation_id, interval):
        raise RuntimeError("temporary local polling failure")

    monkeypatch.setattr(bulk, "poll", interrupted_poll)
    try:
        bulk.run_query(query_path)
    except RuntimeError:
        pass
    query_dir = next(s.queries_dir.iterdir())
    manifest_path = next(query_dir.glob("manifest_*.json"))
    manifest = json.loads(manifest_path.read_text())
    assert manifest["status"] == "RUNNING"
    assert manifest["bulkOperationId"] == "gid://shopify/BulkOperation/99"

    monkeypatch.setattr(
        bulk,
        "poll",
        lambda operation_id, interval: {
            "id": operation_id,
            "status": "COMPLETED",
            "objectCount": "0",
            "rootObjectCount": "0",
            "url": None,
            "partialDataUrl": None,
        },
    )
    bulk.resume_query(query_dir)
    manifest = json.loads(manifest_path.read_text())
    assert manifest["status"] == "COMPLETED"
    assert next(query_dir.glob("result_*.jsonl")).exists()
    bulk.close()
