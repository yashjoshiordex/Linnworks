"""Authentication and session management for Linnworks API."""

import time
from typing import Optional
import requests
from lwsync.config import Config
from lwsync.logger import logger


class LinnworksAuthSession:
    """Represents an active authenticated session for a specific region."""

    def __init__(self, token: str, server: str, expires_in_seconds: int = 1200):
        self.token = token
        # Strip trailing slash if present
        self.server = server.rstrip("/")
        self.expires_at = time.time() + expires_in_seconds

    @property
    def is_expired(self) -> bool:
        """Consider expired 60 seconds before actual timeout to be safe."""
        return time.time() >= (self.expires_at - 60)


class LinnworksAuthManager:
    """Manages regional authorization tokens and re-authentication."""

    def __init__(
        self,
        regional_config: Optional[dict[str, dict[str, str]]] = None,
        # Backward-compatible parameters:
        app_id: Optional[str] = None,
        app_secret: Optional[str] = None,
        tokens: Optional[dict[str, str]] = None,
    ):
        self.regional_config = regional_config or Config.LW_REGIONAL_CONFIG
        self.fallback_app_id = app_id or Config.LW_APP_ID
        self.fallback_app_secret = app_secret or Config.LW_APP_SECRET
        self.fallback_tokens = tokens or Config.LW_TOKENS
        self._sessions: dict[str, LinnworksAuthSession] = {}

    def get_session(self, region: str, force_refresh: bool = False) -> LinnworksAuthSession:
        """Get an active session for the given region, authorizing if necessary."""
        region_key = region.upper()
        session = self._sessions.get(region_key)

        if not force_refresh and session and not session.is_expired:
            return session

        logger.info(f"Authenticating Linnworks session for region '{region_key}'...")

        # Extract regional AppId, AppSecret, and Token
        reg_cfg = self.regional_config.get(region_key, {})
        app_id = reg_cfg.get("app_id") or self.fallback_app_id
        app_secret = reg_cfg.get("app_secret") or self.fallback_app_secret
        token = reg_cfg.get("token") or self.fallback_tokens.get(region_key)

        if not app_id or not app_secret or not token:
            raise ValueError(
                f"Missing Linnworks credentials for region '{region_key}'. "
                f"Ensure app_id, app_secret, and token are configured in .env."
            )

        payload = {
            "ApplicationId": app_id,
            "ApplicationSecret": app_secret,
            "Token": token,
        }

        response = requests.post(
            Config.AUTH_URL,
            json=payload,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            timeout=Config.REQUEST_TIMEOUT,
        )

        if response.status_code != 200:
            logger.error(
                f"Linnworks authorization failed for region {region_key}: "
                f"HTTP {response.status_code} - {response.text}"
            )
            response.raise_for_status()

        data = response.json()
        session_token = data.get("Token")
        server = data.get("Server")

        if not session_token or not server:
            raise RuntimeError(
                f"Invalid auth response from Linnworks for {region_key}: missing Token or Server"
            )

        new_session = LinnworksAuthSession(token=session_token, server=server)
        self._sessions[region_key] = new_session
        logger.info(f"Successfully authenticated region '{region_key}' against {server}")
        return new_session

    def invalidate_session(self, region: str) -> None:
        """Clear cached session for a region so next call will re-authorize."""
        region_key = region.upper()
        if region_key in self._sessions:
            logger.info(f"Invalidating session for region '{region_key}'")
            del self._sessions[region_key]
