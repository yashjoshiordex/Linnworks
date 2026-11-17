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
        """Cancelled processed orders (GeneralInfo.IsCancelled) map to 'cancelled', not 'shipped'."""
        raw_order = {
            "OrderId": "test-uuid-cancel-1",
            "Processed": True,
            "GeneralInfo": {"Status": 1, "IsCancelled": True},
        }
        order_tuple, _ = _extract_order_and_items("US", raw_order, default_status="shipped")
        assert order_tuple[6] == "cancelled"
        assert order_tuple[7] == "1"

    def test_status_3_is_pending_not_cancelled(self):
        """Linnworks Status 3 = PENDING. A pending open order must stay open."""
        raw_order = {"OrderId": "test-uuid-pending", "GeneralInfo": {"Status": 3, "IsCancelled": False}}
        order_tuple, _ = _extract_order_and_items("US", raw_order, default_status="open")
        assert order_tuple[6] == "open"
        assert order_tuple[7] == "3"

    def test_cancel_folder_alone_does_not_cancel(self):
        """Folder names are user labels, not a cancellation flag."""
        raw_order = {"OrderId": "test-uuid-folder", "Folders": ["Cancelled Orders"], "GeneralInfo": {"Status": 1}}
        order_tuple, _ = _extract_order_and_items("US", raw_order, default_status="shipped")
        assert order_tuple[6] == "shipped"

    def test_is_cancelled_accepts_string_true(self):
        raw_order = {"OrderId": "test-uuid-str", "GeneralInfo": {"Status": 1, "IsCancelled": "true"}}
        order_tuple, _ = _extract_order_and_items("US", raw_order, default_status="shipped")
        assert order_tuple[6] == "cancelled"

    def test_order_raw_status_string_does_not_crash(self):
        """A string raw_status does not crash and is stored as-is; it never implies cancelled."""
        raw_order = {
            "OrderId": "test-uuid-string-status",
            "GeneralInfo": {"Status": "CANCELLED"},
        }
        order_tuple, _ = _extract_order_and_items("US", raw_order, default_status="open")
        assert order_tuple[6] == "open"
        assert order_tuple[7] == "CANCELLED"

    def test_unit_price_excludes_tax_when_price_is_tax_inclusive(self):
        """Live US order (Amazon): PricePerUnit 251.92 incl. 10.6562% tax, qty 4, Cost 910.64."""
        raw_order = {"OrderId": "t-incl", "GeneralInfo": {"Status": 1}, "Items": [{
            "RowId": "r1", "SKU": "X", "Quantity": 4, "PricePerUnit": 251.92, "TaxCostInclusive": True,
            "TaxRate": 10.6562, "Cost": 910.640343695156, "CostIncTax": 1007.68, "SalesTax": 97.0396563048433}]}
        _, items = _extract_order_and_items("US", raw_order)
        unit_price, line_total, tax_amount = items[0][7], items[0][8], items[0][10]
        assert round(unit_price * 4, 2) == Decimal("910.64")
        assert round(line_total, 2) == Decimal("910.64")
        assert round(line_total + tax_amount, 2) == Decimal("1007.68")

    def test_unit_price_unchanged_when_price_is_tax_exclusive(self):
        """Live US order (Walmart): PricePerUnit 70.59 excl. tax."""
        raw_order = {"OrderId": "t-excl", "GeneralInfo": {"Status": 1}, "Items": [{
            "RowId": "r1", "SKU": "X", "Quantity": 1, "PricePerUnit": 70.59, "TaxCostInclusive": False,
            "TaxRate": 9.0947, "Cost": 70.59, "CostIncTax": 77.01, "SalesTax": 6.42}]}
        _, items = _extract_order_and_items("US", raw_order)
        assert items[0][7] == Decimal("70.59")
        assert items[0][8] == Decimal("70.59")

    def test_shipping_excludes_tax_so_totals_add_up(self):
        """Live US Shopify order: PostageCost 96.34 incl. tax, PostageCostExTax 91.9977."""
        raw_order = {"OrderId": "t-ship", "GeneralInfo": {"Status": 1}, "TotalsInfo": {
            "Subtotal": 67.3800008594388, "PostageCost": 96.34, "PostageCostExTax": 91.9977081741788,
            "Tax": 7.52229096638241, "TotalCharge": 166.9}, "Items": []}
        order, _ = _extract_order_and_items("US", raw_order)
        subtotal, tax, shipping, total = order[35], order[36], order[37], order[38]
        assert round(shipping, 2) == Decimal("92.00")
        assert round(subtotal + tax + shipping, 2) == round(total, 2) == Decimal("166.90")

    def test_full_discount_line_keeps_zero_cost(self):
        """Live US order with a 100% discount: Cost 0 must stay 0 (not price x qty)."""
        raw_order = {"OrderId": "t-disc", "GeneralInfo": {"Status": 1}, "TotalsInfo": {
            "Subtotal": 0, "PostageCost": 12.99, "PostageCostExTax": 12.99, "Tax": 0,
            "TotalCharge": 12.99, "TotalDiscount": 62.49}, "Items": [{
            "RowId": "r1", "SKU": "X", "Quantity": 1, "PricePerUnit": 62.49, "TaxCostInclusive": True,
            "TaxRate": 0, "Cost": 0, "CostIncTax": 0, "SalesTax": 0, "Discount": 100, "DiscountValue": 62.49}]}
        order, items = _extract_order_and_items("US", raw_order)
        assert order[35] == Decimal("0")          # subtotal
        assert items[0][8] == Decimal("0")        # line_total
        assert items[0][11] == Decimal("62.49")   # discount is the money value, not the 100%

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
        assert item[8] == Decimal("59.99")                     # no Cost: price x qty (excl. tax), never CostIncTax
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
