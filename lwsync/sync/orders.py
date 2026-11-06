"""Orders and order items synchronization worker (Open + Delta + Refunds)."""

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from lwsync.client.base import LinnworksClient
from lwsync.config import Config
from lwsync.db import get_db_cursor
from lwsync.logger import logger
from lwsync.sync.refunds import parse_utc_datetime, sync_refunds
from lwsync.sync.state import get_sync_cursor, record_sync_error, update_sync_success


def _extract_order_and_items(
    region: str,
    raw_order: dict[str, Any],
    default_status: str = "open",
) -> Tuple[tuple, list[tuple]]:
    """Parse raw Linnworks order payload into normalized tuples for lw_orders and lw_order_items."""
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
    totals = raw_order.get("Totals") or {}

    channel_ref = (
        gen_info.get("ReferenceNum")
        or gen_info.get("ExternalReferenceNum")
        or raw_order.get("ReferenceNum")
        or raw_order.get("ChannelReferenceNum")
    )
    source = gen_info.get("Source") or raw_order.get("Source")
    subsource = gen_info.get("SubSource") or raw_order.get("SubSource")

    # Status Mapping
    raw_status = gen_info.get("Status") if "Status" in gen_info else raw_order.get("Status")
    is_parked = bool(gen_info.get("IsParked", False) or gen_info.get("HoldOrWait", False))

    if default_status == "open":
        status = "parked" if is_parked else "open"
    else:
        status = default_status

    # Folders
    folder_list = raw_order.get("FolderName") or raw_order.get("Folders") or []
    folders_str = json.dumps(folder_list) if isinstance(folder_list, list) else str(folder_list)

    # Dates
    date_received = parse_utc_datetime(
        gen_info.get("ReceivedDate") or raw_order.get("ReceivedDate")
    )
    date_dispatch_by = parse_utc_datetime(
        gen_info.get("DespatchByDate") or raw_order.get("DespatchByDate")
    )
    date_processed = parse_utc_datetime(
        raw_order.get("ProcessedDate") or gen_info.get("ProcessedDate")
    )

    # Customer
    customer_name = (
        gen_info.get("Name")
        or addr_info.get("FullName")
        or raw_order.get("CustomerName")
    )
    customer_email = (
        gen_info.get("Email")
        or cust_info.get("EmailAddress")
        or raw_order.get("Email")
    )
    customer_phone = (
        addr_info.get("PhoneNumber")
        or cust_info.get("Phone")
        or raw_order.get("Phone")
    )

    # Shipping Address
    ship_name = addr_info.get("FullName") or customer_name
    ship_company = addr_info.get("Company")
    ship_a1 = addr_info.get("Address1")
    ship_a2 = addr_info.get("Address2")
    ship_a3 = addr_info.get("Address3")
    ship_town = addr_info.get("Town")
    ship_region = addr_info.get("Region")
    ship_postcode = addr_info.get("PostCode")
    ship_country = addr_info.get("Country")

    # Billing Address
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
    currency = gen_info.get("Currency") or raw_order.get("Currency", "USD")
    subtotal = Decimal(str(totals.get("SubTotal", raw_order.get("Subtotal", "0.0000"))))
    tax = Decimal(str(totals.get("Tax", raw_order.get("Tax", "0.0000"))))
    shipping_charge = Decimal(
        str(totals.get("Postage", ship_info.get("Cost", raw_order.get("ShippingCost", "0.0000"))))
    )
    total = Decimal(str(totals.get("Total", raw_order.get("Total", "0.0000"))))
    payment_method = raw_order.get("PaymentMethod") or gen_info.get("PaymentMethod")

    # Shipping details
    shipping_service = ship_info.get("ServiceName") or raw_order.get("PostalServiceName")
    shipping_carrier = ship_info.get("Vendor") or raw_order.get("PostalServiceVendor")
    tracking_number = (
        ship_info.get("TrackingNumber")
        or raw_order.get("PostalTrackingNumber")
        or raw_order.get("TrackingNumber")
    )
    fulfillment_loc_id = (
        raw_order.get("LocationId")
        or raw_order.get("StockLocationId")
        or ship_info.get("PostalServiceId")
    )

    order_tuple = (
        region_key,
        str(order_id),
        int(order_number) if order_number else None,
        str(channel_ref) if channel_ref else None,
        source,
        subsource,
        status,
        int(raw_status) if raw_status is not None else None,
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

    # Line Items
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
            continue

        stock_item_id = item.get("StockItemId")
        sku = item.get("ItemNumber") or item.get("SKU") or item.get("ChannelSku", "UNKNOWN")
        item_title = item.get("ItemTitle") or item.get("Title")
        qty = int(item.get("Qty", item.get("Quantity", 1)))
        unit_price = Decimal(str(item.get("PricePerUnit", item.get("Price", "0.0000"))))
        line_total = Decimal(str(item.get("LineTotal", item.get("Total", unit_price * qty))))
        tax_rate = Decimal(str(item.get("TaxRate", "0.0000")))
        tax_amount = Decimal(str(item.get("TaxAmount", item.get("Tax", "0.0000"))))
        discount = Decimal(str(item.get("LineDiscount", item.get("Discount", "0.0000"))))

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
        ))

    return order_tuple, items_tuples


SQL_UPSERT_ORDER = """
    INSERT INTO lw_orders (
        region, order_id, order_number, channel_reference_number,
        channel_source, channel_subsource, status, raw_status, is_parked, folders,
        date_received, date_dispatch_by, date_processed,
        customer_name, customer_email, customer_phone,
        shipping_name, shipping_company, shipping_address1, shipping_address2, shipping_address3,
        shipping_town, shipping_region, shipping_postcode, shipping_country,
        billing_name, billing_company, billing_address1, billing_address2, billing_address3,
        billing_town, billing_region, billing_postcode, billing_country,
        currency, subtotal, tax, shipping_charge, total, payment_method,
        shipping_service, shipping_carrier, tracking_number, fulfillment_location_id
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
        order_number = COALESCE(VALUES(order_number), order_number),
        channel_reference_number = COALESCE(VALUES(channel_reference_number), channel_reference_number),
        channel_source = COALESCE(VALUES(channel_source), channel_source),
        channel_subsource = COALESCE(VALUES(channel_subsource), channel_subsource),
        status = VALUES(status),
        raw_status = VALUES(raw_status),
        is_parked = VALUES(is_parked),
        folders = VALUES(folders),
        date_received = COALESCE(VALUES(date_received), date_received),
        date_dispatch_by = COALESCE(VALUES(date_dispatch_by), date_dispatch_by),
        date_processed = COALESCE(VALUES(date_processed), date_processed),
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
        fulfillment_location_id = COALESCE(VALUES(fulfillment_location_id), fulfillment_location_id),
        updated_at = CURRENT_TIMESTAMP;
"""

SQL_UPSERT_ITEM = """
    INSERT INTO lw_order_items (
        region, order_id, row_id, stock_item_id, sku, item_title,
        quantity, unit_price, line_total, tax_rate, tax_amount, discount
    ) VALUES (
        %s, %s, %s, %s, %s, %s,
        %s, %s, %s, %s, %s, %s
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
        updated_at = CURRENT_TIMESTAMP;
"""


def sync_open_orders(client: LinnworksClient, region: str, dry_run: bool = False) -> int:
    """Synchronize current open orders with full line items into lw_orders and lw_order_items."""
    region_key = region.upper()
    logger.info(f"[{region_key}] Starting open orders sync...")

    try:
        raw_orders = client.get_open_orders(region_key, page=1, page_size=200)
        if not raw_orders:
            logger.info(f"[{region_key}] No open orders found.")
            return 0

        logger.info(f"[{region_key}] Fetched {len(raw_orders)} open order(s).")

        orders_batch = []
        items_batch = []

        for ro in raw_orders:
            order_tuple, item_tuples = _extract_order_and_items(region_key, ro, default_status="open")
            orders_batch.append(order_tuple)
            items_batch.extend(item_tuples)

        if dry_run:
            logger.info(f"[{region_key}] [DRY-RUN] Would upsert {len(orders_batch)} orders and {len(items_batch)} items.")
            return len(orders_batch)

        chunk_size = 500
        with get_db_cursor() as cursor:
            for i in range(0, len(orders_batch), chunk_size):
                cursor.executemany(SQL_UPSERT_ORDER, orders_batch[i : i + chunk_size])

            for i in range(0, len(items_batch), chunk_size):
                cursor.executemany(SQL_UPSERT_ITEM, items_batch[i : i + chunk_size])

        logger.info(f"[{region_key}] Successfully synced {len(orders_batch)} open orders and {len(items_batch)} items.")
        return len(orders_batch)

    except Exception as e:
        record_sync_error(region_key, "orders", str(e))
        logger.error(f"[{region_key}] Open orders sync failed: {e}")
        raise


def sync_orders_delta(client: LinnworksClient, region: str, dry_run: bool = False) -> int:
    """Delta synchronization for processed/shipped/cancelled orders with 10-minute lookback window."""
    region_key = region.upper()
    now_utc = datetime.now(timezone.utc)
    overlap_minutes = Config.ORDER_DELTA_OVERLAP_MINUTES

    # Load cursor from database
    cursor_val = get_sync_cursor(region_key, "orders")
    if cursor_val:
        try:
            last_dt = datetime.fromisoformat(cursor_val.replace("Z", "+00:00"))
            from_dt = last_dt - timedelta(minutes=overlap_minutes)
        except Exception:
            from_dt = now_utc - timedelta(days=1)
    else:
        # Default lookback if initial run: 7 days
        from_dt = now_utc - timedelta(days=7)

    from_str = from_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    to_str = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

    logger.info(f"[{region_key}] Starting orders delta sync (from={from_str}, to={to_str})...")

    try:
        # Step 1: Open Orders (Always refreshed every run to catch status & line items)
        open_count = sync_open_orders(client, region_key, dry_run=dry_run)

        # Step 2: Processed / Changed Orders Delta
        processed_orders = client.get_processed_orders(region_key, from_str, to_str, page=1, page_size=200)
        logger.info(f"[{region_key}] Fetched {len(processed_orders)} processed order(s) in delta window.")

        processed_batch = []
        processed_items = []

        for po in processed_orders:
            order_tuple, item_tuples = _extract_order_and_items(region_key, po, default_status="shipped")
            processed_batch.append(order_tuple)
            processed_items.extend(item_tuples)

        if not dry_run and processed_batch:
            chunk_size = 500
            with get_db_cursor() as cursor:
                for i in range(0, len(processed_batch), chunk_size):
                    cursor.executemany(SQL_UPSERT_ORDER, processed_batch[i : i + chunk_size])

                for i in range(0, len(processed_items), chunk_size):
                    cursor.executemany(SQL_UPSERT_ITEM, processed_items[i : i + chunk_size])

        # Step 3: Refunds Delta
        sync_refunds(client, region_key, from_str, to_str, dry_run=dry_run)

        if not dry_run:
            # Advance sync state cursor to current time
            update_sync_success(region_key, "orders", new_cursor=to_str)

        total_orders = open_count + len(processed_batch)
        logger.info(f"[{region_key}] Delta orders sync completed successfully ({total_orders} total orders processed).")
        return total_orders

    except Exception as e:
        record_sync_error(region_key, "orders", str(e))
        logger.error(f"[{region_key}] Orders delta sync failed: {e}")
        raise

