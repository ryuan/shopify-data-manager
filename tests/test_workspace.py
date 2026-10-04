from __future__ import annotations

import json
from pathlib import Path

import pytest

from config import Settings
from app.source import SourceData, SourceRow
from app.workspace import Workspace
from app.shopify.bulk import MutationResult
from app.workflows.helpers import MutationPlan


class FakeBulk:
    def __init__(self, fail_after_start=False, errors=None):
        self.fail_after_start = fail_after_start
        self.errors = errors or []
        self.seen_graphql = None
        self.run_calls = 0
        self.resume_calls = 0

    def run_mutation(self, spec, graphql_path, jsonl_path, result_path, client_identifier, on_started=None):
        self.run_calls += 1
        self.seen_graphql = graphql_path.read_text()
        if on_started:
            on_started("gid://shopify/BulkOperation/123", "CREATED")
        if self.fail_after_start:
            raise RuntimeError("lost connection while polling")
        return self._completed_result(result_path)

    def resume_mutation(self, spec, operation_id, result_path):
        self.resume_calls += 1
        return self._completed_result(result_path, operation_id)

    def _completed_result(self, result_path, operation_id="gid://shopify/BulkOperation/123"):
        result_path.write_text('{"__lineNumber":0,"data":{}}\n')
        return MutationResult(
            operation_id=operation_id,
            shopify_status="COMPLETED",
            result_path=result_path,
            operation={"id": operation_id, "status": "COMPLETED"},
            errors=self.errors,
        )


def make_settings(tmp_path):
    settings = Settings(
        root_dir=tmp_path,
        shop="test.myshopify.com",
        api_version="2026-04",
        access_token="token",
        api_key="key",
        api_secret="secret",
    )
    settings.ensure_directories()
    return settings


def make_plan(tmp_path):
    source_path = tmp_path / "source" / "input.csv"
    source_path.write_text("id,title\ngid://shopify/Product/1,Hello\n")
    source = SourceData(
        source_path, "id", ["title"],
        [SourceRow(2, "gid://shopify/Product/1", {"title": "Hello"})],
    )
    return MutationPlan(
        "update-resources",
        source,
        mutations={"product_update": [{"product": {"id": "gid://shopify/Product/1", "title": "Hello"}}]},
        provenance={"product_update": [{"sourceRows": [{"row": 2, "id": "gid://shopify/Product/1"}]}]},
    )


def prepare_graphql(settings):
    path = settings.mutation_definitions_dir / "product_update.graphql"
    path.write_text("mutation ProductUpdate($product: ProductUpdateInput!) { productUpdate(product:$product){userErrors{field message}} }")
    return path


def test_working_preview_snapshots_graphql_and_jsonl(tmp_path):
    settings = make_settings(tmp_path)
    live_gql = prepare_graphql(settings)
    workspace = Workspace(settings, FakeBulk())
    workspace.write_working(make_plan(tmp_path))

    manifest = json.loads((settings.working_dir / "manifest.json").read_text())
    info = manifest["artifacts"]["product_update"]
    assert (settings.working_dir / info["graphql"]).exists()
    assert (settings.working_dir / info["jsonl"]).exists()

    live_gql.write_text("CHANGED LIVE FILE")
    workspace.verify_working()


def test_execution_consumes_working_and_uses_permanent_snapshot(tmp_path):
    settings = make_settings(tmp_path)
    prepare_graphql(settings)
    bulk = FakeBulk()
    workspace = Workspace(settings, bulk)
    workspace.write_working(make_plan(tmp_path))
    permanent = workspace.execute_working()

    assert not (settings.working_dir / "manifest.json").exists()
    manifest = json.loads(next(permanent.glob("manifest_*.json")).read_text())
    info = manifest["artifacts"]["product_update"]
    assert info["bulkOperationId"] == "gid://shopify/BulkOperation/123"
    assert info["status"] == "COMPLETED"
    assert info["shopifyStatus"] == "COMPLETED"
    assert manifest["status"] == "COMPLETED"


def test_interrupted_polling_stays_running_and_can_resume(tmp_path):
    settings = make_settings(tmp_path)
    prepare_graphql(settings)
    bulk = FakeBulk(fail_after_start=True)
    workspace = Workspace(settings, bulk)
    workspace.write_working(make_plan(tmp_path))

    with pytest.raises(RuntimeError, match="lost connection"):
        workspace.execute_working()

    assert not (settings.working_dir / "manifest.json").exists()
    permanent = next(settings.mutations_dir.iterdir())
    manifest = json.loads(next(permanent.glob("manifest_*.json")).read_text())
    info = manifest["artifacts"]["product_update"]
    assert info["bulkOperationId"] == "gid://shopify/BulkOperation/123"
    assert info["status"] == "RUNNING"
    assert manifest["status"] == "RUNNING"

    bulk.fail_after_start = False
    workspace.resume_mutation_run(permanent)
    manifest = json.loads(next(permanent.glob("manifest_*.json")).read_text())
    assert manifest["status"] == "COMPLETED"
    assert bulk.run_calls == 1
    assert bulk.resume_calls == 1


def test_summary_keeps_shopify_error_and_adds_source_row(tmp_path):
    settings = make_settings(tmp_path)
    prepare_graphql(settings)
    bulk = FakeBulk(errors=[{"line": 0, "field": ["product", "title"], "message": "Title can't be blank"}])
    workspace = Workspace(settings, bulk)
    workspace.write_working(make_plan(tmp_path))
    permanent = workspace.execute_working()
    summary = next(permanent.glob("summary_*.txt")).read_text()
    assert "Source row 2 — gid://shopify/Product/1" in summary
    assert "Shopify field: product.title" in summary
    assert "Error: Title can't be blank" in summary
    manifest = json.loads(next(permanent.glob("manifest_*.json")).read_text())
    assert manifest["status"] == "COMPLETED"
    assert manifest["artifacts"]["product_update"]["status"] == "COMPLETED"
    assert len(manifest["artifacts"]["product_update"]["errors"]) == 1


def test_relationship_query_selection_retries_until_media_ids_are_resolved(tmp_path, monkeypatch):
    from app.shopify.relationships import RelationshipResolver
    import app.workspace as workspace_module

    settings = make_settings(tmp_path)
    prepare_graphql(settings)
    bad_dir = settings.queries_dir / "2026-10-02_02-00_bad"
    good_dir = settings.queries_dir / "2026-10-02_01-00_good"
    bad_dir.mkdir(parents=True)
    good_dir.mkdir(parents=True)
    bad = bad_dir / "result_2026-10-02_02-00.jsonl"
    good = good_dir / "result_2026-10-02_01-00.jsonl"
    bad.write_text('{"id":"gid://shopify/MediaImage/999","__parentId":"gid://shopify/Product/1"}\n')
    good.write_text('{"id":"gid://shopify/MediaImage/30","__parentId":"gid://shopify/Product/10"}\n')

    class NoNetwork:
        def execute(self, *args, **kwargs):
            raise AssertionError("network lookup not expected")

    monkeypatch.setattr(workspace_module, "choose", lambda prompt, options: 0)
    workspace = Workspace(settings, FakeBulk())
    selected = workspace.select_relationship_query(
        {"gid://shopify/MediaImage/30"}, RelationshipResolver(NoNetwork())
    )
    assert selected == good


def test_find_unfinished_detects_running_mutation(tmp_path):
    settings = make_settings(tmp_path)
    run_dir = settings.mutations_dir / "2026-10-02_12-00-00_update-resources"
    run_dir.mkdir()
    (run_dir / "manifest_2026-10-02_12-00-00.json").write_text(
        json.dumps({"kind": "mutation-run", "status": "RUNNING", "artifacts": {}})
    )
    unfinished = Workspace.find_unfinished(settings)
    assert unfinished == [
        {
            "kind": "mutation",
            "path": run_dir,
            "label": "mutations/2026-10-02_12-00-00_update-resources",
        }
    ]


def test_grouped_error_maps_child_index_to_source_row(tmp_path):
    settings = make_settings(tmp_path)
    workspace = Workspace(settings, FakeBulk())
    artifact = {
        "provenance": [
            {
                "sourceRows": [
                    {"row": 7, "id": "gid://shopify/ProductVariant/20"},
                    {"row": 11, "id": "gid://shopify/ProductVariant/21"},
                ],
                "items": {
                    "variants": [
                        {"row": 7, "id": "gid://shopify/ProductVariant/20"},
                        {"row": 11, "id": "gid://shopify/ProductVariant/21"},
                    ]
                },
            }
        ]
    }
    lines = workspace._format_result_error(
        artifact,
        {"line": 0, "field": ["variants", "1", "price"], "message": "Invalid price"},
    )
    text = "\n".join(lines)
    assert "Source row 11 — gid://shopify/ProductVariant/21" in text
    assert "Shopify field: variants.1.price" in text
    assert "Error: Invalid price" in text


def test_true_bulk_failure_stops_later_artifacts(tmp_path):
    class FailingBulk(FakeBulk):
        def run_mutation(self, spec, graphql_path, jsonl_path, result_path, client_identifier, on_started=None):
            self.run_calls += 1
            if on_started:
                on_started("gid://shopify/BulkOperation/failed", "CREATED")
            return MutationResult(
                operation_id="gid://shopify/BulkOperation/failed",
                shopify_status="FAILED",
                result_path=None,
                operation={"id": "gid://shopify/BulkOperation/failed", "status": "FAILED"},
                errors=[],
            )

    settings = make_settings(tmp_path)
    prepare_graphql(settings)
    (settings.mutation_definitions_dir / "collection_update.graphql").write_text(
        "mutation CollectionUpdate($input: CollectionInput!) { collectionUpdate(input:$input){userErrors{field message}} }"
    )
    source_path = tmp_path / "source" / "input.csv"
    source_path.write_text("id,title\ngid://shopify/Product/1,Hello\n")
    source = SourceData(
        source_path, "id", ["title"],
        [SourceRow(2, "gid://shopify/Product/1", {"title": "Hello"})],
    )
    plan = MutationPlan(
        "update-resources",
        source,
        mutations={
            "product_update": [{"product": {"id": "gid://shopify/Product/1", "title": "Hello"}}],
            "collection_update": [{"input": {"id": "gid://shopify/Collection/2", "title": "Collection"}}],
        },
        provenance={
            "product_update": [{"sourceRows": [{"row": 2, "id": "gid://shopify/Product/1"}]}],
            "collection_update": [{"sourceRows": [{"row": 3, "id": "gid://shopify/Collection/2"}]}],
        },
    )
    bulk = FailingBulk()
    workspace = Workspace(settings, bulk)
    workspace.write_working(plan)
    permanent = workspace.execute_working()
    manifest = json.loads(next(permanent.glob("manifest_*.json")).read_text())
    assert manifest["status"] == "FAILED"
    assert manifest["artifacts"]["product_update"]["status"] == "FAILED"
    assert manifest["artifacts"]["collection_update"]["status"] == "PENDING"
    assert bulk.run_calls == 1
