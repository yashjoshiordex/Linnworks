"""Locations and stock levels synchronization worker."""

from typing import Any, List
from lwsync.client.base import LinnworksClient
from lwsync.db import get_db_cursor
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

        with get_db_cursor() as cursor:
            cursor.executemany(sql, records)

        update_sync_success(region_key, "locations")
        logger.info(f"[{region_key}] Successfully upserted {len(records)} locations into lw_locations.")
        return len(records)

    except Exception as e:
        record_sync_error(region_key, "locations", str(e))
        logger.error(f"[{region_key}] Locations sync failed: {e}")
        raise


def sync_stock_levels(client: LinnworksClient, region: str, dry_run: bool = False) -> int:
    """Synchronize physical stock levels into lw_stock_levels.
    
    Idempotent on (region, sku, location_id).
    """
    region_key = region.upper()
    logger.info(f"[{region_key}] Starting stock levels sync...")

    try:
        # Fetch stock levels (either across all locations or location by location)
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
                    continue

                on_hand = int(item.get("Quantity", item.get("StockLevel", 0)))
                allocated = int(item.get("InOrders", item.get("Allocated", 0)))
                available = int(item.get("Available", on_hand - allocated))

                batch.append((region_key, str(sku), str(loc_id), on_hand, allocated, available))

            if dry_run:
                logger.info(f"[{region_key}] [DRY-RUN] Would upsert {len(batch)} stock rows.")
                total_upserted += len(batch)
            else:
                with get_db_cursor() as cursor:
                    chunk_size = 500
                    for i in range(0, len(batch), chunk_size):
                        cursor.executemany(sql, batch[i : i + chunk_size])
                total_upserted += len(batch)

        if not dry_run:
            update_sync_success(region_key, "stock_levels")

        logger.info(f"[{region_key}] Successfully synced {total_upserted} stock rows into lw_stock_levels.")
        return total_upserted

    except Exception as e:
        record_sync_error(region_key, "stock_levels", str(e))
        logger.error(f"[{region_key}] Stock levels sync failed: {e}")
        raise

