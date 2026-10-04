# Shopify Data Manager

**Bulk query, validate, and update Shopify catalog data using prepared XLSX/CSV files**

The purpose of this program is to manage large-scale Shopify catalog updates that are cumbersome or impossible to perform through Shopify's native CSV tools.

The program uses a spreadsheet-first workflow. Data can be prepared manually, in Excel, or by another data-processing program. Shopify Data Manager validates the source against Shopify's current GraphQL schema and resource relationships, generates the required bulk mutation files, and lets you inspect the exact mutation before anything is executed.

Mutation preparation and Shopify execution are deliberately separated. Preparing a mutation never writes to Shopify.

## How It Works

```text
Prepared XLSX / CSV source
        ↓
Source + Shopify validation
        ↓
Mutation plan
        ↓
working/ preview
        ↓
Review or execute
        ↓
Permanent mutations/ history
        ↓
Shopify Bulk Operations API
```

Bulk queries are independent from mutation workflows and are retained under `queries/` as the original JSONL plus an automatically generated XLSX file.

## Key Features

- Bulk Shopify Admin GraphQL queries with automatic XLSX output
- Mixed Product, ProductVariant, MediaImage, and Collection updates from one source file
- Shopify schema introspection for valid fields, data types, and value coercion
- Owner-resource Product, Variant, and Collection metafield updates
- Explicit metafield deletion without any Shopify writes during mutation preparation
- Clear handling of blank, clear, and real falsy values such as `0` and `FALSE`
- Live validation of existing Shopify resource IDs
- Variant-to-Product and MediaImage-to-Product relationship validation
- Optional `productId` helper column for child resources
- Query-based MediaImage relationship resolution only when needed
- Safe inspectable mutation previews before execution
- Persistent mutation history, error summaries, and interrupted-operation recovery
- Product and Variant creation workflows

## Quick Start

### Requirements

- Python 3.10+
- Shopify Admin API credentials with access to the required catalog resources

### Install

```bash
pip3 install -r requirements.txt
cp .env.example .env
```

Configure `.env`:

```env
SHOPIFY_SHOP=your-store.myshopify.com
SHOPIFY_API_VERSION=2026-04
SHOPIFY_ACCESS_TOKEN=your_admin_api_access_token
SHOPIFY_API_KEY=your_app_api_key
SHOPIFY_API_SECRET=your_app_api_secret
```

Run:

```bash
python3 main.py
```

Main menu:

```text
[0] Run bulk query
[1] Update existing resources
[2] Create variants for existing products
[3] Create products and their variants
[4] Execute current working mutation
[5] Exit
```

## Core Workflows

### Bulk Queries

Reusable query definitions live in:

```text
graphql/queries/
```

Each query run is saved under `queries/` with:

- the GraphQL query
- the authoritative Shopify JSONL result
- an XLSX convenience file
- a manifest

Nested JSON objects are flattened into dot-notation XLSX columns such as `image.url` or `inventoryItem.sku`. Lists remain compact JSON in the cell.

The original JSONL remains the program's authoritative query artifact.

### Update Existing Resources

The source key is `id`.

A single file can mix:

- Product
- ProductVariant
- MediaImage
- Collection

The resource GID determines the mutation path.

Normal updates use the resource owner's mutation:

- Product → `productUpdate`
- ProductVariant → `productVariantsBulkUpdate`
- Variant position → `productVariantsBulkReorder`
- MediaImage fields → `fileUpdate`
- Media position → `productReorderMedia`
- Collection → `collectionUpdate`

Normal metafield values remain inside Product, Variant, or Collection mutations rather than being routed through `metafieldsSet`.

Explicit metafield clears are generated as previewed `metafieldsDelete` operations.

### Create Variants for Existing Products

The source key is `sku`.

Each row includes `productId`, and rows are grouped by Product for `productVariantsBulkCreate`.

Destination Product IDs are live-validated before mutation generation.

### Create Products and Their Variants

The source key is `sku`.

Rows are grouped into Products by `title` and created through `productSet`.

The workflow separates Product and Variant fields, builds Product options, aggregates media, and warns when repeated Product-level values conflict across Variant rows.

## Validation and Safety

### Resource and Relationship Validation

Existing resource IDs are validated against Shopify before a working mutation is created.

`productId` can be supplied as a helper column for ProductVariant and MediaImage rows.

Variant parent Products are validated directly from current Shopify data.

MediaImage parent context comes from `productId` when supplied. If required context is still missing, the program asks for a saved relationship query and validates the resolved Product against current Shopify data.

If a query is selected, its relationship data is also used as additional validation evidence.

Conflicting or ambiguous source, query, and live Shopify relationships produce a preflight error.

### Blank and Clear Values

Source cells have three meanings:

| Source value | Behavior |
| --- | --- |
| blank / whitespace | leave Shopify unchanged |
| `null`, `[]`, `{}`, or `""` | explicitly clear/remove |
| anything else | set/update |

Falsy values such as `0` and `FALSE` remain real values.

Input conversion is Shopify-schema-aware, so values such as `FALSE`, integers, lists, objects, and JSON are converted only when the destination GraphQL field expects that type.

### Preflight

Preflight errors prevent creation of an executable `working/` mutation.

Examples include:

- nonexistent or incorrect Shopify resource IDs
- relationship conflicts
- unresolved MediaImage parents
- invalid field values
- unsupported clear operations

Warnings are non-blocking.

## Working Preview and Execution History

Every mutation-preparation run replaces `working/`.

A successful preview contains the exact source, GraphQL documents, and JSONL mutation files that would be executed.

```text
working/
├── manifest.json
├── summary.txt
├── source/
└── mutations/
```

Choosing to leave the mutation for review performs no Shopify writes.

When execution begins:

1. working-file hashes are verified
2. the exact preview is copied into a timestamped `mutations/` folder
3. `working/` is consumed
4. Shopify executes the permanent snapshot
5. results, errors, and operation metadata are saved

Failed executions are preserved as well as successful ones.

If the program is interrupted while Shopify may still be processing a BulkOperation, the saved operation ID allows the program to resume checking it later rather than creating a duplicate operation.

`summary.txt` preserves Shopify's original errors and, when possible, maps grouped mutation errors back to the original spreadsheet row.

## Repository Structure

```text
shopify-data-manager/
├── main.py
├── config.py
│
├── app/
│   ├── source.py
│   ├── workspace.py
│   ├── query_output.py
│   ├── utils.py
│   │
│   ├── shopify/
│   │   ├── client.py
│   │   ├── bulk.py
│   │   ├── schema.py
│   │   └── relationships.py
│   │
│   └── workflows/
│       ├── helpers.py
│       ├── update_resources.py
│       ├── create_variants.py
│       └── create_products.py
│
├── graphql/
├── source/
├── queries/
├── working/
├── mutations/
└── tests/
```

Application code lives under `app/`, while source, query, working, and execution-history folders remain separate at the repository root.

## Tests

Install development dependencies:

```bash
pip3 install -r requirements-dev.txt
```

Run:

```bash
pytest
```

The regression suite covers source semantics, Shopify field coercion, mixed-resource updates, metafields, relationship validation, resource ID validation, creation workflows, query XLSX output, mutation previews, recovery, and result summaries.

## Scope and Limitations

This program is designed for controlled catalog-management workflows where source data has already been intentionally prepared.

It is not intended to make merchandising decisions, infer arbitrary catalog relationships, or replace Shopify Admin for normal manual editing.

The source XLSX/CSV should represent deliberate desired catalog changes, and generated mutations should be reviewed when appropriate.

## Project Status

The initial version is actively used and tested against a production Shopify catalog.

Current priorities are reliability, conservative validation, predictable mutation behavior, and keeping the spreadsheet-to-Shopify workflow straightforward.