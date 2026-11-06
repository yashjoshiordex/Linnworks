"""Live Linnworks API client with throttling, retry logic, and error handling."""

import time
from typing import Any, Optional
import requests
from requests.exceptions import RequestException

from lwsync.client.base import LinnworksClient
from lwsync.client.auth import LinnworksAuthManager
from lwsync.config import Config
from lwsync.logger import logger


class LiveClient(LinnworksClient):
    """Live HTTP implementation for Linnworks API."""

    def __init__(self, auth_manager: Optional[LinnworksAuthManager] = None):
        self.auth_manager = auth_manager or LinnworksAuthManager()
        self.session = requests.Session()
        self.last_request_time: dict[str, float] = {}

    def _throttle(self, region: str) -> None:
        """Enforce rate limits between consecutive requests."""
        min_interval = 1.0 / max(Config.RATE_LIMIT_RPS, 0.1)
        last_time = self.last_request_time.get(region, 0.0)
        elapsed = time.time() - last_time
        if elapsed < min_interval:
            time.sleep(min_interval - elapsed)
        self.last_request_time[region] = time.time()

    def _request(
        self,
        region: str,
        method: str,
        endpoint: str,
        params: Optional[dict[str, Any]] = None,
        data: Optional[Any] = None,
        json_body: Optional[Any] = None,
    ) -> Any:
        """Execute HTTP request with throttling, 401 re-auth, and exponential retry on 429/5xx."""
        region_key = region.upper()
        retries = 0
        backoff = Config.RETRY_BACKOFF

        while True:
            self._throttle(region_key)
            auth_session = self.auth_manager.get_session(region_key)
            url = f"{auth_session.server}{endpoint}"
            headers = {
                "Authorization": auth_session.token,
                "Accept": "application/json",
            }
            if json_body is not None:
                headers["Content-Type"] = "application/json"

            try:
                response = self.session.request(
                    method=method,
                    url=url,
                    headers=headers,
                    params=params,
                    data=data,
                    json=json_body,
                    timeout=Config.REQUEST_TIMEOUT,
                )

                # Case 1: Session expired (401) -> re-authorize once and retry
                if response.status_code == 401 and retries < Config.MAX_RETRIES:
                    logger.warning(f"Received 401 Unauthorized for {region_key}. Refreshing token and retrying...")
                    self.auth_manager.invalidate_session(region_key)
                    retries += 1
                    continue

                # Case 2: Rate limit (429) or Server error (5xx) -> exponential backoff retry
                if response.status_code == 429 or response.status_code >= 500:
                    if retries < Config.MAX_RETRIES:
                        retries += 1
                        sleep_time = backoff * (2 ** (retries - 1))
                        logger.warning(
                            f"HTTP {response.status_code} on {endpoint} for {region_key}. "
                            f"Retrying in {sleep_time:.2f}s (attempt {retries}/{Config.MAX_RETRIES})..."
                        )
                        time.sleep(sleep_time)
                        continue

                response.raise_for_status()
                return response.json() if response.content else None

            except (RequestException, ValueError) as err:
                if retries < Config.MAX_RETRIES:
                    retries += 1
                    sleep_time = backoff * (2 ** (retries - 1))
                    logger.warning(
                        f"Request error on {endpoint} for {region_key}: {err}. "
                        f"Retrying in {sleep_time:.2f}s (attempt {retries}/{Config.MAX_RETRIES})..."
                    )
                    time.sleep(sleep_time)
                else:
                    logger.error(f"Request permanently failed on {endpoint} after {retries} retries: {err}")
                    raise

    # -------------------------------------------------------------------------
    # Linnworks API Endpoints
    # -------------------------------------------------------------------------

    def get_locations(self, region: str) -> list[dict[str, Any]]:
        """GET /api/Inventory/GetStockLocations"""
        res = self._request(region, "GET", "/api/Inventory/GetStockLocations")
        return res if isinstance(res, list) else []

    def get_products(
        self, region: str, page: int = 1, page_size: int = 200
    ) -> list[dict[str, Any]]:
        """POST /api/Stock/GetStockItemsFull"""
        payload = {
            "keyword": "",
            "loadCompositeParents": True,
            "loadVariationParents": True,
            "entriesPerPage": min(page_size, 200),
            "pageNumber": page,
            "dataRequirements": ["StockLevels", "Pricing", "Supplier"],
        }
        res = self._request(region, "POST", "/api/Stock/GetStockItemsFull", json_body=payload)
        if isinstance(res, dict) and "Data" in res:
            return res["Data"]
        return res if isinstance(res, list) else []

    def get_stock_items(
        self, region: str, page_number: int = 1, page_size: int = 200
    ) -> list[dict[str, Any]]:
        """Alias for get_products matching the base interface."""
        return self.get_products(region, page=page_number, page_size=page_size)

    def get_product_extended_properties(
        self, region: str, stock_item_ids: Optional[list[str]] = None
    ) -> list[dict[str, Any]]:
        """POST /api/Inventory/GetInventoryItemExtendedProperties"""
        payload = {"inventoryItemIds": stock_item_ids or []}
        res = self._request(
            region, "POST", "/api/Inventory/GetInventoryItemExtendedProperties", json_body=payload
        )
        return res if isinstance(res, list) else []

    def get_stock_levels(
        self, region: str, location_id: Optional[str] = None
    ) -> list[dict[str, Any]]:
        """Retrieve stock levels per SKU and location via GetStockItemsFull (StockLevels requirement)."""
        products = self.get_products(region, page=1, page_size=200)
        flattened = []
        for p in products:
            sku = p.get("ItemNumber")
            # StockLevels array returned per item
            levels = p.get("StockLevels") or []
            for lvl in levels:
                loc = lvl.get("Location") or {}
                loc_id = loc.get("StockLocationId") or lvl.get("StockLocationId")
                if location_id and loc_id and str(loc_id).lower() != str(location_id).lower():
                    continue
                flattened.append({
                    "SKU": sku,
                    "StockLocationId": loc_id,
                    "Quantity": lvl.get("StockLevel", 0),
                    "InOrders": lvl.get("InOrderBook", lvl.get("InOrders", 0)),
                    "Available": lvl.get("Available", lvl.get("StockLevel", 0)),
                })
        return flattened

    def get_open_orders(
        self, region: str, page: int = 1, page_size: int = 100
    ) -> list[dict[str, Any]]:
        """POST /api/OpenOrders/GetOpenOrders"""
        payload = {
            "entriesPerPage": page_size,
            "pageNumber": page,
        }
        res = self._request(region, "POST", "/api/OpenOrders/GetOpenOrders", json_body=payload)
        if isinstance(res, dict) and "Data" in res:
            return res["Data"]
        return res if isinstance(res, list) else []

    def get_processed_orders(
        self, region: str, from_date: str, to_date: str, page: int = 1, page_size: int = 100
    ) -> list[dict[str, Any]]:
        """POST /api/ProcessedOrders/SearchProcessedOrdersPaged"""
        payload = {
            "from": from_date,
            "to": to_date,
            "dateType": "PROCESSED",
            "pageNumber": page,
            "resultsPerPage": page_size,
        }
        res = self._request(
            region, "POST", "/api/ProcessedOrders/SearchProcessedOrdersPaged", json_body=payload
        )
        if isinstance(res, dict) and "ProcessedOrders" in res:
            orders_data = res["ProcessedOrders"]
            if isinstance(orders_data, dict) and "Data" in orders_data:
                return orders_data["Data"]
            return orders_data if isinstance(orders_data, list) else []
        return res if isinstance(res, list) else []

    def get_order_details(
        self, region: str, order_id: str
    ) -> Optional[dict[str, Any]]:
        """POST /api/ProcessedOrders/GetProcessedOrderById"""
        payload = {"pkOrderId": order_id}
        res = self._request(
            region, "POST", "/api/ProcessedOrders/GetProcessedOrderById", json_body=payload
        )
        return res if isinstance(res, dict) else None

    def get_refunds(
        self, region: str, from_date: str, to_date: str
    ) -> list[dict[str, Any]]:
        """POST /api/ReturnsRefunds/SearchReturnsRefundsPaged"""
        payload = {
            "from": from_date,
            "to": to_date,
            "type": "REFUND",
        }
        res = self._request(
            region, "POST", "/api/ReturnsRefunds/SearchReturnsRefundsPaged", json_body=payload
        )
        if isinstance(res, dict) and "Data" in res:
            return res["Data"]
        return res if isinstance(res, list) else []
