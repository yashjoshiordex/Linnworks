"""N1: refunds always record their own sync state (mocked DB, no network)."""

from unittest.mock import MagicMock, patch

import pytest

from lwsync.sync import orders, refunds


@pytest.mark.parametrize("region", ["US", "CA"])
def test_zero_refunds_still_writes_refunds_state(region):
    client = MagicMock()
    client.get_refunds.return_value = []
    with patch.object(refunds, "update_sync_success") as success, \
         patch.object(refunds, "get_db_cursor") as db:
        assert refunds.sync_refunds(client, region, "2026-11-01T00:00:00Z", "2026-11-02T00:00:00Z", update_state=True) == 0
    success.assert_called_once_with(region, "refunds", new_cursor="2026-11-02T00:00:00Z")
    db.assert_not_called()


def test_refunds_with_rows_write_state_after_commit():
    events = []
    cursor = MagicMock()
    cursor.fetchone.return_value = None  # order not in lw_orders yet
    manager = MagicMock()
    manager.__enter__.return_value = cursor
    manager.__exit__.side_effect = lambda *a: events.append("commit")
    client = MagicMock()
    client.get_refunds.return_value = [{"OrderId": "o-1", "RefundId": "r-1", "Amount": 5}]
    with patch.object(refunds, "get_db_cursor", return_value=manager) as db, \
         patch.object(refunds, "update_sync_success", side_effect=lambda *a, **k: events.append("state")):
        assert refunds.sync_refunds(client, "US", "a", "b", update_state=True) == 1
    assert db.call_count == 1  # refund rows and status check share one transaction
    assert events == ["commit", "state"]


def test_refund_failure_records_error_and_never_writes_success():
    client = MagicMock()
    client.get_refunds.side_effect = RuntimeError("refunds API down")
    with patch.object(refunds, "update_sync_success") as success, \
         patch.object(refunds, "record_sync_error") as error:
        with pytest.raises(RuntimeError):
            refunds.sync_refunds(client, "CA", "a", "b", update_state=True)
    success.assert_not_called()
    error.assert_called_once()
    assert error.call_args.args[:2] == ("CA", "refunds")


def test_dry_run_never_writes_state():
    client = MagicMock()
    client.get_refunds.return_value = []
    with patch.object(refunds, "update_sync_success") as success:
        refunds.sync_refunds(client, "US", "a", "b", dry_run=True, update_state=True)
    success.assert_not_called()


def test_skipped_refunds_are_logged_without_values(caplog):
    client = MagicMock()
    client.get_refunds.return_value = [{"Email": "someone@example.com", "Amount": 5}]
    with caplog.at_level("WARNING", logger="lwsync"):
        assert refunds.sync_refunds(client, "US", "a", "b", dry_run=True) == 0
    assert "Skipped 1 refund record" in caplog.text
    assert "someone@example.com" not in caplog.text


def test_orders_delta_asks_refunds_to_update_state():
    client = MagicMock()
    client.get_open_orders.return_value = []
    client.get_processed_orders.return_value = []
    manager = MagicMock()
    manager.__enter__.return_value.fetchall.return_value = []
    with patch.object(orders, "get_db_cursor", return_value=manager), \
         patch.object(orders, "get_sync_cursor", return_value="2026-11-01T00:00:00Z"), \
         patch.object(orders, "update_sync_success"), \
         patch.object(orders, "sync_refunds") as sync_refunds:
        orders.sync_orders_delta(client, "US")
    assert sync_refunds.call_args.kwargs["update_state"] is True
