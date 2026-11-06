# Prompt: BatteryClerk Linnworks → MySQL sync (local build)

Copy everything below the line into your AI assistant. You need MySQL 8 installed locally (for example `brew install mysql`, or the MySQL installer on Windows).

---

You are helping me build a data sync for a client, BatteryClerk. They sell batteries online and use **Linnworks** (an order and inventory management system) with **two separate instances: US and CA**. We need to copy their Linnworks data into a MySQL database on a schedule, so they can report on it and so a later pipeline can use it.

For now I am working **only on my local machine with a local MySQL database**. We do **not** have Linnworks API access yet, so build against realistic sample data first, in a way that lets us switch to the live API later by changing only config.

## Constraints

- **Language:** Python 3.12. Use `requests`, `PyMySQL` (or `mysql-connector-python`), `python-dotenv`, `pytest`. Keep dependencies minimal and list them in `requirements.txt`.
- **Database:** the production target is **MariaDB on AWS RDS** (MySQL-compatible). Write SQL that runs on both MySQL 8 and MariaDB 10.6+: no MySQL-only features (for example, avoid `JSON_TABLE` and functional indexes). Use `utf8mb4`, InnoDB, `DECIMAL` for money, `DATETIME` in **UTC**.
- **Config:** all secrets and settings come from a `.env` file (never committed). Provide `.env.example` with placeholder values only.
- **Regions:** every table has a `region` column (`'US'` or `'CA'`). Each region has its own Linnworks credentials. The same SKU can exist in both regions with different cost and IDs, so products are keyed by `(region, sku)`.
- **Idempotent:** running a sync twice must not create duplicates. Use upserts (`INSERT ... ON DUPLICATE KEY UPDATE`) keyed on natural keys.
- **New tables only:** the client already has other tables in that database (used by Power BI). Never alter or drop anything we did not create. Put all our tables behind one clear naming convention (for example, a prefix `lw_`). Ask me before choosing the final prefix.

## The 8 tables

Design these, with primary keys, unique keys and sensible indexes. Show me the `CREATE TABLE` script for review before writing sync code.

1. **products**: region, Linnworks stock item ID (GUID), SKU, title, cost (purchase price), retail price, category, barcode, weight, created/updated timestamps. Also the 8 extended properties as their own columns: **Brand, Fill SKU, Product Line, MasterSKU, Substitute 1, Substitute 2, Mfr Part Number**, and an 8th to be confirmed (probably **Product Tag**). Unique on (region, sku).
2. **locations**: region, Linnworks location ID (GUID), name (for example `US | AJC`, `CA | AJC`), is_default.
3. **stock_levels**: region, SKU, location ID, on hand (stock level), allocated (in open orders), available, updated_at. Unique on (region, sku, location_id).
4. **orders**: full order data.
   - IDs: region, Linnworks order ID (GUID), order number (NumOrderId), channel reference number.
   - Source: channel source and subsource.
   - Status: open / parked / shipped / cancelled / refunded, plus the raw Linnworks status, a hold/parked flag and the Linnworks folder name(s).
   - Dates: received, dispatch-by and processed.
   - Customer: name, email and phone.
   - Shipping address: name, company, address lines 1–3, town, region, postcode, country.
   - Billing address: the same fields.
   - Money: currency, subtotal, tax, shipping charge, total, payment method.
   - Shipping: service, carrier and tracking number.
   - Location (fulfilment location ID), created_at and updated_at.
   - Unique on (region, order_id).
5. **order_items**: region, order ID, Linnworks row ID, SKU, item title, quantity, unit price, line total, tax rate, tax amount, discount, stock item ID. Unique on (region, order_id, row_id).
6. **order_refunds**: region, order ID, refund ID or reference, SKU (nullable for order-level refunds), quantity, amount, reason, refunded_at. Must support partial refunds. When an order is fully refunded, set orders.status to `refunded`.
7. **pipeline_log**: for a later phase (a 3PL order pipeline). Columns: id, region, order ID, action (`send` / `swap` / `hold` / `error`), details (TEXT), created_at. Create it now; nothing writes to it yet.
8. **sync_state**: one row per (region, entity), for example (`US`, `orders`). Stores last_success_at, last_cursor (for example the last processed date), last_error and run counts. Used for delta syncs.

## Sync behaviour

- **Products and extended properties:** a full sync on a configurable schedule (default every 6 hours). Upsert products, and map extended property names to the columns above. Log any unknown property names instead of crashing.
- **Locations and stock levels:** a full sync on a configurable schedule (default every 30 minutes).
- **Orders:**
  - Every 15 minutes: open orders (full payload, including items).
  - Every 15 minutes: a **delta** sync of orders processed, cancelled or changed since `sync_state.last_cursor`, with a small overlap window (for example, 10 minutes) so nothing is missed.
- **Refunds:** pulled together with the order delta.
- **Run modes:**
  - The CLI can run one job for one region: `python -m lwsync run orders --region US`
  - `python -m lwsync run all`
  - A `--dry-run` flag that prints what would change without writing.
- **Logging:** structured logs to stdout and a rotating file. On failure, update `sync_state.last_error` and exit non-zero (cron will alert on this later).

## Linnworks API: how it works (verify against the docs)

Docs: https://apps.linnworks.net/Api and https://developer.linnworks.com. Check endpoint names and payloads there; don't trust memory.

- **Credentials:** we register an app on the Linnworks developer portal and get an **ApplicationId** and **ApplicationSecret**. A Linnworks admin installs that app on each instance, which gives an **installation Token** per instance. So we will have 1 app ID + 1 secret, and 2 tokens (US and CA).
- **Authentication:** `POST https://api.linnworks.net/api/Auth/AuthorizeByApplication` with ApplicationId, ApplicationSecret and Token. It returns a **session token** and a **Server URL**. All later calls go to `{Server}/api/...` with header `Authorization: <session token>`. Cache the session and re-authenticate on 401.
- **Endpoints we expect to use:** open orders, order details by ID, processed-order search, stock items with extended properties, stock locations, stock levels, and refunds. Find the exact names in the docs.
- **Rate limits:** Linnworks limits calls per minute per endpoint. Build a small client with throttling, retry with backoff on 429 and 5xx, request timeouts, and paging helpers.

## Since we have no API access yet

1. Build a `LinnworksClient` interface with two implementations:
   - `LiveClient`: real HTTP calls, used once we have credentials.
   - `FixtureClient`: reads JSON files from `fixtures/<region>/<endpoint>.json`.
   - The choice comes from `.env` (`LW_MODE=fixture` or `live`).
2. Create realistic fixture files that copy the **shape of real Linnworks API responses**, using the documented response models (field names, nesting, GUIDs, dates). Include enough variety to test: multi-line orders, an order with a discount, a parked/held order, a cancelled order, a fully refunded and a partially refunded order, a product with all extended properties, one with some missing, and the same SKU in both US and CA.
3. Use only made-up customer data in fixtures. Never use real names, emails or addresses.

## Deliverables, in this order (stop for review after each)

1. Project layout and the `CREATE TABLE` script (`sql/001_create_tables.sql`). I already have MySQL installed locally (no Docker); connection details come from `.env`.
2. The Linnworks client (fixture and live), auth, throttling and retries.
3. Product, location and stock sync, with tests.
4. Order, order item and refund sync (open plus delta), with tests.
5. Idempotency tests (running twice gives the same row counts), status-mapping tests and refund tests.
6. A `README.md`: setup, `.env` keys, how to run each job, an example crontab, and how to switch to live mode.

Ask me questions whenever a requirement is unclear instead of guessing, especially on table naming, status mapping, and which Linnworks fields map to which columns.
