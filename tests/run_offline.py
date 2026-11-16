r"""Run only offline suites with network/DB access blocked, ignoring local secrets.

Usage: .venv\Scripts\python.exe tests/run_offline.py
"""

import os
import argparse
from pathlib import Path
import socket
import sys
from unittest.mock import patch

os.environ["PYTHON_DOTENV_DISABLED"] = "1"
os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pymysql
import pytest
import requests


def blocked(*args, **kwargs):
    raise AssertionError("Offline runner blocked real network or database access")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pipeline-only", action="store_true",
                        help="Run only Deliverable 2 CSV/file tests")
    args = parser.parse_args()
    suites = [str(ROOT / "tests/offline/test_pipeline_files.py")] if args.pipeline_only else [
        str(ROOT / "tests/test_client.py"),
        str(ROOT / "tests/test_idempotency.py"),
        str(ROOT / "tests/offline"),
    ]
    with patch.object(socket.socket, "connect", blocked), \
         patch.object(socket.socket, "connect_ex", blocked), \
         patch.object(socket, "create_connection", blocked), \
         patch.object(pymysql, "connect", blocked), \
         patch.object(requests.Session, "request", blocked):
        sys.exit(pytest.main([
            "-q", "-p", "no:cacheprovider",
            *suites,
        ]))
