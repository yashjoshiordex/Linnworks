"""Linnworks clients: the only code that talks to Linnworks, and it only reads.

- LiveClient     real API calls (live.py), logged in by LinnworksAuthManager (auth.py)
- FixtureClient  same methods, but reads made-up JSON from fixtures/ (offline runs and tests)
Both follow the LinnworksClient interface in base.py, so the sync code works with either.
"""

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
