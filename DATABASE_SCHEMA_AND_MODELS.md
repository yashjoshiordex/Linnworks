# MySQL RDS Database Schema & Data Models

## 1. Schema Overview

This schema is designed for an AWS RDS MySQL 8.0 instance. It supports two primary functions:
1. **Power BI Reporting**: Normalized tables representing catalog, inventory, order, and financial metrics.
2. **Operational Logging**: Full audit trail of order fulfillment decisions, SKU swaps, and hold states.

> **Key Design Rule**: Every table includes a `region` column (`'US'` or `'CA'`). Products, orders, and stock levels are partitioned logically by region because costs, stock IDs, and tax treatments differ between the US and Canadian entities.

---

## 2. Table Definitions (The 8 Scope Tables)

```sql
-- Create database with UTF8MB4 charset for full unicode / international text support
CREATE DATABASE IF NOT EXISTS linnworks_dw
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;

USE linnworks_dw;

-- ----------------------------------------------------------------------------
-- Table 1: sync_state
-- Tracks high-water marks for delta syncs across different entities and regions
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sync_state (
    sync_id INT AUTO_INCREMENT PRIMARY KEY,
    sync_type VARCHAR(50) NOT NULL COMMENT 'e.g. orders, products, stock, refunds',
    region ENUM('US', 'CA') NOT NULL,
    last_sync_timestamp DATETIME NOT NULL,
    records_processed INT DEFAULT 0,
    status ENUM('SUCCESS', 'IN_PROGRESS', 'FAILED') DEFAULT 'SUCCESS',
    error_message TEXT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uq_sync_type_region (sync_type, region)
) ENGINE=InnoDB;

-- ----------------------------------------------------------------------------
-- Table 2: products
-- Catalog items with the 8 specified extended properties
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS products (
    product_id INT AUTO_INCREMENT PRIMARY KEY,
    region ENUM('US', 'CA') NOT NULL,
    sku VARCHAR(100) NOT NULL,
    title VARCHAR(255) NOT NULL,
    cost DECIMAL(10, 4) DEFAULT 0.0000,
    category VARCHAR(150) NULL,
    
    -- 8 Extended Properties as dedicated columns for fast indexing & reporting
    brand VARCHAR(100) NULL,
    fill_sku VARCHAR(100) NULL,
    product_line VARCHAR(100) NULL,
    master_sku VARCHAR(100) NULL,
    substitute_1 VARCHAR(100) NULL,
    substitute_2 VARCHAR(100) NULL,
    mfr_part_number VARCHAR(100) NULL,
    custom_property_8 VARCHAR(100) NULL COMMENT 'Property to be named by client',
    
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    
    UNIQUE KEY uq_region_sku (region, sku),
    INDEX idx_category (category),
    INDEX idx_brand (brand),
    INDEX idx_fill_sku (fill_sku)
) ENGINE=InnoDB;

-- ----------------------------------------------------------------------------
-- Table 3: locations
-- Warehouse and fulfillment center locations
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS locations (
    location_id INT AUTO_INCREMENT PRIMARY KEY,
    region ENUM('US', 'CA') NOT NULL,
    linnworks_location_id VARCHAR(100) NOT NULL,
    location_name VARCHAR(150) NOT NULL,
    is_fulfillment_center BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    
    UNIQUE KEY uq_region_lw_loc (region, linnworks_location_id),
    INDEX idx_loc_name (location_name)
) ENGINE=InnoDB;

-- ----------------------------------------------------------------------------
-- Table 4: stock_levels
-- Inventory levels per SKU and warehouse location
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS stock_levels (
    stock_id BIGINT AUTO_INCREMENT PRIMARY KEY,
    region ENUM('US', 'CA') NOT NULL,
    sku VARCHAR(100) NOT NULL,
    linnworks_location_id VARCHAR(100) NOT NULL,
    on_hand INT NOT NULL DEFAULT 0,
    allocated INT NOT NULL DEFAULT 0,
    available INT GENERATED ALWAYS AS (on_hand - allocated) STORED,
    last_synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    
    UNIQUE KEY uq_region_sku_location (region, sku, linnworks_location_id),
    INDEX idx_available (available),
    INDEX idx_sku (sku)
) ENGINE=InnoDB;

-- ----------------------------------------------------------------------------
-- Table 5: orders
-- Header-level order records with customer, addresses, and financials
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS orders (
    order_db_id BIGINT AUTO_INCREMENT PRIMARY KEY,
    region ENUM('US', 'CA') NOT NULL,
    order_id VARCHAR(100) NOT NULL COMMENT 'Linnworks Order ID / NumOrderId',
    channel VARCHAR(100) NULL COMMENT 'e.g., Shopify, Amazon',
    subsource VARCHAR(100) NULL,
    
    -- Status
    status ENUM('open', 'parked', 'shipped', 'cancelled', 'refunded') NOT NULL DEFAULT 'open',
    
    -- Timestamps
    order_date DATETIME NOT NULL,
    shipped_date DATETIME NULL,
    
    -- Customer Information
    customer_name VARCHAR(200) NULL,
    customer_email VARCHAR(200) NULL,
    customer_phone VARCHAR(50) NULL,
    
    -- Shipping Address
    ship_address_1 VARCHAR(255) NULL,
    ship_address_2 VARCHAR(255) NULL,
    ship_city VARCHAR(100) NULL,
    ship_state VARCHAR(100) NULL,
    ship_postal_code VARCHAR(30) NULL,
    ship_country VARCHAR(100) NULL,
    
    -- Billing Address
    bill_address_1 VARCHAR(255) NULL,
    bill_address_2 VARCHAR(255) NULL,
    bill_city VARCHAR(100) NULL,
    bill_state VARCHAR(100) NULL,
    bill_postal_code VARCHAR(30) NULL,
    bill_country VARCHAR(100) NULL,
    
    -- Financials
    currency VARCHAR(10) NOT NULL DEFAULT 'USD',
    subtotal DECIMAL(10, 2) DEFAULT 0.00,
    tax_total DECIMAL(10, 2) DEFAULT 0.00,
    shipping_charge DECIMAL(10, 2) DEFAULT 0.00,
    order_total DECIMAL(10, 2) NOT NULL DEFAULT 0.00,
    payment_method VARCHAR(100) NULL,
    
    -- Carrier & Tracking
    shipping_service VARCHAR(100) NULL,
    tracking_number VARCHAR(150) NULL,
    
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    
    UNIQUE KEY uq_region_order_id (region, order_id),
    INDEX idx_order_date (order_date),
    INDEX idx_status (status),
    INDEX idx_channel (channel)
) ENGINE=InnoDB;

-- ----------------------------------------------------------------------------
-- Table 6: order_items
-- Line item breakdown for each order
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS order_items (
    item_db_id BIGINT AUTO_INCREMENT PRIMARY KEY,
    region ENUM('US', 'CA') NOT NULL,
    order_id VARCHAR(100) NOT NULL,
    item_id VARCHAR(100) NOT NULL COMMENT 'Linnworks OrderItem ID',
    sku VARCHAR(100) NOT NULL,
    item_title VARCHAR(255) NULL,
    quantity INT NOT NULL DEFAULT 1,
    unit_price DECIMAL(10, 2) NOT NULL DEFAULT 0.00,
    tax_rate DECIMAL(5, 2) DEFAULT 0.00,
    discount DECIMAL(10, 2) DEFAULT 0.00,
    total DECIMAL(10, 2) NOT NULL DEFAULT 0.00,
    
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    
    UNIQUE KEY uq_region_order_item (region, order_id, item_id),
    INDEX idx_sku (sku),
    INDEX idx_order_id (order_id)
) ENGINE=InnoDB;

-- ----------------------------------------------------------------------------
-- Table 7: order_refunds
-- Full and partial refund details
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS order_refunds (
    refund_db_id BIGINT AUTO_INCREMENT PRIMARY KEY,
    region ENUM('US', 'CA') NOT NULL,
    refund_id VARCHAR(100) NOT NULL,
    order_id VARCHAR(100) NOT NULL,
    amount DECIMAL(10, 2) NOT NULL,
    reason VARCHAR(255) NULL,
    is_partial BOOLEAN DEFAULT FALSE,
    refund_date DATETIME NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    
    UNIQUE KEY uq_region_refund_id (region, refund_id),
    INDEX idx_order_id (order_id),
    INDEX idx_refund_date (refund_date)
) ENGINE=InnoDB;

-- ----------------------------------------------------------------------------
-- Table 8: pipeline_log
-- Audit log of every routing decision: Send, Swap, Park, or Error
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pipeline_log (
    log_id BIGINT AUTO_INCREMENT PRIMARY KEY,
    region ENUM('US', 'CA') NOT NULL,
    order_id VARCHAR(100) NOT NULL,
    action ENUM('SEND', 'SWAP', 'PARK', 'ERROR') NOT NULL,
    original_sku VARCHAR(100) NULL,
    swapped_sku VARCHAR(100) NULL,
    stock_at_decision INT NULL,
    threshold_setting INT NULL,
    reason VARCHAR(255) NULL,
    notes_added TEXT NULL,
    raw_payload JSON NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    
    INDEX idx_region_action (region, action),
    INDEX idx_order_id (order_id),
    INDEX idx_created_at (created_at)
) ENGINE=InnoDB;
```

---

## 3. Power BI Integration Best Practices
* **DirectQuery vs Import**: Use scheduled Import mode (refreshing every 1 to 2 hours) to avoid overloading the RDS instance while maintaining high dashboard interactivity.
* **Date Dimension**: Leverage `order_date` in `orders` joined to a standard Calendar Date Table in Power BI for YoY and MoM analytics.
* **Relationship Graph**:
  * `orders (region, order_id)` $1 \longleftrightarrow \infty$ `order_items (region, order_id)`
  * `orders (region, order_id)` $1 \longleftrightarrow \infty$ `order_refunds (region, order_id)`
  * `products (region, sku)` $1 \longleftrightarrow \infty$ `stock_levels (region, sku)`
