"""Logging for lwsync: every module does `from lwsync.logger import logger`.

Where logs go
- Always to the console (stdout). Under cron, stdout is appended to cron.log.
- To a file only when the command line asks for it (configure_file_logging), one file per job
  in LW_LOG_DIR: orders.log, catalog.log, health.log, ... Importing lwsync (e.g. in tests)
  never writes a log file.

Line format (times always in UTC):
    2026-11-19 06:15:02 UTC INFO    [orders run=0615-7f3a] [US] Fetched 14042 open order(s).
The run id is the same on every line of one run, so `grep run=0615-7f3a orders.log` shows it all.

Rotation and retention are done by the system's logrotate (deploy/logrotate-lwsync: daily,
kept 90 days, compressed). WatchedFileHandler notices when logrotate moves the file and reopens
it, and several processes can append to the same file safely, unlike RotatingFileHandler.

Secrets: every line, tracebacks included, passes through a redaction step that replaces known
secret values (tokens, app secrets, DB password, session tokens) and e-mail addresses.
Level comes from LW_LOG_LEVEL (default INFO; DEBUG shows every API page and request).
"""

import contextvars
import logging
import os
import re
import secrets
import sys
import time
from datetime import datetime, timezone
from logging.handlers import WatchedFileHandler
from pathlib import Path
from typing import Optional

from lwsync.config import Config

LOGGER_NAME = "lwsync"

# Run label shown in every line, e.g. "orders run=0615-7f3a". "-" outside a run.
_run_label: contextvars.ContextVar[str] = contextvars.ContextVar("lwsync_run_label", default="-")

# Values that must never appear in a log line. Filled from the environment at import, and by
# the auth code with each new Linnworks session token (register_secret).
_secrets: set[str] = set()
_SECRET_ENV_HINTS = ("TOKEN", "SECRET", "PASSWORD", "PWD", "APP_ID")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def register_secret(value: Optional[str]) -> None:
    """Never log this value (e.g. a new session token). Short values are ignored (too common)."""
    if isinstance(value, str) and len(value) >= 6:
        _secrets.add(value)


def _load_env_secrets() -> None:
    for name, value in os.environ.items():
        if name in ("PWD", "OLDPWD"):  # shell's current directory, not a password
            continue
        if any(hint in name.upper() for hint in _SECRET_ENV_HINTS):
            register_secret(value)


def redact(text: str) -> str:
    """Replace secret values and e-mail addresses in text."""
    for value in sorted(_secrets, key=len, reverse=True):  # longest first: no partial leftovers
        if value in text:
            text = text.replace(value, "[REDACTED]")
    return _EMAIL_RE.sub("[email]", text)


class _ContextFilter(logging.Filter):
    """Adds the current run label to every record as %(run)s."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.run = _run_label.get()
        return True


class _RedactingUtcFormatter(logging.Formatter):
    """UTC timestamps, and redaction of the fully formatted line (traceback included)."""

    converter = time.gmtime

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


_FORMAT = "%(asctime)s UTC %(levelname)-7s [%(run)s] %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


def _formatter() -> logging.Formatter:
    return _RedactingUtcFormatter(fmt=_FORMAT, datefmt=_DATEFMT)


def setup_logger(name: str = LOGGER_NAME) -> logging.Logger:
    """Console logging, set up once. File logging is added by configure_file_logging()."""
    log = logging.getLogger(name)
    if getattr(log, "_lwsync_configured", False):
        return log
    _load_env_secrets()
    log.setLevel(getattr(logging, Config.LOG_LEVEL.upper(), logging.INFO))
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(_formatter())
    console.addFilter(_ContextFilter())
    log.addHandler(console)
    log._lwsync_configured = True  # type: ignore[attr-defined]
    return log


def configure_file_logging(job: str, log_dir: Optional[Path] = None) -> Path:
    """Also write this process's log to <LW_LOG_DIR>/<job>.log. Returns the file path."""
    if not re.fullmatch(r"[a-z][a-z0-9_-]*", job):
        raise ValueError(f"Invalid log job name: {job!r}")
    directory = Path(log_dir or Config.LOG_DIR)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{job}.log"
    for handler in list(logger.handlers):
        if isinstance(handler, WatchedFileHandler):
            logger.removeHandler(handler)
            handler.close()
    handler = WatchedFileHandler(path, encoding="utf-8")
    handler.setFormatter(_formatter())
    handler.addFilter(_ContextFilter())
    logger.addHandler(handler)
    return path


def quiet_console_when_unattended() -> None:
    """Under cron (stdout is not a terminal) the job's .log file already has every line, so drop
    the console copy; otherwise cron.log would duplicate it. cron.log then only gets what is
    printed outside logging (e.g. a crash before logging starts, via 2>&1).
    Set LW_LOG_CONSOLE=true to keep the console output anyway."""
    if sys.stdout.isatty() or os.getenv("LW_LOG_CONSOLE", "").strip().lower() in ("1", "true", "yes"):
        return
    for handler in list(logger.handlers):
        if type(handler) is logging.StreamHandler:
            logger.removeHandler(handler)


def start_run(job: str) -> str:
    """Begin a run: sets the label shown on every line and returns the run id (e.g. 0615-7f3a)."""
    run_id = f"{datetime.now(timezone.utc).strftime('%H%M')}-{secrets.token_hex(2)}"
    _run_label.set(f"{job} run={run_id}")
    return run_id


def current_run_label() -> str:
    return _run_label.get()


logger = setup_logger()
