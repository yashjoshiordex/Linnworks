"""MySQL connection helpers and the table-creation step.

All sync code writes through get_db_cursor(), so every sync step is one transaction:
either all its rows are saved or none are.
"""

import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Generator, TypeVar
import pymysql
from pymysql.cursors import DictCursor

from lwsync.config import Config, BASE_DIR
from lwsync.logger import logger


def get_db_connection(database: str | None = None) -> pymysql.Connection:
    """Create and return a PyMySQL connection.
    
    If database is None, connects to Config.DB_NAME.
    """
    db = database if database is not None else Config.DB_NAME
    return pymysql.connect(
        host=Config.DB_HOST,
        port=Config.DB_PORT,
        user=Config.DB_USER,
        password=Config.DB_PASSWORD,
        database=db,
        charset=Config.DB_CHARSET,
        cursorclass=DictCursor,  # rows come back as dicts: row["sku"], not row[0]
        autocommit=False,  # nothing is saved until commit(), see get_db_cursor()
        # Fail instead of hanging forever if RDS is unreachable or stops answering.
        connect_timeout=Config.DB_CONNECT_TIMEOUT,
        read_timeout=Config.DB_READ_TIMEOUT,
        write_timeout=Config.DB_WRITE_TIMEOUT,
    )


# MySQL/MariaDB error codes worth retrying: the server went away or the connection dropped
# (2006, 2013), can't connect right now (2003), deadlock (1213), lock wait timeout (1205).
TRANSIENT_DB_ERRORS = {1205, 1213, 2003, 2006, 2013}

T = TypeVar("T")


def is_transient_db_error(exc: BaseException) -> bool:
    """True for DB errors that usually succeed when the same transaction is simply run again."""
    if isinstance(exc, (pymysql.err.OperationalError, pymysql.err.InternalError)) and exc.args:
        return exc.args[0] in TRANSIENT_DB_ERRORS
    return False


def with_db_retry(write: Callable[[], T], what: str, retries: int | None = None) -> T:
    """Run write() (one complete transaction) again if it fails with a temporary DB error.

    write() must open its own get_db_cursor() so each attempt is a fresh, all-or-nothing
    transaction: a failed attempt is rolled back completely before the next one starts.
    Any other error, or the last failed attempt, is raised to the caller.
    """
    attempts = 1 + (Config.DB_WRITE_RETRIES if retries is None else retries)
    for attempt in range(1, attempts + 1):
        try:
            return write()
        except Exception as exc:
            if attempt == attempts or not is_transient_db_error(exc):
                raise
            wait = 2 * attempt
            logger.warning(
                f"Temporary database error while writing {what} (attempt {attempt}/{attempts}): "
                f"{exc}. Retrying in {wait}s."
            )
            time.sleep(wait)
    raise AssertionError("unreachable")


@contextmanager
def get_db_cursor() -> Generator[pymysql.cursors.DictCursor, None, None]:
    """Yield a cursor inside one transaction: commit if the block finishes, roll back on any error.

    Usage:
        with get_db_cursor() as cursor:
            cursor.execute(...)
    A new connection is opened and closed each time (runs are short and infrequent, so no pool).
    """
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            yield cursor
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:  # connection already gone; the server rolls back on its own
            pass
        raise  # the caller logs it once, with context; no duplicate log line here
    finally:
        try:
            conn.close()
        except Exception:
            pass


# Columns added after the first release. CREATE TABLE IF NOT EXISTS never changes an existing
# table, so databases created earlier get these through ALTER TABLE (see _add_missing_columns).
# Each entry: (table, column, column definition, index name or None). Append only; never edit.
ADDED_COLUMNS: list[tuple[str, str, str, str | None]] = [
    ("lw_order_items", "composite_child_sku",
     "VARCHAR(100) NULL COMMENT 'Composite listing: its single component, i.e. the stock item "
     "actually shipped (normally = Fill SKU). NULL if not composite or several components' "
     "AFTER stock_item_id",
     "idx_order_items_composite_child_sku"),
    ("lw_order_items", "composite_children",
     "JSON NULL COMMENT 'Composite listing: all components as [{\"sku\",\"quantity\",\"stock_item_id\"}]; "
     "quantity = total units for the line' AFTER composite_child_sku",
     None),
]


def _add_missing_columns(cursor) -> None:
    """Add any ADDED_COLUMNS the current database doesn't have yet. Safe to run every time."""
    for table, column, definition, index in ADDED_COLUMNS:
        cursor.execute(
            "SELECT COUNT(*) AS n FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = %s",
            (table, column),
        )
        if cursor.fetchone()["n"]:
            continue
        logger.info(f"Adding column {table}.{column}")
        cursor.execute(f"ALTER TABLE `{table}` ADD COLUMN `{column}` {definition}")
        if index:
            cursor.execute(f"ALTER TABLE `{table}` ADD INDEX `{index}` (`{column}`)")


def ensure_database_and_tables(sql_file_path: Path | None = None) -> None:
    """Ensure the target database exists and execute CREATE TABLE IF NOT EXISTS scripts.
    
    If the tables already exist, CREATE TABLE IF NOT EXISTS simply leaves them intact.
    If they do not exist, they are created.
    """
    if sql_file_path is None:
        sql_file_path = BASE_DIR / "sql" / "001_create_tables.sql"

    logger.info(f"Checking database '{Config.DB_NAME}' and tables...")

    # Step 1: make sure the database itself exists (connect without selecting a database).
    # Only a warning on failure: on RDS the database usually exists and the user may not
    # have the right to create one.
    try:
        server_conn = pymysql.connect(
            host=Config.DB_HOST,
            port=Config.DB_PORT,
            user=Config.DB_USER,
            password=Config.DB_PASSWORD,
            charset=Config.DB_CHARSET,
            cursorclass=DictCursor,
            autocommit=True,
        )
        with server_conn.cursor() as cur:
            cur.execute(
                f"CREATE DATABASE IF NOT EXISTS `{Config.DB_NAME}` "
                f"CHARACTER SET {Config.DB_CHARSET} COLLATE utf8mb4_unicode_ci;"
            )
        server_conn.close()
    except Exception as e:
        logger.warning(f"Could not verify/create database '{Config.DB_NAME}' directly: {e}")

    # Step 2: Run table creation script (contains CREATE TABLE IF NOT EXISTS)
    if not sql_file_path.exists():
        logger.warning(f"SQL file not found at {sql_file_path}. Skipping automatic schema check.")
        return

    sql_content = sql_file_path.read_text(encoding="utf-8")
    
    # Split the file into statements: skip comment lines (--) and blank lines, and end a
    # statement at a line ending with ";". Simple on purpose: the SQL file must keep one
    # statement terminator per line end and no ";" inside strings.
    statements = []
    current = []
    for line in sql_content.splitlines():
        trimmed = line.strip()
        if trimmed.startswith("--") or not trimmed:
            continue
        current.append(line)
        if trimmed.endswith(";"):
            statements.append("\n".join(current))
            current = []

    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            for stmt in statements:
                stmt_clean = stmt.strip()
                if stmt_clean:
                    cursor.execute(stmt_clean)
            # Tables that already existed: add columns introduced since they were created.
            _add_missing_columns(cursor)
        conn.commit()
        logger.info("Database schema check complete (tables verified / created).")
    except Exception as e:
        conn.rollback()
        logger.error(f"Failed to execute schema setup: {e}")
        raise
    finally:
        conn.close()

