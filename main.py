from __future__ import annotations

from config import Settings
from app.source import load_source
from app.utils import choose, choose_file, read_json
from app.workspace import Workspace
from app.shopify.bulk import BulkManager
from app.shopify.client import ShopifyClient
from app.shopify.relationships import RelationshipResolver
from app.shopify.schema import SchemaInspector
from app.workflows.create_products import CreateProductsPlanner
from app.workflows.create_variants import CreateVariantsPlanner
from app.workflows.update_resources import UpdateResourcesPlanner

MENU = [
    "Run bulk query",
    "Update existing resources",
    "Create variants for existing products",
    "Create products and their variants",
    "Execute current working mutation",
    "Exit",
]


def main() -> None:
    settings = Settings()
    settings.ensure_directories()

    client: ShopifyClient | None = None
    bulk: BulkManager | None = None
    schema: SchemaInspector | None = None
    relationships: RelationshipResolver | None = None
    workspace: Workspace | None = None

    def services():
        nonlocal client, bulk, schema, relationships, workspace
        if client is None:
            client = ShopifyClient(settings)
        if bulk is None:
            bulk = BulkManager(client, settings)
        if schema is None:
            schema = SchemaInspector(client)
        if relationships is None:
            relationships = RelationshipResolver(client)
        if workspace is None:
            workspace = Workspace(settings, bulk)
        return bulk, schema, relationships, workspace

    print("Shopify Data Manager")
    print("====================")

    def offer_recovery() -> None:
        unfinished = Workspace.find_unfinished(settings)
        if not unfinished:
            return
        options = [f"Resume {item['kind']}: {item['label']}" for item in unfinished]
        options.append("Continue to main menu")
        try:
            recovery_choice = choose("Unfinished Shopify operation found", options)
        except (KeyboardInterrupt, EOFError):
            print()
            return
        if recovery_choice == len(unfinished):
            return
        try:
            _, _, _, ws = services()
            recovered = ws.recover(unfinished[recovery_choice])
            print(f"Recovered operation saved to: {settings.display_path(recovered)}")
        except (KeyboardInterrupt, EOFError):
            print("\nRecovery canceled. The saved operation remains available.")
        except Exception as exc:
            print(f"\nRecovery error: {exc}")

    offer_recovery()

    try:
        while True:
            try:
                action = choose("Select an action", MENU)
            except (KeyboardInterrupt, EOFError):
                print()
                break
            if action == 5:
                break

            try:
                bulk_manager, schema_service, relationship_service, ws = services()
                if action == 0:
                    query_path = choose_file(
                        settings.query_definitions_dir,
                        suffixes=(".graphql",),
                        label="query file",
                    )
                    query_dir = bulk_manager.run_query(query_path)
                    print(f"Bulk query saved to: {settings.display_path(query_dir)}")

                elif action == 1:
                    ws.clear_working()
                    source = load_source(settings.source_dir, expected_key="id")
                    planner = UpdateResourcesPlanner(schema_service, relationship_service)
                    media_ids = planner.relationship_requirements(source)
                    relationship_query = None
                    if media_ids:
                        relationship_query = ws.select_relationship_query(
                            media_ids, relationship_service
                        )
                    plan = planner.plan(source, relationship_query)
                    handle_plan(plan, ws)

                elif action == 2:
                    ws.clear_working()
                    source = load_source(settings.source_dir, expected_key="sku")
                    plan = CreateVariantsPlanner(schema_service, relationship_service).plan(source)
                    handle_plan(plan, ws)

                elif action == 3:
                    ws.clear_working()
                    source = load_source(settings.source_dir, expected_key="sku")
                    plan = CreateProductsPlanner(schema_service).plan(source)
                    handle_plan(plan, ws)

                elif action == 4:
                    show_current_working(settings)
                    ws.execute_working()

            except (KeyboardInterrupt, EOFError):
                print("\nAction canceled.")
                offer_recovery()
            except Exception as exc:
                print(f"\nError: {exc}")
                offer_recovery()
    finally:
        if bulk is not None:
            bulk.close()


def handle_plan(plan, workspace: Workspace) -> None:
    print(workspace.format_preflight_summary(plan), end="")
    if plan.errors:
        print("Working mutation was not created because preflight detected errors.")
        return

    working_dir = workspace.write_working(plan)
    print(f"Working mutation preview created in: {workspace.settings.display_path(working_dir)}")
    print()

    execute_label = "Execute the current working mutation now"
    if plan.warnings:
        execute_label += " (warnings shown above)"
    next_action = choose(
        "What next?",
        [
            "Leave the mutation in working/ for review",
            execute_label,
        ],
    )
    if next_action == 1:
        workspace.execute_working()


def show_current_working(settings: Settings) -> None:
    manifest_path = settings.working_dir / "manifest.json"
    summary_path = settings.working_dir / "summary.txt"
    if not manifest_path.exists() or not summary_path.exists():
        raise FileNotFoundError("No current working mutation exists.")
    manifest = read_json(manifest_path)
    print("Current working mutation")
    print("------------------------")
    print(f"Created: {manifest['createdAt']}")
    print(summary_path.read_text(encoding="utf-8").rstrip())
    print()


if __name__ == "__main__":
    main()
