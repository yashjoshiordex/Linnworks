"""Client module entry point and factory function."""

from typing import Optional
from lwsync.config import Config
from lwsync.client.base import LinnworksClient
from lwsync.client.live import LiveClient
from lwsync.client.fixture import FixtureClient
from lwsync.client.auth import LinnworksAuthManager


def get_client(mode: Optional[str] = None) -> LinnworksClient:
    """Return an instance of LinnworksClient based on mode ('fixture' or 'live')."""
    selected_mode = (mode or Config.MODE).lower()
    if selected_mode == "live":
        return LiveClient()
    return FixtureClient()


__all__ = [
    "LinnworksClient",
    "LiveClient",
    "FixtureClient",
    "LinnworksAuthManager",
    "get_client",
]
