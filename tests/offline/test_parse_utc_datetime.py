"""Dates from Linnworks are UTC; the stored value must not depend on the computer's time zone."""

import os
import time

import pytest

from lwsync.sync.refunds import parse_utc_datetime


@pytest.fixture(params=["UTC", "Asia/Kolkata", "Australia/Sydney", "America/New_York"])
def local_tz(request):
    """Run the test as if the computer were set to each of these time zones."""
    if not hasattr(time, "tzset"):
        pytest.skip("time.tzset not available on this platform")
    old = os.environ.get("TZ")
    os.environ["TZ"] = request.param
    time.tzset()
    yield request.param
    if old is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = old
    time.tzset()


def test_date_without_zone_is_utc(local_tz):
    # Linnworks v2 sends dates like this, with no zone (e.g. GeneralInfo.ReceivedDate).
    assert parse_utc_datetime("2026-11-16T21:36:26") == "2026-11-16 21:36:26"


def test_fractional_seconds_without_zone(local_tz):
    # LastUpdated comes with fractional seconds.
    assert parse_utc_datetime("2026-11-18T09:32:52.333") == "2026-11-18 09:32:52"


def test_z_suffix_is_utc(local_tz):
    assert parse_utc_datetime("2026-11-16T21:36:26Z") == "2026-11-16 21:36:26"


def test_explicit_offset_is_converted(local_tz):
    assert parse_utc_datetime("2026-11-17T08:36:26+11:00") == "2026-11-16 21:36:26"


@pytest.mark.parametrize("value", [None, ""])
def test_empty_is_none(value):
    assert parse_utc_datetime(value) is None
