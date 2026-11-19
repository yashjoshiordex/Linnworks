"""Production behaviour: logging, DB retry, error state, settings check, CLI steps and exit codes."""

import logging
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from logging.handlers import WatchedFileHandler
from unittest.mock import MagicMock, patch

import pymysql
import pytest

from lwsync import __main__ as cli
from lwsync import db, logger as logmod
from lwsync.config import Config
from lwsync.sync import orders, state


# --------------------------------------------------------------------------- logging

def _formatted(record_msg: str, exc_info=None) -> str:
    record = logging.LogRecord("lwsync", logging.ERROR, __file__, 1, record_msg, None, exc_info)
    logmod._ContextFilter().filter(record)
    return logmod._formatter().format(record)


def test_line_has_utc_time_and_run_label():
    run_id = logmod.start_run("orders")
    line = _formatted("[US] Fetched 3 open order(s).")
    assert f"[orders run={run_id}]" in line
    assert " UTC ERROR " in line
    stamp = datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    assert abs(datetime.now(timezone.utc) - stamp) < timedelta(minutes=1)


def test_secrets_and_emails_are_redacted_including_tracebacks():
    logmod.register_secret("session-token-123456")
    try:
        raise RuntimeError("auth failed for token session-token-123456 (buyer@example.com)")
    except RuntimeError:
        import sys
        line = _formatted("call failed with session-token-123456", exc_info=sys.exc_info())
    assert "session-token-123456" not in line
    assert "buyer@example.com" not in line
    assert line.count("[REDACTED]") >= 2 and "[email]" in line


def test_short_values_and_shell_cwd_are_not_treated_as_secrets(monkeypatch):
    monkeypatch.setenv("PWD", "/home/ubuntu/lwsync")
    logmod.register_secret("abc")  # too short to be a real secret
    logmod._load_env_secrets()
    assert logmod.redact("cd /home/ubuntu/lwsync; abc") == "cd /home/ubuntu/lwsync; abc"


def test_file_logging_one_file_per_job(tmp_path):
    path = logmod.configure_file_logging("orders", log_dir=tmp_path)
    try:
        logmod.logger.info("hello file")
        for h in logmod.logger.handlers:
            h.flush()
        assert path == tmp_path / "orders.log"
        assert "hello file" in path.read_text()
        assert sum(isinstance(h, WatchedFileHandler) for h in logmod.logger.handlers) == 1
        logmod.configure_file_logging("catalog", log_dir=tmp_path)  # replaces, doesn't add
        assert sum(isinstance(h, WatchedFileHandler) for h in logmod.logger.handlers) == 1
    finally:
        for h in list(logmod.logger.handlers):
            if isinstance(h, WatchedFileHandler):
                logmod.logger.removeHandler(h)
                h.close()


def test_invalid_job_name_rejected(tmp_path):
    with pytest.raises(ValueError):
        logmod.configure_file_logging("../etc", log_dir=tmp_path)


def test_console_dropped_when_unattended(monkeypatch):
    console = logging.StreamHandler()
    logmod.logger.addHandler(console)
    try:
        monkeypatch.setattr("sys.stdout.isatty", lambda: False)
        monkeypatch.delenv("LW_LOG_CONSOLE", raising=False)
        logmod.quiet_console_when_unattended()
        assert console not in logmod.logger.handlers
    finally:
        if console in logmod.logger.handlers:
            logmod.logger.removeHandler(console)


# --------------------------------------------------------------------------- DB retry

def _op_error(code):
    return pymysql.err.OperationalError(code, "boom")


@pytest.mark.parametrize("code,transient", [(2006, True), (2013, True), (1213, True), (1205, True),
                                            (2003, True), (1146, False), (1062, False)])
def test_transient_error_classification(code, transient):
    assert db.is_transient_db_error(_op_error(code)) is transient


def test_retry_then_success(monkeypatch):
    monkeypatch.setattr(db.time, "sleep", lambda s: None)
    calls = []

    def write():
        calls.append(1)
        if len(calls) < 3:
            raise _op_error(2013)
        return "done"

    assert db.with_db_retry(write, "test", retries=2) == "done"
    assert len(calls) == 3


def test_permanent_error_not_retried(monkeypatch):
    monkeypatch.setattr(db.time, "sleep", lambda s: None)
    write = MagicMock(side_effect=_op_error(1146))
    with pytest.raises(pymysql.err.OperationalError):
        db.with_db_retry(write, "test", retries=2)
    assert write.call_count == 1


def test_retries_exhausted_raise(monkeypatch):
    monkeypatch.setattr(db.time, "sleep", lambda s: None)
    write = MagicMock(side_effect=_op_error(2006))
    with pytest.raises(pymysql.err.OperationalError):
        db.with_db_retry(write, "test", retries=2)
    assert write.call_count == 3


def test_connection_has_timeouts():
    with patch.object(db.pymysql, "connect") as connect:
        db.get_db_connection()
    kwargs = connect.call_args.kwargs
    assert kwargs["connect_timeout"] == Config.DB_CONNECT_TIMEOUT
    assert kwargs["read_timeout"] == Config.DB_READ_TIMEOUT
    assert kwargs["write_timeout"] == Config.DB_WRITE_TIMEOUT


def test_record_sync_error_never_raises_and_keeps_original(monkeypatch, caplog):
    monkeypatch.setattr(db.time, "sleep", lambda s: None)

    @contextmanager
    def broken():
        raise _op_error(2003)
        yield  # pragma: no cover

    with patch.object(state, "get_db_cursor", broken), caplog.at_level("ERROR", logger="lwsync"):
        state.record_sync_error("US", "orders", "original problem")  # must not raise
    assert "Could not save the orders error" in caplog.text
    assert "original problem" in caplog.text


# --------------------------------------------------------------------------- settings check

def test_problems_for_live_mode(monkeypatch):
    monkeypatch.setattr(Config, "MODE", "live")
    monkeypatch.setattr(Config, "LW_REGIONAL_CONFIG", {"US": {"app_id": "a", "app_secret": "", "token": "t"}})
    monkeypatch.setattr(Config, "LW_API_BASE_URLS", {"US": "https://us-ext.linnworks.net"})
    found = Config.problems(["US"])
    assert "LW_US_APP_SECRET is not set" in found
    assert any("LW_US_API_BASE_URL" in p for p in found)
    assert not any("secret-value" in p for p in found)


def test_bad_mode_and_missing_db(monkeypatch):
    monkeypatch.setattr(Config, "MODE", "bogus")
    monkeypatch.setattr(Config, "DB_HOST", "")
    found = Config.problems([])
    assert any("LW_MODE" in p for p in found) and "DB_HOST is not set" in found


def test_main_exits_2_on_bad_config(monkeypatch):
    monkeypatch.setattr(Config, "MODE", "bogus")
    assert cli.main(["run", "orders", "--skip-db-init"]) == cli.EXIT_CONFIG


# --------------------------------------------------------------------------- CLI steps

@pytest.fixture
def fake_sync(monkeypatch):
    calls = []
    client = MagicMock()
    client.get_all_products.return_value = [{"ItemNumber": "A"}]
    monkeypatch.setattr(cli, "get_client", lambda: client)

    def make(name, fail_regions=()):
        def fn(c, region, **kwargs):
            calls.append((name, region, kwargs.get("raw_products")))
            if region in fail_regions:
                raise RuntimeError(f"{name} broke in {region}")
            return 5
        return fn

    monkeypatch.setattr(cli, "sync_locations", make("locations", fail_regions=("US",)))
    monkeypatch.setattr(cli, "sync_stock_levels", make("stock"))
    monkeypatch.setattr(cli, "sync_products", make("products"))
    monkeypatch.setattr(cli, "sync_orders_delta", make("orders"))
    cli._state.results.clear()
    return calls, client


def test_failed_step_does_not_stop_the_others(fake_sync, caplog):
    calls, client = fake_sync
    with caplog.at_level("ERROR", logger="lwsync"):
        results = cli.execute_sync("all", "ALL")
    assert [(r.region, r.step, r.ok) for r in results] == [
        ("US", "locations", False), ("US", "stock", True), ("US", "products", True), ("US", "orders", True),
        ("CA", "locations", True), ("CA", "stock", True), ("CA", "products", True), ("CA", "orders", True),
    ]
    assert "Traceback" in caplog.text and "locations broke in US" in caplog.text
    assert client.get_all_products.call_count == 2  # catalog fetched once per region
    assert all(raw == [{"ItemNumber": "A"}] for name, _, raw in calls if name in ("stock", "products"))


def test_orders_entity_skips_catalog(fake_sync):
    calls, client = fake_sync
    cli.execute_sync("orders", "CA")
    assert [c[0] for c in calls] == ["orders"]
    client.get_all_products.assert_not_called()


def test_summary_line():
    results = [cli.StepResult("US", "orders", True, 120, 4.2), cli.StepResult("CA", "orders", False, None, 1.0, "X")]
    line = cli.summarize(results, 5.4)
    assert line.startswith("Run finished outcome=failed duration=5s")
    assert "US orders=ok(120,4s)" in line and "CA orders=FAILED(-,1s)" in line


def test_main_exit_codes(monkeypatch):
    monkeypatch.setattr(Config, "MODE", "fixture")
    monkeypatch.setattr(cli, "execute_sync", lambda *a, **k: [cli.StepResult("US", "orders", True, 1)])
    assert cli.main(["run", "orders", "--skip-db-init"]) == cli.EXIT_OK
    monkeypatch.setattr(cli, "execute_sync", lambda *a, **k: [cli.StepResult("US", "orders", False)])
    assert cli.main(["run", "orders", "--skip-db-init"]) == cli.EXIT_FAILED


def test_interrupt_is_recorded_against_the_running_step(monkeypatch):
    monkeypatch.setattr(Config, "MODE", "fixture")

    def interrupted(*a, **k):
        cli._state.region, cli._state.step = "CA", "orders"
        raise cli.RunInterrupted("signal SIGTERM")

    monkeypatch.setattr(cli, "execute_sync", interrupted)
    with patch.object(cli, "record_sync_error") as record:
        assert cli.main(["run", "orders", "--skip-db-init"]) == cli.EXIT_INTERRUPTED
    record.assert_called_once_with("CA", "orders", "interrupted (signal SIGTERM)")
    cli._state.region = cli._state.step = None


# --------------------------------------------------------------------------- health

def _health_rows(age_minutes, error=None):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    rows = []
    for region in ("US", "CA"):
        for entity in ("orders", "refunds", "locations", "stock_levels", "products"):
            rows.append({"region": region, "entity": entity,
                         "last_success_at": now - timedelta(minutes=age_minutes), "last_error": error})
    return rows


def _patch_health_db(monkeypatch, rows):
    cursor = MagicMock()
    cursor.fetchall.return_value = rows

    @contextmanager
    def fake():
        yield cursor

    monkeypatch.setattr(cli, "get_db_cursor", fake)


def test_health_ok(monkeypatch):
    _patch_health_db(monkeypatch, _health_rows(5))
    assert cli.run_health(["US", "CA"]) == cli.EXIT_OK


def test_health_stale_or_failing(monkeypatch, caplog):
    _patch_health_db(monkeypatch, _health_rows(60))  # orders limit is 45 min
    with caplog.at_level("ERROR", logger="lwsync"):
        assert cli.run_health(["US"]) == cli.EXIT_FAILED
    assert "UNHEALTHY US orders" in caplog.text and "UNHEALTHY US products" not in caplog.text
    _patch_health_db(monkeypatch, _health_rows(5, error="HTTP 503"))
    assert cli.run_health(["CA"]) == cli.EXIT_FAILED


def test_health_never_ran(monkeypatch):
    _patch_health_db(monkeypatch, [])
    assert cli.run_health(["US"]) == cli.EXIT_FAILED


# --------------------------------------------------------------------------- orders batch

def test_unreadable_open_order_is_skipped_but_not_marked_deleted(monkeypatch, caplog):
    good = {"OrderId": "o-1", "NumOrderId": 1, "GeneralInfo": {"Status": 1}, "Items": []}
    bad = {"OrderId": "o-2", "NumOrderId": 2, "GeneralInfo": {"Status": 1},
           "Items": [{"RowId": "r", "SKU": "X", "Quantity": "not-a-number"}]}
    no_id = {"NumOrderId": 3, "Items": []}
    with caplog.at_level("WARNING", logger="lwsync"):
        batch, items, items_map, seen = orders._extract_batch("US", [good, bad, no_id], "open")
    assert [row[1] for row in batch] == ["o-1"]
    assert seen == {"o-1", "o-2"}  # o-2 still counts as present, so it is never marked deleted
    assert "o-2" not in items_map  # and its existing lines are not pruned
    assert "Skipped 2 order(s)" in caplog.text


def test_line_without_row_id_is_warned(caplog):
    raw = {"OrderId": "o-1", "NumOrderId": 9, "GeneralInfo": {}, "Items": [{"SKU": "AJC-D5S"}]}
    with caplog.at_level("WARNING", logger="lwsync"):
        _, items = orders._extract_order_and_items("US", raw)
    assert items == [] and "Order 9: skipped a line without a RowId (SKU AJC-D5S)" in caplog.text
