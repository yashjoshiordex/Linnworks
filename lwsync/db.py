"""Database connection and migration helpers."""

from contextlib import contextmanager
from pathlib import Path
from typing import Generator
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
        cursorclass=DictCursor,
        autocommit=False,
    )


@contextmanager
def get_db_cursor() -> Generator[pymysql.cursors.DictCursor, None, None]:
    """Context manager yielding a database cursor with automatic commit/rollback."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            yield cursor
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.error(f"Database transaction error: {e}")
        raise
    finally:
        conn.close()


def ensure_database_and_tables(sql_file_path: Path | None = None) -> None:
    """Ensure the target database exists and execute CREATE TABLE IF NOT EXISTS scripts.
    
    If the tables already exist, CREATE TABLE IF NOT EXISTS simply leaves them intact.
    If they do not exist, they are created.
    """
    if sql_file_path is None:
        sql_file_path = BASE_DIR / "sql" / "001_create_tables.sql"

    logger.info(f"Checking database '{Config.DB_NAME}' and tables...")

    # Step 1: Ensure database itself exists
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
    
    # Split queries by semicolon while ignoring comments
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
        conn.commit()
        logger.info("Database schema check complete (tables verified / created).")
    except Exception as e:
        conn.rollback()
        logger.error(f"Failed to execute schema setup: {e}")
        raise
    finally:
        conn.close()

