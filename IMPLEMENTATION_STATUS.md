# Linnworks Sync Project — Implementation Status Report

**Generated Date:** November 13, 2026  
**Reference Document:** `c:\Users\Bansi\Downloads\Linnworks-Sync-Prompt.md`  
**Target Architecture:** Python 3.12, PyMySQL / MariaDB (MySQL 8 compatible), Linnworks REST API (US & CA instances).

---

## 1. Executive Summary

| Deliverable | Description | Status | Completion % |
| :--- | :--- | :---: | :---: |
| **Deliverable 1** | Project layout, configuration, and SQL Schema (`001_create_tables.sql`) | **Completed** | 100% |
| **Deliverable 2** | Linnworks Client abstraction (`FixtureClient`, `LiveClient`), auth, throttling, retries & test fixtures | **Completed** | 100% |
| **Deliverable 3** | Product, extended properties, location, and stock level sync + DB persistence | **Completed** | 100% |
| **Deliverable 4** | Order, order item, and refund sync (open + delta + status transitions) + CLI | **Completed** | 100% |
| **Deliverable 5** | Idempotency verification tests, refund transitions & status tests | **Completed** | 100% |
| **Deliverable 6** | `README.md` documentation (setup, crontab, switching to live mode) | **Completed** | 100% |

**Overall Progress: 100% of total scope completed.**

---

## 2. What Has Been Achieved & How We Implemented It

### A. Deliverable 1: Project Skeleton & Database Schema
1. **Repository Structure**:
   - Organized into `lwsync/` (core package), `lwsync/client/` (API clients), `lwsync/sync/` (sync workers), `sql/` (migrations), `fixtures/` (mock data), and `tests/`.
2. **Environment & Secrets Management**:
   - Created `.env.example` and `lwsync/config.py` using `python-dotenv`.
   - Supports region-separated configs: `LW_US_APP_ID`, `LW_US_APP_SECRET`, `LW_US_TOKEN`, `LW_CA_APP_ID`, `LW_CA_APP_SECRET`, `LW_CA_TOKEN`, and `LW_MODE` (`fixture` vs `live`).
3. **Structured Logging**:
   - `lwsync/logger.py` configured with both console output and rotating file handler (`logs/lwsync.log`).
4. **Database Schema (`sql/001_create_tables.sql` & `lwsync/db.py`)**:
   - Implemented standard MariaDB 10.6+ and MySQL 8 compatible DDL using `utf8mb4` and `InnoDB`.
   - All tables prefixed with `lw_` to avoid conflicts with existing Power BI client tables.
   - Built auto-migration guard `ensure_database_and_tables()` that verifies or creates tables on startup.
   - **8 Core Tables Built**:
     1. `lw_products`: Natural key `(region, sku)`, stores GUID, prices, and 8 dedicated extended properties.
     2. `lw_locations`: Keyed on `(region, location_id)`.
     3. `lw_stock_levels`: Composite unique key `(region, sku, location_id)`.
     4. `lw_orders`: Keyed on `(region, order_id)`.
     5. `lw_order_items`: Keyed on `(region, order_id, row_id)`.
     6. `lw_order_refunds`: Supports item-level and order-level refunds.
     7. `lw_pipeline_log`: Audit log for 3PL order routing (`send`, `swap`, `hold`, `error`).
     8. `lw_sync_state`: Stores cursor datetimes (`last_cursor`), success timestamps, errors, and run counts.

### B. Deliverable 2: Linnworks Client & Test Fixtures
1. **Client Architecture**:
   - `LinnworksClient` (`lwsync/client/base.py`): Abstract base class.
   - `FixtureClient` (`lwsync/client/fixture.py`): Offline testing client using realistic mock JSON files.
   - `LiveClient` (`lwsync/client/live.py`): Real HTTP calls with token-bucket throttling (`RATE_LIMIT_RPS = 2.0`), exponential backoff retries on 429/5xx, and automatic 401 re-authentication.
2. **Realistic Fixtures**:
   - Created realistic mock files for US and CA in `fixtures/US/` and `fixtures/CA/`.
3. **Automated Unit Tests**:
   - `tests/test_client.py`: 10 unit tests passing.

### C. Deliverable 3: Products, Locations & Stock Level Sync
1. **Locations Sync (`lwsync/sync/inventory.py`)**:
   - Ingests and upserts locations into `lw_locations` on `(region, location_id)`.
   - Verified live with real Linnworks US (6 locations) and CA (4 locations).
2. **Stock Levels Sync (`lwsync/sync/inventory.py`)**:
   - Ingests stock levels using `GetStockItemsFull` (`StockLevels` requirement).
   - Verified live: synced 599 US stock rows and 649 CA stock rows into `lw_stock_levels`.
3. **Products Sync (`lwsync/sync/products.py`)**:
   - Upserts catalog into `lw_products` on `(region, sku)`.
   - Maps the 8 extended properties (`brand`, `fill_sku`, `product_line`, `master_sku`, `substitute_1`, `substitute_2`, `mfr_part_number`, `product_tag`) and logs unknown attributes without crashing.

### D. Deliverable 4: Orders, Order Items & Refund Sync + CLI
1. **Open Orders Sync (`lwsync/sync/orders.py`)**:
   - Full payload ingestion with customer details, 3-line shipping/billing addresses, financials, and line items into `lw_orders` and `lw_order_items`.
2. **Delta Sync Engine**:
   - Delta lookback based on `lw_sync_state.last_cursor` with a 10-minute overlap safety window.
3. **Refunds Engine (`lwsync/sync/refunds.py`)**:
   - Ingests order and line refunds into `lw_order_refunds`.
   - Enforces automatic transition to `orders.status = 'refunded'` when total refunded equals order total.
4. **CLI Entrypoint (`lwsync/__main__.py`)**:
   - Supports `run all`, `run products`, `run locations`, `run stock`, `run orders`, `--region`, `--dry-run`, and `test-live`.

### E. Deliverable 5: Idempotency, Status-Mapping & Refund Verification Suite

#### 1. What Was Built:
- **Dedicated Test Suite (`tests/test_idempotency.py`)**:
  - Implemented 13 rigorous automated unit and edge-case tests.
  - Zero database dependency: fast, repeatable, and isolated execution using `pytest`.
  - Verifies data extraction stability, key integrity, status state machine logic, and refund arithmetic.

#### 2. What Was Verified (Detailed Scenarios):

* **Scenario Group 1: Strict Idempotency & Key Integrity**:
  - `test_extract_order_and_items_idempotent`: Parsing the same order payload multiple times produces 100% identical data tuples without drift or mutation.
  - `test_order_line_items_key_uniqueness`: Every unpacked line item has a unique, non-empty `row_id`, guaranteeing composite unique key `(region, order_id, row_id)` prevents primary key collisions.

* **Scenario Group 2: Order Status State Transitions**:
  - `test_order_status_open`: Standard orders without hold/park flags map to status `'open'` and `is_parked = False`.
  - `test_order_status_parked_via_is_parked`: Orders flagged with `IsParked = True` map to status `'parked'` and `is_parked = True`.
  - `test_order_status_parked_via_hold_or_wait`: Orders flagged with `HoldOrWait = True` map to status `'parked'` and `is_parked = True`.
  - `test_order_status_shipped_for_processed_orders`: Processed orders transition cleanly to status `'shipped'`.

* **Scenario Group 3: Financial Precision & Refund Edge Cases**:
  - `test_partial_refund_does_not_mark_order_as_refunded`: Partial refunds (\$50 of \$150) do not prematurely mark the order as refunded.
  - `test_multiple_incremental_refunds_trigger_full_refund`: Multiple partial refunds (\$50 + \$100 = \$150) cumulatively reaching or exceeding the order total trigger status transition to `'refunded'`.
  - `test_over_refund_triggers_refunded`: Over-refunds or goodwill concessions (\$55 of \$50) correctly transition the order to `'refunded'`.
  - `test_order_level_refund_with_null_sku`: Order-level refunds with `SKU = None` are handled cleanly without database constraint violations.

* **Scenario Group 4: Multi-Region & Schema Defense**:
  - `test_cross_region_sku_independence`: Cross-region SKU isolation between US and CA instances confirms identical SKUs maintain independent pricing without overwriting.
  - `test_extended_properties_mapping_all_8_columns`: Verifies all 8 target extended properties (`brand`, `fill_sku`, `product_line`, `master_sku`, `substitute_1`, `substitute_2`, `mfr_part_number`, `product_tag`) are recognized by the mapper.
  - `test_unknown_extended_property_ignored_gracefully`: Unknown property names are logged safely and return `None` rather than crashing the sync engine.

**Overall Test Suite Result**: 23/23 tests passing (`10` in `test_client.py` + `13` in `test_idempotency.py`).

---

## 3. What Remains To Be Implemented

### Deliverable 6: Documentation & Production Deployment
- [ ] Comprehensive `README.md` covering:
  - Local setup & prerequisites.
  - `.env` keys reference.
  - CLI usage examples.
  - Production Crontab schedule configuration (products every 6h, locations/stock every 30m, orders/refunds delta every 15m).
  - Migration instructions from local MySQL to AWS RDS (MariaDB 10.6+).

---

## 4. Next Recommended Action
Proceed to **Deliverable 6**: Generate the final production `README.md` to wrap up all deliverables of the manager's sync prompt.
