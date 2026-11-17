"""N5: stock levels map GetStockItemsFull keys correctly (mocked DB, no network).

Payload shape copied from a live US GetStockItemsFull response (values changed).
"""

from unittest.mock import MagicMock, patch

import pytest

from lwsync.sync import inventory

AJC = "4d5833b6-a649-4d81-b572-da15682797ef"
DEFAULT = "00000000-0000-0000-0000-000000000000"


def live_product(sku="AJC-D5S"):
    return {
        "ItemNumber": sku,
        "StockLevels": [
            {"StockLevel": 0, "InOrderBook": 716, "InOrders": 716, "Available": -716,
             "Location": {"StockLocationId": DEFAULT, "LocationName": "Default"}},
            {"StockLevel": 4075, "InOrderBook": 3, "InOrders": 3, "Available": 4072,
             "Location": {"StockLocationId": AJC, "LocationName": "US | AJC"}},
        ],
    }


def run(products):
    cursor = MagicMock()
    manager = MagicMock()
    manager.__enter__.return_value = cursor
    with patch.object(inventory, "get_db_cursor", return_value=manager), \
         patch.object(inventory, "update_sync_success"):
        count = inventory.sync_stock_levels(MagicMock(), "US", raw_products=products)
    rows = [row for call in cursor.executemany.call_args_list for row in call.args[1]]
    return count, {(r[1], r[2]): r[3:] for r in rows}


def test_catalog_payload_maps_on_hand_allocated_available():
    count, rows = run([live_product()])
    assert count == 2
    assert rows[("AJC-D5S", AJC)] == (4075, 3, 4072)       # on_hand, allocated, available
    assert rows[("AJC-D5S", DEFAULT)] == (0, 716, -716)    # negative available kept


def test_in_order_book_used_when_in_orders_missing():
    product = live_product()
    for level in product["StockLevels"]:
        level.pop("InOrders")
    _, rows = run([product])
    assert rows[("AJC-D5S", AJC)][1] == 3


def test_missing_available_falls_back_to_on_hand_minus_allocated():
    product = live_product()
    for level in product["StockLevels"]:
        level.pop("Available")
    _, rows = run([product])
    assert rows[("AJC-D5S", AJC)][2] == 4072


@pytest.mark.parametrize("key", ["Quantity", "StockLevel"])
def test_flat_rows_from_client_accept_both_keys(key):
    """Rows from client.get_stock_levels (live uses Quantity, fixtures StockLevel)."""
    client = MagicMock()
    client.get_stock_levels.return_value = [
        {"SKU": "X", "StockLocationId": AJC, key: 7, "InOrders": 2, "Available": 5},
    ]
    cursor = MagicMock()
    manager = MagicMock()
    manager.__enter__.return_value = cursor
    with patch.object(inventory, "get_db_cursor", return_value=manager), \
         patch.object(inventory, "update_sync_success"):
        inventory.sync_stock_levels(client, "US")
    (row,) = cursor.executemany.call_args.args[1]
    assert row[3:] == (7, 2, 5)
