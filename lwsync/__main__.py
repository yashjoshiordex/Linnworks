"""Command-line entry point for lwsync."""

import argparse
import sys
from lwsync.db import ensure_database_and_tables
from lwsync.logger import logger
from lwsync.config import Config
from lwsync.client.auth import LinnworksAuthManager
from lwsync.client.live import LiveClient
from lwsync.client.fixture import FixtureClient
from lwsync.sync.products import sync_products
from lwsync.sync.inventory import sync_locations, sync_stock_levels
from lwsync.sync.orders import sync_open_orders, sync_orders_delta
from lwsync.sync.refunds import sync_refunds


def get_client():
    """Return active client based on LW_MODE."""
    if Config.MODE == "live":
        return LiveClient()
    return FixtureClient()


def test_live_connection() -> None:
    """Test live authentication against Linnworks for US and CA."""
    logger.info("=== Testing Live Linnworks API Connections ===")
    auth_mgr = LinnworksAuthManager()
    regions = ["US", "CA"]
    all_success = True

    for reg in regions:
        logger.info(f"--> Testing authentication for {reg}...")
        try:
            session = auth_mgr.get_session(reg, force_refresh=True)
            logger.info(f" [PASS] Successfully authenticated {reg}!")
            logger.info(f"        Assigned Server: {session.server}")
            logger.info(f"        Session Token:   {session.token[:12]}... (active)")

            # Quick API probe: fetch stock locations
            logger.info(f"--> Probing API (fetching locations for {reg})...")
            client = LiveClient(auth_manager=auth_mgr)
            locations = client.get_locations(reg)
            logger.info(f" [PASS] Retrieved {len(locations)} location(s) from Linnworks {reg}:")
            for loc in locations[:3]:
                logger.info(f"        - {loc.get('LocationName')} (ID: {loc.get('StockLocationId')})")
        except Exception as e:
            all_success = False
            logger.error(f" [FAIL] Connection failed for {reg}: {e}")

    if all_success:
        logger.info("=== ALL REGIONAL LIVE CONNECTIONS VERIFIED SUCCESSFULLY ===")
    else:
        logger.error("=== SOME CONNECTIONS FAILED. Check credentials/network ===")
        sys.exit(1)


def execute_sync(entity: str, region: str, dry_run: bool = False) -> None:
    """Execute sync jobs for products, locations, stock, orders, refunds, or all."""
    client = get_client()
    regions = ["US", "CA"] if region.upper() == "ALL" else [region.upper()]
    entity_lower = entity.lower()

    for reg in regions:
        logger.info(f"=== Starting Sync for Region: {reg} (Entity: {entity_lower}, DryRun: {dry_run}) ===")

        if entity_lower in ["locations", "all"]:
            sync_locations(client, reg, dry_run=dry_run)

        if entity_lower in ["stock", "all"]:
            sync_stock_levels(client, reg, dry_run=dry_run)

        if entity_lower in ["products", "all"]:
            sync_products(client, reg, dry_run=dry_run)

        if entity_lower in ["orders", "all"]:
            sync_orders_delta(client, reg, dry_run=dry_run)

    logger.info(f"=== Sync Completed Successfully for Entity: {entity_lower} ===")


def main() -> None:
    parser = argparse.ArgumentParser(description="Linnworks Data Sync")
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # init-db command
    subparsers.add_parser("init-db", help="Verify and create database & tables if they do not exist")

    # test-live command
    subparsers.add_parser("test-live", help="Test live authentication and connectivity to US and CA Linnworks instances")

    # run command
    run_parser = subparsers.add_parser("run", help="Run sync jobs")
    run_parser.add_argument(
        "entity",
        choices=["all", "products", "locations", "stock", "orders"],
        help="Entity to sync",
    )
    run_parser.add_argument(
        "--region",
        choices=["US", "CA", "ALL"],
        default="ALL",
        help="Region to sync (default: ALL)",
    )
    run_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate sync without writing changes to the database",
    )
    run_parser.add_argument(
        "--skip-db-init",
        action="store_true",
        help="Skip auto-checking/creating tables on startup",
    )

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    # test-live command execution
    if args.command == "test-live":
        test_live_connection()
        return

    # Automatically check & create tables if not existing
    if args.command == "init-db" or (args.command == "run" and not getattr(args, "skip_db_init", False)):
        try:
            ensure_database_and_tables()
        except Exception as e:
            logger.error(f"Failed during database initialization: {e}")
            if args.command == "init-db":
                sys.exit(1)

    if args.command == "init-db":
        logger.info("Database and table verification finished successfully.")
        return

    if args.command == "run":
        try:
            execute_sync(args.entity, args.region, dry_run=args.dry_run)
        except Exception as e:
            logger.error(f"Sync execution failed: {e}")
            sys.exit(1)


if __name__ == "__main__":
    main()
