# Phase 3: 3PL Order Processing Pipeline Implementation Guide

## 1. Overview & Operational Flow

The **3PL Pipeline (Task 2)** is the mission-critical core of the system. It bridges Linnworks order generation with physical warehouse fulfillment through **Extensiv (3PL Central)** for both US and CA regions.

```text
[Linnworks SFTP Export]
         │
         ▼
[1. File Ingestion & Parsing] (Detect Region: US or CA)
         │
         ▼
[2. Inventory Rule Evaluation]
    ├── Query available stock at location: "US | AJC" or "CA | AJC"
    ├── Compare available stock vs Configurable Threshold (20, 30, or 50)
    │
    ├── Scenario A: Stock >= Threshold
    │     └── Proceed with original SKU
    │
    ├── Scenario B: Stock < Threshold (Eligible for 4 SKU Pairs)
    │     ├── Check Substitute SKU stock
    │     ├── If Substitute Stock Available:
    │     │     ├── Swap line SKU to Substitute SKU
    │     │     ├── Linnworks API: Add Order Note on Swap
    │     │     └── Linnworks API: Adjust Stock (+1 Ordered, -1 Shipped)
    │     └── If Substitute Stock Also Insufficient:
    │           └── Escalate to Scenario C
    │
    └── Scenario C: Insufficient Stock / Hold Rule Triggered
          ├── Linnworks API: Park Order on Hold
          ├── Trigger AWS SES Email Alert
          └── Write 'PARK' entry to pipeline_log (Do NOT dispatch to 3PL)
         │
         ▼
[3. Extensiv CSV Generation]
    ├── Format CSV according to 3PL specification
    ├── Write to `.tmp` file in destination folder
    └── Atomic rename to `[order_id].csv` in `/inbound_orders/`
         │
         ▼
[4. Post-Processing & Archiving]
    ├── Record 'SEND' or 'SWAP' action in `pipeline_log`
    ├── Move source Linnworks file to `/archive/YYYY-MM-DD/`
    └── Enforce idempotency hash to block duplicate transmissions
```

---

## 2. Rule Engine Logic & SKU Swapping

### 2.1 The 4 SKU Pairs Matrix
Define the 4 SKU swap pairs in your configuration:

```typescript
// config/skuPairs.ts
export interface SkuPairRule {
  primarySku: string;
  substituteSku: string;
  threshold: number; // e.g., 20, 30, 50
  region: 'US' | 'CA';
  warehouseLocation: string; // "US | AJC" or "CA | AJC"
}

export const SKU_SWAP_RULES: SkuPairRule[] = [
  {
    primarySku: "PRIMARY-SKU-001",
    substituteSku: "SUB-SKU-001",
    threshold: 20, // Configurable threshold
    region: "US",
    warehouseLocation: "US | AJC"
  },
  {
    primarySku: "PRIMARY-SKU-002",
    substituteSku: "SUB-SKU-002",
    threshold: 30,
    region: "US",
    warehouseLocation: "US | AJC"
  },
  {
    primarySku: "PRIMARY-SKU-003",
    substituteSku: "SUB-SKU-003",
    threshold: 50,
    region: "CA",
    warehouseLocation: "CA | AJC"
  },
  {
    primarySku: "PRIMARY-SKU-004",
    substituteSku: "SUB-SKU-004",
    threshold: 20,
    region: "CA",
    warehouseLocation: "CA | AJC"
  }
];
```

### 2.2 Decision Tree Algorithm
For every line item in the order:
1. Identify if the line item's SKU matches any active `primarySku` for that region.
2. If matched:
   * Query Linnworks Stock Level endpoint for location `US | AJC` (or `CA | AJC`).
   * Calculate `Available = (QuantityOnHand - QuantityAllocated)`.
   * **Condition 1**: If `Available >= Threshold`:
     * Fulfill with `primarySku`.
   * **Condition 2**: If `Available < Threshold`:
     * Check substitute SKU inventory at that same location.
     * If `SubstituteAvailable >= LineItemQuantity`:
       * Execute **Swap Action**.
     * Else:
       * Execute **Park Action** (Do not send order to Extensiv).

---

## 3. Linnworks API Integration Details

### 3.1 Adding Order Note on Swap
When a swap occurs, an audit note must be attached to the order in Linnworks:

* **Endpoint**: `POST /api/Orders/SetOrderNotes` (or `POST /api/Orders/AddOrderNote`)
* **Payload Structure**:
  ```json
  {
    "orderId": "1002345",
    "orderNote": {
      "NoteText": "AUTO-SWAP: Substituted PRIMARY-SKU-001 with SUB-SKU-001. Reason: Stock below threshold (Available: 14, Threshold: 20).",
      "NoteType": "Internal",
      "IsInternal": true
    }
  }
  ```

### 3.2 Inventory Balancing Adjustment After Swap
To ensure inventory counts stay strictly aligned with physical bin movements:
1. Return $+1$ (or $+Qty$) to `primarySku` at `US | AJC`.
2. Deduct $-1$ (or $-Qty$) from `substituteSku` at `US | AJC`.

* **Endpoint**: `POST /api/Stock/AdjustStock`
* **Adjustment Payload**:
  ```json
  [
    {
      "SKU": "PRIMARY-SKU-001",
      "LocationId": "ajc-location-guid",
      "Adjustment": 1,
      "Reason": "Pipeline Swap Correction (+Ordered SKU)"
    },
    {
      "SKU": "SUB-SKU-001",
      "LocationId": "ajc-location-guid",
      "Adjustment": -1,
      "Reason": "Pipeline Swap Correction (-Shipped SKU)"
    }
  ]
  ```

### 3.3 Parking (Holding) Orders
If stock is insufficient for both primary and substitute:
* **Endpoint**: `POST /api/Orders/ChangeOrderTag` or `POST /api/Orders/SetOrderGeneralInfo`
* **Action**: Set Order Status to `Parked` / Tag as `ON_HOLD_STOCK`.
* **SES Notification**: Fire email with order number, line SKU, required quantity, and current location inventory.

---

## 4. Extensiv CSV File Generation (Atomic Writes)

### 4.1 The Race Condition Problem
3PL automated schedulers poll the SFTP folder frequently. If your script writes directly to `inbound_orders/order_123.csv`, the 3PL watcher might read the file while it is only 50% written, causing corrupted orders.

### 4.2 The Atomic Rename Solution
Always write to a temporary file (`.tmp`) first, flush the file buffer to disk, and then perform an OS-level atomic rename:

```typescript
import fs from 'fs';
import path from 'path';

export async function writeExtensivCsvSafely(
  targetDir: string,
  fileName: string,
  csvContent: string
): Promise<string> {
  const tempPath = path.join(targetDir, `${fileName}.tmp`);
  const finalPath = path.join(targetDir, `${fileName}.csv`);

  // Step 1: Write full content to .tmp file
  await fs.promises.writeFile(tempPath, csvContent, { encoding: 'utf8', flag: 'w' });

  // Step 2: Atomic rename into final inbound_orders folder
  await fs.promises.rename(tempPath, finalPath);

  return finalPath;
}
```

---

## 5. Idempotency, Duplicate Protection & Error Handling

1. **Unique Order Key**:
   * Compute a hash: `SHA256(region + order_id + line_items_string)`.
   * Check `pipeline_log` before generating CSV. If an entry exists with action `SEND` or `SWAP` for that `order_id`, halt and alert to prevent duplicate shipments.
2. **File Archiving**:
   * Upon successful processing, move input files immediately:
     * Source: `/sftp/linnworks/orders/export_20261105.csv`
     * Destination: `/sftp/linnworks/archive/2026-11/export_20261105_120530.csv`
3. **Retry Strategy**:
   * Wrap Linnworks API network calls in a retry handler with exponential backoff:
     * Attempt 1: Immediate
     * Attempt 2: After 2 seconds
     * Attempt 3: After 5 seconds
   * If all retries fail, write `action = 'ERROR'` into `pipeline_log` and send SES alert.

---

## 6. Automated Test Matrix

Before pushing to production, execute tests validating:

| Test ID | Test Scenario | Expected Outcome |
| :--- | :--- | :--- |
| **TC-01** | Stock = 45, Threshold = 20 | Order proceeds with Original SKU. No swap, no note. |
| **TC-02** | Stock = 15, Threshold = 20, Substitute Stock = 50 | Swap executed. Order note added. Stock adjusted (+1 / -1). CSV written with Substitute SKU. |
| **TC-03** | Stock = 10, Threshold = 20, Substitute Stock = 0 | Order is NOT sent to 3PL. Linnworks API marks order as Parked. SES alert dispatched. `pipeline_log` records `PARK`. |
| **TC-04** | Atomic Write Verification | File appears in destination with `.tmp` before instantaneously renaming to `.csv`. |
| **TC-05** | Duplicate File Re-run | Pipeline recognizes duplicate `order_id` and safely aborts re-transmission. |
| **TC-06** | Unparked Order Resumption | When order is unparked in Linnworks and exported in subsequent run, pipeline re-evaluates stock correctly. |
