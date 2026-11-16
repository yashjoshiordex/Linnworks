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


def sync_products(
    client: LinnworksClient,
    region: str,
    dry_run: bool = False,
    raw_products: Optional[List[Dict[str, Any]]] = None,
) -> int:
    """Synchronize catalog products and the 8 extended properties into lw_products.
    
    Idempotent on (region, sku).
    """
    region_key = region.upper()
    logger.info(f"[{region_key}] Starting products sync...")

    try:
        # Step 1: Ingest stock items across ALL pages
        if raw_products is None:
            raw_products = client.get_all_products(region_key, page_size=200)

        if not raw_products:
            logger.info(f"[{region_key}] No products returned from client.")
            return 0

        logger.info(f"[{region_key}] Fetched {len(raw_products)} total products (all pages).")

        # Step 2: Build property lookup map per StockItemId
        properties_by_item_id: Dict[str, Dict[str, str]] = {}
        ext_props_loaded = False

        # First, attempt to extract properties embedded in raw_products (from GetStockItemsFull)
        for p in raw_products:
            item_id = p.get("StockItemId")
            item_props = p.get("ItemExtendedProperties") or p.get("ExtendedProperties") or []
            if item_id and item_props:
                if item_id not in properties_by_item_id:
                    properties_by_item_id[item_id] = {}
                for prop in item_props:
                    prop_name = (prop.get("ProperyName") or prop.get("PropertyName", "")).strip().lower()
                    prop_val = prop.get("PropertyValue", "")
                    col = EXTENDED_PROPERTY_MAP.get(prop_name)
                    if col:
                        properties_by_item_id[item_id][col] = prop_val

        # If not embedded (e.g. legacy fixture client), fall back to client method
        if not properties_by_item_id:
            try:
                extended_props = client.get_product_extended_properties(region_key)
                for prop in extended_props:
                    item_id = prop.get("StockItemId") or prop.get("fkStockItemId")
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
            except Exception as e:
                logger.warning(
                    f"[{region_key}] Could not fetch extended properties: {e}. "
                    "Extended property columns will NOT be overwritten for existing rows."
                )

        if properties_by_item_id:
            ext_props_loaded = True

        # Step 3: Prepare records for bulk upsert
        # When extended properties failed to load, use COALESCE to protect existing values
        if ext_props_loaded:
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
        else:
            # Extended properties fetch failed — use COALESCE to preserve existing values
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
                    brand = COALESCE(VALUES(brand), brand),
                    fill_sku = COALESCE(VALUES(fill_sku), fill_sku),
                    product_line = COALESCE(VALUES(product_line), product_line),
                    master_sku = COALESCE(VALUES(master_sku), master_sku),
                    substitute_1 = COALESCE(VALUES(substitute_1), substitute_1),
                    substitute_2 = COALESCE(VALUES(substitute_2), substitute_2),
                    mfr_part_number = COALESCE(VALUES(mfr_part_number), mfr_part_number),
                    product_tag = COALESCE(VALUES(product_tag), product_tag),
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
                region_key,
                stock_item_id,
                str(sku)[:100],
                str(title)[:500] if title is not None else None,
                cost,
                retail_price,
                str(category)[:255] if category is not None else None,
                str(barcode)[:100] if barcode is not None else None,
                weight,
                str(brand)[:100] if brand is not None else None,
                str(fill_sku)[:100] if fill_sku is not None else None,
                str(product_line)[:100] if product_line is not None else None,
                str(master_sku)[:100] if master_sku is not None else None,
                str(sub_1)[:100] if sub_1 is not None else None,
                str(sub_2)[:100] if sub_2 is not None else None,
                str(mpn) if mpn is not None else None,
                str(tag)[:255] if tag is not None else None,
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

