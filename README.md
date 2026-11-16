# BatteryClerk Linnworks → MySQL Data Sync

Production data synchronization service for **BatteryClerk**, replicating order, product, inventory, and refund data from **two distinct Linnworks instances (US and CA)** into a MySQL 8 / AWS RDS MariaDB 10.6+ database.

The normalized schema powers downstream business intelligence reporting in **Power BI** and provides the foundational inventory data for the automated **Extensiv 3PL order fulfillment pipeline**.

---

## Table of Contents
1. [Architecture & Features](#1-architecture--features)
2. [Prerequisites & System Requirements](#2-prerequisites--system-requirements)
3. [Installation & Setup](#3-installation--setup)
4. [Configuration Reference (`.env`)](#4-configuration-reference-env)
5. [Database Schema (The 8 Tables)](#5-database-schema-the-8-tables)
6. [CLI Usage & Job Execution](#6-cli-usage--job-execution)
7. [Production Scheduling (Crontab Example)](#7-production-scheduling-crontab-example)
8. [Switching Between Fixture Mode and Live API](#8-switching-between-fixture-mode-and-live-api)
9. [Running Automated Tests](#9-running-automated-tests)
10. [Troubleshooting & FAQ](#10-troubleshooting--faq)

---

## 1. Architecture & Features

- **Multi-Tenant Regional Keying**: Both `US` and `CA` Linnworks accounts are ingested into unified tables, strictly partitioned by a `region` column. Natural keys (e.g. `(region, sku)`, `(region, order_id)`) allow identical SKUs across US and CA to maintain independent pricing, inventory, and identifiers.
- **Strict Idempotency**: All writes execute atomic bulk upserts (`INSERT ... ON DUPLICATE KEY UPDATE`). Re-running a sync multiple times produces zero row count drift and zero duplicate records.
- **Table Name Isolation (`lw_`)**: All 8 tables use the `lw_` prefix to prevent collisions with pre-existing client tables or Power BI datasets.
- **Dual-Mode Client Strategy**:
  - `LW_MODE=fixture`: Runs offline against realistic mock JSON responses (`fixtures/US/`, `fixtures/CA/`) without API keys.
  - `LW_MODE=live`: Direct HTTP integration with Linnworks REST API.
- **Resilient API Architecture**:
  - Token-bucket rate limiting (`LW_RATE_LIMIT_RPS=2.0`, or 120 calls/min, staying safely below Linnworks' 150 calls/min limit).
  - Self-healing auth (catches HTTP 401, re-authenticates via `AuthorizeByApplication`, and transparently retries).
  - Exponential backoff retry logic on HTTP 429 and 5xx server errors.
- **Incremental Delta Sync with Safety Overlap**: Orders use cursor tracking (`last_cursor` in `lw_sync_state`) with a configurable **10-minute lookback overlap window** to guarantee no orders changed mid-sync are missed.
- **Refund State Machine**: Evaluates cumulative refunds against order totals; automatically marks `lw_orders.status = 'refunded'` when an order is fully refunded.

---

## 2. Prerequisites & System Requirements

- **Python**: `3.12+`
- **Database**:
  - Local Development: MySQL 8.0+ or MariaDB 10.6+ (installed locally or via Docker).
  - Production Target: AWS RDS MariaDB 10.6+ (MySQL-compatible).
- **Network**: Outbound HTTPS (Port 443) access to `api.linnworks.net`, `us-ext.linnworks.net`, and `eu-ext.linnworks.net`.

---

## 3. Installation & Setup

### Step 1: Clone Repository & Create Virtual Environment
```bash
git clone https://github.com/yashjoshiordex/Linnworks.git
cd Linnworks

# Create Python 3.12 virtual environment
python -m venv .venv

# Activate virtual environment
# Windows (PowerShell):
.\.venv\Scripts\Activate.ps1
# Linux / macOS:
source .venv/bin/activate
```

### Step 2: Install Dependencies
```bash
pip install --upgrade pip
pip install -r requirements.txt
```

### Step 3: Configure Environment Variables
Copy `.env.example` to `.env`:
```bash
# Windows (PowerShell):
Copy-Item .env.example .env
# Linux / macOS:
cp .env.example .env
```
Open `.env` and fill in your database credentials and Linnworks application tokens.

### Step 4: Initialize Database & Verify Tables
Run the database auto-initialization command:
```bash
python -m lwsync init-db
```
*This verifies connection to MySQL, creates the database if missing, and executes `sql/001_create_tables.sql` using `CREATE TABLE IF NOT EXISTS`. Existing tables are left untouched.*

---

## 4. Configuration Reference (`.env`)

| Variable | Default | Description |
| :--- | :--- | :--- |
| `DB_HOST` | `127.0.0.1` | MySQL / MariaDB host address. |
| `DB_PORT` | `3306` | MySQL port. |
| `DB_NAME` | `linnworks_sync` | Target database name. |
| `DB_USER` | `root` | Database username. |
| `DB_PASSWORD` | `""` | Database password. |
| `DB_CHARSET` | `utf8mb4` | Character set (supports full unicode/emojis). |
| `LW_MODE` | `fixture` | Set to `live` for real API calls, or `fixture` for offline mock testing. |
| `LW_TABLE_PREFIX` | `lw_` | Table prefix to isolate tables from Power BI datasets. |
| `LW_US_APP_ID` | - | Linnworks Application ID for BatteryClerk US. |
| `LW_US_APP_SECRET` | - | Linnworks Application Secret for BatteryClerk US. |
| `LW_US_TOKEN` | - | Linnworks Installation Token for BatteryClerk US. |
| `LW_CA_APP_ID` | - | Linnworks Application ID for BatteryClerk CA. |
| `LW_CA_APP_SECRET` | - | Linnworks Application Secret for BatteryClerk CA. |
| `LW_CA_TOKEN` | - | Linnworks Installation Token for BatteryClerk CA. |
| `LW_RATE_LIMIT_RPS` | `2.0` | Maximum requests per second per regional client (120 calls/min). |
| `LW_ORDERS_RATE_LIMIT_RPS` | `0.5` | Pacing for `/v2/orders` endpoint (30 calls/min; 1 req every 2.0s). |
| `LW_RATE_LIMIT_COOLDOWN` | `60` | Fallback cooldown period in seconds on HTTP 429 without Retry-After. |
| `LW_REQUEST_TIMEOUT`| `30` | HTTP request timeout in seconds. |
| `LW_MAX_RETRIES` | `3` | Maximum retry attempts for 429/5xx errors. |
| `LW_RETRY_BACKOFF` | `1.5` | Base backoff factor in seconds for exponential retries. |
| `LW_ORDER_DELTA_OVERLAP_MINUTES` | `10` | Safety overlap window (in minutes) applied to delta cursors. |
| `LW_LOG_LEVEL` | `INFO` | Logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`). |
| `LW_LOG_DIR` | `logs` | Directory for rotating log files. |

---

## 5. Database Schema (The 8 Tables)

All tables use `ENGINE=InnoDB`, `DEFAULT CHARSET=utf8mb4`, and store datetimes in **UTC**.

1. **`lw_products`**: Catalog items keyed on `(region, sku)`. Stores prices, weight, barcode, and the **8 dedicated extended properties**:
   - `brand`, `fill_sku`, `product_line`, `master_sku`, `substitute_1`, `substitute_2`, `mfr_part_number`, `product_tag`.
2. **`lw_locations`**: Warehouse fulfillment centers keyed on `(region, location_id)`.
3. **`lw_stock_levels`**: Physical inventory levels keyed on `(region, sku, location_id)`. Tracks `on_hand`, `allocated`, and `available`.
4. **`lw_orders`**: Master orders keyed on `(region, order_id)`. Tracks channel reference, status (`open`, `parked`, `shipped`, `cancelled`, `refunded`), dates, customer details, 3-line shipping/billing addresses, monetary totals (`DECIMAL(12, 4)`), shipping carrier, and tracking numbers.
5. **`lw_order_items`**: Line items keyed on `(region, order_id, row_id)`.
6. **`lw_order_refunds`**: Order-level and item-level refund transactions keyed on `(region, order_id, refund_id)`.
7. **`lw_pipeline_log`**: Audit log for the automated 3PL order routing pipeline (`send`, `swap`, `hold`, `error`).
8. **`lw_sync_state`**: High-watermark cursor table keyed on `(region, entity)`. Records `last_cursor`, `last_success_at`, `run_count`, and `last_error`.

---

## 6. CLI Usage & Job Execution

All jobs are run through the modular Python CLI:

### A. Pre-Flight Connectivity Test (Live Mode)
Verify credentials, session authorization, and API reachability:
```bash
python -m lwsync test-live
```

### B. Ingesting Catalog & Inventory (Deliverable 3)
```bash
# Sync Warehouse Locations (US and CA)
python -m lwsync run locations

# Sync Stock Levels across all locations
python -m lwsync run stock

# Sync Products and Extended Properties
python -m lwsync run products
```

### C. Ingesting Orders & Refunds (Deliverable 4)
```bash
# Sync Open Orders + Delta Processed Orders + Refunds
python -m lwsync run orders
```

### D. Full Synchronization
```bash
# Sync everything sequentially (locations -> stock -> products -> orders)
python -m lwsync run all
```

### E. Region-Specific Runs
To run a sync job for a single instance:
```bash
python -m lwsync run products --region US
python -m lwsync run orders --region CA
```

### F. Dry-Run Mode
Preview counts and test API transformations without writing to the database:
```bash
python -m lwsync run all --dry-run
python -m lwsync run orders --dry-run
```

---

## 7. Production Scheduling (Crontab Example)

On your Linux production server (e.g. AWS EC2 instance running Ubuntu), configure crontab jobs matching the client business intervals.

Open the crontab editor:
```bash
crontab -e
```

Add the following schedule entries:

```cron
# ==============================================================================
# BatteryClerk Linnworks -> MySQL RDS Synchronization Schedule (UTC)
# ==============================================================================
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
PROJECT_DIR=/opt/linnworks
PYTHON_BIN=/opt/linnworks/.venv/bin/python

# 1. Orders & Refunds (Every 15 minutes)
# Delta sync processed/changed orders + open orders + refunds with 10-min overlap
*/15 * * * * cd $PROJECT_DIR && $PYTHON_BIN -m lwsync run orders >> $PROJECT_DIR/logs/cron_orders.log 2>&1

# 2. Locations & Stock Levels (Every 30 minutes)
# Refresh physical on-hand, allocated, and available inventory levels
*/30 * * * * cd $PROJECT_DIR && $PYTHON_BIN -m lwsync run locations >> $PROJECT_DIR/logs/cron_inventory.log 2>&1
5,35 * * * * cd $PROJECT_DIR && $PYTHON_BIN -m lwsync run stock >> $PROJECT_DIR/logs/cron_inventory.log 2>&1

# 3. Products & Extended Properties (Every 6 hours)
# Full catalog upsert including the 8 extended properties
0 */6 * * * cd $PROJECT_DIR && $PYTHON_BIN -m lwsync run products >> $PROJECT_DIR/logs/cron_products.log 2>&1

# ==============================================================================
```

> **Alerting on Failure**: If any sync job fails, `lwsync` logs the error into `lw_sync_state.last_error` and exits with a **non-zero exit code (`sys.exit(1)`)**. Standard cron monitoring tools or AWS CloudWatch agents monitoring the log files will immediately trigger alerts.

---

## 8. Switching Between Fixture Mode and Live API

The codebase is engineered so switching from local testing to live API integration requires **changing only one variable in `.env`**:

### Running in Test / Offline Mode:
```env
LW_MODE=fixture
```
*Uses mock JSON responses in `fixtures/US/` and `fixtures/CA/`. No outbound network requests are made.*

### Running in Live Production Mode:
```env
LW_MODE=live
```
*Communicates directly with the live Linnworks REST APIs for US and CA using the credentials specified in `.env`.*

### Transition Pre-Flight Checklist:
1. Ensure `.env` contains valid credentials for both instances (`LW_US_*` and `LW_CA_*`).
2. Run `python -m lwsync test-live` to verify session token generation and server assignment.
3. Run `python -m lwsync run all --dry-run` to inspect data volume and response shapes.
4. Run `python -m lwsync run all` to execute the first full live baseline sync.

---

## 9. Running Automated Tests

The test suite contains 23 automated tests verifying client auth, throttling, retries, idempotency, status transitions, and refund logic:

```bash
# Run the entire test suite
pytest

# Run with verbose output
pytest -v

# Run specific test suites
pytest tests/test_client.py
pytest tests/test_idempotency.py
```

### Test Coverage Highlights:
- **`test_client.py`** (10 tests): Validates fixture loading, session token caching, token-bucket rate throttling, 429 exponential backoff retries, and 401 re-authentication.
- **`test_idempotency.py`** (13 tests): Validates repeated extraction stability, unique line item `row_id` integrity, order status mapping (`open`, `parked`, `shipped`, `refunded`), partial vs cumulative full refunds, order-level null SKUs, and cross-region SKU independence.

---

## 10. Troubleshooting & FAQ

#### Q: How do I resolve `Access denied for user 'root'@'...' (using password: YES)`?
Verify your database credentials in `.env`. Ensure your MySQL user has privileges to create and modify tables on the target database:
```sql
GRANT ALL PRIVILEGES ON linnworks_sync.* TO 'root'@'%';
FLUSH PRIVILEGES;
```

#### Q: How do I resolve DBeaver error `Public Key Retrieval is not allowed`?
In DBeaver, right-click the database connection $\rightarrow$ **Edit Connection** $\rightarrow$ **Driver properties** $\rightarrow$ set `allowPublicKeyRetrieval=TRUE` and `useSSL=FALSE`.

#### Q: How are unknown extended properties handled?
If Linnworks returns an extended property not in the 8 target columns, `lwsync` logs a warning (`Unmapped extended property 'xyz'. Ignored.`) and continues without throwing an error.

#### Q: Where are application logs stored?
Logs are written to both standard output (`stdout`) and a rotating file at `logs/lwsync.log` (maximum 10 MB per file, up to 5 archived backups).

---

## License & Support
Internal proprietary system built for **BatteryClerk**. For questions or infrastructure support, contact the integration engineering team.

