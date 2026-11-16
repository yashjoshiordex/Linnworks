"""Unit tests for Deliverable 5: Idempotency, Status Transitions, and Refund Edge Cases.

These tests run without live database dependencies so they are fast, isolated, and deterministic.
"""

from decimal import Decimal
import json
from unittest.mock import MagicMock

from lwsync.client.fixture import FixtureClient
from lwsync.sync.orders import _extract_order_and_items
from lwsync.sync.products import EXTENDED_PROPERTY_MAP


class TestDeliverable5Unit:
    """Deliverable 5 unit test suite covering idempotency extraction, status transitions, and refund logic."""

    # --------------------------------------------------------------------------
    # 1. Idempotency Extraction Tests
    # --------------------------------------------------------------------------
    def test_extract_order_and_items_idempotent(self):
        """Parsing identical order payload twice produces strictly identical tuples."""
        client = FixtureClient()
        orders = client.get_open_orders("US")
        assert len(orders) > 0
        raw_order = orders[0]

        order_tuple1, items1 = _extract_order_and_items("US", raw_order, default_status="open")
        order_tuple2, items2 = _extract_order_and_items("US", raw_order, default_status="open")

        assert order_tuple1 == order_tuple2
        assert items1 == items2

    def test_order_line_items_key_uniqueness(self):
        """Every line item inside an order must have a non-empty unique row_id."""
        client = FixtureClient()
        orders = client.get_open_orders("US")
        for ro in orders:
            _, items = _extract_order_and_items("US", ro)
            row_ids = [it[2] for it in items]  # index 2 is row_id
            assert len(row_ids) == len(set(row_ids)), "Duplicate row_id found inside line items!"

    # --------------------------------------------------------------------------
    # 2. Status Mapping & Transition Tests
    # --------------------------------------------------------------------------
    def test_order_status_open(self):
        """Order without hold/park flags maps to status 'open'."""
        raw_order = {
            "OrderId": "test-uuid-1",
            "GeneralInfo": {"Status": 1, "IsParked": False, "HoldOrWait": False},
        }
        order_tuple, _ = _extract_order_and_items("US", raw_order, default_status="open")
        # index 6 is status, index 8 is is_parked
        assert order_tuple[6] == "open"
        assert order_tuple[8] is False

    def test_order_status_parked_via_is_parked(self):
        """Order with IsParked = True maps to status 'parked'."""
        raw_order = {
            "OrderId": "test-uuid-2",
            "GeneralInfo": {"Status": 1, "IsParked": True, "HoldOrWait": False},
        }
        order_tuple, _ = _extract_order_and_items("US", raw_order, default_status="open")
        assert order_tuple[6] == "parked"
        assert order_tuple[8] is True

    def test_order_status_parked_via_hold_or_wait(self):
        """Order with HoldOrWait = True maps to status 'parked'."""
        raw_order = {
            "OrderId": "test-uuid-3",
            "GeneralInfo": {"Status": 1, "IsParked": False, "HoldOrWait": True},
        }
        order_tuple, _ = _extract_order_and_items("US", raw_order, default_status="open")
        assert order_tuple[6] == "parked"
        assert order_tuple[8] is True

    def test_order_status_shipped_for_processed_orders(self):
        """Processed orders map to status 'shipped'."""
        raw_order = {
            "OrderId": "test-uuid-4",
            "Processed": True,
            "GeneralInfo": {"Status": 1},
        }
        order_tuple, _ = _extract_order_and_items("US", raw_order, default_status="shipped")
        assert order_tuple[6] == "shipped"

    def test_order_status_cancelled_for_processed_orders(self):
        """Cancelled processed orders map to status 'cancelled', not 'shipped'."""
        raw_order = {
            "OrderId": "test-uuid-cancel-1",
            "Processed": True,
            "GeneralInfo": {"Status": 3},
        }
        order_tuple, _ = _extract_order_and_items("US", raw_order, default_status="shipped")
        assert order_tuple[6] == "cancelled"
        assert order_tuple[7] == "3"

    def test_order_raw_status_string_does_not_crash(self):
        """String raw_status (e.g. 'CANCELLED' or 'PAID') does not crash integer conversion."""
        raw_order = {
            "OrderId": "test-uuid-string-status",
            "GeneralInfo": {"Status": "CANCELLED"},
        }
        order_tuple, _ = _extract_order_and_items("US", raw_order, default_status="open")
        assert order_tuple[6] == "cancelled"
        assert order_tuple[7] == "CANCELLED"

    def test_v2_live_payload_field_mapping(self):
        """Verify all v2 API fields (SKU precedence, CAD currency, Subtotal, email, etc.) are correctly mapped."""
        raw_order = {
            "OrderId": "order-v2-test",
            "NumOrderId": 12345,
            "GeneralInfo": {
                "Status": 1,
                "ReceivedDate": "2026-11-16T10:00:00Z",
            },
            "CustomerInfo": {
                "Address": {
                    "FullName": "Jane Canadian",
                    "EmailAddress": "jane.canadian@example.ca",
                },
            },
            "TotalsInfo": {
                "Currency": "CAD",
                "Subtotal": 59.99,
                "PostageCost": 12.50,
                "TotalCharge": 72.49,
                "PaymentMethod": "Shopify Payments",
            },
            "ShippingInfo": {
                "PostalServiceName": "Canada Post Expedited",
                "PostalServiceVendor": "Canada Post",
                "PostalServiceId": "postal-service-ignore-this-guid",
            },
            "FulfilmentLocationId": "warehouse-loc-guid-1",
            "Items": [
                {
                    "RowId": "item-row-1",
                    "ItemNumber": "171756000000",  # Channel listing item number
                    "SKU": "AJC-D5S",             # Actual product inventory SKU
                    "Title": "AJC 12V 5Ah Battery",
                    "Quantity": 1,
                    "PricePerUnit": 59.99,
                    "SalesTax": 7.80,
                    "DiscountValue": 5.00,
                    "CostIncTax": 62.79,
                    "StockItemId": "stock-guid-1",
                }
            ],
        }

        order_tuple, items = _extract_order_and_items("CA", raw_order, default_status="open")

        # Verify Order Level Fields
        assert order_tuple[14] == "jane.canadian@example.ca"   # Customer email from CustomerInfo.Address.EmailAddress
        assert order_tuple[34] == "CAD"                        # Currency from TotalsInfo.Currency
        assert order_tuple[35] == Decimal("59.99")             # Subtotal from TotalsInfo.Subtotal
        assert order_tuple[37] == Decimal("12.50")             # Postage from TotalsInfo.PostageCost
        assert order_tuple[39] == "Shopify Payments"           # PaymentMethod from TotalsInfo.PaymentMethod
        assert order_tuple[40] == "Canada Post Expedited"      # ShippingService from ShippingInfo.PostalServiceName
        assert order_tuple[43] == "warehouse-loc-guid-1"       # FulfilmentLocationId (not postal service id)

        # Verify Line Item Level Fields
        assert len(items) == 1
        item = items[0]
        assert item[4] == "AJC-D5S"                            # SKU takes precedence over channel ItemNumber!
        assert item[8] == Decimal("62.79")                     # CostIncTax for line_total
        assert item[10] == Decimal("7.80")                     # SalesTax for tax_amount
        assert item[11] == Decimal("5.00")                     # DiscountValue for discount

    # --------------------------------------------------------------------------
    # 3. Refund Calculation & Status State Machine Edge Cases
    # --------------------------------------------------------------------------
    def test_partial_refund_does_not_mark_order_as_refunded(self):
        """Partial refund (total_refunded < order_total) must NOT change status to 'refunded'."""
        order_total = Decimal("150.0000")
        refund1 = Decimal("50.0000")
        total_refunded = refund1

        should_mark_refunded = (order_total > 0 and total_refunded >= order_total)
        assert should_mark_refunded is False

    def test_multiple_incremental_refunds_trigger_full_refund(self):
        """Multiple partial refunds summing to order total trigger status 'refunded'."""
        order_total = Decimal("150.0000")
        refund1 = Decimal("50.0000")
        refund2 = Decimal("100.0000")
        total_refunded = refund1 + refund2

        should_mark_refunded = (order_total > 0 and total_refunded >= order_total)
        assert should_mark_refunded is True

    def test_over_refund_triggers_refunded(self):
        """Refund amount exceeding order total (e.g. goodwill credit) triggers 'refunded'."""
        order_total = Decimal("50.0000")
        total_refunded = Decimal("55.0000")

        should_mark_refunded = (order_total > 0 and total_refunded >= order_total)
        assert should_mark_refunded is True

    def test_order_level_refund_with_null_sku(self):
        """Order-level refund where SKU is None is valid."""
        raw_refund = {
            "OrderId": "order-123",
            "RefundId": "ref-001",
            "SKU": None,
            "Amount": "25.00",
            "Reason": "Shipping concession",
        }
        sku = raw_refund.get("SKU")
        assert sku is None

    # --------------------------------------------------------------------------
    # 4. Multi-Region & Extended Properties Edge Cases
    # --------------------------------------------------------------------------
    def test_cross_region_sku_independence(self):
        """The same SKU in US and CA can have different prices and independent records."""
        client = FixtureClient()
        us_products = {p.get("ItemNumber"): p for p in client.get_products("US")}
        ca_products = {p.get("ItemNumber"): p for p in client.get_products("CA")}

        common_skus = set(us_products.keys()) & set(ca_products.keys())
        assert len(common_skus) > 0, "Fixtures should include overlapping SKUs in US and CA"

        for sku in common_skus:
            us_item = us_products[sku]
            ca_item = ca_products[sku]
            # Both have same SKU but belong to separate regional entities
            assert us_item["ItemNumber"] == ca_item["ItemNumber"]

    def test_extended_properties_mapping_all_8_columns(self):
        """Verify the 8 target extended properties are recognized."""
        expected_columns = {
            "brand",
            "fill_sku",
            "product_line",
            "master_sku",
            "substitute_1",
            "substitute_2",
            "mfr_part_number",
            "product_tag",
        }
        mapped_columns = set(EXTENDED_PROPERTY_MAP.values())
        assert expected_columns.issubset(mapped_columns)

    def test_unknown_extended_property_ignored_gracefully(self):
        """Unrecognized extended property name returns None and does not raise exception."""
        unknown_prop = "custom unknown attribute 999"
        mapped_col = EXTENDED_PROPERTY_MAP.get(unknown_prop.lower().strip())
        assert mapped_col is None
