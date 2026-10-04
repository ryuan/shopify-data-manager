# Shopify Data Manager

A schema-driven CLI for bulk querying, validating, and updating Shopify catalog data from XLSX/CSV files.

Shopify Data Manager is designed for catalog workflows where the desired data has already been prepared manually, in Excel, or by another data-processing program. It supports updating existing products, variants, media, collections, and metafields, as well as creating new products and adding variants to existing products.

The program separates mutation preparation from Shopify execution. Source data is validated against Shopify's current schema and resource relationships, mutation JSONL is generated into a reviewable working directory, and nothing is written to Shopify until the user explicitly chooses to execute it.

> **Note**
>
> This is production-oriented catalog tooling, not a plug-and-play Shopify app. The current implementation assumes a Shopify-oriented data model and is intended for users who understand the catalog changes represented by their source XLSX/CSV files.

## How It Works

```text
Prepared XLSX/CSV ──> source parsing + Shopify validation
                                  │
                                  v
                           mutation planning
                                  │
                                  v
                          working/ preview
                                  │
                     ┌────────────┴────────────┐
                     │                         │
                  review                    execute
                                               │
                                               v
                                   permanent mutations/ record
                                               │
                                               v
                                    Shopify Bulk Operations API
```

A typical mutation run:

1. Loads the single XLSX/CSV file in `source/`.
2. Interprets blank, clear, and real source values.
3. Introspects Shopify input types and metafield definitions as needed.
4. Validates existing resource IDs and Product relationships against current Shopify data.
5. Builds only the mutation types required by the source file.
6. Writes the exact GraphQL and JSONL mutation files to `working/` for review.
7. If execution is selected, preserves that exact preview under `mutations/` before sending it to Shopify.
8. Downloads results and records Shopify errors, source-row context, and operation metadata.

Bulk queries are independent from mutation workflows and are retained under `queries/` for later inspection or relationship lookups.

## Key Features

- Mixed Product, ProductVariant, MediaImage, and Collection updates from one source file.
- Shopify schema introspection for valid fields, input types, and value coercion.
- Product-, Variant-, and Collection-level metafield updates through their owning resource mutations.
- Explicit metafield deletion without performing Shopify writes during mutation preparation.
- Clear distinction between blank cells, explicit clear values, and real falsy values such as `0` and `FALSE`.
- Live validation of existing Shopify resource IDs before mutation generation.
- Variant-to-Product and MediaImage-to-Product relationship validation.
- Optional `productId` helper column when parent Product context is already known.
- Saved-query relationship resolution only when Shopify cannot provide the required relationship directly.
- Safe working mutation previews with file hashes before execution.
- Persistent query and mutation histories with timestamped artifacts.
- Interrupted BulkOperation recovery using saved Shopify operation IDs.
- Source-row-aware Shopify error reporting.
- Product creation and Variant creation workflows.
- Automatic XLSX conversion of Shopify bulk-query results for Excel-based review and lookup workflows.

## Quick Start

### 1. Install dependencies

```bash
pip3 install -r requirements.txt
```

For development/testing dependencies:

```bash
pip3 install -r requirements-dev.txt
```

### 2. Configure Shopify credentials

Copy the environment template:

```bash
cp .env.example .env
```

Then populate `.env`:

```env
SHOPIFY_SHOP=your-store.myshopify.com
SHOPIFY_API_VERSION=2026-04
SHOPIFY_ACCESS_TOKEN=your_admin_api_access_token
SHOPIFY_API_KEY=your_app_api_key
SHOPIFY_API_SECRET=your_app_api_secret
```

The program uses Shopify's maintained `shopifyapp` Python package for Admin GraphQL requests.

### 3. Add source/query files

For mutation workflows, place exactly one XLSX/CSV source file in:

```text
source/
```

Reusable Shopify bulk-query definitions live in:

```text
graphql/queries/
```

### 4. Run the program

```bash
python3 main.py
```

The interactive menu provides:

```text
[0] Run bulk query
[1] Update existing resources
[2] Create variants for existing products
[3] Create products and their variants
[4] Execute current working mutation
[5] Exit
```

## Mutation Workflows

### Update Existing Resources

The source key is `id`.

One source file can mix:

- Product
- ProductVariant
- MediaImage
- Collection

The Shopify GID identifies the resource type, and the source columns determine which mutation operations are needed.

Current mutation paths include:

```text
Product fields / metafields          -> productUpdate
ProductVariant fields / metafields   -> productVariantsBulkUpdate
Variant positions                    -> productVariantsBulkReorder
MediaImage fields                    -> fileUpdate
Media positions                      -> productReorderMedia
Collection fields / metafields       -> collectionUpdate
Metafield clears                     -> metafieldsDelete
```

Normal metafield values remain inside the owning Product, Variant, or Collection mutation rather than being routed through `metafieldsSet`.

### Create Variants for Existing Products

The source key is `sku`, with `productId` identifying the existing destination Product.

Rows are grouped by Product and created through `productVariantsBulkCreate` using `PRESERVE_STANDALONE_VARIANT`. Destination Product IDs are validated before mutation generation.

### Create Products and Their Variants

The source key is `sku`.

Rows are grouped into Products by `title` and created through `productSet`. The workflow separates Product and Variant fields, aggregates media, builds Product options, and checks repeated Product-level values across Variant rows for conflicts.

## Source Values and Relationships

Blank and explicit-empty values have different meanings:

| Source value | Behavior |
| --- | --- |
| blank / whitespace | leave the Shopify value unchanged |
| `null`, `[]`, `{}`, or `""` | explicitly clear/remove the value |
| anything else | set/update the value |

Falsy values such as `0` and `FALSE` remain real values. Primitive and structured values are converted according to the destination Shopify GraphQL input type rather than guessed globally from the spreadsheet text.

Some Shopify mutations require a parent Product ID even when the source row represents a child resource.

`productId` can be supplied as a helper column for ProductVariant and MediaImage rows. Variant relationships can also be resolved and validated directly from current Shopify data. MediaImage relationships use source `productId` when available and request a saved bulk-query result only when Product context cannot otherwise be resolved.

When source data, saved-query relationships, and current Shopify data provide overlapping relationship information, the program uses that information for validation. Critical conflicts or ambiguous relationships prevent mutation generation.

## Queries and Output

Reusable `.graphql` query definitions are stored under `graphql/queries/`.

Each successful query is saved in a timestamped directory:

```text
queries/<timestamp>_query/
    query_<timestamp>.graphql
    result_<timestamp>.jsonl
    result_<timestamp>.xlsx
    manifest_<timestamp>.json
```

The JSONL is the authoritative Shopify result used internally by the program.

An XLSX convenience file is generated automatically for inspection, Excel formulas/lookups, and preparation of later mutation source files. Nested objects are flattened using dot notation such as `image.url` or `inventoryItem.sku`, while lists remain compact JSON values inside cells.

Bulk queries use `groupObjects: true` so nested Shopify resources retain their `__parentId` relationships.

## Recovery, Artifacts, and Execution History

Each new mutation-preparation run replaces the current `working/` preview:

```text
working/
    manifest.json
    summary.txt
    source/
    mutations/
```

Preparing or reviewing this directory performs no Shopify writes.

When execution begins, the program verifies the working-file hashes and copies the exact preview into a permanent timestamped directory under `mutations/`. Shopify executes that preserved snapshot rather than regenerating the mutation.

Executed mutation records contain the source file, GraphQL operations, JSONL inputs, downloaded results, manifest, and summary. Failed executions are retained as well as successful ones.

If the local program is interrupted while Shopify may still be processing a BulkOperation, the Shopify operation ID is already stored in the manifest. The program can resume checking that operation later rather than creating a duplicate mutation.

`summary.txt` preserves Shopify's returned errors and, where possible, maps errors from grouped mutation inputs back to the originating XLSX/CSV row.

## Repository Structure

```text
main.py                 # CLI entry point and workflow orchestration
config.py               # environment settings and project paths

app/
    source.py           # XLSX/CSV parsing and source-value semantics
    workspace.py        # working previews, mutation history, recovery
    query_output.py     # query JSONL -> XLSX conversion
    utils.py            # general CLI/file helpers

    shopify/
        client.py       # shopifyapp GraphQL wrapper
        bulk.py         # BulkOperation execution and polling
        schema.py       # input introspection and value coercion
        relationships.py # resource and Product relationship validation

    workflows/
        helpers.py
        update_resources.py
        create_variants.py
        create_products.py

graphql/                # reusable query and mutation definitions
source/                 # current mutation source XLSX/CSV
queries/                # persistent bulk-query artifacts
working/                # current replaceable mutation preview
mutations/              # persistent executed-mutation history
tests/                  # offline regression tests
```

## Scope and Limitations

- The current implementation is optimized around a Shopify catalog-management workflow rather than a generic ETL framework.
- Mutation source data is expected to represent deliberate desired catalog changes.
- Shopify schemas and API behavior can change, so GraphQL operations and input handling may require maintenance over time.
- Saved query results can provide relationship context, but current Shopify data is used for live validation wherever the API exposes the necessary relationship.
- Query XLSX files are convenience outputs; the original Shopify JSONL remains the authoritative query artifact.
- Generated mutation previews should be reviewed on representative or high-impact updates before execution.

## Project Status

This repository is actively developed production tooling rather than a packaged Shopify application. The architecture favors explicit source data, reproducible mutation artifacts, conservative validation, and predictable bulk execution over a minimal one-command interface.
