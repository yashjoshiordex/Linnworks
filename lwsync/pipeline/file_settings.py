"""Regional GI filesystem settings, independent of the draft swap rules."""

from dataclasses import dataclass
from pathlib import Path
from collections.abc import Mapping
import os


@dataclass(frozen=True)
class FileSettings:
    region: str
    input_dir: Path
    output_dir: Path
    archive_dir: Path
    temp_dir: Path
    held_dir: Path
    error_dir: Path
    lock_file: Path
    stable_seconds: int = 60

    def __post_init__(self):
        if self.region not in ("US", "CA"):
            raise ValueError("Unknown pipeline region")
        if self.stable_seconds < 60:
            raise ValueError("Stable interval must be at least 60 seconds")
        folders = [self.input_dir, self.output_dir, self.archive_dir,
                   self.temp_dir, self.held_dir, self.error_dir]
        resolved = [path.resolve() for path in folders]
        for index, folder in enumerate(resolved):
            for other in resolved[index + 1:]:
                if folder == other or folder in other.parents or other in folder.parents:
                    raise ValueError("Pipeline folders must be separate, non-overlapping paths")
        lock = self.lock_file.resolve()
        if any(lock == path or path in lock.parents for path in resolved):
            raise ValueError("Lock file must be outside pipeline data folders")

    @classmethod
    def from_env(cls, region: str, env: Mapping[str, str] | None = None):
        values = os.environ if env is None else env
        region = region.upper()
        if region not in ("US", "CA"):
            raise ValueError("Unknown pipeline region")
        prefix = f"LW_PIPELINE_{region}_"
        for suffix in ("HELD_DIR", "ERROR_DIR"):
            if not values.get(prefix + suffix, "").strip():
                raise ValueError(f"Required setting: {prefix}{suffix}")
        defaults = {
            "INPUT_DIR": "new_orders", "OUTPUT_DIR": "orders",
            "ARCHIVE_DIR": "archive", "TEMP_DIR": ".pipeline_tmp",
        }
        paths = {name.lower(): Path(values.get(prefix + name) or f"/data/{region.lower()}/{child}")
                 for name, child in defaults.items()}
        return cls(region=region, **paths,
                   held_dir=Path(values[prefix + "HELD_DIR"]),
                   error_dir=Path(values[prefix + "ERROR_DIR"]),
                   lock_file=Path(values.get("LW_PIPELINE_LOCK_FILE") or "/run/lock/lwsync-pipeline.lock"),
                   stable_seconds=int(values.get("LW_PIPELINE_STABLE_SECONDS", "60")))
