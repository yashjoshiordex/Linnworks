"""All settings, read once from environment variables (normally the repo's .env file).

.env.example lists every variable. A variable already set in the environment wins over .env
(load_dotenv never overrides), which is how tests force DB_NAME=lwsync_test.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# Base directory of the repository
BASE_DIR = Path(__file__).resolve().parent.parent

# Load .env if present
env_path = BASE_DIR / ".env"
if env_path.exists():
    load_dotenv(dotenv_path=env_path)
else:
    load_dotenv()


class Config:
    """Application configuration."""

    # Execution mode: 'live' calls the real Linnworks API; 'fixture' (default) reads made-up
    # JSON from fixtures/ so the code can run with no credentials and no network.
    MODE: str = os.getenv("LW_MODE", "fixture").lower()

    # Database Settings
    DB_HOST: str = os.getenv("DB_HOST", "127.0.0.1")
    DB_PORT: int = int(os.getenv("DB_PORT", "3306"))
    DB_NAME: str = os.getenv("DB_NAME", "linnworks_sync")
    DB_USER: str = os.getenv("DB_USER", "root")
    DB_PASSWORD: str = os.getenv("DB_PASSWORD", "")
    DB_CHARSET: str = os.getenv("DB_CHARSET", "utf8mb4")

    # Linnworks app credentials. US and CA are separate Linnworks accounts, each with its own
    # installation token. App ID/secret can be per region or shared (LW_APP_ID / LW_APP_SECRET).
    LW_REGIONAL_CONFIG: dict[str, dict[str, str]] = {
        "US": {
            "app_id": os.getenv("LW_US_APP_ID") or os.getenv("LW_APP_ID", ""),
            "app_secret": os.getenv("LW_US_APP_SECRET") or os.getenv("LW_APP_SECRET", ""),
            "token": os.getenv("LW_US_TOKEN", ""),
        },
        "CA": {
            "app_id": os.getenv("LW_CA_APP_ID") or os.getenv("LW_APP_ID", ""),
            "app_secret": os.getenv("LW_CA_APP_SECRET") or os.getenv("LW_APP_SECRET", ""),
            "token": os.getenv("LW_CA_TOKEN", ""),
        },
    }

    # Older single-app settings, still accepted as fallbacks by LinnworksAuthManager.
    LW_APP_ID: str = os.getenv("LW_APP_ID", "")
    LW_APP_SECRET: str = os.getenv("LW_APP_SECRET", "")
    LW_TOKENS: dict[str, str] = {
        "US": os.getenv("LW_US_TOKEN", ""),
        "CA": os.getenv("LW_CA_TOKEN", ""),
    }

    # Host for the v2 orders endpoint. This is where Linnworks HOSTS the account, not the
    # country it sells in: the CA account lives on the EU servers, so CA uses
    # https://eu-api.linnworks.net. All other calls use the server returned at login (auth.py).
    LW_API_BASE_URLS: dict[str, str] = {
        "US": os.getenv("LW_US_API_BASE_URL", ""),
        "CA": os.getenv("LW_CA_API_BASE_URL", ""),
    }
    AUTH_URL: str = "https://api.linnworks.net/api/Auth/AuthorizeByApplication"
    # HTTP behaviour: timeout per request (seconds), retries on 429/5xx/network errors, and the
    # exponential back-off base between retries.
    REQUEST_TIMEOUT: int = int(os.getenv("LW_REQUEST_TIMEOUT", "30"))
    MAX_RETRIES: int = int(os.getenv("LW_MAX_RETRIES", "3"))
    RETRY_BACKOFF: float = float(os.getenv("LW_RETRY_BACKOFF", "1.5"))

    # Rate limiting (requests per second, per region), so we stay well under Linnworks' limits.
    RATE_LIMIT_RPS: float = float(os.getenv("LW_RATE_LIMIT_RPS", "2.0"))
    # Conservative v2 order pacing; this is not a claimed vendor quota.
    ORDERS_RATE_LIMIT_RPS: float = float(os.getenv("LW_ORDERS_RATE_LIMIT_RPS", "0.5"))
    # Seconds to wait after HTTP 429 (too many requests) when Linnworks sends no Retry-After header.
    RATE_LIMIT_COOLDOWN: float = float(os.getenv("LW_RATE_LIMIT_COOLDOWN", "60"))

    # Each orders run starts this many minutes before the previous run's cursor, so an order
    # updated right at the boundary is never missed (re-reading it is harmless: upserts).
    ORDER_DELTA_OVERLAP_MINUTES: int = int(os.getenv("LW_ORDER_DELTA_OVERLAP_MINUTES", "10"))
    # Logging: level, and the folder for <job>.log files (absolute path in production, e.g.
    # /home/ubuntu/lwsync-logs; relative paths are inside the project folder).
    LOG_LEVEL: str = os.getenv("LW_LOG_LEVEL", "INFO")
    LOG_DIR: Path = BASE_DIR / os.getenv("LW_LOG_DIR", "logs")
    # The command line also writes <LOG_DIR>/<job>.log unless this is false.
    LOG_TO_FILE: bool = os.getenv("LW_LOG_TO_FILE", "true").strip().lower() in ("1", "true", "yes")
    # An API call slower than this (seconds) is logged as a warning.
    SLOW_REQUEST_SECONDS: float = float(os.getenv("LW_SLOW_REQUEST_SECONDS", "10"))
    FIXTURES_DIR: Path = BASE_DIR / "fixtures"

    # Database timeouts (seconds): a dead or unreachable RDS fails the run instead of hanging it.
    DB_CONNECT_TIMEOUT: int = int(os.getenv("DB_CONNECT_TIMEOUT", "10"))
    DB_READ_TIMEOUT: int = int(os.getenv("DB_READ_TIMEOUT", "120"))
    DB_WRITE_TIMEOUT: int = int(os.getenv("DB_WRITE_TIMEOUT", "120"))
    # Extra attempts for a write that hit a temporary DB error (lost connection, deadlock).
    DB_WRITE_RETRIES: int = int(os.getenv("DB_WRITE_RETRIES", "2"))

    # `lwsync health`: a job is unhealthy if its last success is older than this (minutes).
    HEALTH_ORDERS_MAX_AGE_MIN: int = int(os.getenv("LW_HEALTH_ORDERS_MAX_AGE_MIN", "45"))
    HEALTH_CATALOG_MAX_AGE_MIN: int = int(os.getenv("LW_HEALTH_CATALOG_MAX_AGE_MIN", "300"))

    @classmethod
    def problems(cls, regions: list[str], needs_db: bool = True) -> list[str]:
        """Settings that are missing or invalid for a run (names only, never values).

        Checked at start-up so a bad .env fails at once with a clear message (exit code 2)
        instead of halfway through a run.
        """
        found = []
        if cls.MODE not in ("live", "fixture"):
            found.append(f"LW_MODE must be 'live' or 'fixture' (got '{cls.MODE}')")
        if needs_db:
            for name in ("DB_HOST", "DB_NAME", "DB_USER"):
                if not getattr(cls, name):
                    found.append(f"{name} is not set")
        if cls.MODE == "live":
            for region in regions:
                cfg = cls.LW_REGIONAL_CONFIG.get(region, {})
                for key, env in (("app_id", "APP_ID"), ("app_secret", "APP_SECRET"), ("token", "TOKEN")):
                    if not cfg.get(key):
                        found.append(f"LW_{region}_{env} is not set")
                if cls.LW_API_BASE_URLS.get(region, "").rstrip("/") not in (
                    "https://us-api.linnworks.net", "https://eu-api.linnworks.net"
                ):
                    found.append(f"LW_{region}_API_BASE_URL must be https://us-api.linnworks.net "
                                 "or https://eu-api.linnworks.net")
        return found

