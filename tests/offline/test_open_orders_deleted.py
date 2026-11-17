"""N4: open orders deleted in Linnworks are marked 'deleted' (mocked DB, no network)."""

from unittest.mock import MagicMock, patch

import pytest
import requests

from lwsync.sync import orders


def fake_db(existing_rows):
    """Return (context manager, cursor) where SELECT returns existing_rows."""
    cursor = MagicMock()
    cursor.fetchall.return_value = existing_rows
    manager = MagicMock()
    manager.__enter__.return_value = cursor
    return manager, cursor


def update_calls(cursor):
    return [c for c in cursor.execute.call_args_list if c.args[0].lstrip().startswith("UPDATE lw_orders")]


def rows(*ids):
    return [{"order_id": i, "num_order_id": n} for n, i in enumerate(ids, start=1000)]


def test_marks_only_missing_orders_and_scopes_by_region():
    _, cursor = fake_db(rows("keep-1", "gone-1", "gone-2"))
    count = orders._mark_missing_open_orders_deleted(cursor, "CA", {"keep-1", "new-1"})
    assert count == 2
    select_sql, select_params = cursor.execute.call_args_list[0].args
    assert "region = %s" in select_sql and "status IN ('open', 'parked')" in select_sql
    assert select_params == ("CA",)
    (call,) = update_calls(cursor)
    sql, params = call.args
    assert "SET status = 'deleted'" in sql
    # Re-checks status so an order processed meanwhile is never overwritten.
    assert "status IN ('open', 'parked')" in sql
    assert params == ("CA", "gone-1", "gone-2")


def test_no_update_when_nothing_missing():
    _, cursor = fake_db(rows("a", "b"))
    assert orders._mark_missing_open_orders_deleted(cursor, "US", {"a", "b"}) == 0
    assert update_calls(cursor) == []


def test_large_delete_is_chunked():
    ids = [f"id-{i}" for i in range(1201)]
    _, cursor = fake_db(rows(*ids))
    assert orders._mark_missing_open_orders_deleted(cursor, "US", set()) == 1201
    sizes = [len(c.args[1]) - 1 for c in update_calls(cursor)]  # minus the region param
    assert sizes == [500, 500, 201]


def test_values_are_parameterised_not_interpolated():
    nasty = "x'); DROP TABLE lw_orders; --"
    _, cursor = fake_db(rows(nasty))
    orders._mark_missing_open_orders_deleted(cursor, "US", set())
    (call,) = update_calls(cursor)
    assert nasty not in call.args[0]
    assert nasty in call.args[1]


@pytest.mark.parametrize("region", ["US", "CA"])
def test_empty_open_list_marks_all_open_orders_deleted(region):
    """Client deleted every open order: an empty (complete) fetch must still run the check."""
    client = MagicMock()
    client.get_open_orders.return_value = []
    manager, cursor = fake_db(rows("old-1", "old-2"))
    with patch.object(orders, "get_db_cursor", return_value=manager):
        assert orders.sync_open_orders(client, region) == 0
    (call,) = update_calls(cursor)
    assert call.args[1] == (region, "old-1", "old-2")


def test_returned_orders_are_kept_open():
    client = MagicMock()
    client.get_open_orders.return_value = [{"OrderId": "live-1", "NumOrderId": 1, "Items": []}]
    manager, cursor = fake_db(rows("live-1", "gone-1"))
    with patch.object(orders, "get_db_cursor", return_value=manager):
        assert orders.sync_open_orders(client, "US") == 1
    (call,) = update_calls(cursor)
    assert call.args[1] == ("US", "gone-1")


def test_failed_fetch_never_marks_anything():
    client = MagicMock()
    client.get_open_orders.side_effect = requests.Timeout("page 3 failed")
    with patch.object(orders, "get_db_cursor") as db, \
         patch.object(orders, "record_sync_error") as error:
        with pytest.raises(requests.Timeout):
            orders.sync_open_orders(client, "US")
    db.assert_not_called()
    error.assert_called_once()


def test_dry_run_never_touches_db():
    client = MagicMock()
    client.get_open_orders.return_value = []
    with patch.object(orders, "get_db_cursor") as db:
        assert orders.sync_open_orders(client, "US", dry_run=True) == 0
    db.assert_not_called()


def test_db_error_while_marking_propagates_and_is_recorded():
    client = MagicMock()
    client.get_open_orders.return_value = []
    manager, cursor = fake_db(rows("old-1"))
    cursor.execute.side_effect = [None, RuntimeError("lock wait timeout")]  # SELECT ok, UPDATE fails
    with patch.object(orders, "get_db_cursor", return_value=manager), \
         patch.object(orders, "record_sync_error") as error:
        with pytest.raises(RuntimeError, match="lock wait timeout"):
            orders.sync_open_orders(client, "US")
    error.assert_called_once()
    assert error.call_args.args[:2] == ("US", "orders")
