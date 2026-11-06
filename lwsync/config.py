"""Configuration manager loading environment variables from .env."""

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

    # Execution Mode: 'fixture' or 'live'
    MODE: str = os.getenv("LW_MODE", "fixture").lower()

    # Database Settings
    DB_HOST: str = os.getenv("DB_HOST", "127.0.0.1")
    DB_PORT: int = int(os.getenv("DB_PORT", "3306"))
    DB_NAME: str = os.getenv("DB_NAME", "linnworks_sync")
    DB_USER: str = os.getenv("DB_USER", "root")
    DB_PASSWORD: str = os.getenv("DB_PASSWORD", "")
    DB_CHARSET: str = os.getenv("DB_CHARSET", "utf8mb4")

    # Linnworks Regional App Credentials & Tokens
    # Supports separate AppId/AppSecret per region (US and CA) with global fallback
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

    # Backward compatibility mappings
    LW_APP_ID: str = os.getenv("LW_APP_ID", "")
    LW_APP_SECRET: str = os.getenv("LW_APP_SECRET", "")
    LW_TOKENS: dict[str, str] = {
        "US": os.getenv("LW_US_TOKEN", ""),
        "CA": os.getenv("LW_CA_TOKEN", ""),
    }

    # API Configuration
    AUTH_URL: str = "https://api.linnworks.net/api/Auth/AuthorizeByApplication"
    REQUEST_TIMEOUT: int = int(os.getenv("LW_REQUEST_TIMEOUT", "30"))
    MAX_RETRIES: int = int(os.getenv("LW_MAX_RETRIES", "3"))
    RETRY_BACKOFF: float = float(os.getenv("LW_RETRY_BACKOFF", "1.5"))

    # Rate limiting (requests per second per region)
    RATE_LIMIT_RPS: float = float(os.getenv("LW_RATE_LIMIT_RPS", "2.0"))

    # Sync Configuration
    ORDER_DELTA_OVERLAP_MINUTES: int = int(os.getenv("LW_ORDER_DELTA_OVERLAP_MINUTES", "10"))
    LOG_LEVEL: str = os.getenv("LW_LOG_LEVEL", "INFO")
    LOG_DIR: Path = BASE_DIR / os.getenv("LW_LOG_DIR", "logs")
    FIXTURES_DIR: Path = BASE_DIR / "fixtures"

    # Table Prefix
    TABLE_PREFIX: str = os.getenv("LW_TABLE_PREFIX", "lw_")
