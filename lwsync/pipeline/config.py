"""Configuration and SKU Swap Matrix for the 3PL Order Processing Pipeline."""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from lwsync.config import BASE_DIR, Config


@dataclass(frozen=True)
class SkuPairRule:
    """SKU substitution rule definition."""
    primary_sku: str
    substitute_sku: str
    threshold: int
    region: str
    warehouse_location: str
    location_id: str


# Verified warehouse location GUIDs from lw_locations
WAREHOUSE_LOCATIONS = {
    "US": {
        "name": "US | AJC",
        "location_id": os.getenv("3PL_US_LOCATION_ID", "4d5833b6-a649-4d81-b572-da15682797ef"),
    },
    "CA": {
        "name": "CA | AJC",
        "location_id": os.getenv("3PL_CA_LOCATION_ID", "faaab11f-76e5-469f-b105-d9472408f85c"),
    },
}

# The 4 Primary-Substitute SKU pairs defined in the specification
DEFAULT_SKU_SWAP_RULES: List[SkuPairRule] = [
    SkuPairRule(
        primary_sku=os.getenv("SKU_PAIR_1_PRIMARY", "PRIMARY-SKU-001"),
        substitute_sku=os.getenv("SKU_PAIR_1_SUB", "SUB-SKU-001"),
        threshold=int(os.getenv("SKU_PAIR_1_THRESHOLD", "20")),
        region="US",
        warehouse_location=WAREHOUSE_LOCATIONS["US"]["name"],
        location_id=WAREHOUSE_LOCATIONS["US"]["location_id"],
    ),
    SkuPairRule(
        primary_sku=os.getenv("SKU_PAIR_2_PRIMARY", "PRIMARY-SKU-002"),
        substitute_sku=os.getenv("SKU_PAIR_2_SUB", "SUB-SKU-002"),
        threshold=int(os.getenv("SKU_PAIR_2_THRESHOLD", "30")),
        region="US",
        warehouse_location=WAREHOUSE_LOCATIONS["US"]["name"],
        location_id=WAREHOUSE_LOCATIONS["US"]["location_id"],
    ),
    SkuPairRule(
        primary_sku=os.getenv("SKU_PAIR_3_PRIMARY", "PRIMARY-SKU-003"),
        substitute_sku=os.getenv("SKU_PAIR_3_SUB", "SUB-SKU-003"),
        threshold=int(os.getenv("SKU_PAIR_3_THRESHOLD", "50")),
        region="CA",
        warehouse_location=WAREHOUSE_LOCATIONS["CA"]["name"],
        location_id=WAREHOUSE_LOCATIONS["CA"]["location_id"],
    ),
    SkuPairRule(
        primary_sku=os.getenv("SKU_PAIR_4_PRIMARY", "PRIMARY-SKU-004"),
        substitute_sku=os.getenv("SKU_PAIR_4_SUB", "SUB-SKU-004"),
        threshold=int(os.getenv("SKU_PAIR_4_THRESHOLD", "20")),
        region="CA",
        warehouse_location=WAREHOUSE_LOCATIONS["CA"]["name"],
        location_id=WAREHOUSE_LOCATIONS["CA"]["location_id"],
    ),
]


class PipelineConfig:
    """Pipeline filesystem and operational settings."""

    BASE_DIR: Path = BASE_DIR

    # SFTP / Local directory structure
    SFTP_ROOT: Path = Path(os.getenv("3PL_SFTP_ROOT", str(BASE_DIR / "data" / "sftp")))

    # Inbound: where Linnworks deposits exported orders
    LINNWORKS_EXPORT_DIR: Path = Path(
        os.getenv("3PL_LINNWORKS_EXPORT_DIR", str(SFTP_ROOT / "linnworks" / "orders"))
    )

    # Outbound: destination directories polled by Extensiv (3PL Central)
    EXTENSIV_US_INBOUND_DIR: Path = Path(
        os.getenv("3PL_US_INBOUND_DIR", str(SFTP_ROOT / "3pl_us" / "inbound_orders"))
    )
    EXTENSIV_CA_INBOUND_DIR: Path = Path(
        os.getenv("3PL_CA_INBOUND_DIR", str(SFTP_ROOT / "3pl_ca" / "inbound_orders"))
    )

    # Archive and failed directories
    ARCHIVE_DIR: Path = Path(os.getenv("3PL_ARCHIVE_DIR", str(SFTP_ROOT / "archive")))
    FAILED_DIR: Path = Path(os.getenv("3PL_FAILED_DIR", str(SFTP_ROOT / "failed")))

    # Rules definition
    SKU_SWAP_RULES: List[SkuPairRule] = DEFAULT_SKU_SWAP_RULES

    # AWS SES / Alert configuration
    SES_REGION: str = os.getenv("AWS_SES_REGION", "us-east-1")
    ALERT_EMAIL_FROM: str = os.getenv("3PL_ALERT_EMAIL_FROM", "alerts@batteryclerk.com")
    ALERT_EMAIL_TO: str = os.getenv("3PL_ALERT_EMAIL_TO", "operations@batteryclerk.com")
    ENABLE_EMAIL_ALERTS: bool = os.getenv("3PL_ENABLE_EMAIL_ALERTS", "false").lower() in ("true", "1", "yes")

    @classmethod
    def ensure_directories(cls) -> None:
        """Create all required pipeline directories if they do not exist."""
        for p in (
            cls.LINNWORKS_EXPORT_DIR,
            cls.EXTENSIV_US_INBOUND_DIR,
            cls.EXTENSIV_CA_INBOUND_DIR,
            cls.ARCHIVE_DIR,
            cls.FAILED_DIR,
        ):
            p.mkdir(parents=True, exist_ok=True)

    @classmethod
    def get_destination_dir(cls, region: str) -> Path:
        """Return the destination 3PL inbound directory for a region."""
        region_upper = region.upper()
        if region_upper == "CA":
            return cls.EXTENSIV_CA_INBOUND_DIR
        return cls.EXTENSIV_US_INBOUND_DIR
