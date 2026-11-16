"""Live Linnworks API client with throttling, retry logic, and error handling."""

import time
import json
import math
from email.utils import parsedate_to_datetime
from typing import Any, Optional
import requests
from requests.exceptions import HTTPError, RequestException

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

    def _throttle(self, region: str, endpoint: str = "") -> None:
        """Enforce rate limits between consecutive requests."""
        min_interval = 1.0 / max(Config.RATE_LIMIT_RPS, 0.1)
        if endpoint.endswith("/v2/orders"):
            min_interval = max(min_interval, 1.0 / max(Config.ORDERS_RATE_LIMIT_RPS, 0.01))
        last_time = self.last_request_time.get(region, float("-inf"))
        elapsed = time.monotonic() - last_time
        if elapsed < min_interval:
            time.sleep(min_interval - elapsed)
        self.last_request_time[region] = time.monotonic()

    @staticmethod
    def _retry_after_seconds(response: requests.Response) -> Optional[float]:
        """Parse Retry-After seconds or HTTP-date; ignore malformed values."""
        value = response.headers.get("Retry-After")
        if not isinstance(value, str) or not value.strip():
            return None
        try:
            seconds = float(value)
        except ValueError:
            try:
                date = parsedate_to_datetime(value)
                if date.tzinfo is None:
                    return None
                seconds = max(0.0, date.timestamp() - time.time())
            except (TypeError, ValueError, OverflowError):
                return None
        return seconds if math.isfinite(seconds) and seconds >= 0 else None

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
            self._throttle(region_key, endpoint)
            auth_session = self.auth_manager.get_session(region_key)
            if endpoint.startswith("http://") or endpoint.startswith("https://"):
                url = endpoint
            else:
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
                        retry_after = self._retry_after_seconds(response)
                        if retry_after is not None:
                            sleep_time = max(sleep_time, retry_after)
                        elif response.status_code == 429:
                            sleep_time = max(
                                sleep_time,
                                max(Config.RATE_LIMIT_COOLDOWN, 1.0) * (2 ** (retries - 1)),
                            )
                        logger.warning(
                            f"HTTP {response.status_code} on {endpoint} for {region_key}. "
                            f"Retrying in {sleep_time:.2f}s (attempt {retries}/{Config.MAX_RETRIES})..."
                        )
                        time.sleep(sleep_time)
                        continue

                response.raise_for_status()
                return response.json() if response.content else None

            except (RequestException, ValueError) as err:
                if (
                    isinstance(err, HTTPError)
                    and err.response is not None
                    and err.response.status_code == 400
                    and endpoint.endswith("/v2/orders")
                ):
                    # Handle problem-details, JSON strings and plain-text errors.
                    # Never dump arbitrary JSON objects, HTML, or response headers.
                    try:
                        problem = err.response.json()
                    except ValueError:
                        problem = None
                    message = ""
                    if isinstance(problem, dict):
                        details = {
                            key: problem[key]
                            for key in problem
                            if key.lower() in ("title", "detail", "errors", "message", "error", "errormessage")
                        }
                        if details:
                            message = json.dumps(details, ensure_ascii=True)
                    elif isinstance(problem, str):
                        message = problem
                    elif problem is None:
                        text = err.response.text.strip()
                        content_type = err.response.headers.get("Content-Type", "").lower()
                        if text and "<" not in text and (
                            not content_type or content_type.startswith("text/plain")
                        ):
                            message = text
                    if not message:
                        if not err.response.content:
                            message = "API returned an empty response body"
                        else:
                            message = "API returned no recognized validation message"
                    token = auth_session.token
                    if isinstance(token, str) and token:
                        message = message.replace(token, "[REDACTED]")
                    # Keep messages on one log line, even for plain-text responses.
                    message = json.dumps(message, ensure_ascii=True)
                    raise HTTPError(
                        f"HTTP 400 from v2 orders; validation: {message[:2000]}",
                        response=err.response,
                        request=err.request,
                    ) from None
                # Permanent client errors must surface immediately, not be retried.
                if isinstance(err, HTTPError) and err.response is not None:
                    if 400 <= err.response.status_code < 500 and err.response.status_code not in (401, 429):
                        raise
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
            "dataRequirements": ["StockLevels", "Pricing", "Supplier", "ExtendedProperties"],
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

    def get_all_products(
        self, region: str, page_size: int = 200
    ) -> list[dict[str, Any]]:
        """Fetch ALL products across all pages via traditional page-based pagination."""
        all_products: list[dict[str, Any]] = []
        page = 1
        while True:
            batch = self.get_products(region, page=page, page_size=page_size)
            if not batch:
                break
            all_products.extend(batch)
            logger.info(f"[{region.upper()}] Products page {page}: fetched {len(batch)} items (total so far: {len(all_products)})")
            if len(batch) < page_size:
                break  # Last page
            page += 1
        return all_products

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
        products = self.get_all_products(region, page_size=200)
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

    def _v2_orders_url(self, region: str) -> str:
        """Use an explicit account host; never infer hosting from US/CA labels."""
        region_key = region.upper()
        if region_key not in ("US", "CA"):
            raise ValueError(f"Unsupported Linnworks region: {region_key}")
        origin = Config.LW_API_BASE_URLS.get(region_key, "").strip().rstrip("/")
        if origin not in ("https://eu-api.linnworks.net", "https://us-api.linnworks.net"):
            raise ValueError(
                f"Set LW_{region_key}_API_BASE_URL to the account's verified "
                "https://eu-api.linnworks.net or https://us-api.linnworks.net origin"
            )
        return f"{origin}/v2/orders"

    def get_orders_v2(
        self,
        region: str,
        from_date: Optional[str] = None,
        include_processed: bool = True,
        entries_per_page: int = 100,
        search_token: Optional[str] = None,
        order_ids: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        """Fetch one documented GET /v2/orders page. Errors always propagate."""
        url = self._v2_orders_url(region)
        if entries_per_page < 1:
            raise ValueError("entries_per_page must be positive")
        if order_ids:
            # The API ignores all other query parameters when IDs are supplied.
            params: dict[str, Any] = {"id": order_ids}
        else:
            params = {
                "entriesPerPage": min(entries_per_page, 200),
                "includeProcessed": "true" if include_processed else "false",
                "onlyPaid": "false",
            }
            # An explicit full-history lower bound avoids depending on the
            # server's behavior for an omitted date, without dropping old open
            # orders by limiting the full refresh to the delta lookback window.
            params["fromDate"] = from_date or "1970-01-01T00:00:00Z"
            if search_token:
                params["searchToken"] = search_token
        result = self._request(region, "GET", url, params=params)
        if not isinstance(result, dict):
            raise ValueError("Invalid v2 orders response: expected an object")
        for key in ("OpenOrders", "ProcessedOrders"):
            if key not in result or (
                result[key] is not None
                and (not isinstance(result[key], list)
                     or any(not isinstance(order, dict) for order in result[key]))
            ):
                raise ValueError(f"Invalid v2 orders response: {key}")
        if "NextSearchToken" not in result:
            raise ValueError("Invalid v2 orders response: missing NextSearchToken")
        token = result["NextSearchToken"]
        if token is not None and (not isinstance(token, str) or not token.strip()):
            raise ValueError("Invalid v2 orders response: NextSearchToken")
        return result

    def _collect_orders_v2(
        self,
        region: str,
        include_processed: bool,
        page_size: int,
        from_date: Optional[str] = None,
        order_ids: Optional[list[str]] = None,
    ) -> list[dict[str, Any]]:
        """Consume every token page, failing on cycles instead of returning partial data."""
        orders = []
        search_token = None
        seen_tokens: set[str] = set()
        page_number = 0
        while True:
            result = self.get_orders_v2(
                region,
                from_date=from_date,
                include_processed=include_processed,
                entries_per_page=page_size,
                search_token=search_token,
                order_ids=order_ids,
            )
            page_number += 1
            logger.info(
                f"[{region.upper()}] v2 orders page {page_number}: "
                f"{len(result['OpenOrders'] or [])} open, "
                f"{len(result['ProcessedOrders'] or [])} processed; "
                f"more_pages={result['NextSearchToken'] is not None}"
            )
            for key, default_status in (("OpenOrders", "open"), ("ProcessedOrders", "shipped")):
                if key == "ProcessedOrders" and not include_processed:
                    continue
                for order in (result[key] or []):
                    gen_info = order.get("GeneralInfo") or {}
                    raw_st = gen_info.get("Status") if "Status" in gen_info else order.get("Status")
                    is_canc = (
                        raw_st in (3, "3", "CANCELLED", "Cancelled", "cancelled")
                        or order.get("IsCancelled") is True
                        or order.get("Cancelled") is True
                        or gen_info.get("IsCancelled") is True
                        or gen_info.get("Cancelled") is True
                        or (isinstance(order.get("FolderName"), list) and any("cancel" in str(f).lower() for f in order.get("FolderName")))
                        or (isinstance(gen_info.get("FolderName"), list) and any("cancel" in str(f).lower() for f in gen_info.get("FolderName")))
                    )
                    status = "cancelled" if is_canc else default_status
                    orders.append({**order, "_sync_status": status})
            next_token = result["NextSearchToken"]
            if next_token is None:
                return orders
            if order_ids:
                raise ValueError("Unexpected pagination in v2 order-ID lookup")
            if next_token in seen_tokens:
                raise ValueError("Repeated v2 orders NextSearchToken; retrieval incomplete")
            seen_tokens.add(next_token)
            search_token = next_token

    def get_open_orders(
        self, region: str, page: int = 1, page_size: int = 100
    ) -> list[dict[str, Any]]:
        """Return all open orders via v2 token pagination; page must be 1."""
        if page != 1:
            raise ValueError("v2 orders use token pagination; call with page=1")
        return self._collect_orders_v2(region, False, page_size)

    def get_processed_orders(
        self, region: str, from_date: str, to_date: str, page: int = 1, page_size: int = 100
    ) -> list[dict[str, Any]]:
        """Return all updated open/processed orders since from_date using v2.

        to_date is retained for interface compatibility. v2 has no upper-bound
        parameter; overlap on the next run safely replays newer updates.
        """
        if page != 1:
            raise ValueError("v2 orders use token pagination; call with page=1")
        return self._collect_orders_v2(region, True, page_size, from_date=from_date)

    def get_order_details(
        self, region: str, order_id: str
    ) -> Optional[dict[str, Any]]:
        """Retrieve an open or processed order through the v2 ID filter."""
        orders = self._collect_orders_v2(region, True, 100, order_ids=[order_id])
        if len(orders) > 1:
            raise ValueError("Unexpected multiple orders for a single v2 order ID")
        return orders[0] if orders else None

    def get_refunds(
        self, region: str, from_date: str, to_date: str, page_size: int = 100
    ) -> list[dict[str, Any]]:
        """POST /api/ReturnsRefunds/SearchReturnsRefundsPaged across all pages."""
        all_refunds: list[dict[str, Any]] = []
        page_num = 1
        page_size = min(page_size, 100)

        while True:
            payload = {
                "from": from_date,
                "to": to_date,
                "dateType": "PROCESSED",
                "searchField": "",
                "exactMatch": False,
                "searchTerm": "",
                "pageNum": page_num,
                "numEntriesPerPage": page_size,
                "historyType": "REFUNDS",
            }
            res = self._request(
                region, "POST", "/api/ReturnsRefunds/SearchReturnsRefundsPaged", json_body=payload
            )
            if isinstance(res, dict) and isinstance(res.get("Data"), list):
                items = res["Data"]
            elif isinstance(res, list):
                items = res
            else:
                raise ValueError("Invalid refunds response; refusing to mark sync successful")

            all_refunds.extend(items)
            logger.info(
                f"[{region.upper()}] Refunds page {page_num}: fetched {len(items)} items (total: {len(all_refunds)})"
            )

            if len(items) < page_size:
                break
            page_num += 1

        return all_refunds
