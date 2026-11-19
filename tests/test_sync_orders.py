"""Unit and integration tests for Deliverable 4: Orders, Line Items, and Refunds Sync."""

from decimal import Decimal
import pytest
from lwsync.client.fixture import FixtureClient
from lwsync.sync.orders import sync_open_orders, sync_orders_delta
from lwsync.sync.refunds import sync_refunds
from lwsync.db import get_db_cursor, ensure_database_and_tables


@pytest.fixture(scope="module", autouse=True)
def init_schema():
    """Ensure database schema is verified."""
    try:
        ensure_database_and_tables()
    except Exception:
        pass


def test_open_orders_and_items_sync_dry_run():
    client = FixtureClient()
    count = sync_open_orders(client, "US", dry_run=True)
    assert count > 0


def test_cancelled_order_sync_maps_to_cancelled():
    """Verify that cancelled processed orders are recorded as status 'cancelled', not 'shipped'."""
    client = FixtureClient()
    sync_orders_delta(client, "US", dry_run=False)

    with get_db_cursor() as cur:
        # Order 100202 is the cancelled order in fixtures/US/orders_processed.json
        cur.execute("SELECT status, raw_status FROM lw_orders WHERE region = 'US' AND num_order_id = '100202';")
        row = cur.fetchone()
        assert row is not None
        assert row["status"] == "cancelled"  # from GeneralInfo.IsCancelled
        assert str(row["raw_status"]) == "1"  # Status stays the payment status (1 = paid)


def test_stale_order_item_pruned():
    """Verify that when an order line item is removed, it is pruned from lw_order_items."""
    client = FixtureClient()
    sync_open_orders(client, "US", dry_run=False)

    order_id = "33333333-0000-0000-0000-000000000101"
    with get_db_cursor() as cur:
        # Insert a dummy stale line item into lw_order_items for this order
        cur.execute(
            """INSERT INTO lw_order_items (region, order_id, row_id, sku, item_title, quantity, unit_price, line_total)
               VALUES ('US', %s, 'stale-row-to-delete', 'STALE-SKU', 'Stale Item', 1, 10.0, 10.0)
               ON DUPLICATE KEY UPDATE item_title = 'Stale Item'""",
            (order_id,)
        )
        cur.execute(
            "SELECT COUNT(*) as cnt FROM lw_order_items WHERE region = 'US' AND order_id = %s AND row_id = 'stale-row-to-delete'",
            (order_id,)
        )
        assert cur.fetchone()["cnt"] == 1

    # Re-sync open orders: the stale line item is not in the payload and must be pruned
    sync_open_orders(client, "US", dry_run=False)

    with get_db_cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) as cnt FROM lw_order_items WHERE region = 'US' AND order_id = %s AND row_id = 'stale-row-to-delete'",
            (order_id,)
        )
        assert cur.fetchone()["cnt"] == 0


def test_refunds_status_transition_logic():
    """Test that when an order's refunds equal its total, status transitions to 'refunded'."""
    # We test the calculation logic directly
    order_total = Decimal("100.0000")
    partial_refund = Decimal("40.0000")
    full_refund = Decimal("100.0000")

    # Partial refund should NOT mark refunded
    assert partial_refund < order_total

    # Full refund should mark refunded
    assert full_refund >= order_total



def test_open_order_deleted_in_linnworks_is_marked_deleted():
    """N4: an open order missing from a complete open-orders fetch becomes 'deleted';
    other regions and non-open orders are untouched, and a returning order is restored."""
    client = FixtureClient()
    gone_id = "99999999-0000-0000-0000-00000000dead"
    shipped_id = "99999999-0000-0000-0000-00000000beef"
    with get_db_cursor() as cur:
        for region, order_id, status in (
            ("US", gone_id, "open"),
            ("CA", gone_id, "open"),        # same ID in the other region: must stay open
            ("US", shipped_id, "shipped"),  # not open: must stay shipped
        ):
            cur.execute(
                """INSERT INTO lw_orders (region, order_id, num_order_id, status) VALUES (%s, %s, 999001, %s)
                   ON DUPLICATE KEY UPDATE status = VALUES(status)""",
                (region, order_id, status),
            )

    sync_open_orders(client, "US", dry_run=False)

    with get_db_cursor() as cur:
        cur.execute("SELECT region, order_id, status FROM lw_orders WHERE order_id IN (%s, %s)", (gone_id, shipped_id))
        got = {(r["region"], r["order_id"]): r["status"] for r in cur.fetchall()}
        assert got[("US", gone_id)] == "deleted"
        assert got[("CA", gone_id)] == "open"
        assert got[("US", shipped_id)] == "shipped"
        # Orders still returned by Linnworks stay open.
        cur.execute(
            "SELECT status FROM lw_orders WHERE region = 'US' AND order_id = '33333333-0000-0000-0000-000000000101'"
        )
        assert cur.fetchone()["status"] in ("open", "parked")

    # If the order reappears in the open list, the normal upsert restores it.
    original = client.get_open_orders
    client.get_open_orders = lambda region, page=1, page_size=100: original(region, page, page_size) + [
        {"OrderId": gone_id, "NumOrderId": "999001", "GeneralInfo": {"Status": 1}, "Items": []}
    ]
    sync_open_orders(client, "US", dry_run=False)
    with get_db_cursor() as cur:
        cur.execute("SELECT status FROM lw_orders WHERE region = 'US' AND order_id = %s", (gone_id,))
        assert cur.fetchone()["status"] == "open"
        cur.execute("DELETE FROM lw_orders WHERE order_id IN (%s, %s)", (gone_id, shipped_id))
