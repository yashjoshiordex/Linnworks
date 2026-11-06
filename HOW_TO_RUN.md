# How to Run the Linnworks Sync Project

This guide provides step-by-step instructions for running the project locally, setting up database connections, auto-generating tables, and running data sync jobs.

---

## 1. Prerequisites

- **Python 3.12**
- **MySQL 8.0+** or **MariaDB 10.6+** (Running locally via Windows Service or Docker)
- **DBeaver** (or any database GUI client)

---

## 2. Environment Configuration (`.env`)

Ensure you have a `.env` file in the root directory (`d:\Yash Joshi\linnworks\.env`):

```env
# Database Settings
DB_HOST=127.0.0.1
DB_PORT=3306
DB_NAME=linnworks_sync
DB_USER=root
DB_PASSWORD=root
DB_CHARSET=utf8mb4

# Execution Mode: 'fixture' (offline mock data) or 'live' (real API)
LW_MODE=fixture

# Table prefix
LW_TABLE_PREFIX=lw_

# Linnworks Live Credentials (Required only when LW_MODE=live)
LW_APP_ID=
LW_APP_SECRET=
LW_US_TOKEN=
LW_CA_TOKEN=
```

---

## 3. Starting Your Local MySQL Database

Before running any script, make sure your MySQL database is active:

- **If using Docker:**
  ```powershell
  docker start mysql-local
  ```
  *(Or start it via Docker Desktop GUI)*

- **If using Windows Service:**
  - Press `Win + R` $\rightarrow$ type `services.msc` $\rightarrow$ Enter.
  - Locate **MySQL80** $\rightarrow$ Right-click $\rightarrow$ **Start**.

---

## 4. Running the Project

Open PowerShell in the project directory (`D:\Yash Joshi\linnworks`):

```powershell
cd "D:\Yash Joshi\linnworks"
```

### A. Activate the Virtual Environment (Recommended)

```powershell
.\.venv\Scripts\Activate.ps1
```
*(Your prompt will now show `(.venv)`).*

---

### B. Auto-Generate Database & Tables

The project includes built-in auto-migration. You can verify or auto-generate tables at any time:

```powershell
# With active venv:
python -m lwsync init-db

# Or direct without activating venv:
.\.venv\Scripts\python.exe -m lwsync init-db
```

> **Automatic Schema Verification:**  
> When you run any sync job (`python -m lwsync run ...`), the system **automatically checks for tables first**. If any table is missing, it creates it. If the tables already exist, it leaves them and their data untouched.

---

### C. Testing Live Linnworks API Connectivity

Before syncing large datasets, verify that your US and CA Linnworks credentials can connect, authenticate, and read data:

```powershell
# With active venv:
python -m lwsync test-live

# Or direct without activating venv:
.\.venv\Scripts\python.exe -m lwsync test-live
```
*This will authenticate both US and CA instances, display their assigned server URLs (e.g. `https://us-ext.linnworks.net`), and probe the API by fetching real warehouse locations.*

---

### D. Running Sync Jobs

You can run sync commands either with the virtual environment activated (`python -m ...`) or directly via `.\.venv\Scripts\python.exe -m ...`.

#### 1. Sync Warehouse Locations:
```powershell
python -m lwsync run locations
# Or: .\.venv\Scripts\python.exe -m lwsync run locations
```

#### 2. Sync Physical Stock Levels:
```powershell
python -m lwsync run stock
# Or: .\.venv\Scripts\python.exe -m lwsync run stock
```

#### 3. Sync Products & Extended Properties:
```powershell
python -m lwsync run products
# Or: .\.venv\Scripts\python.exe -m lwsync run products
```

#### 4. Sync Orders & Refunds (Open + Delta):
```powershell
python -m lwsync run orders
# Or: .\.venv\Scripts\python.exe -m lwsync run orders
```

#### 5. Sync Everything (All Entities, US + CA):
```powershell
python -m lwsync run all
# Or: .\.venv\Scripts\python.exe -m lwsync run all
```

#### 6. Sync a Specific Region:
```powershell
# Sync products for US only
python -m lwsync run products --region US

# Sync stock for CA only
python -m lwsync run stock --region CA

# Sync orders for US only
python -m lwsync run orders --region US

# Sync locations for US only
python -m lwsync run locations --region US
```

#### 7. Dry Run Mode (Preview Changes Without Writing to Database):
```powershell
python -m lwsync run orders --dry-run
# Or: .\.venv\Scripts\python.exe -m lwsync run all --dry-run
```

#### 4. Dry Run Mode (Preview changes without writing to DB):
```powershell
python -m lwsync run all --dry-run
```

---

## 5. Viewing Tables & Data in DBeaver

1. Open **DBeaver**.
2. Click **Database** $\rightarrow$ **New Database Connection...** (`Ctrl + Shift + N`).
3. Select **MySQL** and click **Next**.
4. Fill in connection details:
   - **Host:** `127.0.0.1`
   - **Port:** `3306`
   - **Database:** `linnworks_sync`
   - **Username:** `root`
   - **Password:** `root` (or your configured password)
5. Switch to the **Driver properties** tab:
   - Set `allowPublicKeyRetrieval` = `TRUE`
   - Set `useSSL` = `FALSE`
6. Click **Test Connection** $\rightarrow$ Click **Finish**.
7. Expand `linnworks_sync` $\rightarrow$ `Databases` $\rightarrow$ `linnworks_sync` $\rightarrow$ `Tables`.

You will see all **8 tables**:
1. `lw_products`
2. `lw_locations`
3. `lw_stock_levels`
4. `lw_orders`
5. `lw_order_items`
6. `lw_order_refunds`
7. `lw_pipeline_log`
8. `lw_sync_state`

---

## 6. Running Automated Tests

To run the automated test suite with pytest:

```powershell
.\.venv\Scripts\pytest
```

---

## 7. Logs

All application runs log structured output to:
- Console (`stdout`)
- Rotating file handler: `logs/lwsync.log`

