"""Composite listings: the component (stock item actually shipped) is stored on the order line."""

import json

import pytest

from lwsync.client.fixture import FixtureClient
from lwsync.db import ADDED_COLUMNS, _add_missing_columns, ensure_database_and_tables, get_db_cursor
from lwsync.sync.orders import _composite_children, _extract_order_and_items, sync_open_orders

# Positions in the lw_order_items tuple (appended after discount, index 11).
CHILD_SKU, CHILDREN = 12, 13


def _order(items):
    return {"OrderId": "order-1", "NumOrderId": 1, "GeneralInfo": {"Status": 1}, "Items": items}


def test_single_component_like_live_data():
    # Shape seen on live CA data: parent qty 8, one child with the total qty 8.
    item = {"RowId": "r1", "SKU": "CA-AJC-D8S-V-0-191433", "Quantity": 8, "PricePerUnit": 10,
            "CompositeSubItems": [{"SKU": "AJC-D8S", "ItemNumber": "AJC-D8S", "Quantity": 8,
                                   "StockItemId": "stock-1", "ParentItemId": "r1"}]}
    sku, children = _composite_children(item)
    assert sku == "AJC-D8S"
    assert json.loads(children) == [{"sku": "AJC-D8S", "quantity": 8, "stock_item_id": "stock-1"}]
    _, items = _extract_order_and_items("CA", _order([item]))
    assert items[0][4] == "CA-AJC-D8S-V-0-191433"  # the line keeps the listing SKU
    assert items[0][CHILD_SKU] == "AJC-D8S"
    assert json.loads(items[0][CHILDREN])[0]["quantity"] == 8


@pytest.mark.parametrize("subs", [None, [], [{"SKU": "", "Quantity": 1}], ["not-a-dict"]])
def test_not_composite(subs):
    item = {"RowId": "r1", "SKU": "AJC-D5S", "Quantity": 1}
    if subs is not None:
        item["CompositeSubItems"] = subs
    assert _composite_children(item) == (None, None)
    _, items = _extract_order_and_items("US", _order([item]))
    assert items[0][CHILD_SKU] is None and items[0][CHILDREN] is None


def test_several_components_keep_all_but_no_single_sku():
    item = {"RowId": "r1", "SKU": "KIT-1", "Quantity": 1, "CompositeSubItems": [
        {"SKU": "AJC-D5S", "Quantity": 2, "StockItemId": "a"},
        {"SKU": "AJC-D7S", "Quantity": 1, "StockItemId": None},
    ]}
    sku, children = _composite_children(item)
    assert sku is None
    assert [c["sku"] for c in json.loads(children)] == ["AJC-D5S", "AJC-D7S"]
    assert json.loads(children)[1]["stock_item_id"] is None


def test_component_without_sku_falls_back_to_item_number():
    item = {"RowId": "r1", "SKU": "L-1", "CompositeSubItems": [{"ItemNumber": "AJC-D9S", "Quantity": 1}]}
    assert _composite_children(item)[0] == "AJC-D9S"


@pytest.fixture(scope="module")
def schema():
    try:
        ensure_database_and_tables()
    except Exception as exc:  # no local MySQL available
        pytest.skip(f"MySQL not available: {exc}")


def test_component_is_persisted(schema):
    sync_open_orders(FixtureClient(), "US", dry_run=False)
    with get_db_cursor() as cur:
        cur.execute(
            "SELECT composite_child_sku, composite_children FROM lw_order_items "
            "WHERE region = 'US' AND row_id = '44444444-0000-0000-0000-000000000103'"
        )
        row = cur.fetchone()
        cur.execute(
            "SELECT composite_child_sku FROM lw_order_items "
            "WHERE region = 'US' AND row_id = '44444444-0000-0000-0000-000000000101'"
        )
        plain = cur.fetchone()
    assert row["composite_child_sku"] == "AJC-D12S"
    assert json.loads(row["composite_children"])[0]["sku"] == "AJC-D12S"
    assert plain["composite_child_sku"] is None


def test_existing_table_gets_new_columns(schema):
    """A database created before these columns existed is upgraded, and a second run is a no-op."""
    with get_db_cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS lw_order_items_migration_check")
        cur.execute("CREATE TABLE lw_order_items_migration_check (id INT PRIMARY KEY, stock_item_id VARCHAR(64) NULL)")
    original = list(ADDED_COLUMNS)
    try:
        ADDED_COLUMNS[:] = [(("lw_order_items_migration_check",) + entry[1:]) for entry in original]
        for _ in range(2):  # second pass must not fail (columns already exist)
            with get_db_cursor() as cur:
                _add_missing_columns(cur)
        with get_db_cursor() as cur:
            cur.execute(
                "SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() "
                "AND TABLE_NAME = 'lw_order_items_migration_check' ORDER BY ORDINAL_POSITION"
            )
            columns = [r["COLUMN_NAME"] for r in cur.fetchall()]
            cur.execute("SHOW INDEX FROM lw_order_items_migration_check WHERE Column_name = 'composite_child_sku'")
            indexed = cur.fetchall()
    finally:
        ADDED_COLUMNS[:] = original
        with get_db_cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS lw_order_items_migration_check")
    assert columns == ["id", "stock_item_id", "composite_child_sku", "composite_children"]
    assert indexed
