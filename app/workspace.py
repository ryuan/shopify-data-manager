from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any

from config import Settings
from app.utils import choose, read_json, sha256, timestamp, write_json, write_jsonl
from app.shopify.bulk import BulkManager, EXECUTION_ORDER, SPECS
from app.shopify.relationships import RelationshipResolver
from app.workflows.helpers import MutationPlan

PROGRAM_VERSION = "1.0.0-alpha.2"


class Workspace:
    def __init__(self, settings: Settings, bulk: BulkManager):
        self.settings = settings
        self.bulk = bulk

    # ----- Working preview -----

    def clear_working(self) -> None:
        if self.settings.working_dir.exists():
            shutil.rmtree(self.settings.working_dir)
        self.settings.working_dir.mkdir(parents=True, exist_ok=True)

    def write_working(self, plan: MutationPlan) -> Path:
        if plan.errors:
            raise RuntimeError("Cannot create working mutation while preflight has errors.")

        self.clear_working()
        mutation_dir = self.settings.working_dir / "mutations"
        source_dir = self.settings.working_dir / "source"
        mutation_dir.mkdir(parents=True)
        source_dir.mkdir(parents=True)

        copied_source = source_dir / plan.source.path.name
        shutil.copy2(plan.source.path, copied_source)

        artifacts: dict[str, dict[str, Any]] = {}
        for name in EXECUTION_ORDER:
            rows = plan.mutations.get(name)
            if not rows:
                continue
            spec = SPECS[name]
            graphql_source = self.settings.mutation_definitions_dir / spec.graphql_filename
            if not graphql_source.exists():
                raise FileNotFoundError(f"Missing GraphQL mutation definition: {graphql_source}")
            graphql_dest = mutation_dir / f"{name}.graphql"
            jsonl_dest = mutation_dir / f"{name}.jsonl"
            shutil.copy2(graphql_source, graphql_dest)
            line_count = write_jsonl(jsonl_dest, rows)
            artifacts[name] = {
                "graphql": str(graphql_dest.relative_to(self.settings.working_dir)),
                "jsonl": str(jsonl_dest.relative_to(self.settings.working_dir)),
                "lines": line_count,
                "sha256Graphql": sha256(graphql_dest),
                "sha256Jsonl": sha256(jsonl_dest),
                "responseField": spec.response_field,
                "pollNestedJobs": spec.poll_nested_jobs,
                "provenance": plan.provenance.get(name, []),
            }

        relationship = None
        if plan.relationship_query:
            relationship = {
                "path": self.settings.display_path(plan.relationship_query),
                "sha256": sha256(plan.relationship_query),
            }

        manifest = {
            "programVersion": PROGRAM_VERSION,
            "kind": "working-mutation",
            "action": plan.action,
            "createdAt": timestamp(),
            "shop": self.settings.shop_domain,
            "apiVersion": self.settings.api_version,
            "source": {
                "path": str(copied_source.relative_to(self.settings.working_dir)),
                "originalFilename": plan.source.path.name,
                "sha256": sha256(copied_source),
                "keyColumn": plan.source.key_column,
            },
            "relationshipQuery": relationship,
            "warnings": plan.warnings,
            "artifacts": artifacts,
        }
        write_json(self.settings.working_dir / "manifest.json", manifest)
        (self.settings.working_dir / "summary.txt").write_text(
            self.format_preflight_summary(plan, artifacts), encoding="utf-8"
        )
        return self.settings.working_dir

    def verify_working(self) -> dict[str, Any]:
        manifest_path = self.settings.working_dir / "manifest.json"
        if not manifest_path.exists():
            raise FileNotFoundError("No current working mutation exists.")
        manifest = read_json(manifest_path)
        source = self.settings.working_dir / manifest["source"]["path"]
        if not source.exists() or sha256(source) != manifest["source"]["sha256"]:
            raise RuntimeError("Working source file changed after preview generation. Generate a new preview.")
        for name, info in manifest.get("artifacts", {}).items():
            gql = self.settings.working_dir / info["graphql"]
            jsonl = self.settings.working_dir / info["jsonl"]
            if not gql.exists() or sha256(gql) != info["sha256Graphql"]:
                raise RuntimeError(f"Working GraphQL mutation '{name}' changed after preview generation.")
            if not jsonl.exists() or sha256(jsonl) != info["sha256Jsonl"]:
                raise RuntimeError(f"Working JSONL mutation '{name}' changed after preview generation.")
        return manifest

    # ----- Mutation execution -----

    def execute_working(self) -> Path:
        manifest = self.verify_working()
        artifacts = manifest.get("artifacts") or {}
        if not artifacts:
            raise RuntimeError("Current working preview contains no mutation files.")

        ts = timestamp()
        action = manifest["action"]
        permanent_dir = self.settings.mutations_dir / f"{ts}_{action}"
        permanent_dir.mkdir(parents=True, exist_ok=False)

        source_info = manifest["source"]
        working_source = self.settings.working_dir / source_info["path"]
        source_dest = permanent_dir / f"{Path(source_info['originalFilename']).stem}_{ts}{working_source.suffix}"
        shutil.copy2(working_source, source_dest)

        summary_dest = permanent_dir / f"summary_{ts}.txt"
        shutil.copy2(self.settings.working_dir / "summary.txt", summary_dest)

        persistent_artifacts: dict[str, dict[str, Any]] = {}
        for name, info in artifacts.items():
            working_gql = self.settings.working_dir / info["graphql"]
            working_jsonl = self.settings.working_dir / info["jsonl"]
            gql_dest = permanent_dir / f"{name}_{ts}.graphql"
            jsonl_dest = permanent_dir / f"{name}_{ts}.jsonl"
            shutil.copy2(working_gql, gql_dest)
            shutil.copy2(working_jsonl, jsonl_dest)
            persistent_artifacts[name] = {
                "graphql": gql_dest.name,
                "jsonl": jsonl_dest.name,
                "lines": info["lines"],
                "sha256Graphql": sha256(gql_dest),
                "sha256Jsonl": sha256(jsonl_dest),
                "responseField": info["responseField"],
                "pollNestedJobs": info["pollNestedJobs"],
                "provenance": info.get("provenance") or [],
                "status": "PENDING",
            }

        persistent_manifest: dict[str, Any] = {
            "programVersion": PROGRAM_VERSION,
            "kind": "mutation-run",
            "action": action,
            "createdAt": manifest["createdAt"],
            "executionStartedAt": ts,
            "shop": self.settings.shop_domain,
            "apiVersion": self.settings.api_version,
            "source": {
                "filename": source_dest.name,
                "sha256": sha256(source_dest),
                "keyColumn": source_info["keyColumn"],
            },
            "relationshipQuery": manifest.get("relationshipQuery"),
            "warnings": manifest.get("warnings") or [],
            "artifacts": persistent_artifacts,
            "status": "RUNNING",
        }
        manifest_path = permanent_dir / f"manifest_{ts}.json"
        write_json(manifest_path, persistent_manifest)

        # From this point forward the permanent snapshot is the source of truth.
        self.clear_working()
        print(f"Execution snapshot created: {self.settings.display_path(permanent_dir)}")
        return self._execute_permanent(permanent_dir, manifest_path, summary_dest, persistent_manifest)

    def resume_mutation_run(self, permanent_dir: Path) -> Path:
        manifest_path = next(permanent_dir.glob("manifest_*.json"), None)
        summary_path = next(permanent_dir.glob("summary_*.txt"), None)
        if not manifest_path or not summary_path:
            raise FileNotFoundError(f"Mutation record '{permanent_dir.name}' is missing its manifest or summary.")
        manifest = read_json(manifest_path)
        if manifest.get("status") != "RUNNING":
            raise RuntimeError("Mutation record is not recoverable because it is not RUNNING.")
        print(f"Resuming mutation: {self.settings.display_path(permanent_dir)}")
        return self._execute_permanent(permanent_dir, manifest_path, summary_path, manifest)

    def _execute_permanent(
        self,
        permanent_dir: Path,
        manifest_path: Path,
        summary_path: Path,
        manifest: dict[str, Any],
    ) -> Path:
        artifacts = manifest.get("artifacts") or {}
        manifest["status"] = "RUNNING"
        manifest.pop("error", None)
        write_json(manifest_path, manifest)

        for name in EXECUTION_ORDER:
            if name not in artifacts:
                continue
            info = artifacts[name]
            if info.get("status") == "COMPLETED":
                continue
            if info.get("status") == "FAILED":
                manifest["status"] = "FAILED"
                break

            spec = SPECS[name]
            result_path = permanent_dir / self._result_filename(name, manifest)
            try:
                if info.get("status") == "RUNNING" and info.get("bulkOperationId"):
                    print(f"\nResuming {name} ({info['lines']} line(s))...")
                    result = self.bulk.resume_mutation(spec, info["bulkOperationId"], result_path)
                else:
                    graphql_path = permanent_dir / info["graphql"]
                    jsonl_path = permanent_dir / info["jsonl"]
                    client_identifier = info.get("clientIdentifier") or f"{manifest['executionStartedAt']}-{name}"
                    info["clientIdentifier"] = client_identifier
                    write_json(manifest_path, manifest)

                    def on_started(operation_id: str, shopify_status: str, *, artifact=info) -> None:
                        artifact["bulkOperationId"] = operation_id
                        artifact["status"] = "RUNNING"
                        artifact["shopifyStatus"] = shopify_status
                        artifact["startedAt"] = timestamp()
                        write_json(manifest_path, manifest)

                    print(f"\nExecuting {name} ({info['lines']} line(s))...")
                    result = self.bulk.run_mutation(
                        spec=spec,
                        graphql_path=graphql_path,
                        jsonl_path=jsonl_path,
                        result_path=result_path,
                        client_identifier=client_identifier,
                        on_started=on_started,
                    )

                info.update(
                    {
                        "bulkOperationId": result.operation_id,
                        "status": "COMPLETED" if result.completed else "FAILED",
                        "shopifyStatus": result.shopify_status,
                        "operation": result.operation,
                        "result": result.result_path.name if result.result_path else None,
                        "resultSha256": sha256(result.result_path) if result.result_path else None,
                        "errors": result.errors,
                        "finishedAt": timestamp(),
                    }
                )
                write_json(manifest_path, manifest)
                self._write_execution_summary(summary_path, manifest)

                if result.completed:
                    print(
                        f"{name} completed: {info['lines']} input line(s), "
                        f"{len(result.errors)} error(s)"
                    )
                else:
                    print(f"{name} failed with Shopify status {result.shopify_status}.")
                    manifest["status"] = "FAILED"
                    break

            except Exception as exc:
                # If Shopify already created the operation, leave it RUNNING. The next launch
                # can resume polling the saved BulkOperation ID instead of starting it again.
                if info.get("bulkOperationId") and info.get("status") == "RUNNING":
                    info["lastLocalError"] = str(exc)
                    manifest["lastLocalError"] = str(exc)
                    write_json(manifest_path, manifest)
                    self._write_execution_summary(summary_path, manifest)
                    raise

                info.update({"status": "FAILED", "error": str(exc), "finishedAt": timestamp()})
                manifest.update({"status": "FAILED", "error": str(exc), "executionFinishedAt": timestamp()})
                write_json(manifest_path, manifest)
                self._write_execution_summary(summary_path, manifest)
                raise

        if all(info.get("status") == "COMPLETED" for info in artifacts.values()):
            manifest["status"] = "COMPLETED"
            manifest["executionFinishedAt"] = timestamp()
        elif manifest.get("status") != "RUNNING":
            manifest["status"] = "FAILED"
            manifest["executionFinishedAt"] = timestamp()

        write_json(manifest_path, manifest)
        self._write_execution_summary(summary_path, manifest)
        print(f"\nMutation execution finished. Permanent record: {self.settings.display_path(permanent_dir)}")
        return permanent_dir

    def _write_execution_summary(self, summary_path: Path, manifest: dict[str, Any]) -> None:
        current = summary_path.read_text(encoding="utf-8") if summary_path.exists() else ""
        base = current.split("\nExecution summary\n", 1)[0].rstrip()
        lines = [base, "", "Execution summary", "-----------------"]
        lines.append(f"Status: {manifest.get('status')}")
        if manifest.get("lastLocalError"):
            lines.append(f"Last local error: {manifest['lastLocalError']}")

        for name in EXECUTION_ORDER:
            info = (manifest.get("artifacts") or {}).get(name)
            if not info:
                continue
            line = f"{name}: {info.get('status')}; input lines={info.get('lines')}"
            if info.get("shopifyStatus"):
                line += f"; Shopify={info['shopifyStatus']}"
            if "errors" in info:
                line += f"; errors={len(info.get('errors') or [])}"
            lines.append(line)
            for error in info.get("errors") or []:
                lines.extend(self._format_result_error(info, error))
            if info.get("error"):
                lines.append(f"  Error: {info['error']}")

        summary_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")

    def _format_result_error(self, artifact: dict[str, Any], error: dict[str, Any]) -> list[str]:
        line_number = error.get("line")
        display_line = line_number + 1 if isinstance(line_number, int) else line_number
        field = error.get("field")
        field_text = self._field_text(field)
        source = self._source_for_error(artifact, line_number, field)
        lines = [f"  JSONL line {display_line}:"]
        if source:
            lines.append(f"    Source row {source.get('row')} — {source.get('id')}")
        if field_text:
            lines.append(f"    Shopify field: {field_text}")
        lines.append(f"    Error: {error.get('message')}")
        return lines

    @staticmethod
    def _field_text(field: Any) -> str:
        if isinstance(field, list):
            return ".".join(str(part) for part in field)
        if field is None:
            return ""
        return str(field)

    @staticmethod
    def _source_for_error(
        artifact: dict[str, Any], line_number: Any, field: Any
    ) -> dict[str, Any] | None:
        provenance = artifact.get("provenance") or []
        if not isinstance(line_number, int) or not (0 <= line_number < len(provenance)):
            return None
        root = provenance[line_number] or {}
        field_parts = [str(part) for part in field] if isinstance(field, list) else []
        items = root.get("items") or {}
        for item_name, refs in items.items():
            if item_name in field_parts:
                idx = field_parts.index(item_name)
                if idx + 1 < len(field_parts) and field_parts[idx + 1].isdigit():
                    child_idx = int(field_parts[idx + 1])
                    if 0 <= child_idx < len(refs):
                        return refs[child_idx]
        source_rows = root.get("sourceRows") or []
        return source_rows[0] if len(source_rows) == 1 else None

    def _result_filename(self, name: str, manifest: dict[str, Any]) -> str:
        return f"{name}_result_{manifest['executionStartedAt']}.jsonl"

    # ----- Recovery -----

    @staticmethod
    def find_unfinished(settings: Settings) -> list[dict[str, Any]]:
        unfinished: list[dict[str, Any]] = []
        for manifest_path in settings.queries_dir.glob("*/manifest_*.json"):
            manifest = read_json(manifest_path)
            if manifest.get("status") == "RUNNING" and manifest.get("bulkOperationId"):
                unfinished.append(
                    {"kind": "query", "path": manifest_path.parent, "label": settings.display_path(manifest_path.parent)}
                )
        for manifest_path in settings.mutations_dir.glob("*/manifest_*.json"):
            manifest = read_json(manifest_path)
            if manifest.get("status") == "RUNNING":
                unfinished.append(
                    {"kind": "mutation", "path": manifest_path.parent, "label": settings.display_path(manifest_path.parent)}
                )
        return sorted(unfinished, key=lambda item: item["path"].name)

    def recover(self, item: dict[str, Any]) -> Path:
        if item["kind"] == "query":
            return self.bulk.resume_query(item["path"])
        if item["kind"] == "mutation":
            return self.resume_mutation_run(item["path"])
        raise ValueError(f"Unknown recovery kind: {item['kind']}")

    # ----- Relationship query selection -----

    def select_relationship_query(
        self,
        media_ids: set[str],
        resolver: RelationshipResolver,
    ) -> Path:
        candidates = sorted(
            self.settings.queries_dir.glob("*/result_*.jsonl"),
            key=lambda p: p.parent.name,
            reverse=True,
        )
        if not candidates:
            raise FileNotFoundError(
                "Media reordering requires a saved query result, but no completed query results exist in 'queries/'."
            )

        remaining = list(candidates)
        while remaining:
            if len(remaining) == 1:
                selected = remaining[0]
                print(f"Using relationship query: {self.settings.display_path(selected)}")
            else:
                selected = remaining[
                    choose(
                        "Select query result for resource relationships",
                        [self.settings.display_path(path) for path in remaining],
                    )
                ]

            candidates_by_media = resolver.product_candidates_from_query(selected, media_ids)
            missing = sorted(media_id for media_id in media_ids if not candidates_by_media.get(media_id))
            ambiguous = {
                media_id: candidates_by_media[media_id]
                for media_id in media_ids
                if len(candidates_by_media.get(media_id, set())) > 1
            }
            if not missing and not ambiguous:
                return selected

            if missing:
                print("Selected query result is missing MediaImage relationship(s):")
                for gid in missing:
                    print(f"  - {gid}")
            if ambiguous:
                print("Selected query result maps MediaImage ID(s) to multiple Products:")
                for gid, products in sorted(ambiguous.items()):
                    print(f"  - {gid}: {', '.join(sorted(products))}")
                print("Supply productId in the source to disambiguate these MediaImage rows.")
            remaining.remove(selected)
            if remaining:
                print("Select another saved query result, or press Ctrl-C to cancel this action.")

        raise RuntimeError(
            "None of the saved query results provide one unambiguous Product relationship "
            "for every MediaImage that requires relationship resolution."
        )

    # ----- Display -----

    def format_preflight_summary(
        self,
        plan: MutationPlan,
        artifacts: dict[str, dict[str, Any]] | None = None,
    ) -> str:
        artifact_counts = artifacts or {name: {"lines": len(rows)} for name, rows in plan.mutations.items()}
        lines = [
            "Preflight summary",
            "-----------------",
            f"Action: {plan.action}",
            f"Source: {self.settings.display_path(plan.source.path)}",
            f"Key column: {plan.source.key_column}",
            "Mutation output:",
        ]
        if artifact_counts:
            for name, info in artifact_counts.items():
                lines.append(f"  - {name}: {info['lines']} JSONL line(s)")
        else:
            lines.append("  - none")
        if plan.relationship_query:
            lines.append(f"Relationship query: {self.settings.display_path(plan.relationship_query)}")
        if plan.warnings:
            lines.extend(["", "Warnings:"])
            lines.extend(f"  - {warning}" for warning in plan.warnings)
        if plan.errors:
            lines.extend(["", "Validation errors:"])
            lines.extend(f"  - {error}" for error in plan.errors)
        else:
            lines.extend(["", "No validation errors detected."])
        return "\n".join(lines) + "\n"
