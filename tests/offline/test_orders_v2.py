"""V2 API and cursor regression tests using fake responses and database mocks."""

from unittest.mock import MagicMock, patch
import json

import pytest
import requests

from lwsync.client.live import LiveClient
from lwsync.config import Config
from lwsync.sync import orders


@pytest.fixture
def client(monkeypatch):
    # Deliberately reverse hosting vs business region to catch region inference.
    monkeypatch.setattr(Config, "LW_API_BASE_URLS", {
        "US": "https://eu-api.linnworks.net",
        "CA": "https://us-api.linnworks.net",
    })
    return LiveClient(auth_manager=MagicMock())


def page(open_orders=None, processed=None, token=None):
    return {
        "OpenOrders": open_orders,
        "ProcessedOrders": processed,
        "NextSearchToken": token,
    }


@pytest.mark.parametrize("region,host", [
    ("US", "https://eu-api.linnworks.net"),
    ("CA", "https://us-api.linnworks.net"),
])
@pytest.mark.parametrize("updated", [False, True])
def test_all_pages_and_configured_host(client, region, host, updated):
    responses = [
        page([{"OrderId": "open-1"}], [{"OrderId": "processed-1"}] if updated else None, "next-1"),
        page(token="next-2"),  # Empty page must not end token pagination.
        page([{"OrderId": "open-2"}]),
    ]
    with patch.object(client, "_request", side_effect=responses) as request:
        if updated:
            result = client.get_processed_orders(region, "2026-11-01T00:00:00Z", "2026-11-02T00:00:00Z")
        else:
            result = client.get_open_orders(region)
    assert len(result) == (3 if updated else 2)
    assert result[0]["_sync_status"] == "open"
    if updated:
        assert result[1]["_sync_status"] == "shipped"
    assert request.call_count == 3
    for call in request.call_args_list:
        assert call.args == (region, "GET", f"{host}/v2/orders")
        assert call.kwargs["params"]["includeProcessed"] == str(updated).lower()
        assert "toDate" not in call.kwargs["params"]
        if updated:
            assert call.kwargs["params"]["fromDate"] == "2026-11-01T00:00:00Z"
        else:
            assert call.kwargs["params"]["fromDate"] == "1970-01-01T00:00:00Z"
    assert "searchToken" not in request.call_args_list[0].kwargs["params"]
    assert request.call_args_list[1].kwargs["params"]["searchToken"] == "next-1"
    assert request.call_args_list[2].kwargs["params"]["searchToken"] == "next-2"


def test_no_sixty_page_limit(client):
    responses = [page([{"OrderId": str(i)}], token=f"token-{i}") for i in range(65)]
    responses.append(page([{"OrderId": "last"}]))
    with patch.object(client, "_request", side_effect=responses):
        assert len(client.get_open_orders("US")) == 66


@pytest.mark.parametrize("tokens", [["a", "a"], ["a", "b", "a"]])
def test_token_cycles_fail(client, tokens):
    with patch.object(client, "_request", side_effect=[page(token=t) for t in tokens]):
        with pytest.raises(ValueError, match="Repeated"):
            client.get_open_orders("US")


@pytest.mark.parametrize("bad", [None, [], {},
    {"OpenOrders": [], "ProcessedOrders": []},
    page(open_orders={}), page(open_orders=["invalid"]), page(token=""), page(token=123),
])
def test_malformed_response_fails(client, bad):
    with patch.object(client, "_request", return_value=bad):
        with pytest.raises(ValueError, match="Invalid v2"):
            client.get_open_orders("US")


@pytest.mark.parametrize("host", ["", "http://eu-api.linnworks.net", "https://example.com", "https://eu-api.linnworks.net/v2"])
def test_invalid_host_fails_before_auth(client, monkeypatch, host):
    monkeypatch.setattr(Config, "LW_API_BASE_URLS", {"US": host})
    with pytest.raises(ValueError, match="LW_US_API_BASE_URL"):
        client.get_open_orders("US")
    client.auth_manager.get_session.assert_not_called()


def test_detail_lookup_is_v2(client):
    with patch.object(client, "_request", return_value=page(processed=[{"OrderId": "id-1"}])) as request:
        assert client.get_order_details("CA", "id-1")["OrderId"] == "id-1"
    assert request.call_args.args == ("CA", "GET", "https://us-api.linnworks.net/v2/orders")
    assert request.call_args.kwargs["params"] == {"id": ["id-1"]}


@pytest.mark.parametrize("updated", [False, True])
def test_later_page_error_has_no_fallback(client, updated):
    with patch.object(client, "_request", side_effect=[page(token="next"), requests.Timeout("timeout")]) as request:
        with pytest.raises(requests.Timeout):
            if updated:
                client.get_processed_orders("US", "start", "end")
            else:
                client.get_open_orders("US")
    assert request.call_count == 2
    assert all(call.args[2].endswith("/v2/orders") for call in request.call_args_list)


@pytest.mark.parametrize("stage", ["open", "updated", "refunds", "persist"])
@pytest.mark.parametrize("region", ["US", "CA"])
def test_failure_never_advances_cursor(client, stage, region):
    failure = requests.Timeout(f"{stage} failed")
    client.get_open_orders = MagicMock(return_value=[])
    client.get_processed_orders = MagicMock(return_value=[])
    client.get_refunds = MagicMock(return_value=[])
    if stage == "open":
        client.get_open_orders.side_effect = failure
    elif stage == "updated":
        client.get_processed_orders.side_effect = failure
    elif stage == "refunds":
        client.get_refunds.side_effect = failure
    else:
        client.get_processed_orders.return_value = [{"OrderId": "id-1"}]

    with patch.object(orders, "get_sync_cursor", return_value="2026-11-01T00:00:00Z"), \
         patch.object(orders, "update_sync_success") as success, \
         patch.object(orders, "record_sync_error") as error, \
         patch("lwsync.sync.refunds.record_sync_error"), \
         patch.object(orders, "get_db_cursor", side_effect=failure) as db:
        with pytest.raises(requests.Timeout, match=f"{stage} failed"):
            orders.sync_orders_delta(client, region)
        success.assert_not_called()
        error.assert_called()
        if stage != "persist":
            db.assert_not_called()
    if stage == "open":
        client.get_processed_orders.assert_not_called()


@pytest.mark.parametrize("region", ["US", "CA"])
def test_empty_success_advances_cursor(client, region):
    client.get_open_orders = MagicMock(return_value=[])
    client.get_processed_orders = MagicMock(return_value=[])
    client.get_refunds = MagicMock(return_value=[])
    with patch.object(orders, "get_sync_cursor", return_value="2026-11-01T00:00:00Z"), \
         patch.object(orders, "update_sync_success") as success, \
         patch.object(orders, "record_sync_error") as error:
        assert orders.sync_orders_delta(client, region) == 0
    success.assert_called_once()
    assert success.call_args.args == (region, "orders")
    assert success.call_args.kwargs["new_cursor"].endswith("Z")
    error.assert_not_called()


def test_refund_api_failure_propagates(client):
    with patch.object(client, "_request", side_effect=requests.Timeout("refund error")):
        with pytest.raises(requests.Timeout):
            client.get_refunds("US", "start", "end")


def test_permanent_http_error_is_not_retried(client):
    response = requests.Response()
    response.status_code = 403
    response._content = b"forbidden"
    with patch.object(client.session, "request", return_value=response) as request, \
         patch.object(client, "_throttle"):
        with pytest.raises(requests.HTTPError):
            client.get_open_orders("US")
    request.assert_called_once()


def test_dry_run_failure_does_not_write_error_state(client):
    client.get_open_orders = MagicMock(side_effect=requests.Timeout("offline"))
    with patch.object(orders, "record_sync_error") as error, \
         patch.object(orders, "update_sync_success") as success:
        with pytest.raises(requests.Timeout):
            orders.sync_orders_delta(client, "US", dry_run=True, lookback_days=1)
    error.assert_not_called()
    success.assert_not_called()


@pytest.mark.parametrize("region", ["US", "CA"])
@pytest.mark.parametrize("stage", ["open", "updated"])
def test_page_failure_through_sync_preserves_cursor(client, region, stage):
    responses = [page(token="next"), requests.Timeout("later page failed")]
    if stage == "updated":
        responses.insert(0, page())  # Successful open-order refresh first.
    with patch.object(client, "_request", side_effect=responses) as request, \
         patch.object(orders, "get_sync_cursor", return_value="2026-11-01T00:00:00Z"), \
         patch.object(orders, "get_db_cursor") as db, \
         patch.object(orders, "update_sync_success") as success, \
         patch.object(orders, "record_sync_error"), \
         patch.object(orders, "sync_refunds") as refunds:
        with pytest.raises(requests.Timeout, match="later page failed"):
            orders.sync_orders_delta(client, region)
        success.assert_not_called()
        db.assert_not_called()
        refunds.assert_not_called()
    assert all(call.args[2].endswith("/v2/orders") for call in request.call_args_list)


@pytest.mark.parametrize("region", ["US", "CA"])
def test_nonempty_success_persists_before_advancing_cursor(client, region):
    events = []
    cursor = MagicMock()
    cursor.executemany.side_effect = lambda *args: events.append("persist")
    manager = MagicMock()
    manager.__enter__.return_value = cursor
    manager.__exit__.side_effect = lambda *args: events.append("commit")
    with patch.object(client, "_request", side_effect=[page(), page(processed=[{"OrderId": "id-1"}])]), \
         patch.object(orders, "get_sync_cursor", return_value="2026-11-01T00:00:00Z"), \
         patch.object(orders, "get_db_cursor", return_value=manager), \
         patch.object(orders, "sync_refunds", side_effect=lambda *args, **kwargs: events.append("refunds")), \
         patch.object(orders, "update_sync_success", side_effect=lambda *args, **kwargs: events.append("cursor")):
        assert orders.sync_orders_delta(client, region) == 1
    assert events == ["persist", "commit", "refunds", "cursor"]


def test_bad_request_reports_validation_without_retry_or_token(client):
    response = requests.Response()
    response.status_code = 400
    response._content = b'{"title":"Validation failed","errors":{"fromDate":["A date is required"]},"detail":"test-secret","unrelated":"private"}'
    client.auth_manager.get_session.return_value.token = "test-secret"
    with patch.object(client.session, "request", return_value=response) as request, \
         patch.object(client, "_throttle"):
        with pytest.raises(requests.HTTPError) as raised:
            client.get_open_orders("US")
    assert "fromDate" in str(raised.value)
    assert "A date is required" in str(raised.value)
    assert "test-secret" not in str(raised.value)
    assert "private" not in str(raised.value)
    assert "[REDACTED]" in str(raised.value)
    assert raised.value.response is response
    request.assert_called_once()


def test_bad_request_non_json_body_is_not_exposed(client):
    response = requests.Response()
    response.status_code = 400
    response._content = b"<html>private arbitrary response</html>"
    with patch.object(client.session, "request", return_value=response) as request, \
         patch.object(client, "_throttle"):
        with pytest.raises(requests.HTTPError) as raised:
            client.get_open_orders("US")
    assert "private" not in str(raised.value)
    request.assert_called_once()


@pytest.mark.parametrize("body,content_type,expected", [
    (b"", "application/json", "empty response body"),
    (b'"fromDate must be provided"', "application/json", "fromDate must be provided"),
    (b'{"ErrorMessage":"Invalid fromDate"}', "application/json", "Invalid fromDate"),
    (b"Invalid fromDate: test-secret", "text/plain", "Invalid fromDate: [REDACTED]"),
    (b'{"unexpected":"private-data"}', "application/json", "no recognized validation message"),
])
def test_bad_request_other_formats_are_diagnostic(client, body, content_type, expected):
    response = requests.Response()
    response.status_code = 400
    response._content = body
    response.headers["Content-Type"] = content_type
    client.auth_manager.get_session.return_value.token = "test-secret"
    with patch.object(client.session, "request", return_value=response) as request, \
         patch.object(client, "_throttle"):
        with pytest.raises(requests.HTTPError) as raised:
            client.get_open_orders("US")
    assert expected in str(raised.value)
    assert "test-secret" not in str(raised.value)
    assert "private-data" not in str(raised.value)
    request.assert_called_once()


def http_response(status, payload=None, retry_after=None):
    response = requests.Response()
    response.status_code = status
    response._content = json.dumps(payload).encode() if payload is not None else b""
    if retry_after is not None:
        response.headers["Retry-After"] = retry_after
    return response


@pytest.mark.parametrize("header,expected", [
    ("90", 90),
    ("Thu, 01 Jan 1970 00:01:30 GMT", 90),
    (None, 60),
    ("invalid", 60),
    ("-1", 60),
    ("NaN", 60),
    ("Infinity", 60),
    ("0", 1.5),
])
def test_rate_limit_honors_retry_after_or_cooldown(client, monkeypatch, header, expected):
    monkeypatch.setattr(Config, "RATE_LIMIT_COOLDOWN", 60)
    monkeypatch.setattr(Config, "RETRY_BACKOFF", 1.5)
    responses = [http_response(429, retry_after=header), http_response(200, page())]
    with patch.object(client.session, "request", side_effect=responses) as request, \
         patch.object(client, "_throttle"), \
         patch("lwsync.client.live.time.time", return_value=0), \
         patch("lwsync.client.live.time.sleep") as sleep:
        assert client.get_open_orders("US") == []
    sleep.assert_called_once_with(expected)
    assert request.call_count == 2
    assert request.call_args_list[0].kwargs["params"] == request.call_args_list[1].kwargs["params"]


def test_rate_limit_on_later_page_retries_same_token(client, monkeypatch):
    monkeypatch.setattr(Config, "RATE_LIMIT_COOLDOWN", 60)
    responses = [
        http_response(200, page([{"OrderId": "first"}], token="next")),
        http_response(429),
        http_response(200, page([{"OrderId": "second"}])),
    ]
    with patch.object(client.session, "request", side_effect=responses) as request, \
         patch.object(client, "_throttle"), \
         patch("lwsync.client.live.time.sleep") as sleep:
        result = client.get_open_orders("US")
    assert [order["OrderId"] for order in result] == ["first", "second"]
    assert request.call_args_list[1].kwargs["params"]["searchToken"] == "next"
    assert request.call_args_list[2].kwargs["params"]["searchToken"] == "next"
    sleep.assert_called_once_with(60)


def test_rate_limit_exhaustion_is_bounded(client, monkeypatch):
    monkeypatch.setattr(Config, "MAX_RETRIES", 2)
    monkeypatch.setattr(Config, "RATE_LIMIT_COOLDOWN", 60)
    with patch.object(client.session, "request", side_effect=[http_response(429) for _ in range(3)]) as request, \
         patch.object(client, "_throttle"), \
         patch("lwsync.client.live.time.sleep") as sleep:
        with pytest.raises(requests.HTTPError):
            client.get_open_orders("US")
    assert request.call_count == 3
    assert [call.args[0] for call in sleep.call_args_list] == [60, 120]


def test_orders_pacing_uses_monotonic_clock_and_separate_accounts(client, monkeypatch):
    monkeypatch.setattr(Config, "RATE_LIMIT_RPS", 2)
    monkeypatch.setattr(Config, "ORDERS_RATE_LIMIT_RPS", 0.5)
    with patch("lwsync.client.live.time.monotonic", side_effect=[100, 100, 100.5, 102, 102, 102]), \
         patch("lwsync.client.live.time.sleep") as sleep:
        client._throttle("US", "https://us-api.linnworks.net/v2/orders")
        client._throttle("US", "https://us-api.linnworks.net/v2/orders")
        client._throttle("CA", "https://eu-api.linnworks.net/v2/orders")
    sleep.assert_called_once_with(1.5)
