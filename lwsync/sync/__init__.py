"""Sync jobs: read from Linnworks (through a client) and upsert into the lw_ tables.

  products.py   lw_products (catalog + the 8 extended properties)
  inventory.py  lw_locations, lw_stock_levels
  orders.py     lw_orders, lw_order_items (also marks vanished open orders as deleted)
  refunds.py    lw_order_refunds
  state.py      lw_sync_state: last success, cursor and last error per region and entity

Every job is idempotent: rows are keyed (e.g. region + sku) and written with
INSERT ... ON DUPLICATE KEY UPDATE, so running a job twice gives the same result.
"""

from lwsync.sync.products import sync_products
from lwsync.sync.inventory import sync_locations, sync_stock_levels
from lwsync.sync.orders import sync_open_orders, sync_orders_delta
from lwsync.sync.refunds import sync_refunds
from lwsync.sync.state import get_sync_cursor, update_sync_success, record_sync_error

__all__ = [
    "sync_products",
    "sync_locations",
    "sync_stock_levels",
    "sync_open_orders",
    "sync_orders_delta",
    "sync_refunds",
    "get_sync_cursor",
    "update_sync_success",
    "record_sync_error",
]
