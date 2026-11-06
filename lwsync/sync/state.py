"""Sync state and cursor management helper."""

from datetime import datetime, timezone
from typing import Optional, Tuple
from lwsync.db import get_db_cursor
from lwsync.logger import logger


def get_sync_cursor(region: str, entity: str) -> Optional[str]:
    """Retrieve the last_cursor for a given region and entity."""
    with get_db_cursor() as cursor:
        cursor.execute(
            """
            SELECT last_cursor FROM lw_sync_state
            WHERE region = %s AND entity = %s
            LIMIT 1;
            """,
            (region.upper(), entity.lower()),
        )
        row = cursor.fetchone()
        return row["last_cursor"] if row and row.get("last_cursor") else None


def update_sync_success(
    region: str,
    entity: str,
    new_cursor: Optional[str] = None,
) -> None:
    """Update lw_sync_state with success timestamp and increment run_count."""
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with get_db_cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO lw_sync_state (region, entity, last_success_at, last_cursor, last_error, run_count, created_at, updated_at)
            VALUES (%s, %s, %s, %s, NULL, 1, %s, %s)
            ON DUPLICATE KEY UPDATE
                last_success_at = VALUES(last_success_at),
                last_cursor = COALESCE(VALUES(last_cursor), last_cursor),
                last_error = NULL,
                run_count = run_count + 1,
                updated_at = VALUES(updated_at);
            """,
            (region.upper(), entity.lower(), now_utc, new_cursor, now_utc, now_utc),
        )


def record_sync_error(region: str, entity: str, error_message: str) -> None:
    """Record an error message in lw_sync_state."""
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with get_db_cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO lw_sync_state (region, entity, last_error, run_count, created_at, updated_at)
            VALUES (%s, %s, %s, 1, %s, %s)
            ON DUPLICATE KEY UPDATE
                last_error = VALUES(last_error),
                updated_at = VALUES(updated_at);
            """,
            (region.upper(), entity.lower(), error_message, now_utc, now_utc),
        )

