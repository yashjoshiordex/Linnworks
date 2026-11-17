"""Test-wide safety: tests that touch MySQL always use a separate test database.

.env usually points at a real database (local copy of live data, or RDS). Without this,
DB-backed tests would write fixture orders and products into it. Runs before any test
module imports lwsync.config, and load_dotenv() never overrides variables already set.
Override the name with LWSYNC_TEST_DB_NAME if needed.
"""

import os

_test_db = os.environ.get("LWSYNC_TEST_DB_NAME", "lwsync_test")
if os.environ.get("DB_NAME") and os.environ["DB_NAME"] != _test_db:
    print(f"[tests] Ignoring DB_NAME={os.environ['DB_NAME']}; tests use {_test_db}")
os.environ["DB_NAME"] = _test_db
