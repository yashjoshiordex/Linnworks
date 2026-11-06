"""Fixture-based client reading realistic offline JSON payloads."""

import json
from pathlib import Path
from typing import Any, Optional
from lwsync.client.base import LinnworksClient
from lwsync.config import Config
from lwsync.logger import logger


class FixtureClient(LinnworksClient):
    """Offline Linnworks client that reads realistic fixture files."""

    def __init__(self, fixtures_dir: Optional[Path] = None):
        self.fixtures_dir = fixtures_dir or Config.FIXTURES_DIR

    def _load_json(self, region: str, filename: str) -> Any:
        """Load JSON file from fixtures/<region>/<filename>."""
        region_key = region.upper()
        file_path = self.fixtures_dir / region_key / filename
        if not file_path.exists():
            logger.warning(f"Fixture file not found: {file_path}")
            return []

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as err:
            logger.error(f"Failed to load fixture {file_path}: {err}")
            raise

    def get_locations(self, region: str) -> list[dict[str, Any]]:
        """Load locations from fixtures/<region>/locations.json."""
        data = self._load_json(region, "locations.json")
        return data if isinstance(data, list) else []

    def get_products(
        self, region: str, page: int = 1, page_size: int = 100
    ) -> list[dict[str, Any]]:
        """Load products from fixtures/<region>/products.json with paging."""
        data = self._load_json(region, "products.json")
        items = data if isinstance(data, list) else []
        start = (page - 1) * page_size
        return items[start : start + page_size]

    def get_product_extended_properties(
        self, region: str, stock_item_ids: Optional[list[str]] = None
    ) -> list[dict[str, Any]]:
        """Load extended properties from fixtures/<region>/extended_properties.json."""
        data = self._load_json(region, "extended_properties.json")
        if not isinstance(data, list):
            return []
        if stock_item_ids:
            id_set = set(stock_item_ids)
            return [prop for prop in data if prop.get("fkStockItemId") in id_set]
        return data

    def get_stock_levels(
        self, region: str, location_id: Optional[str] = None
    ) -> list[dict[str, Any]]:
        """Load stock levels from fixtures/<region>/stock_levels.json."""
        data = self._load_json(region, "stock_levels.json")
        levels = data if isinstance(data, list) else []
        if location_id:
            return [l for l in levels if l.get("LocationId") == location_id]
        return levels

    def get_open_orders(
        self, region: str, page: int = 1, page_size: int = 100
    ) -> list[dict[str, Any]]:
        """Load open orders from fixtures/<region>/orders_open.json."""
        data = self._load_json(region, "orders_open.json")
        items = data if isinstance(data, list) else []
        start = (page - 1) * page_size
        return items[start : start + page_size]

    def get_processed_orders(
        self, region: str, from_date: str, to_date: str, page: int = 1, page_size: int = 100
    ) -> list[dict[str, Any]]:
        """Load processed orders from fixtures/<region>/orders_processed.json."""
        data = self._load_json(region, "orders_processed.json")
        items = data if isinstance(data, list) else []
        start = (page - 1) * page_size
        return items[start : start + page_size]

    def get_order_details(
        self, region: str, order_id: str
    ) -> Optional[dict[str, Any]]:
        """Find order details from processed or open orders fixture."""
        for filename in ("orders_processed.json", "orders_open.json"):
            items = self._load_json(region, filename)
            if isinstance(items, list):
                for item in items:
                    if (
                        item.get("OrderId") == order_id
                        or item.get("pkOrderID") == order_id
                    ):
                        return item
        return None

    def get_refunds(
        self, region: str, from_date: str, to_date: str
    ) -> list[dict[str, Any]]:
        """Load refunds from fixtures/<region>/refunds.json."""
        data = self._load_json(region, "refunds.json")
        return data if isinstance(data, list) else []
