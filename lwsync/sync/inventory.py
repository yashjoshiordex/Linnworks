"""Sync stock locations (lw_locations) and stock per SKU and location (lw_stock_levels).

Locations are e.g. "US | AJC", "Default", "Review". The pipeline reads stock for the master
SKU at the AJC location (US | AJC / CA | AJC).
Stock columns: on_hand = Linnworks "Level", allocated = "In Order" (reserved by open orders),
available = "Available" (on_hand - allocated; can be negative).
"""

from typing import Any, Dict, List, Optional
from lwsync.client.base import LinnworksClient
from lwsync.db import get_db_cursor, with_db_retry
from lwsync.logger import logger
from lwsync.sync.state import update_sync_success, record_sync_error


def sync_locations(client: LinnworksClient, region: str, dry_run: bool = False) -> int:
    """Synchronize warehouse locations into lw_locations.
    
    Idempotent on (region, location_id).
    """
    region_key = region.upper()
    logger.info(f"[{region_key}] Starting locations sync...")

    try:
        raw_locations = client.get_locations(region_key)
        if not raw_locations:
            logger.info(f"[{region_key}] No locations returned.")
            return 0

        logger.info(f"[{region_key}] Fetched {len(raw_locations)} locations.")

        if dry_run:
            logger.info(f"[{region_key}] [DRY-RUN] Would upsert {len(raw_locations)} locations.")
            return len(raw_locations)

        sql = """
            INSERT INTO lw_locations (
                region, location_id, name, is_default
            ) VALUES (%s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                name = VALUES(name),
                is_default = VALUES(is_default),
                updated_at = CURRENT_TIMESTAMP;
        """

        records = []
        for loc in raw_locations:
            loc_id = loc.get("StockLocationId") or loc.get("LocationId")
            name = loc.get("LocationName") or loc.get("Name", "Unknown")
            is_default = bool(loc.get("IsDefault", False))
            if loc_id:
                records.append((region_key, str(loc_id), name, is_default))
            else:
                logger.warning(f"[{region_key}] Skipped location without an ID: '{name}'")

        def write() -> None:
            with get_db_cursor() as cursor:
                cursor.executemany(sql, records)

        with_db_retry(write, f"{region_key} locations")

        update_sync_success(region_key, "locations")
        logger.info(f"[{region_key}] Successfully upserted {len(records)} locations into lw_locations.")
        return len(records)

    except Exception as e:
        record_sync_error(region_key, "locations", str(e))
        logger.error(f"[{region_key}] Locations sync failed: {e}")  # traceback: logged by the CLI
        raise


def _first_value(item: Dict[str, Any], *keys: str) -> Any:
    """First key present with a non-None value (a real 0 is kept, unlike `a or b`)."""
    for key in keys:
        value = item.get(key)
        if value is not None:
            return value
    return None


def _first_int(item: Dict[str, Any], *keys: str) -> int:
    """Like _first_value, as an int; 0 if none of the keys has a value."""
    value = _first_value(item, *keys)
    return int(value) if value is not None else 0


def sync_stock_levels(
    client: LinnworksClient,
    region: str,
    dry_run: bool = False,
    raw_products: Optional[List[Dict[str, Any]]] = None,
) -> int:
    """Synchronize physical stock levels into lw_stock_levels.
    
    Idempotent on (region, sku, location_id).
    """
    region_key = region.upper()
    logger.info(f"[{region_key}] Starting stock levels sync...")

    try:
        # Two sources with the same result:
        # - raw_products given (the "all" command): reuse the catalog already downloaded.
        # - otherwise: client.get_stock_levels() downloads the catalog itself.
        # Both flatten each item's StockLevels list into one row per (sku, location).
        skipped = 0  # stock entries without a SKU or location ID (warned about below)
        if raw_products is not None:
            stock_rows = []
            for p in raw_products:
                sku = p.get("ItemNumber") or p.get("SKU")
                levels = p.get("StockLevels") or []
                for lvl in levels:
                    loc = lvl.get("Location") or {}
                    loc_id = loc.get("StockLocationId") or lvl.get("StockLocationId")
                    if not (sku and loc_id):
                        skipped += 1
                        continue
                    # GetStockItemsFull keys: StockLevel (on hand), InOrders / InOrderBook, Available.
                    stock_rows.append({
                        "SKU": sku,
                        "StockLocationId": loc_id,
                        "StockLevel": lvl.get("StockLevel"),
                        "InOrders": lvl.get("InOrders", lvl.get("InOrderBook")),
                        "Available": lvl.get("Available"),
                    })
        else:
            stock_rows = client.get_stock_levels(region_key)
        total_upserted = 0

        sql = """
            INSERT INTO lw_stock_levels (
                region, sku, location_id, on_hand, allocated, available
            ) VALUES (%s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                on_hand = VALUES(on_hand),
                allocated = VALUES(allocated),
                available = VALUES(available),
                updated_at = CURRENT_TIMESTAMP;
        """

        if stock_rows:
            batch = []
            for item in stock_rows:
                sku = item.get("SKU") or item.get("ItemNumber")
                loc_id = item.get("StockLocationId") or item.get("LocationId")
                if not sku or not loc_id:
                    skipped += 1
                    continue

                # Different sources name the same values differently; take the first one present.
                # Available falls back to on_hand - allocated if Linnworks didn't send it.
                on_hand = _first_int(item, "Quantity", "StockLevel")
                allocated = _first_int(item, "InOrders", "InOrderBook", "Allocated")
                raw_available = _first_value(item, "Available")
                available = int(raw_available) if raw_available is not None else on_hand - allocated

                batch.append((region_key, str(sku), str(loc_id), on_hand, allocated, available))

            if dry_run:
                logger.info(f"[{region_key}] [DRY-RUN] Would upsert {len(batch)} stock rows.")
                total_upserted += len(batch)
            else:
                def write() -> None:
                    with get_db_cursor() as cursor:
                        chunk_size = 500
                        for i in range(0, len(batch), chunk_size):
                            cursor.executemany(sql, batch[i : i + chunk_size])

                with_db_retry(write, f"{region_key} stock levels")
                total_upserted += len(batch)

        if skipped:
            logger.warning(f"[{region_key}] Skipped {skipped} stock entries without a SKU or location ID.")

        if not dry_run:
            update_sync_success(region_key, "stock_levels")

        logger.info(f"[{region_key}] Successfully synced {total_upserted} stock rows into lw_stock_levels.")
        return total_upserted

    except Exception as e:
        record_sync_error(region_key, "stock_levels", str(e))
        logger.error(f"[{region_key}] Stock levels sync failed: {e}")  # traceback: logged by the CLI
        raise

