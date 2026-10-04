from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import httpx

from config import Settings
from app.query_output import jsonl_to_xlsx
from app.utils import iter_jsonl, sha256, slugify, timestamp, write_json, read_json
from app.shopify.client import ShopifyClient


@dataclass(frozen=True)
class MutationSpec:
    name: str
    graphql_filename: str
    response_field: str
    poll_nested_jobs: bool = False


SPECS: dict[str, MutationSpec] = {
    "product_update": MutationSpec("product_update", "product_update.graphql", "productUpdate"),
    "variant_update": MutationSpec("variant_update", "variant_update.graphql", "productVariantsBulkUpdate"),
    "variant_reorder": MutationSpec("variant_reorder", "variant_reorder.graphql", "productVariantsBulkReorder"),
    "file_update": MutationSpec("file_update", "file_update.graphql", "fileUpdate"),
    "media_reorder": MutationSpec("media_reorder", "media_reorder.graphql", "productReorderMedia", True),
    "collection_update": MutationSpec("collection_update", "collection_update.graphql", "collectionUpdate", True),
    "metafields_delete": MutationSpec("metafields_delete", "metafields_delete.graphql", "metafieldsDelete"),
    "create_variants": MutationSpec("create_variants", "create_variants.graphql", "productVariantsBulkCreate"),
    "create_products": MutationSpec("create_products", "create_products.graphql", "productSet"),
}

EXECUTION_ORDER = [
    "product_update",
    "variant_update",
    "variant_reorder",
    "file_update",
    "media_reorder",
    "collection_update",
    "metafields_delete",
    "create_variants",
    "create_products",
]

RUN_BULK_QUERY = r"""
mutation RunBulkQuery($query: String!, $groupObjects: Boolean!) {
  bulkOperationRunQuery(query: $query, groupObjects: $groupObjects) {
    bulkOperation { id status }
    userErrors { field message code }
  }
}
"""

STAGED_UPLOADS_CREATE = r"""
mutation StagedUploadsCreate($input: [StagedUploadInput!]!) {
  stagedUploadsCreate(input: $input) {
    stagedTargets { url resourceUrl parameters { name value } }
    userErrors { field message }
  }
}
"""

RUN_BULK_MUTATION = r"""
mutation RunBulkMutation(
  $mutation: String!
  $stagedUploadPath: String!
  $clientIdentifier: String
) {
  bulkOperationRunMutation(
    mutation: $mutation
    stagedUploadPath: $stagedUploadPath
    clientIdentifier: $clientIdentifier
  ) {
    bulkOperation { id status }
    userErrors { field message code }
  }
}
"""

GET_BULK_OPERATION = r"""
query BulkOperation($id: ID!) {
  bulkOperation(id: $id) {
    id status type objectCount rootObjectCount fileSize
    url partialDataUrl errorCode createdAt completedAt
  }
}
"""

GET_JOB = r"""
query Job($id: ID!) { job(id: $id) { id done } }
"""

TERMINAL_STATUSES = {"COMPLETED", "FAILED", "CANCELED", "EXPIRED"}


@dataclass
class MutationResult:
    operation_id: str
    shopify_status: str
    result_path: Path | None
    operation: dict[str, Any]
    errors: list[dict[str, Any]]

    @property
    def completed(self) -> bool:
        return self.shopify_status == "COMPLETED"


class BulkManager:
    def __init__(self, client: ShopifyClient, settings: Settings):
        self.client = client
        self.settings = settings
        self.http = httpx.Client(timeout=120.0, follow_redirects=True)

    def close(self) -> None:
        self.http.close()

    # ----- Queries -----

    def run_query(self, query_path: Path) -> Path:
        ts = timestamp()
        query_dir = self.settings.queries_dir / f"{ts}_{slugify(query_path.stem)}"
        query_dir.mkdir(parents=True, exist_ok=False)

        saved_query = query_dir / f"query_{ts}.graphql"
        saved_result = query_dir / f"result_{ts}.jsonl"
        manifest_path = query_dir / f"manifest_{ts}.json"
        query_text = query_path.read_text(encoding="utf-8")
        saved_query.write_text(query_text, encoding="utf-8")

        manifest: dict[str, Any] = {
            "kind": "query",
            "createdAt": ts,
            "shop": self.settings.shop_domain,
            "apiVersion": self.settings.api_version,
            "query": saved_query.name,
            "querySha256": sha256(saved_query),
            "status": "PENDING",
        }
        write_json(manifest_path, manifest)

        try:
            data = self.client.execute(RUN_BULK_QUERY, {"query": query_text, "groupObjects": True})
            payload = data["bulkOperationRunQuery"]
            self._raise_user_errors(payload.get("userErrors") or [], "bulkOperationRunQuery")
            operation_id = payload["bulkOperation"]["id"]
            manifest.update(
                {
                    "bulkOperationId": operation_id,
                    "status": "RUNNING",
                    "shopifyStatus": payload["bulkOperation"]["status"],
                }
            )
            write_json(manifest_path, manifest)
            return self._finish_query(query_dir, manifest_path, saved_result, manifest)
        except Exception as exc:
            # If Shopify already created the operation, keep RUNNING so startup recovery can resume it.
            if not manifest.get("bulkOperationId"):
                manifest["status"] = "FAILED"
            manifest["error"] = str(exc)
            write_json(manifest_path, manifest)
            raise

    def resume_query(self, query_dir: Path) -> Path:
        manifest_path = next(query_dir.glob("manifest_*.json"), None)
        if not manifest_path:
            raise FileNotFoundError(f"No query manifest found in '{query_dir.name}'.")
        manifest = read_json(manifest_path)
        if manifest.get("status") != "RUNNING" or not manifest.get("bulkOperationId"):
            raise RuntimeError("Query is not recoverable because it is not a saved RUNNING operation.")
        ts = manifest["createdAt"]
        saved_result = query_dir / f"result_{ts}.jsonl"
        return self._finish_query(query_dir, manifest_path, saved_result, manifest)

    def _finish_query(
        self,
        query_dir: Path,
        manifest_path: Path,
        saved_result: Path,
        manifest: dict[str, Any],
    ) -> Path:
        operation = self.poll(manifest["bulkOperationId"], self.settings.query_poll_seconds)
        manifest["shopifyStatus"] = operation["status"]
        manifest["operation"] = operation
        url = operation.get("url") or operation.get("partialDataUrl")

        if operation["status"] == "COMPLETED":
            if url:
                self.download(url, saved_result)
            else:
                saved_result.touch()
            xlsx_result = saved_result.with_suffix(".xlsx")
            xlsx_error = None
            try:
                jsonl_to_xlsx(saved_result, xlsx_result)
            except Exception as exc:
                xlsx_error = str(exc)
                if xlsx_result.exists():
                    xlsx_result.unlink()
            manifest.update(
                {
                    "status": "COMPLETED",
                    "result": saved_result.name,
                    "resultSha256": sha256(saved_result),
                    "finishedAt": timestamp(),
                }
            )
            if xlsx_result.exists():
                manifest.update(
                    {
                        "xlsx": xlsx_result.name,
                        "xlsxSha256": sha256(xlsx_result),
                    }
                )
            if xlsx_error:
                manifest["xlsxError"] = xlsx_error
                print(f"Warning: query JSONL was saved, but XLSX conversion failed: {xlsx_error}")
            write_json(manifest_path, manifest)
            return query_dir

        if url:
            partial = query_dir / f"partial_result_{manifest['createdAt']}.jsonl"
            self.download(url, partial)
            manifest.update({"result": partial.name, "resultSha256": sha256(partial)})
        manifest.update({"status": "FAILED", "finishedAt": timestamp()})
        write_json(manifest_path, manifest)
        raise RuntimeError(f"Bulk query ended with Shopify status {operation['status']}.")

    # ----- Mutations -----

    def run_mutation(
        self,
        spec: MutationSpec,
        graphql_path: Path,
        jsonl_path: Path,
        result_path: Path,
        client_identifier: str,
        on_started: Callable[[str, str], None] | None = None,
    ) -> MutationResult:
        staged_path = self._stage_jsonl(jsonl_path)
        mutation_text = graphql_path.read_text(encoding="utf-8")
        data = self.client.execute(
            RUN_BULK_MUTATION,
            {
                "mutation": mutation_text,
                "stagedUploadPath": staged_path,
                "clientIdentifier": client_identifier,
            },
        )
        payload = data["bulkOperationRunMutation"]
        self._raise_user_errors(payload.get("userErrors") or [], "bulkOperationRunMutation")
        operation_id = payload["bulkOperation"]["id"]
        start_status = payload["bulkOperation"]["status"]
        if on_started:
            on_started(operation_id, start_status)
        return self.resume_mutation(spec, operation_id, result_path)

    def resume_mutation(
        self,
        spec: MutationSpec,
        operation_id: str,
        result_path: Path,
    ) -> MutationResult:
        operation = self.poll(operation_id, self.settings.mutation_poll_seconds)
        downloaded: Path | None = None
        url = operation.get("url") or operation.get("partialDataUrl")
        if url:
            downloaded = result_path
            self.download(url, downloaded)
        elif operation["status"] == "COMPLETED":
            downloaded = result_path
            downloaded.touch()

        errors = self.analyze_result(downloaded, spec.response_field) if downloaded else []
        if spec.poll_nested_jobs and downloaded and operation["status"] == "COMPLETED":
            self.poll_jobs(self.extract_job_ids(downloaded, spec.response_field))

        return MutationResult(operation_id, operation["status"], downloaded, operation, errors)

    # ----- Polling / transfer -----

    def poll(self, operation_id: str, interval: int) -> dict[str, Any]:
        started = time.monotonic()
        while True:
            data = self.client.execute(GET_BULK_OPERATION, {"id": operation_id})
            operation = data.get("bulkOperation")
            if not operation:
                raise RuntimeError(f"Bulk operation '{operation_id}' could not be retrieved.")
            status = operation["status"]
            elapsed = int(time.monotonic() - started)
            print(
                f"[{elapsed:>5}s] {status:<10} objects={operation.get('objectCount')} root={operation.get('rootObjectCount')}",
                end="\n" if status in TERMINAL_STATUSES else "\r",
                flush=True,
            )
            if status in TERMINAL_STATUSES:
                return operation
            time.sleep(interval)

    def poll_jobs(self, job_ids: list[str]) -> None:
        pending = set(job_ids)
        if not pending:
            return
        print(f"Waiting for {len(pending)} Shopify job(s)...")
        while pending:
            completed: set[str] = set()
            for job_id in pending:
                data = self.client.execute(GET_JOB, {"id": job_id})
                job = data.get("job")
                if not job:
                    raise RuntimeError(f"Shopify job '{job_id}' could not be retrieved.")
                if job.get("done"):
                    completed.add(job_id)
            pending -= completed
            if pending:
                time.sleep(self.settings.job_poll_seconds)
        print("Shopify job(s) completed.")

    def download(self, url: str, output_path: Path) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with self.http.stream("GET", url) as response:
            response.raise_for_status()
            with output_path.open("wb") as fh:
                for chunk in response.iter_bytes():
                    fh.write(chunk)

    def _stage_jsonl(self, jsonl_path: Path) -> str:
        data = self.client.execute(
            STAGED_UPLOADS_CREATE,
            {
                "input": [
                    {
                        "resource": "BULK_MUTATION_VARIABLES",
                        "filename": jsonl_path.name,
                        "mimeType": "text/jsonl",
                        "httpMethod": "POST",
                    }
                ]
            },
        )
        payload = data["stagedUploadsCreate"]
        self._raise_user_errors(payload.get("userErrors") or [], "stagedUploadsCreate")
        targets = payload.get("stagedTargets") or []
        if not targets:
            raise RuntimeError("Shopify returned no staged upload target.")
        target = targets[0]
        params = {item["name"]: item["value"] for item in target["parameters"]}
        staged_path = params.get("key")
        if not staged_path:
            raise RuntimeError("Staged upload target did not contain a 'key' parameter.")
        with jsonl_path.open("rb") as fh:
            response = self.http.post(
                target["url"], data=params, files={"file": (jsonl_path.name, fh, "text/jsonl")}
            )
            response.raise_for_status()
        return staged_path

    @staticmethod
    def _raise_user_errors(errors: list[dict[str, Any]], operation: str) -> None:
        if errors:
            details = "; ".join(f"{err.get('field')}: {err.get('message')}" for err in errors)
            raise RuntimeError(f"{operation} returned userErrors: {details}")

    @staticmethod
    def analyze_result(path: Path, response_field: str) -> list[dict[str, Any]]:
        errors: list[dict[str, Any]] = []
        for line in iter_jsonl(path):
            line_number = line.get("__lineNumber")
            for error in line.get("errors") or []:
                errors.append(
                    {"line": line_number, "field": error.get("path"), "message": error.get("message")}
                )
            payload = (line.get("data") or {}).get(response_field) or {}
            for error in (payload.get("userErrors") or []) + (payload.get("mediaUserErrors") or []):
                errors.append({"line": line_number, **error})
        return errors

    @staticmethod
    def extract_job_ids(path: Path, response_field: str) -> list[str]:
        jobs: list[str] = []
        for line in iter_jsonl(path):
            payload = (line.get("data") or {}).get(response_field) or {}
            job = payload.get("job") or {}
            if job.get("id"):
                jobs.append(job["id"])
        return jobs
