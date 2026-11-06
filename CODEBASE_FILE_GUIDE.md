# Codebase Reference & File-by-File Implementation Guide

This document provides a detailed breakdown of **every file in the project**, explaining **what code was written**, **why it was written**, and the **key classes and functions** contained inside.

---

## 1. Directory Tree Overview

```text
D:\Yash Joshi\linnworks\
├── .env                              <- Active secrets & local environment variables
├── .env.example                      <- Template for credentials (safe for Git)
├── requirements.txt                  <- Python project dependencies
├── HOW_TO_RUN.md                     <- User guide for running commands & DBeaver
├── IMPLEMENTATION_STATUS.md          <- Phased progress report against manager prompt
├── DEVELOPMENT_PLAN_DIAGRAMS.md      <- System architecture & flowchart diagrams
├── CODEBASE_FILE_GUIDE.md            <- (This file) Complete file-by-file code guide
│
├── sql/
│   └── 001_create_tables.sql         <- MariaDB / MySQL 8 schema (8 tables with lw_ prefix)
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
│   └── lwsync.log                    <- Rotating log file output
│
├── lwsync/                           <- Main Python package
│   ├── __init__.py                   <- Package initializer
│   ├── __main__.py                   <- CLI entry point (run, init-db, test-live)
│   ├── config.py                     <- Central configuration & .env reader
│   ├── logger.py                     <- Dual console/file rotating logger
│   ├── db.py                         <- PyMySQL connection & auto-table creator
│   │
│   └── client/                       <- API abstraction layer
│       ├── __init__.py               <- Exports client classes
│       ├── base.py                   <- Abstract base class (LinnworksClient)
│       ├── auth.py                   <- Token authentication & session caching
│       ├── fixture.py                <- Mock client reading from fixtures/
│       └── live.py                   <- Real HTTP client with rate limiter & retries
│
└── tests/
    ├── __init__.py                   <- Test package initializer
    └── test_client.py                <- 10 unit tests for client, auth, throttling, retries
```

---

## 2. Configuration & Infrastructure Files

### `requirements.txt`
- **Purpose**: Lists minimal, lightweight dependencies as requested by the manager prompt.
- **Contents**:
  - `requests>=2.31.0`: For HTTP communication with the Linnworks REST API.
  - `PyMySQL>=1.1.0`: Pure Python MySQL client for connecting to MySQL 8 / AWS RDS MariaDB.
  - `python-dotenv>=1.0.0`: Loads variables from `.env` into `os.environ`.
  - `pytest>=8.0.0`: Test runner for automated unit and integration tests.

---

### `.env` & `.env.example`
- **Purpose**: Centralizes database credentials, application credentials, and execution flags without hardcoding secrets in Python code.
- **Key Variables Configured**:
  - `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`: Database connection settings.
  - `LW_MODE`: Set to `live` (to talk to Linnworks) or `fixture` (for local offline testing).
  - `LW_TABLE_PREFIX`: Set to `lw_` to prevent collisions with client Power BI tables.
  - `LW_US_APP_ID`, `LW_US_APP_SECRET`, `LW_US_TOKEN`: Dedicated credentials for the BatteryClerk US instance.
  - `LW_CA_APP_ID`, `LW_CA_APP_SECRET`, `LW_CA_TOKEN`: Dedicated credentials for the BatteryClerk CA instance.
  - `LW_RATE_LIMIT_RPS=2.0`: Enforces safe rate limiting (120 calls/min) within Linnworks' 150 calls/min limit.

---

### `sql/001_create_tables.sql`
- **Purpose**: Creates all **8 core tables** with MariaDB 10.6+ and MySQL 8 compatibility using `InnoDB`, `utf8mb4`, and UTC datetimes.
- **Tables Created**:
  1. `lw_products`: Natural key `(region, sku)`. Stores GUIDs, pricing, and 8 extended property columns (`brand`, `fill_sku`, `product_line`, `master_sku`, `substitute_1`, `substitute_2`, `mfr_part_number`, `product_tag`).
  2. `lw_locations`: Warehouse locations keyed on `(region, location_id)`.
  3. `lw_stock_levels`: Physical inventory levels keyed on `(region, sku, location_id)`.
  4. `lw_orders`: Master orders table keyed on `(region, order_id)`. Stores customer, shipping/billing addresses, status, and financials.
  5. `lw_order_items`: Order line items keyed on `(region, order_id, row_id)`.
  6. `lw_order_refunds`: Partial and full refund ledger keyed on `(region, order_id, refund_id)`.
  7. `lw_pipeline_log`: Audit trail for future 3PL order routing (`send`, `swap`, `hold`, `error`).
  8. `lw_sync_state`: High-watermark tracking table storing `(region, entity)`, `last_cursor`, `last_success_at`, `run_count`, and `last_error`.

---

## 3. Core Application Code (`lwsync/`)

### `lwsync/config.py`
- **Purpose**: Parses `.env` variables and provides typed settings to the rest of the application.
- **Key Structure**:
  - `Config.LW_REGIONAL_CONFIG`: Maps distinct `(app_id, app_secret, token)` pairs for `US` and `CA`.
  - Exposes database connection parameters, rate limits, timeouts, and table prefixes.

---

### `lwsync/logger.py`
- **Purpose**: Provides standardized, structured logging across all jobs.
- **Features**:
  - Outputs colorized logs to the terminal (`stdout`).
  - Writes to a rotating file (`logs/lwsync.log`) with a max size of 10 MB and up to 5 archived backups.

---

### `lwsync/db.py`
- **Purpose**: PyMySQL connection management and automatic table lifecycle handling.
- **Key Functions**:
  - `get_db_connection()`: Opens and returns an active PyMySQL connection.
  - `get_db_cursor()`: A Python context manager (`with get_db_cursor() as cursor:`) that automatically commits on success and rolls back transactions on error.
  - `ensure_database_and_tables()`:
    - Runs `CREATE DATABASE IF NOT EXISTS linnworks_sync`.
    - Parses and executes `sql/001_create_tables.sql`.
    - **Safe & Idempotent**: If tables already exist, it does not overwrite or drop them. If they are missing, it creates them.

---

### `lwsync/__main__.py`
- **Purpose**: Command-line interface (CLI) entry point for developers and scheduled cron tasks.
- **Available Commands**:
  - `python -m lwsync init-db`: Explicitly checks and creates database and tables.
  - `python -m lwsync test-live`: Authenticates against real Linnworks US & CA APIs and tests location retrieval.
  - `python -m lwsync run <entity> --region <US|CA|ALL> [--dry-run]`: Runs sync jobs. Automatically calls `ensure_database_and_tables()` before syncing to guarantee schema integrity.

---

## 4. API Client Layer (`lwsync/client/`)

### `lwsync/client/base.py`
- **Purpose**: Abstract base class (`LinnworksClient`) defining the contract for both live and mock clients.
- **Key Abstract Methods**:
  - `get_stock_items(region, page_number, page_size)`
  - `get_locations(region)`
  - `get_stock_levels(region, location_id)`
  - `get_open_orders(region)`
  - `get_order_details(region, order_id)`
  - `get_processed_orders(region, from_date, to_date)`
  - `get_refunds(region, from_date, to_date)`

---

### `lwsync/client/auth.py`
- **Purpose**: Handles Linnworks authentication and session caching.
- **Key Classes**:
  - `LinnworksAuthSession`: Holds the active `Token`, assigned `Server` URL, and expiration timestamp.
  - `LinnworksAuthManager`:
    - Calls `POST https://api.linnworks.net/api/Auth/AuthorizeByApplication` using region-specific `(AppId, AppSecret, Token)` pairs.
    - Caches session tokens in memory for fast reuse.
    - `invalidate_session(region)`: Flushes expired tokens when an HTTP 401 response is encountered.

---

### `lwsync/client/live.py`
- **Purpose**: Production HTTP client for live Linnworks operations.
- **Key Features**:
  - **Rate Limiting (`_throttle`)**: Enforces `Config.RATE_LIMIT_RPS` (e.g., 2.0 requests per second) per region to prevent 429 errors.
  - **Self-Healing Auth (401 Handler)**: If an auth token expires mid-sync, it automatically invalidates the cache, calls `AuthorizeByApplication`, and retries.
  - **Exponential Backoff**: Automatically catches HTTP 429 and 5xx responses and retries up to `MAX_RETRIES` with increasing sleep intervals.

---

### `lwsync/client/fixture.py`
- **Purpose**: Offline mock client for testing and development without live credentials or internet access.
- **Behavior**:
  - Reads realistic mock JSON responses from `fixtures/US/` and `fixtures/CA/`.
  - Implements the exact same methods as `LiveClient`, allowing the sync engine to run seamlessly in either mode.

---

## 5. Mock Fixtures (`fixtures/`)

Realistic JSON payloads mimicking live Linnworks API responses:
- `products.json`: Catalog items with SKU, Title, PurchasePrice, RetailPrice, BarcodeNumber, Weight.
- `extended_properties.json`: Extended product properties matching the 8 target columns.
- `locations.json`: Warehouses (`US | AJC`, `CA | AJC`, etc.).
- `stock_levels.json`: Item inventory levels (`Quantity`, `InOrders`, `Available`).
- `orders_open.json`: Multi-line customer orders with full addresses, tax, shipping, and discount.
- `orders_processed.json`: Completed/shipped orders with tracking numbers.
- `refunds.json`: Partial item-level and full order-level refund objects.

---

## 6. Test Suite (`tests/`)

### `tests/test_client.py`
- **Purpose**: Automated regression tests executed via `pytest`.
- **Test Cases Covered (10 Tests)**:
  1. `test_fixture_client_products`: Verifies product parsing from mock files.
  2. `test_fixture_client_locations`: Verifies location retrieval.
  3. `test_fixture_client_stock_levels`: Verifies stock level loading.
  4. `test_fixture_client_orders`: Verifies open and processed orders parsing.
  5. `test_fixture_client_refunds`: Verifies refund parsing.
  6. `test_auth_manager_session_caching`: Verifies session token caching.
  7. `test_live_client_retry_on_429`: Tests exponential backoff on rate limits.
  8. `test_live_client_reauth_on_401`: Tests token refresh when session expires.
  9. `test_live_client_throttling`: Validates requests-per-second interval delays.
  10. `test_regional_sku_isolation`: Tests that US and CA instances maintain separate SKU instances.

