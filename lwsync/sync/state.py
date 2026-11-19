"""Read and write lw_sync_state: one row per (region, entity), e.g. ("US", "orders").

- last_success_at  when the job last finished without error (UTC)
- last_cursor      where the next run continues from (orders: last "updated since" time)
- last_error       message from the last failure; cleared by the next success
- run_count        number of successful runs
This table is the first place to look when checking whether the sync is healthy.
"""

from datetime import datetime, timezone
from typing import Optional, Tuple
from lwsync.db import get_db_cursor, with_db_retry
from lwsync.logger import logger, redact


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
    """Record a successful run: set last_success_at, clear last_error, add 1 to run_count.

    If new_cursor is None the saved cursor is kept (COALESCE), so jobs without a cursor
    don't wipe one.
    """
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
    """Save the failure message. last_success_at and the cursor are left alone, so the next
    run retries from the same point.

    Never raises: if the database itself is the problem, saving the error fails too, and that
    second error must not replace the original one. It is logged instead, and the caller goes
    on to raise the original error.
    """
    message = redact(str(error_message))[:2000]
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    def write() -> None:
        with get_db_cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO lw_sync_state (region, entity, last_error, run_count, created_at, updated_at)
                VALUES (%s, %s, %s, 0, %s, %s)
                ON DUPLICATE KEY UPDATE
                    last_error = VALUES(last_error),
                    updated_at = VALUES(updated_at);
                """,
                (region.upper(), entity.lower(), message, now_utc, now_utc),
            )

    try:
        with_db_retry(write, f"{region.upper()} {entity} error state")
    except Exception as exc:
        logger.error(
            f"[{region.upper()}] Could not save the {entity} error to lw_sync_state ({exc}); "
            f"original error: {message}"
        )
