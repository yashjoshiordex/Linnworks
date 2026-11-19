"""Command-line entry point for lwsync.

Usage (from the project folder):
    python -m lwsync run orders  --region ALL     open + changed orders and refunds (cron: every 15 min)
    python -m lwsync run catalog --region ALL     locations, stock, products, one download (cron: every 2 h)
    python -m lwsync run all     --region ALL     catalog + orders (first run: add --days 35)
    python -m lwsync run products|stock|locations one catalog part on its own
    python -m lwsync health                       exit 1 if a job hasn't succeeded recently (cron: every 15 min)
    python -m lwsync init-db                      create the database and lw_ tables if missing
    python -m lwsync test-live                    log in to US and CA and list a few locations

Options for "run": --days N (orders look-back, max 35), --dry-run (read Linnworks, write nothing),
--skip-db-init (don't run the CREATE TABLE script first; use this in cron).

Each run logs a start line, one line per step and a summary with outcome, duration and counts.
Log file per job in LW_LOG_DIR: orders.log, catalog.log, sync.log (run all), health.log, admin.log.

Steps are independent: if one step of a region fails, the other steps and regions still run.
Exit codes: 0 all good, 1 a step failed / unhealthy, 2 bad configuration, 130 interrupted.
"""

import argparse
import signal
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from lwsync import __version__
from lwsync.client.auth import LinnworksAuthManager
from lwsync.client.fixture import FixtureClient
from lwsync.client.live import LiveClient
from lwsync.config import BASE_DIR, Config
from lwsync.db import ensure_database_and_tables, get_db_cursor
from lwsync.logger import configure_file_logging, logger, quiet_console_when_unattended, start_run
from lwsync.sync.inventory import sync_locations, sync_stock_levels
from lwsync.sync.orders import sync_orders_delta
from lwsync.sync.products import sync_products
from lwsync.sync.state import record_sync_error

EXIT_OK, EXIT_FAILED, EXIT_CONFIG, EXIT_INTERRUPTED = 0, 1, 2, 130
REGIONS = ["US", "CA"]

# Steps per "run" entity, in order. Locations first: stock rows and orders refer to location IDs.
CATALOG_STEPS = ["locations", "stock", "products"]
ENTITY_STEPS = {
    "locations": ["locations"],
    "stock": ["stock"],
    "products": ["products"],
    "catalog": CATALOG_STEPS,
    "orders": ["orders"],
    "all": CATALOG_STEPS + ["orders"],
}
# lw_sync_state entity written by each step (used to record an interruption).
STATE_ENTITY = {"locations": "locations", "stock": "stock_levels", "products": "products", "orders": "orders"}
LOG_JOB = {"orders": "orders", "all": "sync"}  # everything else logs to catalog.log


class RunInterrupted(BaseException):
    """Raised by the SIGTERM handler. A BaseException on purpose: the `except Exception` blocks
    in the sync steps must not swallow it; it goes straight to main(), which records it."""


@dataclass
class StepResult:
    region: str
    step: str
    ok: bool
    count: Optional[int] = None
    seconds: float = 0.0
    error: str = ""


@dataclass
class RunState:
    """What is running right now, so an interruption can be recorded against the right step."""
    region: Optional[str] = None
    step: Optional[str] = None
    results: list[StepResult] = field(default_factory=list)


_state = RunState()


def get_client():
    """Return the client for LW_MODE: "live" calls Linnworks, anything else reads fixtures/."""
    if Config.MODE == "live":
        return LiveClient()
    return FixtureClient()


def code_version() -> str:
    """Package version plus the git commit, when the code is a git checkout."""
    try:
        commit = subprocess.run(
            ["git", "-C", str(BASE_DIR), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
    except Exception:
        commit = ""
    return f"{__version__} ({commit or 'no git'})"


def _on_sigterm(signum, frame):
    raise RunInterrupted(f"signal {signal.Signals(signum).name}")


def _log_unhandled(exc_type, exc, tb):
    """Last resort: anything that escapes main() is logged with its traceback before exiting."""
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc, tb)
        return
    logger.critical("Unhandled error, process stopping:\n" + "".join(traceback.format_exception(exc_type, exc, tb)))


def _run_step(region: str, step: str, fn: Callable[[], Any]) -> StepResult:
    """Run one step; a failure is logged with its traceback and returned, never raised."""
    _state.region, _state.step = region, step
    started = time.monotonic()
    try:
        count = fn()
        result = StepResult(region, step, True, count if isinstance(count, int) else None,
                            time.monotonic() - started)
        logger.info(f"[{region}] Step {step} ok ({result.count} rows, {result.seconds:.0f}s)")
    except Exception as exc:
        result = StepResult(region, step, False, None, time.monotonic() - started,
                            f"{type(exc).__name__}: {exc}")
        logger.exception(f"[{region}] Step {step} FAILED after {result.seconds:.0f}s: {result.error}")
    # Cleared only when the step ended normally: on an interruption (a BaseException passing
    # through here) region/step stay set, so main() can record which step was stopped.
    _state.region = _state.step = None
    _state.results.append(result)
    return result


def execute_sync(entity: str, region: str, dry_run: bool = False,
                 lookback_days: Optional[int] = None) -> list[StepResult]:
    """Run the steps of an entity for one or both regions. Returns one StepResult per step."""
    client = get_client()
    regions = REGIONS if region.upper() == "ALL" else [region.upper()]
    steps = ENTITY_STEPS[entity.lower()]
    results: list[StepResult] = []

    for reg in regions:
        # The catalog (about 126k items) takes minutes to download. When stock AND products run,
        # fetch it once and give both the same list. If the pre-fetch fails, each step falls back
        # to fetching the catalog itself.
        cached_products = None
        if "stock" in steps and "products" in steps:
            try:
                cached_products = client.get_all_products(reg, page_size=200)
                logger.info(f"[{reg}] Catalog fetched once ({len(cached_products)} products); "
                            "reusing it for stock and products.")
            except Exception as exc:
                logger.warning(f"[{reg}] Could not pre-fetch the catalog ({exc}); stock and products "
                               "will each fetch it.", exc_info=True)

        actions = {
            "locations": lambda r=reg: sync_locations(client, r, dry_run=dry_run),
            "stock": lambda r=reg: sync_stock_levels(client, r, dry_run=dry_run, raw_products=cached_products),
            "products": lambda r=reg: sync_products(client, r, dry_run=dry_run, raw_products=cached_products),
            "orders": lambda r=reg: sync_orders_delta(client, r, dry_run=dry_run, lookback_days=lookback_days),
        }
        for step in steps:
            results.append(_run_step(reg, step, actions[step]))
        cached_products = None  # free the catalog's memory before the next region
    return results


def summarize(results: list[StepResult], seconds: float) -> str:
    outcome = "ok" if results and all(r.ok for r in results) else "failed"
    parts = []
    for reg in dict.fromkeys(r.region for r in results):
        steps = " ".join(
            f"{r.step}={'ok' if r.ok else 'FAILED'}({r.count if r.count is not None else '-'},{r.seconds:.0f}s)"
            for r in results if r.region == reg
        )
        parts.append(f"{reg} {steps}")
    return f"Run finished outcome={outcome} duration={seconds:.0f}s | " + " | ".join(parts)


def run_health(regions: list[str]) -> int:
    """Check lw_sync_state: every job must have succeeded recently and have no pending error."""
    limits = {"orders": Config.HEALTH_ORDERS_MAX_AGE_MIN, "refunds": Config.HEALTH_ORDERS_MAX_AGE_MIN,
              "locations": Config.HEALTH_CATALOG_MAX_AGE_MIN, "stock_levels": Config.HEALTH_CATALOG_MAX_AGE_MIN,
              "products": Config.HEALTH_CATALOG_MAX_AGE_MIN}
    try:
        with get_db_cursor() as cur:
            cur.execute("SELECT region, entity, last_success_at, last_error FROM lw_sync_state")
            rows = {(r["region"], r["entity"]): r for r in cur.fetchall()}
    except Exception as exc:
        logger.exception(f"Health check could not read lw_sync_state: {exc}")
        return EXIT_FAILED

    now = datetime.now(timezone.utc).replace(tzinfo=None)  # DB times are UTC, stored without zone
    problems = []
    for region in regions:
        for entity, max_age in limits.items():
            row = rows.get((region, entity))
            if not row or not row["last_success_at"]:
                problems.append(f"{region} {entity}: never succeeded")
                continue
            age_min = (now - row["last_success_at"]).total_seconds() / 60
            if age_min > max_age:
                problems.append(f"{region} {entity}: last success {age_min:.0f} min ago (limit {max_age})")
            if row["last_error"]:
                problems.append(f"{region} {entity}: last run failed: {row['last_error'][:300]}")
    for problem in problems:
        logger.error(f"UNHEALTHY {problem}")
    if problems:
        logger.error(f"Health check failed: {len(problems)} problem(s)")
        return EXIT_FAILED
    logger.info(f"Health check ok ({', '.join(regions)})")
    return EXIT_OK


def test_live_connection() -> int:
    """Test live authentication against Linnworks for US and CA."""
    logger.info("=== Testing Live Linnworks API Connections ===")
    auth_mgr = LinnworksAuthManager()
    all_success = True
    for reg in REGIONS:
        logger.info(f"--> Testing authentication for {reg}...")
        try:
            session = auth_mgr.get_session(reg, force_refresh=True)
            logger.info(f" [PASS] Authenticated {reg} against {session.server} (session token hidden)")
            locations = LiveClient(auth_manager=auth_mgr).get_locations(reg)
            logger.info(f" [PASS] Retrieved {len(locations)} location(s) from Linnworks {reg}:")
            for loc in locations[:3]:
                logger.info(f"        - {loc.get('LocationName')} (ID: {loc.get('StockLocationId')})")
        except Exception as e:
            all_success = False
            logger.exception(f" [FAIL] Connection failed for {reg}: {e}")
    return EXIT_OK if all_success else EXIT_FAILED


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m lwsync", description="Linnworks Data Sync")
    sub = parser.add_subparsers(dest="command", help="Command to run")
    sub.add_parser("init-db", help="Create the database and lw_ tables if they do not exist")
    sub.add_parser("test-live", help="Test live authentication and connectivity to US and CA")
    health = sub.add_parser("health", help="Exit 1 if a sync job hasn't succeeded recently")
    health.add_argument("--region", choices=["US", "CA", "ALL"], default="ALL")
    run = sub.add_parser("run", help="Run sync jobs")
    run.add_argument("entity", choices=list(ENTITY_STEPS), help="What to sync")
    run.add_argument("--region", choices=["US", "CA", "ALL"], default="ALL",
                     help="Region to sync (default: ALL)")
    run.add_argument("--days", type=int, default=None,
                     help="Orders look-back in days, overriding the saved cursor (Linnworks max: 35)")
    run.add_argument("--dry-run", action="store_true", help="Read Linnworks but write nothing to the database")
    run.add_argument("--skip-db-init", action="store_true",
                     help="Don't run the CREATE TABLE script first (use in cron)")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.command:
        build_parser().print_help()
        return EXIT_CONFIG

    job = {"run": LOG_JOB.get(getattr(args, "entity", ""), "catalog"), "health": "health"}.get(args.command, "admin")
    if Config.LOG_TO_FILE:
        configure_file_logging(job)
        quiet_console_when_unattended()
    run_id = start_run(job)
    regions = REGIONS if getattr(args, "region", "ALL") == "ALL" else [args.region]
    started = time.monotonic()
    detail = f" entity={args.entity} days={args.days} dry_run={args.dry_run}" if args.command == "run" else ""
    logger.info(f"Run started run_id={run_id} version={code_version()} command={args.command}{detail} "
                f"regions={','.join(regions)} mode={Config.MODE} db={Config.DB_HOST}/{Config.DB_NAME}")

    # Check settings first: a bad .env stops here with a clear message (exit 2).
    if args.command == "test-live":
        problems = Config.problems(REGIONS, needs_db=False)
    elif args.command == "init-db":
        problems = Config.problems([], needs_db=True)
    else:
        problems = Config.problems(regions)
    if problems:
        for problem in problems:
            logger.error(f"Configuration problem: {problem}")
        logger.error("Run stopped: fix .env and try again (exit code 2)")
        return EXIT_CONFIG

    signal.signal(signal.SIGTERM, _on_sigterm)
    try:
        if args.command == "test-live":
            return test_live_connection()
        if args.command == "health":
            return run_health(regions)

        if args.command == "init-db" or not args.skip_db_init:
            try:
                ensure_database_and_tables()
            except Exception as exc:
                logger.exception(f"Database setup failed: {exc}")
                if args.command == "init-db":
                    return EXIT_FAILED
        if args.command == "init-db":
            logger.info("Database and table check finished.")
            return EXIT_OK

        results = execute_sync(args.entity, args.region, dry_run=args.dry_run, lookback_days=args.days)
        failed = [r for r in results if not r.ok]
        (logger.error if failed else logger.info)(summarize(results, time.monotonic() - started))
        return EXIT_FAILED if failed else EXIT_OK

    except (KeyboardInterrupt, RunInterrupted) as exc:
        reason = str(exc) or "Ctrl-C"
        region, step = _state.region, _state.step
        logger.error(f"Run interrupted ({reason}) during {region or '-'} {step or '-'} after "
                     f"{time.monotonic() - started:.0f}s; the open transaction is rolled back.")
        if region and step and not getattr(args, "dry_run", False):
            record_sync_error(region, STATE_ENTITY[step], f"interrupted ({reason})")
        if _state.results:
            logger.error(summarize(_state.results, time.monotonic() - started))
        return EXIT_INTERRUPTED


if __name__ == "__main__":
    sys.excepthook = _log_unhandled
    sys.exit(main())
