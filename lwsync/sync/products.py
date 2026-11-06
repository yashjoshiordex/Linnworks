"""Products and extended properties synchronization worker."""

from decimal import Decimal
from typing import Any, Dict, List, Optional
from lwsync.client.base import LinnworksClient
from lwsync.db import get_db_cursor
from lwsync.logger import logger
from lwsync.sync.state import update_sync_success, record_sync_error

# Explicit mapping for the 8 extended properties defined in prompt
# Property Name in Linnworks -> Database Column Name
EXTENDED_PROPERTY_MAP = {
    "brand": "brand",
    "fill sku": "fill_sku",
    "fillsku": "fill_sku",
    "product line": "product_line",
    "productline": "product_line",
    "mastersku": "master_sku",
    "master sku": "master_sku",
    "substitute 1": "substitute_1",
    "substitute1": "substitute_1",
    "substitute 2": "substitute_2",
    "substitute2": "substitute_2",
    "mfr part number": "mfr_part_number",
    "mfrpartnumber": "mfr_part_number",
    "mpn": "mfr_part_number",
    "product tag": "product_tag",
    "producttag": "product_tag",
}


def sync_products(client: LinnworksClient, region: str, dry_run: bool = False) -> int:
    """Synchronize catalog products and the 8 extended properties into lw_products.
    
    Idempotent on (region, sku).
    """
    region_key = region.upper()
    logger.info(f"[{region_key}] Starting products sync...")

    try:
        # Step 1: Ingest stock items page-by-page or batch
        if hasattr(client, "get_products"):
            raw_products = client.get_products(region_key, page=1, page_size=200)
        else:
            raw_products = client.get_stock_items(region_key, page_number=1, page_size=200)

        if not raw_products:
            logger.info(f"[{region_key}] No products returned from client.")
            return 0

        logger.info(f"[{region_key}] Fetched {len(raw_products)} products.")

        # Step 2: Build property lookup map per StockItemId (if client exposes extended properties)
        # Some endpoints return extended properties attached to items, others via separate call
        properties_by_item_id: Dict[str, Dict[str, str]] = {}
        if hasattr(client, "_load_fixture"):
            # If in fixture mode, read mock extended properties
            mock_props = client._load_fixture(region_key, "extended_properties")
            for prop in mock_props:
                item_id = prop.get("StockItemId")
                prop_name = (prop.get("ProperyName") or prop.get("PropertyName", "")).strip().lower()
                prop_val = prop.get("PropertyValue", "")
                if item_id:
                    if item_id not in properties_by_item_id:
                        properties_by_item_id[item_id] = {}
                    col = EXTENDED_PROPERTY_MAP.get(prop_name)
                    if col:
                        properties_by_item_id[item_id][col] = prop_val
                    else:
                        logger.warning(
                            f"[{region_key}] Unmapped extended property '{prop_name}' on item {item_id}. Ignored."
                        )

        # Step 3: Prepare records for bulk upsert
        sql = """
            INSERT INTO lw_products (
                region, stock_item_id, sku, title, cost, retail_price,
                category, barcode, weight,
                brand, fill_sku, product_line, master_sku,
                substitute_1, substitute_2, mfr_part_number, product_tag
            ) VALUES (
                %s, %s, %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s, %s
            )
            ON DUPLICATE KEY UPDATE
                stock_item_id = VALUES(stock_item_id),
                title = VALUES(title),
                cost = VALUES(cost),
                retail_price = VALUES(retail_price),
                category = VALUES(category),
                barcode = VALUES(barcode),
                weight = VALUES(weight),
                brand = VALUES(brand),
                fill_sku = VALUES(fill_sku),
                product_line = VALUES(product_line),
                master_sku = VALUES(master_sku),
                substitute_1 = VALUES(substitute_1),
                substitute_2 = VALUES(substitute_2),
                mfr_part_number = VALUES(mfr_part_number),
                product_tag = VALUES(product_tag),
                updated_at = CURRENT_TIMESTAMP;
        """

        records = []
        for p in raw_products:
            sku = p.get("ItemNumber") or p.get("SKU")
            if not sku:
                continue

            stock_item_id = p.get("StockItemId") or p.get("ItemId")
            title = p.get("ItemTitle") or p.get("Title")
            cost = Decimal(str(p.get("PurchasePrice") or p.get("Cost", "0.0000")))
            retail_price = Decimal(str(p.get("RetailPrice") or "0.0000"))
            category = p.get("CategoryName") or p.get("Category")
            barcode = p.get("BarcodeNumber") or p.get("Barcode")
            weight = Decimal(str(p.get("Weight") or "0.0000"))

            # Extract extended properties from lookup or inline payload
            props = properties_by_item_id.get(stock_item_id, {})
            brand = props.get("brand") or p.get("Brand")
            fill_sku = props.get("fill_sku") or p.get("FillSKU")
            product_line = props.get("product_line") or p.get("ProductLine")
            master_sku = props.get("master_sku") or p.get("MasterSKU")
            sub_1 = props.get("substitute_1") or p.get("Substitute1")
            sub_2 = props.get("substitute_2") or p.get("Substitute2")
            mpn = props.get("mfr_part_number") or p.get("MfrPartNumber")
            tag = props.get("product_tag") or p.get("ProductTag")

            records.append((
                region_key, stock_item_id, str(sku), title, cost, retail_price,
                category, barcode, weight,
                brand, fill_sku, product_line, master_sku,
                sub_1, sub_2, mpn, tag
            ))

        if dry_run:
            logger.info(f"[{region_key}] [DRY-RUN] Would upsert {len(records)} products.")
            return len(records)

        # Step 4: Execute bulk upsert in 500-item chunks
        chunk_size = 500
        with get_db_cursor() as cursor:
            for i in range(0, len(records), chunk_size):
                cursor.executemany(sql, records[i : i + chunk_size])

        update_sync_success(region_key, "products")
        logger.info(f"[{region_key}] Successfully upserted {len(records)} products into lw_products.")
        return len(records)

    except Exception as e:
        record_sync_error(region_key, "products", str(e))
        logger.error(f"[{region_key}] Products sync failed: {e}")
        raise

