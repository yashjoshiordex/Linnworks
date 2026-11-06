"""Unit tests for Linnworks clients (FixtureClient and LiveClient)."""

import json
from unittest.mock import MagicMock, patch
import pytest

from lwsync.client import get_client, FixtureClient, LiveClient, LinnworksAuthManager
from lwsync.client.auth import LinnworksAuthSession


def test_client_factory():
    """Verify get_client returns correct client implementation."""
    fixture_client = get_client("fixture")
    assert isinstance(fixture_client, FixtureClient)

    live_client = get_client("live")
    assert isinstance(live_client, LiveClient)


def test_fixture_locations():
    """Verify FixtureClient loads US and CA locations correctly."""
    client = FixtureClient()

    us_locs = client.get_locations("US")
    assert len(us_locs) >= 2
    assert us_locs[0]["LocationName"] == "US | AJC Main"
    assert us_locs[0]["IsDefault"] is True

    ca_locs = client.get_locations("CA")
    assert len(ca_locs) >= 2
    assert ca_locs[0]["LocationName"] == "CA | AJC Ontario"


def test_fixture_products_and_cross_region_skus():
    """Verify same SKU exists in US and CA with different costs and IDs."""
    client = FixtureClient()

    us_prods = client.get_products("US")
    ca_prods = client.get_products("CA")

    us_d5s = next((p for p in us_prods if p["ItemNumber"] == "AJC-D5S"), None)
    ca_d5s = next((p for p in ca_prods if p["ItemNumber"] == "AJC-D5S"), None)

    assert us_d5s is not None
    assert ca_d5s is not None

    # Different IDs and costs across regions
    assert us_d5s["StockItemId"] != ca_d5s["StockItemId"]
    assert us_d5s["PurchasePrice"] == 18.50
    assert ca_d5s["PurchasePrice"] == 24.00
    assert us_d5s["RetailPrice"] == 39.99
    assert ca_d5s["RetailPrice"] == 49.99


def test_fixture_extended_properties_filtering():
    """Verify extended properties are loaded and can be filtered by stock item ID."""
    client = FixtureClient()

    props = client.get_product_extended_properties(
        "US", stock_item_ids=["11111111-0000-0000-0000-000000000001"]
    )
    assert len(props) >= 8

    prop_names = {p["ProperyName"] for p in props}
    expected_props = {
        "Brand",
        "Fill SKU",
        "Product Line",
        "MasterSKU",
        "Substitute 1",
        "Substitute 2",
        "Mfr Part Number",
        "Product Tag",
    }
    assert expected_props.issubset(prop_names)
    assert "CustomInternalCode" in prop_names  # Unknown property for test


def test_fixture_stock_levels():
    """Verify stock levels load properly per region and can filter by location."""
    client = FixtureClient()

    levels = client.get_stock_levels("US", location_id="00000000-0000-0000-0000-000000000001")
    assert len(levels) >= 2
    assert all(l["LocationId"] == "00000000-0000-0000-0000-000000000001" for l in levels)


def test_fixture_orders_open_and_processed():
    """Verify open and processed orders load with required variety."""
    client = FixtureClient()

    open_orders = client.get_open_orders("US")
    assert len(open_orders) >= 2

    # Check multi-line order
    multi_line = next((o for o in open_orders if len(o.get("Items", [])) > 1), None)
    assert multi_line is not None
    assert len(multi_line["Items"]) == 2

    # Check parked order
    parked = next((o for o in open_orders if o["GeneralInfo"].get("HoldOrParked") is True), None)
    assert parked is not None
    assert "Parked" in parked["GeneralInfo"]["FolderName"]

    # Check processed orders (shipped & cancelled)
    processed_orders = client.get_processed_orders("US", from_date="", to_date="")
    cancelled = next((o for o in processed_orders if o["GeneralInfo"].get("Status") == 3), None)
    assert cancelled is not None

    # Check order details lookup
    order_details = client.get_order_details("US", "33333333-0000-0000-0000-000000000101")
    assert order_details is not None
    assert order_details["NumOrderId"] == "100101"


def test_fixture_refunds():
    """Verify refunds include both order-level and line-item partial refunds."""
    client = FixtureClient()

    refunds = client.get_refunds("US", from_date="", to_date="")
    assert len(refunds) >= 3

    # Full/partial item refund
    item_refund = next((r for r in refunds if r.get("SKU") is not None), None)
    assert item_refund is not None
    assert item_refund["Amount"] > 0

    # Order-level refund where SKU is None
    order_refund = next((r for r in refunds if r.get("SKU") is None), None)
    assert order_refund is not None
    assert order_refund["Amount"] == 5.00


def test_auth_session_expiration():
    """Verify auth session expiration logic."""
    active_session = LinnworksAuthSession(token="abc", server="https://test.linnworks.net", expires_in_seconds=300)
    assert active_session.is_expired is False

    expired_session = LinnworksAuthSession(token="xyz", server="https://test.linnworks.net", expires_in_seconds=30)
    assert expired_session.is_expired is True


@patch("requests.post")
def test_auth_manager_authorize(mock_post):
    """Verify auth manager sends credentials to Linnworks authorize endpoint."""
    mock_post.return_value.status_code = 200
    mock_post.return_value.json.return_value = {
        "Token": "mock_session_token",
        "Server": "https://eu-ext.linnworks.net",
    }

    manager = LinnworksAuthManager(
        app_id="app-123",
        app_secret="sec-456",
        tokens={"US": "token-us-789"},
    )
    session = manager.get_session("US")
    assert session.token == "mock_session_token"
    assert session.server == "https://eu-ext.linnworks.net"
    assert not session.is_expired


@patch("requests.Session.request")
def test_live_client_retry_and_reauth(mock_request):
    """Verify LiveClient retries on 429 and re-authorizes on 401."""
    # Setup mock responses: first 401 (re-auth), second 429 (rate limit), third 200 (success)
    resp_401 = MagicMock()
    resp_401.status_code = 401
    resp_401.content = b'{"message": "Unauthorized"}'

    resp_429 = MagicMock()
    resp_429.status_code = 429
    resp_429.content = b'{"message": "Rate limited"}'

    resp_200 = MagicMock()
    resp_200.status_code = 200
    resp_200.content = b'[{"LocationName": "Mock Loc"}]'
    resp_200.json.return_value = [{"LocationName": "Mock Loc"}]

    mock_request.side_effect = [resp_401, resp_429, resp_200]

    mock_auth = MagicMock()
    mock_auth.get_session.return_value = LinnworksAuthSession("token", "https://api.linnworks.net", 1200)

    live_client = LiveClient(auth_manager=mock_auth)
    # Patch sleep to make test run fast
    with patch("time.sleep"):
        res = live_client.get_locations("US")

    assert len(res) == 1
    assert res[0]["LocationName"] == "Mock Loc"
    assert mock_auth.invalidate_session.called
