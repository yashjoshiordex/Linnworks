"""Tests for Deliverable 3: Products, Locations, and Stock Level Sync."""

import pytest
from decimal import Decimal
from lwsync.client.fixture import FixtureClient
from lwsync.sync.products import sync_products
from lwsync.sync.inventory import sync_locations, sync_stock_levels
from lwsync.db import get_db_cursor, ensure_database_and_tables


@pytest.fixture(scope="module", autouse=True)
def setup_tables():
    """Ensure database tables exist before running sync tests."""
    ensure_database_and_tables()


def test_sync_products_dry_run():
    client = FixtureClient()
    count = sync_products(client, "US", dry_run=True)
    assert count > 0


def test_sync_products_idempotent():
    client = FixtureClient()
    # First sync run
    count1 = sync_products(client, "US", dry_run=False)
    assert count1 > 0

    # Query DB count
    with get_db_cursor() as cur:
        cur.execute("SELECT COUNT(*) AS cnt FROM lw_products WHERE region = 'US';")
        db_count1 = cur.fetchone()["cnt"]
        assert db_count1 == count1

        # Check extended property mapping on one known item
        cur.execute("SELECT * FROM lw_products WHERE region = 'US' AND sku = 'AJC-D35S' LIMIT 1;")
        row = cur.fetchone()
        assert row is not None
        assert row["brand"] == "AJC Battery"
        assert row["master_sku"] == "AJC-D35S-M"

    # Second sync run (must not duplicate or inflate row count)
    count2 = sync_products(client, "US", dry_run=False)
    with get_db_cursor() as cur:
        cur.execute("SELECT COUNT(*) AS cnt FROM lw_products WHERE region = 'US';")
        db_count2 = cur.fetchone()["cnt"]
        assert db_count2 == db_count1


def test_sync_locations_and_stock():
    client = FixtureClient()
    loc_count = sync_locations(client, "US", dry_run=False)
    assert loc_count > 0

    stock_count = sync_stock_levels(client, "US", dry_run=False)
    assert stock_count > 0

    with get_db_cursor() as cur:
        cur.execute("SELECT COUNT(*) AS cnt FROM lw_locations WHERE region = 'US';")
        assert cur.fetchone()["cnt"] == loc_count

        cur.execute("SELECT COUNT(*) AS cnt FROM lw_stock_levels WHERE region = 'US';")
        assert cur.fetchone()["cnt"] == stock_count

