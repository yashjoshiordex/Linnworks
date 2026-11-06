-- ============================================================================
-- Linnworks Data Sync Schema (001_create_tables.sql)
-- Target: MariaDB 10.6+ & MySQL 8.0+ on AWS RDS / Local MySQL
-- Charset: utf8mb4 | Engine: InnoDB | Timestamps: UTC DATETIME
-- Prefix convention: lw_ (isolated from existing client tables)
-- ============================================================================

-- ----------------------------------------------------------------------------
-- 1. Products
-- Catalog items with 8 dedicated extended property columns
-- Unique on (region, sku)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS lw_products (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    region VARCHAR(10) NOT NULL COMMENT 'Region identifier (e.g. US, CA)',
    stock_item_id VARCHAR(64) NULL COMMENT 'Linnworks StockItemId (GUID)',
    sku VARCHAR(100) NOT NULL COMMENT 'Stock Keeping Unit',
    title VARCHAR(255) NOT NULL,
    cost DECIMAL(10, 4) NOT NULL DEFAULT 0.0000 COMMENT 'Purchase price',
    retail_price DECIMAL(10, 2) NOT NULL DEFAULT 0.00 COMMENT 'Retail price',
    category VARCHAR(150) NULL,
    barcode VARCHAR(100) NULL,
    weight DECIMAL(10, 3) NOT NULL DEFAULT 0.000 COMMENT 'Weight in kg/lbs',
    
    -- 8 Extended Properties as dedicated columns
    brand VARCHAR(100) NULL,
    fill_sku VARCHAR(100) NULL,
    product_line VARCHAR(100) NULL,
    master_sku VARCHAR(100) NULL,
    substitute_1 VARCHAR(100) NULL,
    substitute_2 VARCHAR(100) NULL,
    mfr_part_number VARCHAR(100) NULL,
    product_tag VARCHAR(100) NULL COMMENT '8th extended property',
    
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    
    UNIQUE KEY uq_products_region_sku (region, sku),
    INDEX idx_products_stock_item_id (stock_item_id),
    INDEX idx_products_category (category),
    INDEX idx_products_brand (brand),
    INDEX idx_products_fill_sku (fill_sku),
    INDEX idx_products_master_sku (master_sku)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ----------------------------------------------------------------------------
-- 2. Locations
-- Warehouse / fulfillment locations
-- Unique on (region, location_id)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS lw_locations (
    id INT AUTO_INCREMENT PRIMARY KEY,
    region VARCHAR(10) NOT NULL COMMENT 'Region identifier (e.g. US, CA)',
    location_id VARCHAR(64) NOT NULL COMMENT 'Linnworks StockLocationId (GUID)',
    name VARCHAR(150) NOT NULL COMMENT 'e.g. US | AJC, CA | AJC',
    is_default BOOLEAN NOT NULL DEFAULT FALSE,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    
    UNIQUE KEY uq_locations_region_loc (region, location_id),
    INDEX idx_locations_name (name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ----------------------------------------------------------------------------
-- 3. Stock Levels
-- Inventory levels per SKU and warehouse location
-- Unique on (region, sku, location_id)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS lw_stock_levels (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    region VARCHAR(10) NOT NULL COMMENT 'Region identifier (e.g. US, CA)',
    sku VARCHAR(100) NOT NULL,
    location_id VARCHAR(64) NOT NULL COMMENT 'Linnworks StockLocationId (GUID)',
    on_hand INT NOT NULL DEFAULT 0 COMMENT 'Stock level on hand',
    allocated INT NOT NULL DEFAULT 0 COMMENT 'Stock in open orders',
    available INT NOT NULL DEFAULT 0 COMMENT 'Available stock (on_hand - allocated)',
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    
    UNIQUE KEY uq_stock_region_sku_loc (region, sku, location_id),
    INDEX idx_stock_sku (sku),
    INDEX idx_stock_location (location_id),
    INDEX idx_stock_available (available)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ----------------------------------------------------------------------------
-- 4. Orders
-- Full header-level order records, addresses, customer & financials
-- Unique on (region, order_id)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS lw_orders (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    region VARCHAR(10) NOT NULL COMMENT 'Region identifier (e.g. US, CA)',
    order_id VARCHAR(64) NOT NULL COMMENT 'Linnworks Order ID (GUID)',
    num_order_id VARCHAR(50) NULL COMMENT 'Linnworks NumOrderId',
    channel_reference_number VARCHAR(100) NULL COMMENT 'External channel reference',
    
    -- Source
    source VARCHAR(100) NULL COMMENT 'Channel source e.g. SHOPIFY, AMAZON',
    subsource VARCHAR(100) NULL COMMENT 'Channel subsource e.g. store name',
    
    -- Status
    status VARCHAR(50) NOT NULL DEFAULT 'open' COMMENT 'open / parked / shipped / cancelled / refunded',
    raw_status VARCHAR(50) NULL COMMENT 'Raw Linnworks status string or code',
    is_hold_or_parked BOOLEAN NOT NULL DEFAULT FALSE COMMENT 'Hold or parked flag',
    folder_names VARCHAR(255) NULL COMMENT 'Linnworks folder names (comma separated)',
    
    -- Dates (UTC)
    received_date DATETIME NULL,
    dispatch_by_date DATETIME NULL,
    processed_date DATETIME NULL,
    
    -- Customer
    customer_name VARCHAR(200) NULL,
    customer_email VARCHAR(200) NULL,
    customer_phone VARCHAR(50) NULL,
    
    -- Shipping Address
    shipping_name VARCHAR(200) NULL,
    shipping_company VARCHAR(200) NULL,
    shipping_address1 VARCHAR(255) NULL,
    shipping_address2 VARCHAR(255) NULL,
    shipping_address3 VARCHAR(255) NULL,
    shipping_town VARCHAR(100) NULL,
    shipping_region VARCHAR(100) NULL,
    shipping_postcode VARCHAR(50) NULL,
    shipping_country VARCHAR(100) NULL,
    
    -- Billing Address
    billing_name VARCHAR(200) NULL,
    billing_company VARCHAR(200) NULL,
    billing_address1 VARCHAR(255) NULL,
    billing_address2 VARCHAR(255) NULL,
    billing_address3 VARCHAR(255) NULL,
    billing_town VARCHAR(100) NULL,
    billing_region VARCHAR(100) NULL,
    billing_postcode VARCHAR(50) NULL,
    billing_country VARCHAR(100) NULL,
    
    -- Financials
    currency VARCHAR(10) NOT NULL DEFAULT 'USD',
    subtotal DECIMAL(10, 2) NOT NULL DEFAULT 0.00,
    tax DECIMAL(10, 2) NOT NULL DEFAULT 0.00,
    shipping_charge DECIMAL(10, 2) NOT NULL DEFAULT 0.00,
    total DECIMAL(10, 2) NOT NULL DEFAULT 0.00,
    payment_method VARCHAR(100) NULL,
    
    -- Shipping Details
    shipping_service VARCHAR(100) NULL,
    shipping_carrier VARCHAR(100) NULL,
    tracking_number VARCHAR(150) NULL,
    
    -- Fulfillment Location & System Dates
    location_id VARCHAR(64) NULL COMMENT 'Fulfillment location ID (GUID)',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    
    UNIQUE KEY uq_orders_region_order_id (region, order_id),
    INDEX idx_orders_status (status),
    INDEX idx_orders_received_date (received_date),
    INDEX idx_orders_processed_date (processed_date),
    INDEX idx_orders_num_order_id (num_order_id),
    INDEX idx_orders_channel_ref (channel_reference_number),
    INDEX idx_orders_source (source)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ----------------------------------------------------------------------------
-- 5. Order Items
-- Line items for each order
-- Unique on (region, order_id, row_id)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS lw_order_items (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    region VARCHAR(10) NOT NULL COMMENT 'Region identifier (e.g. US, CA)',
    order_id VARCHAR(64) NOT NULL COMMENT 'Linnworks Order ID (GUID)',
    row_id VARCHAR(64) NOT NULL COMMENT 'Linnworks Item RowId (GUID)',
    sku VARCHAR(100) NOT NULL,
    item_title VARCHAR(255) NULL,
    quantity INT NOT NULL DEFAULT 1,
    unit_price DECIMAL(10, 2) NOT NULL DEFAULT 0.00,
    line_total DECIMAL(10, 2) NOT NULL DEFAULT 0.00,
    tax_rate DECIMAL(5, 2) NOT NULL DEFAULT 0.00,
    tax_amount DECIMAL(10, 2) NOT NULL DEFAULT 0.00,
    discount DECIMAL(10, 2) NOT NULL DEFAULT 0.00,
    stock_item_id VARCHAR(64) NULL COMMENT 'Linnworks StockItemId (GUID)',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    
    UNIQUE KEY uq_order_items_region_order_row (region, order_id, row_id),
    INDEX idx_order_items_sku (sku),
    INDEX idx_order_items_order_id (order_id),
    INDEX idx_order_items_stock_item_id (stock_item_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ----------------------------------------------------------------------------
-- 6. Order Refunds
-- Order and line item refunds (full and partial)
-- Unique on (region, refund_id)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS lw_order_refunds (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    region VARCHAR(10) NOT NULL COMMENT 'Region identifier (e.g. US, CA)',
    order_id VARCHAR(64) NOT NULL COMMENT 'Linnworks Order ID (GUID)',
    refund_id VARCHAR(100) NOT NULL COMMENT 'Refund ID or reference',
    sku VARCHAR(100) NULL COMMENT 'Nullable for order-level refunds',
    quantity INT NULL COMMENT 'Nullable for order-level refunds',
    amount DECIMAL(10, 2) NOT NULL DEFAULT 0.00,
    reason VARCHAR(255) NULL,
    refunded_at DATETIME NOT NULL COMMENT 'UTC datetime of refund',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    
    UNIQUE KEY uq_refunds_region_refund_id (region, refund_id),
    INDEX idx_refunds_order_id (order_id),
    INDEX idx_refunds_refunded_at (refunded_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ----------------------------------------------------------------------------
-- 7. Pipeline Log
-- Audit log reserved for the 3PL fulfillment pipeline
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS lw_pipeline_log (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    region VARCHAR(10) NOT NULL COMMENT 'Region identifier (e.g. US, CA)',
    order_id VARCHAR(64) NOT NULL,
    action VARCHAR(20) NOT NULL COMMENT 'send / swap / hold / error',
    details TEXT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    
    INDEX idx_pipeline_region_action (region, action),
    INDEX idx_pipeline_order_id (order_id),
    INDEX idx_pipeline_created_at (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ----------------------------------------------------------------------------
-- 8. Sync State
-- Tracks high-water mark / cursor and execution metrics for delta syncs
-- Unique on (region, entity)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS lw_sync_state (
    id INT AUTO_INCREMENT PRIMARY KEY,
    region VARCHAR(10) NOT NULL COMMENT 'Region identifier (e.g. US, CA)',
    entity VARCHAR(50) NOT NULL COMMENT 'Entity name (e.g. products, locations, stock_levels, orders)',
    last_success_at DATETIME NULL,
    last_cursor VARCHAR(100) NULL COMMENT 'Last processed timestamp or cursor',
    last_error TEXT NULL,
    run_count INT NOT NULL DEFAULT 0,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    
    UNIQUE KEY uq_sync_region_entity (region, entity)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
