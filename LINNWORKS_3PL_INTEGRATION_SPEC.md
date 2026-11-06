# Linnworks to Extensiv 3PL & MySQL RDS Integration Specification

## 1. Executive Summary & Objective

This document outlines the end-to-end architecture, technical scope, and implementation roadmap for integrating **Linnworks (US and CA accounts)** with **Extensiv (3PL Central)** warehouses and an **AWS RDS MySQL** database for **Power BI** reporting.

### Core Business Objectives:
1. **Automated Order Fulfillment (Task 2 / Phase 3 - High Priority)**:
   * Process order files exported by Linnworks for both US and CA regions.
   * Evaluate warehouse stock at specific fulfillment locations (`US | AJC` and `CA | AJC`).
   * Apply intelligent SKU swapping rules based on configurable stock threshold limits (20, 30, 50).
   * Park (hold) orders automatically if stock is unavailable and alert operations via AWS SES.
   * Safely generate Extensiv-compliant CSV files in jailed SFTP directories.
   * Update Linnworks via API (audit notes, park statuses, and inventory balancing adjustments).

2. **Enterprise Data Warehouse & Reporting Sync (Task 1 / Phase 2)**:
   * Synchronize products, 8 extended properties, stock levels, orders, line items, and refunds from Linnworks API into an 8-table MySQL database.
   * Provide a normalized, query-optimized schema to power business intelligence dashboards in Power BI.

3. **Secure Cloud Infrastructure (Phase 1)**:
   * Provision an AWS EC2 instance (`t4g.small`).
   * Set up an OpenSSH SFTP server with isolated, chrooted directories for `3pl_us`, `3pl_ca`, and `linnworks`.
   * Restrict access via IP whitelisting and configure AWS SES for system notifications.

---

## 2. High-Level Architecture Diagram

```mermaid
flowchart TD
    subgraph Linnworks ERP
        LW_US["Linnworks US"]
        LW_CA["Linnworks CA"]
    end

    subgraph AWS Cloud Infrastructure [AWS Cloud - EC2 & RDS]
        EC2["EC2 Server (t4g.small)\nUbuntu / OpenSSH SFTP"]
        
        subgraph SFTP Jails
            SFTP_LW["/sftp/linnworks/orders/"]
            SFTP_US["/sftp/3pl_us/inbound_orders/"]
            SFTP_CA["/sftp/3pl_ca/inbound_orders/"]
        end

        subgraph Task 2: 3PL Pipeline (Phase 3 - Priority 1)
            RuleEngine["Rule & Stock Check Engine\n(Thresholds: 20, 30, 50)"]
            CsvGen["Extensiv CSV Builder\n(Atomic Write: .tmp -> .csv)"]
        end

        subgraph Task 1: Sync Engine (Phase 2 - Priority 2)
            SyncJob["Delta Sync Service\n(Every 15 mins)"]
        end

        subgraph Storage & Notifications
            RDS[("MySQL RDS (8 Tables)\nRegion-aware Keying")]
            SES["AWS SES / SMTP\nEmail Alerts"]
        end
    end

    subgraph External Warehouses & Analytics
        Extensiv_US["3PL US (Extensiv)"]
        Extensiv_CA["3PL CA (Extensiv)"]
        PowerBI["Power BI Dashboards"]
    end

    %% Task 2 Data Flow
    LW_US -->|Order export| SFTP_LW
    LW_CA -->|Order export| SFTP_LW
    SFTP_LW --> RuleEngine
    RuleEngine -->|Stock check, note, park, fix| LW_US
    RuleEngine -->|Stock check, note, park, fix| LW_CA
    RuleEngine -->|Log actions| RDS
    RuleEngine -->|Park alert| SES
    RuleEngine --> CsvGen
    CsvGen --> SFTP_US
    CsvGen --> SFTP_CA
    SFTP_US --> Extensiv_US
    SFTP_CA --> Extensiv_CA

    %% Task 1 Data Flow
    LW_US -->|API Sync: Products, Stock, Orders| SyncJob
    LW_CA -->|API Sync: Products, Stock, Orders| SyncJob
    SyncJob -->|Delta Writes| RDS
    RDS --> PowerBI
```

---

## 3. Implementation Sequence & Strategic Rationale

Your manager specifically requested the execution sequence:
$$\mathbf{Phase\ 1} \longrightarrow \mathbf{Phase\ 3} \longrightarrow \mathbf{Phase\ 2} \longrightarrow \mathbf{Phase\ 4}$$

### Why Phase 3 before Phase 2?

| Phase | Component | Business Impact | Urgency |
| :--- | :--- | :--- | :--- |
| **Phase 1** | Setup & Infrastructure | Prerequisite for both pipelines. Creates servers, SFTP, and DB. | **Immediate** |
| **Phase 3** | Task 2: 3PL Fulfillment Pipeline | **Direct Revenue & Operations**: Controls daily order dispatch to warehouses. Without this, warehouses ship wrong/out-of-stock SKUs, causing delivery failures, stockouts, and customer complaints. | **Critical (Must do first)** |
| **Phase 2** | Task 1: Linnworks Sync to MySQL | **Analytics & Reporting**: Powers Power BI reports. Business can operate and fulfill orders even if reports are built one week later. | **Secondary** |
| **Phase 4** | Testing, Monitoring & Go-Live | Quality assurance, integration tests, and production rollout. | **Final** |

---

## 4. Phase Breakdown & Deliverables

### Phase 1: Setup & Foundations
1. **Linnworks API Connection (US & CA)**:
   * App authorization, API keys, and session tokens.
   * HTTP 429 rate limit backoff mechanism and centralized logger.
2. **EC2 & SFTP Setup**:
   * Deploy AWS EC2 (`t4g.small`).
   * Configure OpenSSH chroot jail for `3pl_us`, `3pl_ca`, and `linnworks`.
   * Configure directory permissions (`inbound_orders`, `archive`, `failed`).
   * Security group IP whitelisting for Linnworks and 3PL IP ranges.
   * Configure AWS SES credentials for alerting.
3. **Database Schema (RDS MySQL)**:
   * Create the 8 normalized tables with `(region, sku)` and `(region, order_id)` composite keys.
   * Apply indexes on search/filter columns (e.g., status, order date, SKU).

---

### Phase 3: Task 2 — 3PL Order Processing Pipeline (High Priority)
1. **Order Rule Engine**:
   * Parse Linnworks order export files per region (`US`, `CA`).
   * Stock verification against Linnworks stock at `US | AJC` and `CA | AJC` locations.
   * Evaluate the 4 SKU swap pairs against the configurable threshold (e.g., 20, 30, 50).
   * Apply Hold / Park criteria when neither main SKU nor substitute has sufficient inventory.
2. **Extensiv CSV Builder**:
   * Transform order payloads into Extensiv-compliant CSV format.
   * Write file to `.tmp` extension, then execute an **atomic file rename** into `/inbound_orders/` to eliminate race conditions.
3. **Linnworks Bi-Directional Actions**:
   * **On Swap**: Call Linnworks API to append order note; perform stock balance adjustment (+1 ordered SKU, -1 shipped SKU).
   * **On Hold**: Call Linnworks API to park the order.
4. **Alerts & Resilience**:
   * Send AWS SES email alerts on parked orders or unhandled pipeline exceptions.
   * Record every decision (`SEND`, `SWAP`, `PARK`, `ERROR`) in `pipeline_log`.
   * Archive processed files and enforce duplicate order suppression.
5. **Testing**:
   * Comprehensive unit and integration test suite covering all swap pairs, threshold edges, and hold scenarios.

---

### Phase 2: Task 1 — Linnworks to MySQL Data Sync
1. **Product Sync**:
   * Extract SKU, title, cost, category, and 8 extended properties:
     * *Brand, Fill SKU, Product Line, MasterSKU, Substitute 1, Substitute 2, Mfr Part Number, +1 named property*.
   * Upsert into `products` table keyed by `(region, sku)`.
2. **Locations & Stock Level Sync**:
   * Sync all inventory locations.
   * Upsert stock metrics: `on_hand`, `allocated`, `available` per `(region, sku, location_id)`.
3. **Orders & Order Items Sync**:
   * Customer details, shipping/billing addresses, pricing, tax rates, shipping service, and tracking numbers.
   * Initial backfill followed by recurring delta sync (every 15 minutes) based on `sync_state`.
4. **Refunds Sync**:
   * Capture full and partial refund transactions; update order status to `refunded`.
5. **Data Parity Audits**:
   * Automated verification queries matching MySQL totals with Linnworks UI figures for US and CA.

---

### Phase 4: Testing & Go-Live
1. **End-to-End Simulation**:
   * Dry-run simulated orders across US and CA accounts with both 3PLs.
   * Verify unparked orders get re-exported and processed cleanly.
2. **Go-Live & Production Scheduling**:
   * Production crons / systemd services setup with automated restarts.
   * CloudWatch alarms and health monitoring.
   * Operational runbooks and schema documentation handover.
