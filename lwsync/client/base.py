"""Abstract base class defining the Linnworks client interface."""

from abc import ABC, abstractmethod
from typing import Any, Optional


class LinnworksClient(ABC):
    """Abstract client interface for interacting with Linnworks API."""

    @abstractmethod
    def get_locations(self, region: str) -> list[dict[str, Any]]:
        """Retrieve warehouse and fulfillment locations for a region."""
        pass

    @abstractmethod
    def get_products(
        self, region: str, page: int = 1, page_size: int = 100
    ) -> list[dict[str, Any]]:
        """Retrieve catalog product items with paging."""
        pass

    @abstractmethod
    def get_product_extended_properties(
        self, region: str, stock_item_ids: Optional[list[str]] = None
    ) -> list[dict[str, Any]]:
        """Retrieve extended properties for product items."""
        pass

    @abstractmethod
    def get_stock_levels(
        self, region: str, location_id: Optional[str] = None
    ) -> list[dict[str, Any]]:
        """Retrieve stock levels for inventory items across locations."""
        pass

    @abstractmethod
    def get_open_orders(
        self, region: str, page: int = 1, page_size: int = 100
    ) -> list[dict[str, Any]]:
        """Retrieve open/unfulfilled orders with order items."""
        pass

    @abstractmethod
    def get_processed_orders(
        self, region: str, from_date: str, to_date: str, page: int = 1, page_size: int = 100
    ) -> list[dict[str, Any]]:
        """Search and retrieve processed, shipped, or cancelled orders within date range."""
        pass

    @abstractmethod
    def get_order_details(
        self, region: str, order_id: str
    ) -> Optional[dict[str, Any]]:
        """Retrieve complete order details for a specific order ID."""
        pass

    @abstractmethod
    def get_refunds(
        self, region: str, from_date: str, to_date: str
    ) -> list[dict[str, Any]]:
        """Retrieve refunds within the specified time range."""
        pass
