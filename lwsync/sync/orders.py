"""Sync orders into lw_orders and their lines into lw_order_items.

One orders run (sync_orders_delta) does three steps:
  1. Open orders: ALL current open orders are re-read every run (status, location and lines can
     change at any time). Open orders in the DB that Linnworks no longer returns -> 'deleted'.
  2. Changed orders: everything updated since the last run (cursor in lw_sync_state, minus a
     10-minute overlap), including processed ones. That is how cancellations are picked up.
  3. Refunds for the same window (refunds.py).

Status values in lw_orders: open, parked (unpaid / on hold), shipped (processed), cancelled,
refunded, deleted. In these accounts nothing is processed (shipped) in Linnworks; the
Processed list only holds cancellations.

Money: Linnworks is inconsistent between channels, so values are normalised here. The
meaning of each column is documented in sql/001_create_tables.sql.
"""

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from lwsync.client.base import LinnworksClient
from lwsync.config import Config
from lwsync.db import get_db_cursor, with_db_retry
from lwsync.logger import logger
from lwsync.sync.refunds import parse_utc_datetime, sync_refunds
from lwsync.sync.state import get_sync_cursor, record_sync_error, update_sync_success


# Linnworks rejects a v2 orders fromDate older than this (HTTP 400 "From Date must be within
# the last 35 days", confirmed 18 Nov 2026). Windows are clamped to it, with a 5-minute margin
# so a run that starts right at the limit isn't rejected.
V2_MAX_LOOKBACK = timedelta(days=35) - timedelta(minutes=5)


def _money(*values: Any, default: Any = "0") -> Decimal:
    """First value that is not None/empty, as Decimal. Keeps a real 0 (unlike `a or b`)."""
    for value in values:
        if value is not None and value != "":
            return Decimal(str(value))
    return Decimal(str(default))


def _is_true(value: Any) -> bool:
    """True for a real True or the string "true" (any case). Linnworks sends both."""
    return value is True or (isinstance(value, str) and value.strip().lower() == "true")


def _composite_children(item: dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """Components of a composite listing line, as (single child SKU, JSON list of all children).

    Most listing SKUs are composite items in Linnworks with exactly one component, the master
    stock item that is actually shipped (e.g. CA-AJC-D7.5S-N-0-131347 -> AJC-D7S); about 85-91%
    of open-order lines on 18 Nov 2026. The API nests them under the line as CompositeSubItems.
    The child's Quantity is the total units for the line (parent qty 8 -> child qty 8). All money
    stays on the parent line, so children are stored for reference only, not as extra lines.
    Returns (None, None) for a non-composite line; the single SKU is None if there are several.
    """
    subs = [s for s in (item.get("CompositeSubItems") or []) if isinstance(s, dict)]
    children = [
        {
            "sku": str(s.get("SKU") or s.get("ItemNumber") or "")[:100],
            "quantity": int(s.get("Quantity") or 0),
            "stock_item_id": str(s["StockItemId"]) if s.get("StockItemId") else None,
        }
        for s in subs
    ]
    children = [c for c in children if c["sku"]]
    if not children:
        return None, None
    single = children[0]["sku"] if len(children) == 1 else None
    return single, json.dumps(children)


def is_cancelled_order(raw_order: dict[str, Any]) -> bool:
    """True if Linnworks flags the order as cancelled.

    Linnworks sets GeneralInfo.IsCancelled on cancelled orders (confirmed on every cancelled
    processed order in the live US and CA accounts). Do NOT use GeneralInfo.Status for this:
    its values are 0 unpaid, 1 paid, 2 return, 3 pending, 4 resend, so 3 means PENDING.
    Folder names are not used either: they are user-defined labels, not a cancellation flag.
    """
    gen_info = raw_order.get("GeneralInfo") or {}
    return _is_true(gen_info.get("IsCancelled")) or _is_true(raw_order.get("IsCancelled"))


def _extract_order_and_items(
    region: str,
    raw_order: dict[str, Any],
    default_status: str = "open",
) -> Tuple[tuple, list[tuple]]:
    """Turn one raw Linnworks order into (row for lw_orders, rows for lw_order_items).

    Pure function, no DB access, so it is easy to unit test. The tuple order must match the
    column order in SQL_UPSERT_ORDER / SQL_UPSERT_ITEM below.
    Most values try several field names (`a or b or c`) because older Linnworks endpoints and
    the fixtures use different names than v2 (e.g. pkOrderID vs OrderId).
    """
    region_key = region.upper()

    # Order ID & Numbers
    order_id = (
        raw_order.get("OrderId")
        or raw_order.get("pkOrderID")
        or raw_order.get("nOrderId")
    )
    order_number = raw_order.get("NumOrderId") or raw_order.get("nOrderId")

    gen_info = raw_order.get("GeneralInfo") or {}
    cust_info = raw_order.get("CustomerInfo") or {}
    addr_info = cust_info.get("Address") or raw_order.get("ShippingAddress") or {}
    bill_info = cust_info.get("BillingAddress") or addr_info
    ship_info = raw_order.get("ShippingInfo") or {}
    totals = raw_order.get("TotalsInfo") or raw_order.get("Totals") or {}

    channel_ref = (
        gen_info.get("ReferenceNum")
        or gen_info.get("ExternalReferenceNum")
        or raw_order.get("ReferenceNum")
        or raw_order.get("ChannelReferenceNum")
    )
    source = gen_info.get("Source") or raw_order.get("Source")
    subsource = gen_info.get("SubSource") or raw_order.get("SubSource")

    # Status. raw_status keeps Linnworks' own code for reference (0 unpaid, 1 paid, 2 return,
    # 3 pending, 4 resend). Our status is decided in this order:
    #   cancelled flag -> 'cancelled'; from the processed list -> 'shipped';
    #   open and on hold / unpaid -> 'parked'; otherwise 'open'.
    raw_status = gen_info.get("Status") if "Status" in gen_info else raw_order.get("Status")
    is_parked = bool(gen_info.get("IsParked", False) or gen_info.get("HoldOrParked", False) or gen_info.get("HoldOrWait", False))

    effective_status = raw_order.get("_sync_status") or default_status
    is_cancelled = effective_status == "cancelled" or is_cancelled_order(raw_order)

    if is_cancelled:
        status = "cancelled"
    elif effective_status in ("shipped", "processed") or raw_order.get("Processed") is True:
        status = "shipped"
    elif effective_status == "open":
        status = "parked" if is_parked else "open"
    else:
        status = effective_status

    # Folders are user-defined labels in Linnworks, stored as a JSON list for reference only.
    folder_list = raw_order.get("FolderName") or raw_order.get("Folders") or []
    folders_str = json.dumps(folder_list) if isinstance(folder_list, list) else str(folder_list)

    # Dates, stored as UTC. ProcessedOn is empty for cancelled orders (Linnworks sends none),
    # so processed_date stays NULL for them.
    date_received = parse_utc_datetime(
        gen_info.get("ReceivedDate") or raw_order.get("ReceivedDate") or raw_order.get("dReceivedDate")
    )
    date_dispatch_by = parse_utc_datetime(
        gen_info.get("DespatchByDate") or raw_order.get("DespatchByDate") or raw_order.get("dDispatchByDate")
    )
    date_processed = parse_utc_datetime(
        raw_order.get("ProcessedOn")
        or raw_order.get("ProcessedDate")
        or gen_info.get("ProcessedDate")
        or raw_order.get("dProcessedOn")
    )

    # Customer and addresses. Amazon orders may be redacted in Linnworks later, but the DB keeps
    # what was synced before (COALESCE in the upsert never overwrites a value with NULL).
    # Unpaid Amazon orders have no name or address yet.
    customer_name = (
        gen_info.get("Name")
        or addr_info.get("FullName")
        or raw_order.get("CustomerName")
        or raw_order.get("cFullName")
    )
    customer_email = (
        addr_info.get("EmailAddress")
        or cust_info.get("EmailAddress")
        or gen_info.get("Email")
        or raw_order.get("Email")
        or raw_order.get("cEmailAddress")
    )
    customer_phone = (
        addr_info.get("PhoneNumber")
        or cust_info.get("Phone")
        or raw_order.get("Phone")
    )

    # Shipping Address
    ship_name = addr_info.get("FullName") or customer_name
    ship_company = addr_info.get("Company")
    ship_a1 = addr_info.get("Address1") or raw_order.get("Address1")
    ship_a2 = addr_info.get("Address2") or raw_order.get("Address2")
    ship_a3 = addr_info.get("Address3") or raw_order.get("Address3")
    ship_town = addr_info.get("Town") or raw_order.get("Town")
    ship_region = addr_info.get("Region") or raw_order.get("Region")
    ship_postcode = addr_info.get("PostCode") or raw_order.get("cPostCode")
    ship_country = addr_info.get("Country") or raw_order.get("Country")

    # Billing address: falls back to the shipping address when Linnworks has none.
    bill_name = bill_info.get("FullName") or ship_name
    bill_company = bill_info.get("Company") or ship_company
    bill_a1 = bill_info.get("Address1") or ship_a1
    bill_a2 = bill_info.get("Address2") or ship_a2
    bill_a3 = bill_info.get("Address3") or ship_a3
    bill_town = bill_info.get("Town") or ship_town
    bill_region = bill_info.get("Region") or ship_region
    bill_postcode = bill_info.get("PostCode") or ship_postcode
    bill_country = bill_info.get("Country") or ship_country

    # Financials
    currency = (
        totals.get("Currency")
        or gen_info.get("Currency")
        or raw_order.get("Currency")
        or ("CAD" if region_key == "CA" else "USD")
    )
    # Money fields use the first value that is present (a real 0 is kept; `a or b` would skip it).
    subtotal = _money(totals.get("Subtotal"), totals.get("SubTotal"), raw_order.get("Subtotal"), raw_order.get("SubTotal"))
    tax = _money(totals.get("Tax"), totals.get("TotalTax"), raw_order.get("Tax"), raw_order.get("fTax"))
    # Shipping excluding tax: any tax on shipping is already inside `tax`, so total = subtotal + tax + shipping.
    shipping_charge = _money(
        totals.get("PostageCostExTax"),
        totals.get("PostageCost"),
        totals.get("Postage"),
        ship_info.get("Cost"),
        raw_order.get("ShippingCost"),
        raw_order.get("fPostageCost"),
    )
    total = _money(totals.get("TotalCharge"), totals.get("Total"), raw_order.get("Total"), raw_order.get("fTotalCharge"))
    payment_method = (
        totals.get("PaymentMethod")
        or raw_order.get("PaymentMethod")
        or gen_info.get("PaymentMethod")
    )

    # Shipping details
    shipping_service = (
        ship_info.get("PostalServiceName")
        or ship_info.get("ServiceName")
        or raw_order.get("PostalServiceName")
    )
    shipping_carrier = (
        ship_info.get("PostalServiceVendor")
        or ship_info.get("Vendor")
        or raw_order.get("PostalServiceVendor")
    )
    tracking_number = (
        ship_info.get("TrackingNumber")
        or raw_order.get("PostalTrackingNumber")
        or raw_order.get("TrackingNumber")
    )
    # The location the order is assigned to (Default, US | AJC, ...); see lw_locations.
    fulfillment_loc_id = (
        raw_order.get("FulfilmentLocationId")
        or raw_order.get("LocationId")
        or raw_order.get("StockLocationId")
    )

    order_tuple = (
        region_key,
        str(order_id),
        int(order_number) if order_number else None,
        str(channel_ref) if channel_ref else None,
        source,
        subsource,
        status,
        str(raw_status) if raw_status is not None else None,
        is_parked,
        folders_str,
        date_received,
        date_dispatch_by,
        date_processed,
        customer_name,
        customer_email,
        customer_phone,
        ship_name,
        ship_company,
        ship_a1,
        ship_a2,
        ship_a3,
        ship_town,
        ship_region,
        ship_postcode,
        ship_country,
        bill_name,
        bill_company,
        bill_a1,
        bill_a2,
        bill_a3,
        bill_town,
        bill_region,
        bill_postcode,
        bill_country,
        currency,
        subtotal,
        tax,
        shipping_charge,
        total,
        payment_method,
        shipping_service,
        shipping_carrier,
        tracking_number,
        str(fulfillment_loc_id) if fulfillment_loc_id else None,
    )

    # Line items. RowId identifies a line within the order and is the key in lw_order_items.
    # SKU is the CHANNEL listing SKU (e.g. AJC-D5S-A-0-136709-V), not always the inventory SKU.
    # For composite listings the stock item actually shipped is in composite_child_sku
    # (normally equal to the listing's Fill SKU in lw_products).
    items_tuples = []
    raw_items = raw_order.get("Items") or []
    for item in raw_items:
        row_id = (
            item.get("RowId")
            or item.get("pkRowId")
            or item.get("ItemId")
            or item.get("StockItemId")
        )
        if not row_id:
            logger.warning(f"[{region_key}] Order {order_number or order_id}: skipped a line without a RowId "
                           f"(SKU {item.get('SKU') or item.get('ItemNumber') or '?'})")
            continue

        stock_item_id = item.get("StockItemId")
        sku = (
            item.get("SKU")
            or item.get("ChannelSKU")
            or item.get("ChannelSku")
            or item.get("ItemNumber")
            or "UNKNOWN"
        )
        item_title = item.get("ItemTitle") or item.get("Title")
        qty = int(item.get("Qty", item.get("Quantity", 1)))
        unit_price = _money(item.get("PricePerUnit"), item.get("Price"))
        # Cost = line total excluding tax, after discount (0 on a 100% discount line).
        line_total = _money(item.get("Cost"), item.get("LineTotal"), default=unit_price * qty)
        tax_rate = _money(item.get("TaxRate"))
        # PricePerUnit includes tax when TaxCostInclusive is true (e.g. Amazon) and excludes it
        # otherwise (e.g. Walmart). Store unit_price excluding tax, consistent with line_total (Cost).
        if _is_true(item.get("TaxCostInclusive")) and tax_rate > 0:
            unit_price = (unit_price / (1 + tax_rate / 100)).quantize(Decimal("0.0001"))
        tax_amount = _money(item.get("SalesTax"), item.get("TaxAmount"), item.get("Tax"))
        # DiscountValue is money; "Discount" is a percentage in Linnworks, so it is not used here.
        discount = _money(item.get("DiscountValue"), item.get("LineDiscount"))
        composite_child_sku, composite_children = _composite_children(item)

        items_tuples.append((
            region_key,
            str(order_id),
            str(row_id),
            str(stock_item_id) if stock_item_id else None,
            str(sku),
            item_title,
            qty,
            unit_price,
            line_total,
            tax_rate,
            tax_amount,
            discount,
            composite_child_sku,
            composite_children,
        ))

    return order_tuple, items_tuples


# Insert a new order or update the existing one (key: region + order_id).
# Status and money are always overwritten with the latest values. Most other fields use
# COALESCE(new, old): an empty value from Linnworks never wipes data we already have.
SQL_UPSERT_ORDER = """
    INSERT INTO lw_orders (
        region, order_id, num_order_id, channel_reference_number,
        source, subsource, status, raw_status, is_hold_or_parked, folder_names,
        received_date, dispatch_by_date, processed_date,
        customer_name, customer_email, customer_phone,
        shipping_name, shipping_company, shipping_address1, shipping_address2, shipping_address3,
        shipping_town, shipping_region, shipping_postcode, shipping_country,
        billing_name, billing_company, billing_address1, billing_address2, billing_address3,
        billing_town, billing_region, billing_postcode, billing_country,
        currency, subtotal, tax, shipping_charge, total, payment_method,
        shipping_service, shipping_carrier, tracking_number, location_id
    ) VALUES (
        %s, %s, %s, %s,
        %s, %s, %s, %s, %s, %s,
        %s, %s, %s,
        %s, %s, %s,
        %s, %s, %s, %s, %s,
        %s, %s, %s, %s,
        %s, %s, %s, %s, %s,
        %s, %s, %s, %s,
        %s, %s, %s, %s, %s, %s,
        %s, %s, %s, %s
    )
    ON DUPLICATE KEY UPDATE
        num_order_id = COALESCE(VALUES(num_order_id), num_order_id),
        channel_reference_number = COALESCE(VALUES(channel_reference_number), channel_reference_number),
        source = COALESCE(VALUES(source), source),
        subsource = COALESCE(VALUES(subsource), subsource),
        status = VALUES(status),
        raw_status = VALUES(raw_status),
        is_hold_or_parked = VALUES(is_hold_or_parked),
        folder_names = VALUES(folder_names),
        received_date = COALESCE(VALUES(received_date), received_date),
        dispatch_by_date = COALESCE(VALUES(dispatch_by_date), dispatch_by_date),
        processed_date = COALESCE(VALUES(processed_date), processed_date),
        customer_name = COALESCE(VALUES(customer_name), customer_name),
        customer_email = COALESCE(VALUES(customer_email), customer_email),
        customer_phone = COALESCE(VALUES(customer_phone), customer_phone),
        shipping_name = COALESCE(VALUES(shipping_name), shipping_name),
        shipping_company = COALESCE(VALUES(shipping_company), shipping_company),
        shipping_address1 = COALESCE(VALUES(shipping_address1), shipping_address1),
        shipping_address2 = COALESCE(VALUES(shipping_address2), shipping_address2),
        shipping_address3 = COALESCE(VALUES(shipping_address3), shipping_address3),
        shipping_town = COALESCE(VALUES(shipping_town), shipping_town),
        shipping_region = COALESCE(VALUES(shipping_region), shipping_region),
        shipping_postcode = COALESCE(VALUES(shipping_postcode), shipping_postcode),
        shipping_country = COALESCE(VALUES(shipping_country), shipping_country),
        billing_name = COALESCE(VALUES(billing_name), billing_name),
        billing_company = COALESCE(VALUES(billing_company), billing_company),
        billing_address1 = COALESCE(VALUES(billing_address1), billing_address1),
        billing_address2 = COALESCE(VALUES(billing_address2), billing_address2),
        billing_address3 = COALESCE(VALUES(billing_address3), billing_address3),
        billing_town = COALESCE(VALUES(billing_town), billing_town),
        billing_region = COALESCE(VALUES(billing_region), billing_region),
        billing_postcode = COALESCE(VALUES(billing_postcode), billing_postcode),
        billing_country = COALESCE(VALUES(billing_country), billing_country),
        currency = VALUES(currency),
        subtotal = VALUES(subtotal),
        tax = VALUES(tax),
        shipping_charge = VALUES(shipping_charge),
        total = VALUES(total),
        payment_method = COALESCE(VALUES(payment_method), payment_method),
        shipping_service = COALESCE(VALUES(shipping_service), shipping_service),
        shipping_carrier = COALESCE(VALUES(shipping_carrier), shipping_carrier),
        tracking_number = COALESCE(VALUES(tracking_number), tracking_number),
        location_id = COALESCE(VALUES(location_id), location_id),
        updated_at = CURRENT_TIMESTAMP;
"""

# Insert or update one order line (key: region + order_id + row_id).
SQL_UPSERT_ITEM = """
    INSERT INTO lw_order_items (
        region, order_id, row_id, stock_item_id, sku, item_title,
        quantity, unit_price, line_total, tax_rate, tax_amount, discount,
        composite_child_sku, composite_children
    ) VALUES (
        %s, %s, %s, %s, %s, %s,
        %s, %s, %s, %s, %s, %s,
        %s, %s
    )
    ON DUPLICATE KEY UPDATE
        stock_item_id = VALUES(stock_item_id),
        sku = VALUES(sku),
        item_title = VALUES(item_title),
        quantity = VALUES(quantity),
        unit_price = VALUES(unit_price),
        line_total = VALUES(line_total),
        tax_rate = VALUES(tax_rate),
        tax_amount = VALUES(tax_amount),
        discount = VALUES(discount),
        composite_child_sku = VALUES(composite_child_sku),
        composite_children = VALUES(composite_children),
        updated_at = CURRENT_TIMESTAMP;
"""


def _prune_stale_order_items(cursor, region: str, order_items_map: dict[str, set[str]]) -> None:
    """Delete DB lines that are no longer on the order in Linnworks (e.g. a line was removed).

    order_items_map: order_id -> the RowIds Linnworks returned now. Lines not in that set are
    deleted; an order with no lines at all loses all its lines.
    """
    for order_id, row_ids in order_items_map.items():
        if row_ids:
            placeholders = ",".join(["%s"] * len(row_ids))
            sql = f"DELETE FROM lw_order_items WHERE region = %s AND order_id = %s AND row_id NOT IN ({placeholders})"
            cursor.execute(sql, (region, order_id, *row_ids))
        else:
            sql = "DELETE FROM lw_order_items WHERE region = %s AND order_id = %s"
            cursor.execute(sql, (region, order_id))


def _mark_missing_open_orders_deleted(cursor, region: str, seen_order_ids: set[str]) -> int:
    """Mark open/parked orders that Linnworks no longer returns as 'deleted'.

    Only call this after a COMPLETE open-orders fetch (the live client raises on any
    incomplete pagination, so reaching this point means the list is the full set).
    An order that was processed in the meantime is set back to shipped/cancelled by the
    processed-orders delta in the same run; one that reappears as open is set back to
    open by the next upsert, so a wrong mark heals itself on the following run.
    """
    # Runs inside the caller's transaction: any failure rolls back the whole open-orders write.
    cursor.execute(
        "SELECT order_id, num_order_id FROM lw_orders WHERE region = %s AND status IN ('open', 'parked');",
        (region,),
    )
    missing_rows = [row for row in cursor.fetchall() if row["order_id"] not in seen_order_ids]
    if not missing_rows:
        return 0

    missing = [row["order_id"] for row in missing_rows]
    # Order numbers only (no customer data), capped so a mass delete doesn't flood the log.
    sample = [str(row["num_order_id"]) for row in missing_rows[:20]]
    more = f" (+{len(missing_rows) - 20} more)" if len(missing_rows) > 20 else ""
    logger.info(f"[{region}] Open orders no longer in Linnworks, marking deleted: {', '.join(sample)}{more}")

    chunk_size = 500
    for i in range(0, len(missing), chunk_size):
        chunk = missing[i : i + chunk_size]
        placeholders = ",".join(["%s"] * len(chunk))
        cursor.execute(
            "UPDATE lw_orders SET status = 'deleted', updated_at = CURRENT_TIMESTAMP "
            f"WHERE region = %s AND status IN ('open', 'parked') AND order_id IN ({placeholders});",
            (region, *chunk),
        )
    return len(missing)


def _extract_batch(
    region: str, raw_orders: List[Dict[str, Any]], default_status: str
) -> Tuple[list, list, dict[str, set[str]], set[str]]:
    """Map a list of raw orders, skipping (and reporting) any order that can't be read.

    Returns (order rows, line rows, order_id -> RowIds, order IDs seen). An order that fails to
    map is logged with its order number and left out, but its ID still counts as "seen" so the
    deleted-order check never marks a real, open order as deleted just because one field was
    odd. Its existing DB row and lines are left untouched until a later run reads it fine.
    """
    orders_batch: list = []
    items_batch: list = []
    order_items_map: dict[str, set[str]] = {}
    seen_ids: set[str] = set()
    failed: list[str] = []
    for raw in raw_orders:
        order_id = raw.get("OrderId") or raw.get("pkOrderID")
        if order_id:
            seen_ids.add(str(order_id))
        try:
            if not order_id:
                raise ValueError("order has no OrderId")
            order_tuple, item_tuples = _extract_order_and_items(region, raw, default_status=default_status)
        except Exception as exc:
            failed.append(f"{raw.get('NumOrderId') or order_id or '?'} ({exc})")
            continue
        orders_batch.append(order_tuple)
        items_batch.extend(item_tuples)
        order_items_map[order_tuple[1]] = {item[2] for item in item_tuples}
    if failed:
        logger.warning(
            f"[{region}] Skipped {len(failed)} order(s) that could not be read: "
            f"{'; '.join(failed[:10])}{' ...' if len(failed) > 10 else ''}"
        )
    return orders_batch, items_batch, order_items_map, seen_ids


def sync_open_orders(client: LinnworksClient, region: str, dry_run: bool = False) -> int:
    """Re-read ALL open orders (with lines) and upsert them, in one transaction.

    Open orders that were in lw_orders but are no longer returned by Linnworks
    (deleted in Linnworks) are marked 'deleted'.
    """
    region_key = region.upper()
    logger.info(f"[{region_key}] Starting open orders sync...")

    try:
        # Raises on any failed or incomplete page, so an empty list here really means "no open orders".
        raw_orders = client.get_open_orders(region_key, page=1, page_size=200) or []
        logger.info(f"[{region_key}] Fetched {len(raw_orders)} open order(s).")

        # order_items_map: order_id -> RowIds seen now; seen_ids: every open order ID Linnworks
        # returned (including any that couldn't be read), for the deleted-order check.
        orders_batch, items_batch, order_items_map, seen_ids = _extract_batch(region_key, raw_orders, "open")

        if dry_run:
            logger.info(f"[{region_key}] [DRY-RUN] Would upsert {len(orders_batch)} orders and {len(items_batch)} items (deleted-order check skipped).")
            return len(orders_batch)

        # One transaction for orders, lines, removed lines and the deleted check: all or nothing.
        # with_db_retry runs the whole transaction again after a temporary DB error.
        chunk_size = 500

        def write() -> int:
            with get_db_cursor() as cursor:
                for i in range(0, len(orders_batch), chunk_size):
                    cursor.executemany(SQL_UPSERT_ORDER, orders_batch[i : i + chunk_size])

                for i in range(0, len(items_batch), chunk_size):
                    cursor.executemany(SQL_UPSERT_ITEM, items_batch[i : i + chunk_size])

                _prune_stale_order_items(cursor, region_key, order_items_map)

                return _mark_missing_open_orders_deleted(cursor, region_key, seen_ids)

        deleted_count = with_db_retry(write, f"{region_key} open orders")

        if deleted_count:
            logger.info(f"[{region_key}] Marked {deleted_count} open order(s) no longer in Linnworks as deleted.")
        logger.info(f"[{region_key}] Successfully synced {len(orders_batch)} open orders and {len(items_batch)} items.")
        return len(orders_batch)

    except Exception as e:
        if not dry_run:
            record_sync_error(region_key, "orders", str(e))
        logger.error(f"[{region_key}] Open orders sync failed: {e}")  # traceback: logged by the CLI
        raise


def sync_orders_delta(
    client: LinnworksClient, region: str, dry_run: bool = False, lookback_days: Optional[int] = None
) -> int:
    """One full orders run: open orders, then orders changed since the last run, then refunds.

    Window start ("from"):
      --days N given          -> now minus N days (use the maximum, 35, on the very first run)
      saved cursor            -> previous run's end time minus ORDER_DELTA_OVERLAP_MINUTES
      no cursor (first run)   -> now minus 7 days
    The cursor only moves forward after every step succeeded, so a failed run is simply
    retried from the same point next time.
    """
    region_key = region.upper()
    now_utc = datetime.now(timezone.utc)
    overlap_minutes = Config.ORDER_DELTA_OVERLAP_MINUTES

    if lookback_days is not None and lookback_days > 0:
        from_dt = now_utc - timedelta(days=lookback_days)
    else:
        # Load cursor from database
        cursor_val = get_sync_cursor(region_key, "orders")
        if cursor_val:
            try:
                last_dt = datetime.fromisoformat(cursor_val.replace("Z", "+00:00"))
                from_dt = last_dt - timedelta(minutes=overlap_minutes)
            except Exception:
                # Unreadable cursor: fall back to the last day rather than failing the run.
                from_dt = now_utc - timedelta(days=1)
        else:
            # Default lookback if initial run: 7 days
            from_dt = now_utc - timedelta(days=7)

    # Never ask for more than Linnworks allows. This matters for --days > 35 and for a cursor
    # that is older than 35 days (sync stopped for over a month): changes older than the
    # limit can't be read from v2 any more, so the log says so instead of failing every run.
    earliest = now_utc - V2_MAX_LOOKBACK
    if from_dt < earliest:
        logger.warning(
            f"[{region_key}] Requested start {from_dt.strftime('%Y-%m-%dT%H:%M:%SZ')} is older than "
            f"Linnworks allows (35 days); using {earliest.strftime('%Y-%m-%dT%H:%M:%SZ')}. "
            "Changes to processed orders before that date are not picked up."
        )
        from_dt = earliest

    from_str = from_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    to_str = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

    logger.info(f"[{region_key}] Starting orders delta sync (from={from_str}, to={to_str})...")

    try:
        # Step 1: Open Orders (Always refreshed every run to catch status & line items)
        open_count = sync_open_orders(client, region_key, dry_run=dry_run)

        # Step 2: every order (open or processed) updated since from_str. Open orders appear again
        # here, which is harmless: the upsert gives the same result.
        processed_orders = client.get_processed_orders(region_key, from_str, to_str, page=1, page_size=200)
        logger.info(f"[{region_key}] Fetched {len(processed_orders)} processed order(s) in delta window.")

        processed_batch, processed_items, order_items_map, _ = _extract_batch(
            region_key, processed_orders, "shipped"
        )

        if not dry_run and processed_batch:
            chunk_size = 500

            def write() -> None:
                with get_db_cursor() as cursor:
                    for i in range(0, len(processed_batch), chunk_size):
                        cursor.executemany(SQL_UPSERT_ORDER, processed_batch[i : i + chunk_size])

                    for i in range(0, len(processed_items), chunk_size):
                        cursor.executemany(SQL_UPSERT_ITEM, processed_items[i : i + chunk_size])

                    _prune_stale_order_items(cursor, region_key, order_items_map)

            with_db_retry(write, f"{region_key} changed orders")

        # Step 3: Refunds Delta
        # update_state=True so lw_sync_state gets a 'refunds' row on every run (even with 0 refunds).
        sync_refunds(client, region_key, from_str, to_str, dry_run=dry_run, update_state=True)

        if not dry_run:
            # Advance sync state cursor to current time
            update_sync_success(region_key, "orders", new_cursor=to_str)

        total_orders = open_count + len(processed_batch)
        logger.info(f"[{region_key}] Delta orders sync completed successfully ({total_orders} total orders processed).")
        return total_orders

    except Exception as e:
        if not dry_run:
            record_sync_error(region_key, "orders", str(e))
        logger.error(f"[{region_key}] Orders delta sync failed: {e}")  # traceback: logged by the CLI
        raise

