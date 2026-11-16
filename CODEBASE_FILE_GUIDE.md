# Codebase Reference & File-by-File Implementation Guide

This document provides a comprehensive breakdown of **every file, module, and worker in the project**, detailing **what code was written**, **why it was written**, and the **key classes, methods, and functions** implemented.

---

## 1. Directory Tree Overview

```text
D:\Yash Joshi\linnworks\
├── .env                              <- Active secrets & local environment variables
├── .env.example                      <- Template for credentials (safe for Git)
├── requirements.txt                  <- Python project dependencies (requests, PyMySQL, python-dotenv, pytest)
├── HOW_TO_RUN.md                     <- User guide for running commands & DBeaver
├── IMPLEMENTATION_STATUS.md          <- Phased progress report against manager prompt
├── DEVELOPMENT_PLAN_DIAGRAMS.md      <- System architecture & flowchart diagrams
├── CODEBASE_FILE_GUIDE.md            <- (This file) Complete file-by-file code guide
├── README.md                         <- Production deployment & crontab reference guide
│
├── sql/
│   └── 001_create_tables.sql         <- MariaDB 10.6+ / MySQL 8 schema (8 tables with lw_ prefix)
│
├── fixtures/                         <- Realistic sample JSON responses (Test / Offline Mode)
│   ├── US/                           <- US mock store data
│   │   ├── products.json
│   │   ├── extended_properties.json
│   │   ├── locations.json
│   │   ├── stock_levels.json
│   │   ├── orders_open.json
│   │   ├── orders_processed.json
│   │   └── refunds.json
│   └── CA/                           <- CA mock store data
│
├── logs/
│   └── lwsync.log                    <- Rotating log file output (max 10MB, 5 backups)
│
├── lwsync/                           <- Main Python package
│   ├── __init__.py                   <- Package initializer
│   ├── __main__.py                   <- CLI entry point (run, init-db, test-live)
│   ├── config.py                     <- Central configuration & .env reader
│   ├── logger.py                     <- Dual console/file rotating logger
│   ├── db.py                         <- PyMySQL connection & auto-table creator
│   │
│   ├── client/                       <- API abstraction layer
│   │   ├── __init__.py               <- Exports client classes
│   │   ├── base.py                   <- Abstract base class (LinnworksClient)
│   │   ├── auth.py                   <- Token authentication & session caching
│   │   ├── fixture.py                <- Mock client reading from fixtures/
│   │   └── live.py                   <- Real HTTP client with rate limiter, v2 orders, & retries
│   │
│   └── sync/                         <- Core synchronization workers
│       ├── __init__.py               <- Exports sync functions
│       ├── products.py               <- Products & 8 extended properties sync worker
│       ├── inventory.py              <- Locations & physical stock levels sync worker
│       ├── orders.py                 <- Open orders & processed delta sync worker
│       ├── refunds.py                <- Refunds ledger & status state machine worker
│       └── state.py                  <- High-watermark sync state & cursor manager
│
└── tests/                            <- Automated test suite (90 passing tests)
    ├── __init__.py                   <- Test package initializer
    ├── conftest.py                   <- Shared fixtures
    ├── run_offline.py                <- Isolated runner (blocks network/DB to test offline integrity)
    ├── test_client.py                <- 10 tests for client auth, throttling, retries, fixtures
    ├── test_idempotency.py           <- 13 tests for idempotency, refund arithmetic, status transitions
    ├── test_sync_inventory.py        <- Integration tests for products, locations, and stock (US + CA)
    ├── test_sync_orders.py           <- Integration tests for open orders and refund state transitions
    └── offline/
        ├── conftest.py               <- Offline mocking harness
        └── test_orders_v2.py         <- 60 tests for v2 token pagination, rate limits, error resilience
```

---

## 2. Configuration & Infrastructure Files

### `requirements.txt`
- **Purpose**: Lists minimal, lightweight dependencies as required by the specification.
- **Dependencies**:
  - `requests>=2.31.0`: For HTTP REST API calls to Linnworks.
  - `PyMySQL>=1.1.0`: Pure Python database driver for MySQL 8 and AWS RDS MariaDB 10.6+.
  - `python-dotenv>=1.0.0`: Loads environment variables from `.env`.
  - `pytest>=8.0.0`: Automated test runner.

---

### `.env` & `.env.example`
- **Purpose**: Centralizes database parameters, regional Linnworks credentials, and runtime flags.
- **Key Settings**:
  - `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_CHARSET`: Database connection.
  - `LW_MODE`: `live` (talks to Linnworks REST API) or `fixture` (offline testing against local JSON).
  - `LW_TABLE_PREFIX`: Defaults to `lw_` to ensure client Power BI tables are untouched.
  - `LW_US_APP_ID`, `LW_US_APP_SECRET`, `LW_US_TOKEN`: Linnworks US instance credentials.
  - `LW_CA_APP_ID`, `LW_CA_APP_SECRET`, `LW_CA_TOKEN`: Linnworks CA instance credentials.
  - `LW_US_API_BASE_URL` & `LW_CA_API_BASE_URL`: Regional API base origins (e.g. `https://us-api.linnworks.net` and `https://eu-api.linnworks.net`).
  - `LW_RATE_LIMIT_RPS=2.0`: Token-bucket rate limit enforcing max 2 requests per second.
  - `LW_REQUEST_TIMEOUT=30`, `LW_MAX_RETRIES=3`, `LW_RETRY_BACKOFF=1.5`: Network resilience settings.

---

### `sql/001_create_tables.sql`
- **Purpose**: Implements MySQL 8 and MariaDB 10.6+ compatible DDL using `InnoDB`, `utf8mb4`, and UTC datetimes.
- **8 Core Tables**:
  1. `lw_products`: Natural key `(region, sku)`. Stores Linnworks GUID, titles, costs, retail prices, categories, barcodes, weight, and the 8 dedicated extended properties (`brand`, `fill_sku`, `product_line`, `master_sku`, `substitute_1`, `substitute_2`, `mfr_part_number`, `product_tag`).
  2. `lw_locations`: Warehouse locations keyed on `(region, location_id)`.
  3. `lw_stock_levels`: Physical inventory levels keyed on `(region, sku, location_id)`. Tracks `on_hand`, `allocated`, and `available`.
  4. `lw_orders`: Master orders table keyed on `(region, order_id)`. Stores order numbers, channel references, status (`open`, `parked`, `shipped`, `cancelled`, `refunded`), timestamps, full customer data, 3-line addresses, and financials.
  5. `lw_order_items`: Order line items keyed on `(region, order_id, row_id)`.
  6. `lw_order_refunds`: Partial and full refund ledger keyed on `(region, order_id, refund_id)`.
  7. `lw_pipeline_log`: Audit trail table for downstream 3PL order routing (`send`, `swap`, `hold`, `error`).
  8. `lw_sync_state`: High-watermark state tracking table storing `(region, entity)`, `last_cursor`, `last_success_at`, `run_count`, and `last_error`.

---

## 3. Core Application Code (`lwsync/`)

### `lwsync/config.py`
- **Purpose**: Parses environment variables and provides typed settings to the rest of the application.
- **Key Features**:
  - `Config.LW_REGIONAL_CONFIG`: Maps distinct `(app_id, app_secret, token)` pairs for `US` and `CA`.
  - `Config.LW_API_BASE_URLS`: Maps regional REST base URLs for v2 endpoints.
  - Parses integers, floats, booleans, and sets safe defaults for rate limits and lookback windows.

---

### `lwsync/logger.py`
- **Purpose**: Standardized, structured logging across all sync jobs.
- **Features**:
  - Colorized console output (`stdout`).
  - Rotating file handler (`logs/lwsync.log`) with a max size of 10 MB and up to 5 archived backups.

---

### `lwsync/db.py`
- **Purpose**: PyMySQL connection management and schema migration guard.
- **Key Functions**:
  - `get_db_connection()`: Creates an active PyMySQL connection using `.env` settings.
  - `get_db_cursor()`: Context manager (`with get_db_cursor() as cursor:`) providing automatic commit on success and rollback on exceptions.
  - `ensure_database_and_tables()`: Automatically creates the database if missing and runs `001_create_tables.sql` with `CREATE TABLE IF NOT EXISTS`. Never alters or drops existing data.

---

### `lwsync/__main__.py`
- **Purpose**: Command-line interface (CLI) entry point for developers and scheduled cron tasks.
- **Supported Commands**:
  - `python -m lwsync init-db`: Verifies or creates the database schema.
  - `python -m lwsync test-live`: Verifies live Linnworks credentials and probes locations for US and CA.
  - `python -m lwsync run <entity>`:
    - Entities: `all`, `products`, `locations`, `stock`, `orders`.
    - `--region <US|CA|ALL>`: Targets a specific region or both (default: `ALL`).
    - `--days <N>`: Configures a custom lookback window in days for delta orders sync.
    - `--dry-run`: Previews data that would be synced without writing to MySQL.
    - `--skip-db-init`: Skips schema verification for faster cron execution.

---

## 4. API Client Layer (`lwsync/client/`)

### `lwsync/client/base.py`
- **Purpose**: Abstract base class (`LinnworksClient`) defining the contract for both live and mock clients.
- **Contract Methods**:
  - `get_stock_items(region, page_number, page_size)`
  - `get_all_products(region, page_size)`
  - `get_locations(region)`
  - `get_stock_levels(region, location_id)`
  - `get_open_orders(region)`
  - `get_order_details(region, order_id)`
  - `get_processed_orders(region, from_date, to_date)`
  - `get_refunds(region, from_date, to_date)`

---

### `lwsync/client/auth.py`
- **Purpose**: Handles Linnworks authentication and session lifecycle management.
- **Key Classes**:
  - `LinnworksAuthSession`: Holds active session `Token`, assigned `Server` URL, and expiration datetime.
  - `LinnworksAuthManager`:
    - Calls `POST https://api.linnworks.net/api/Auth/AuthorizeByApplication`.
    - Caches session tokens in memory per region.
    - `invalidate_session(region)`: Flushes cached token on HTTP 401 Unauthorized responses.

---

### `lwsync/client/live.py`
- **Purpose**: Production HTTP client communicating with live Linnworks endpoints.
- **Key Features**:
  - **Monotonic Rate Limiting (`_throttle`)**: Enforces rate limiting per region using a monotonic clock to avoid time drift.
  - **Self-Healing Auth**: Automatically intercepts HTTP 401, refreshes session token, and retries request.
  - **Rate Limit & Backoff Handling**: Respects server `Retry-After` headers on HTTP 429, falling back to exponential backoff.
  - **v2 Orders Token-Based Pagination**:
    - `_v2_orders_url(region)`: Resolves verified regional endpoints (`https://us-api.linnworks.net/v2/orders` or `https://eu-api.linnworks.net/v2/orders`).
    - `get_orders_v2()`: Calls `GET /v2/orders` with token-based cursor (`NextSearchToken`).
    - `_collect_orders_v2()`: Traverses all token pages, detects infinite token cycles, and streams orders without arbitrary page limits.
  - **Product Catalog Ingestion**:
    - `get_all_products()`: Loops across all pages until the catalog is completely ingested.
    - `get_product_extended_properties()`: Fetches item-level custom extended properties.

---

### `lwsync/client/fixture.py`
- **Purpose**: Offline mock client reading realistic sample payloads from `fixtures/US/` and `fixtures/CA/`.
- **Behavior**:
  - Implements the complete `LinnworksClient` interface without network dependencies.
  - Supports offline testing, CI/CD validation, and local development.

---

## 5. Synchronization Workers (`lwsync/sync/`)

### `lwsync/sync/products.py`
- **Purpose**: Full catalog synchronization worker.
- **Key Logic**:
  - Ingests all products across all pages via `client.get_all_products()`.
  - Maps custom Linnworks extended properties to the 8 dedicated columns: `brand`, `fill_sku`, `product_line`, `master_sku`, `substitute_1`, `substitute_2`, `mfr_part_number`, and `product_tag`.
  - **COALESCE Extended-Property Protection**: If the extended properties API call fails or returns empty, the upsert query uses `COALESCE(VALUES(col), col)` so existing property values in MySQL are never overwritten with `NULL`.
  - Idempotent upsert keyed on natural key `(region, sku)`.

---

### `lwsync/sync/inventory.py`
- **Purpose**: Locations and physical stock levels synchronization worker.
- **Key Logic**:
  - `sync_locations()`: Ingests warehouse locations into `lw_locations`, keyed on `(region, location_id)`.
  - `sync_stock_levels()`: Ingests physical inventory into `lw_stock_levels`, keyed on `(region, sku, location_id)`.
  - Tracks `on_hand` (Stock Level), `allocated` (In Orders), and `available` (Free to Sell).
  - Updates `updated_at = CURRENT_TIMESTAMP` and updates `lw_sync_state`.

---

### `lwsync/sync/orders.py`
- **Purpose**: Order and line item synchronization engine.
- **Key Logic**:
  - `_extract_order_and_items()`: Unpacks order payloads, maps 3-line shipping/billing addresses, financial totals (`subtotal`, `tax`, `shipping_charge`, `total`), and line items.
  - **Status State Machine**:
    - Unprocessed orders map to `'open'` (or `'parked'` if `IsParked = True` or `HoldOrWait = True`).
    - Completed orders transition to `'shipped'` with `processed_date`.
  - `sync_open_orders()`: Fully refreshes all active open orders (e.g. ~13,000 orders) so no pending order is missed.
  - `sync_orders_delta()`:
    - Queries processed and updated orders within a lookback window (default: saved cursor with 10-min overlap; configurable via `--days`).
    - Performs bulk upsert into `lw_orders` keyed on `(region, order_id)` and `lw_order_items` keyed on `(region, order_id, row_id)`.
    - **In-Place Updates**: Existing orders transition their status (e.g. `parked` $\rightarrow$ `shipped`) without duplicating rows.
    - Advances high-watermark cursor in `lw_sync_state`.

---

### `lwsync/sync/refunds.py`
- **Purpose**: Refunds synchronization and automated order status transition engine.
- **Key Logic**:
  - Ingests line-level and order-level refunds into `lw_order_refunds` keyed on `(region, order_id, refund_id)`.
  - **Automatic Full Refund Detection**:
    - Calculates `SUM(amount)` of all refunds for affected orders.
    - If total refunded amount $\ge$ order total, automatically executes:
      `UPDATE lw_orders SET status = 'refunded', updated_at = CURRENT_TIMESTAMP WHERE region = %s AND order_id = %s;`
    - Leaves partially refunded orders at their current status.

---

### `lwsync/sync/state.py`
- **Purpose**: High-watermark sync state and cursor tracker.
- **Key Functions**:
  - `get_sync_cursor(region, entity)`: Retrieves `last_cursor` for delta tracking.
  - `update_sync_success(region, entity, new_cursor)`: Records `last_success_at = CURRENT_TIMESTAMP`, advances `last_cursor`, increments `run_count`, and resets `last_error = NULL`.
  - `record_sync_error(region, entity, error_message)`: Records failure details in `last_error` for monitoring and alerting.

---

## 6. Mock Fixtures Layer (`fixtures/`)

Realistic JSON payloads mimicking live Linnworks API response structures:
- `products.json`: Catalog items with SKU, Title, PurchasePrice, RetailPrice, BarcodeNumber, Weight.
- `extended_properties.json`: Extended properties matching the 8 target columns.
- `locations.json`: Warehouses (`US | AJC`, `CA | AJC`, etc.).
- `stock_levels.json`: Item inventory levels (`Quantity`, `InOrders`, `Available`).
- `orders_open.json`: Multi-line customer orders with full addresses, tax, shipping, and discount.
- `orders_processed.json`: Completed/shipped orders with tracking numbers and dispatch dates.
- `refunds.json`: Partial item-level and full order-level refund objects.

---

## 7. Automated Test Suite (`tests/`)

The repository contains **90 automated tests** covering unit logic, integration workflows, and offline resilience.

### Test Files Breakdown:
1. **`tests/test_client.py` (10 tests)**:
   - Validates `FixtureClient` parsing for products, extended properties, locations, stock, orders, and refunds.
   - Tests `LinnworksAuthManager` session caching and expiration.
   - Verifies `LiveClient` 401 re-authentication and 429 backoff retries.
2. **`tests/test_idempotency.py` (13 tests)**:
   - Verifies repeated extraction produces zero drift or mutation.
   - Ensures line-item `row_id` uniqueness.
   - Tests order status transitions (`open`, `parked`, `shipped`).
   - Verifies financial arithmetic for partial refunds, cumulative full refunds, and over-refunds.
   - Confirms cross-region SKU independence between US and CA.
   - Validates all 8 extended properties and ensures unknown properties are safely ignored.
3. **`tests/test_sync_inventory.py` (6 tests)**:
   - Tests products, locations, and stock level sync for both `US` and `CA`.
   - Validates dry-run execution and verifies database idempotency (no row inflation on re-runs).
4. **`tests/test_sync_orders.py` (2 tests)**:
   - Tests open orders ingestion, item mapping, and refund status transition logic.
5. **`tests/offline/test_orders_v2.py` (60 tests)**:
   - Offline verification of v2 orders endpoints across US and CA origins.
   - Tests token-based pagination, absence of 60-page limits, token cycle failure prevention, rate limit cooldowns, and cursor advancement safety.
6. **`tests/run_offline.py`**:
   - Test harness that strictly blocks socket and database connections to guarantee test suite independence.

### How to Run Tests:
```powershell
# Run the full test suite
.\.venv\Scripts\python.exe -m pytest -v

# Run only the isolated offline test suite (no DB or network required)
.\.venv\Scripts\python.exe tests/run_offline.py
```
