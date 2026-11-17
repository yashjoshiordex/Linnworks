"""Refunds synchronization and order status state machine worker."""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, List, Optional
from lwsync.client.base import LinnworksClient
from lwsync.db import get_db_cursor
from lwsync.logger import logger
from lwsync.sync.state import record_sync_error, update_sync_success


def parse_utc_datetime(dt_str: Optional[str]) -> Optional[str]:
    """Parse ISO or standard datetime string and return MySQL UTC string."""
    if not dt_str:
        return None
    try:
        clean = dt_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return dt_str[:19].replace("T", " ")


def sync_refunds(
    client: LinnworksClient,
    region: str,
    from_date: str,
    to_date: str,
    dry_run: bool = False,
    update_state: bool = False,
) -> int:
    """Synchronize refunds into lw_order_refunds and transition fully refunded orders.
    
    Idempotent on (region, order_id, refund_id).
    Business rule: When total refunded >= order total, update lw_orders.status = 'refunded'.
    """
    region_key = region.upper()
    logger.info(f"[{region_key}] Starting refunds sync (from={from_date}, to={to_date})...")

    try:
        raw_refunds = client.get_refunds(region_key, from_date, to_date)
        if not raw_refunds:
            logger.info(f"[{region_key}] No refunds found in window.")
            if not dry_run and update_state:
                update_sync_success(region_key, "refunds", new_cursor=to_date)
            return 0

        logger.info(f"[{region_key}] Fetched {len(raw_refunds)} refund record(s).")

        refund_records = []
        affected_order_ids = set()
        skipped = 0

        for r in raw_refunds:
            order_id = (
                r.get("OrderId")
                or r.get("pkOrderID")
                or r.get("nOrderId")
                or r.get("fkOrderId")
                or r.get("fkOrderID")
            )
            refund_id = (
                r.get("RefundId")
                or r.get("pkRefundId")
                or r.get("RefundHeaderId")
                or r.get("RefundRowId")
                or r.get("fkRefundId")
                or r.get("Reference")
                or r.get("RowId")
            )
            if not order_id or not refund_id:
                skipped += 1
                continue

            sku = r.get("SKU") or r.get("ItemNumber") or r.get("ChannelSKU")
            raw_qty = r.get("Quantity") if r.get("Quantity") is not None else r.get("Qty")
            qty = int(raw_qty) if raw_qty is not None else 0
            amount = Decimal(
                str(
                    r.get("Amount")
                    or r.get("RefundAmount")
                    or r.get("TotalRefundAmount")
                    or r.get("Total")
                    or "0.0000"
                )
            )
            reason = (
                r.get("Reason")
                or r.get("RefundReason")
                or r.get("ReasonCategory")
                or "Customer Refund"
            )
            refunded_at = parse_utc_datetime(
                r.get("RefundedAt")
                or r.get("RefundDate")
                or r.get("Date")
                or r.get("CreatedDate")
                or r.get("ActionDate")
            )

            if not refunded_at:
                refunded_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

            refund_records.append((
                region_key,
                str(order_id),
                str(refund_id),
                str(sku) if sku else None,
                qty,
                amount,
                reason,
                refunded_at,
            ))
            affected_order_ids.add(str(order_id))

        if skipped:
            # Usually means the API field names differ from what we map; keys only, no customer data.
            logger.warning(
                f"[{region_key}] Skipped {skipped} refund record(s) without an order or refund ID "
                f"(keys seen: {sorted(raw_refunds[0].keys()) if isinstance(raw_refunds[0], dict) else type(raw_refunds[0]).__name__})"
            )

        if dry_run:
            logger.info(f"[{region_key}] [DRY-RUN] Would upsert {len(refund_records)} refund rows.")
            return len(refund_records)

        # Step 1: Bulk Upsert into lw_order_refunds
        sql_refunds = """
            INSERT INTO lw_order_refunds (
                region, order_id, refund_id, sku, quantity, amount, reason, refunded_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                sku = VALUES(sku),
                quantity = VALUES(quantity),
                amount = VALUES(amount),
                reason = VALUES(reason),
                refunded_at = VALUES(refunded_at);
        """

        chunk_size = 500
        # One transaction for both steps: refund rows and the 'refunded' status commit together or not at all.
        with get_db_cursor() as cursor:
            for i in range(0, len(refund_records), chunk_size):
                cursor.executemany(sql_refunds, refund_records[i : i + chunk_size])

            # Step 2: Evaluate Full Refund Status for Affected Orders
            # Prompt requirement: "When an order is fully refunded, set orders.status to refunded."
            for oid in affected_order_ids:
                # Get order total
                cursor.execute(
                    "SELECT total, status FROM lw_orders WHERE region = %s AND order_id = %s LIMIT 1;",
                    (region_key, oid),
                )
                order_row = cursor.fetchone()
                if not order_row:
                    continue

                order_total = Decimal(str(order_row.get("total", "0.0000")))

                # Calculate sum of all refunds for this order
                cursor.execute(
                    "SELECT COALESCE(SUM(amount), 0.0000) AS total_refunded FROM lw_order_refunds WHERE region = %s AND order_id = %s;",
                    (region_key, oid),
                )
                sum_row = cursor.fetchone()
                total_refunded = Decimal(str(sum_row.get("total_refunded", "0.0000")))

                if order_total > 0 and total_refunded >= order_total:
                    cursor.execute(
                        "UPDATE lw_orders SET status = 'refunded', updated_at = CURRENT_TIMESTAMP WHERE region = %s AND order_id = %s;",
                        (region_key, oid),
                    )
                    logger.info(f"[{region_key}] Order {oid} is fully refunded ({total_refunded} >= {order_total}). Status set to 'refunded'.")

        if not dry_run and update_state:
            update_sync_success(region_key, "refunds", new_cursor=to_date)
        logger.info(f"[{region_key}] Successfully synced {len(refund_records)} refunds into lw_order_refunds.")
        return len(refund_records)

    except Exception as e:
        if not dry_run and update_state:
            record_sync_error(region_key, "refunds", str(e))
        logger.error(f"[{region_key}] Refunds sync failed: {e}")
        raise

