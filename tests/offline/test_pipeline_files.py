"""Deliverable 2: real temporary files and synthetic CSV; no live connections."""

from datetime import date
from pathlib import Path
import csv
import io
import os
import sys
import time

import pytest

from lwsync.pipeline.csv_io import BOM, HEADER, CsvFormatError, parse_csv
from lwsync.pipeline.file_settings import FileSettings
from lwsync.pipeline import files


def export(rows=None, *, newline="\r\n", quoting=csv.QUOTE_MINIMAL, bom=False):
    if rows is None:
        rows = [line()]
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator=newline, quoting=quoting)
    writer.writerow(HEADER)
    writer.writerows(rows)
    return (BOM if bom else b"") + output.getvalue().encode("utf-8")


def line(order="123", sku="TEST-A", quantity="2"):
    row = [""] * len(HEADER)
    row[0], row[27], row[29] = order, sku, quantity
    row[2] = 'Fictional Customer, "Example"'
    row[28] = "Synthetic battery"
    row[35] = "Test note\r\nsecond line"
    row[38], row[43], row[44] = "FIRST", "FILL", "SECOND"
    return row


@pytest.mark.parametrize("newline", ["\r\n", "\n", "\r"])
@pytest.mark.parametrize("quoting", [csv.QUOTE_MINIMAL, csv.QUOTE_ALL])
@pytest.mark.parametrize("bom", [False, True])
def test_csv_roundtrip_exact(newline, quoting, bom):
    payload = export(newline=newline, quoting=quoting, bom=bom)
    document = parse_csv(payload)
    assert document.render() == payload
    assert document.rows[0].values[35] == "Test note\r\nsecond line"
    assert document.rows[0].values[38] == "FIRST"
    assert document.rows[0].values[44] == "SECOND"


def test_minimal_positional_replacement():
    document = parse_csv(export(quoting=csv.QUOTE_ALL, bom=True))
    row = document.rows[0]
    changed = row.replace({27: 'TEST-"B"', 38: "NEW-FIRST", 44: "NEW-SECOND"})
    result = parse_csv(document.prefix + document.header.raw + changed)
    assert result.rows[0].values[27] == 'TEST-"B"'
    for index in range(len(HEADER)):
        if index not in (27, 38, 44):
            old_start, old_end = row.spans[index]
            new_start, new_end = result.rows[0].spans[index]
            assert row.raw[old_start:old_end] == changed[new_start:new_end]


def test_unquoted_replacement_and_noop():
    row = parse_csv(export()).rows[0]
    assert row.replace({27: "TEST-B"}) == row.raw.replace(b"TEST-A", b"TEST-B")
    assert row.replace({27: "TEST-A"}) == row.raw
    with pytest.raises(CsvFormatError, match="quoting style"):
        row.replace({27: "TEST,B"})


def test_subset_order_grouping_and_unterminated_final_record():
    payload = export([line("001"), line("2"), line("1")]).rstrip(b"\r\n")
    document = parse_csv(payload)
    assert list(document.orders()) == ["1", "2"]
    assert len(document.orders()["1"]) == 2
    assert document.render() == payload
    assert document.render([document.rows[1]]) == document.header.raw + document.rows[1].raw


@pytest.mark.parametrize("payload", [
    b"", b"bad header\n", export() + b'"unclosed',
    export() + b'"closed"x\n', export() + b'bad"quote\n',
    export() + b"short,row\n", export().replace(b"TEST-A", b"\xff"),
    export().replace(b"TEST-A", b"\x00"),
])
def test_malformed_file_rejected_without_customer_data_in_error(payload):
    with pytest.raises(CsvFormatError) as caught:
        parse_csv(payload)
    assert "Fictional Customer" not in str(caught.value)


@pytest.mark.parametrize("quantity", ["0", "-1", "1.5", "", "2e1", "2147483648", "9" * 5000])
def test_invalid_quantities(quantity):
    with pytest.raises(CsvFormatError, match="quantity"):
        parse_csv(export([line(quantity=quantity)]))


def test_invalid_order_id_and_blank_sku():
    with pytest.raises(CsvFormatError, match="order ID"):
        parse_csv(export([line(order="not-a-guid")]))
    with pytest.raises(CsvFormatError, match="empty SKU"):
        parse_csv(export([line(sku=" ")]))


def old_file(path, data=b"synthetic", *, age=120):
    path.write_bytes(data)
    timestamp = time.time() - age
    os.utime(path, (timestamp, timestamp))
    return path


def test_pickup_skips_recent_wrong_region_and_nonfiles(tmp_path):
    ready = old_file(tmp_path / "us_ajc_orders1.csv")
    cs = old_file(tmp_path / "us_cs_orders1.csv")
    old_file(tmp_path / "us_ajc_orders2.csv", age=0)
    old_file(tmp_path / "ca_ajc_orders1.csv")
    old_file(tmp_path / "other.csv")
    (tmp_path / "us_ajc_orders3.csv").mkdir()
    assert files.pickup_files(tmp_path, "US") == (ready, cs)
    with pytest.raises(ValueError):
        files.pickup_files(tmp_path, "EU")


def test_stable_read_and_later_modification(tmp_path):
    path = old_file(tmp_path / "us_ajc_orders.csv", export())
    snapshot = files.read_stable(path)
    assert snapshot.data == export()
    path.write_bytes(b"changed")
    with pytest.raises(files.UnstableFileError):
        files.assert_unchanged(snapshot)
    with pytest.raises(files.UnstableFileError):
        files.read_stable(path)


def test_read_detects_change_during_read(tmp_path, monkeypatch):
    path = old_file(tmp_path / "us_ajc_orders.csv")
    original = files.os.fstat
    calls = 0

    def fstat(descriptor):
        nonlocal calls
        calls += 1
        if calls == 2:
            path.write_bytes(b"a changed upload")
        return original(descriptor)

    monkeypatch.setattr(files.os, "fstat", fstat)
    with pytest.raises(files.UnstableFileError):
        files.read_stable(path)


@pytest.fixture
def folders(tmp_path):
    staging = tmp_path / ".pipeline_tmp"
    output = tmp_path / "orders"
    staging.mkdir()
    output.mkdir()
    return staging, output


def test_atomic_publish_never_exposes_partial_file(folders, monkeypatch):
    staging, output = folders
    destination = output / "us_ajc_orders1.csv"
    payload = export()
    original = files._rename_exclusive

    def inspect(source, target):
        assert source.parent == staging
        assert source.read_bytes() == payload
        assert list(output.iterdir()) == []
        original(source, target)

    monkeypatch.setattr(files, "_rename_exclusive", inspect)
    assert files.atomic_publish(payload, destination, staging) == destination
    assert destination.read_bytes() == payload
    assert list(staging.iterdir()) == []
    if os.name == "posix":
        assert destination.stat().st_mode & 0o777 == 0o660


def test_destination_collision_preserves_existing_and_cleans_temp(folders):
    staging, output = folders
    destination = output / "orders.csv"
    destination.write_bytes(b"existing")
    with pytest.raises(FileExistsError):
        files.atomic_publish(export(), destination, staging)
    assert destination.read_bytes() == b"existing"
    assert list(staging.iterdir()) == []


def test_all_held_subset_creates_no_output(folders):
    staging, output = folders
    document = parse_csv(export())
    assert files.publish_csv(document, output / "orders.csv", staging, rows=()) is None
    assert list(staging.iterdir()) == list(output.iterdir()) == []


def test_selected_rows_keep_header_order_and_original_name(folders):
    staging, output = folders
    document = parse_csv(export([line("1"), line("2"), line("1")], bom=True))
    rows = (document.rows[0], document.rows[2])
    target = output / "us_ajc_orders2026-11-16.csv"
    assert files.publish_csv(document, target, staging, rows) == target
    assert target.read_bytes() == document.render(rows)
    assert len(parse_csv(target.read_bytes()).rows) == 2


def test_failed_rename_leaves_no_partial_output(folders, monkeypatch):
    staging, output = folders

    def fail(*args):
        raise OSError("simulated storage failure")

    monkeypatch.setattr(files, "_rename_exclusive", fail)
    with pytest.raises(OSError):
        files.atomic_publish(export(), output / "orders.csv", staging)
    assert list(output.iterdir()) == list(staging.iterdir()) == []


def test_temp_directory_must_be_outside_output(folders):
    _, output = folders
    with pytest.raises(ValueError):
        files.atomic_publish(b"data", output / "orders.csv", output)


def test_cross_filesystem_staging_refused(folders, monkeypatch):
    staging, output = folders
    original = Path.stat

    def different_device(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        if path == staging:
            # Preserve all other fields used by Path.resolve/is_dir.
            fields = list(info)
            fields[2] += 1
            return os.stat_result(fields)
        return info

    monkeypatch.setattr(Path, "stat", different_device)
    with pytest.raises(OSError, match="share a filesystem"):
        files.atomic_publish(b"data", output / "orders.csv", staging)
    assert list(output.iterdir()) == list(staging.iterdir()) == []


@pytest.mark.parametrize("interval", [0, 59, float("nan"), float("inf")])
def test_unsafe_stability_intervals_rejected(tmp_path, interval):
    with pytest.raises(ValueError):
        files.pickup_files(tmp_path, "US", stable_seconds=interval)
    with pytest.raises(ValueError):
        files.read_stable(tmp_path / "input.csv", stable_seconds=interval)


def test_archive_error_and_collision_preserve_original(tmp_path):
    path = old_file(tmp_path / "us_ajc_orders.csv", export())
    snapshot = files.read_stable(path)
    archive = tmp_path / "archive"
    result = files.move_input(snapshot, archive, archive_date=date(2026, 11, 16))
    assert result == archive / "2026-11-16" / path.name
    assert result.read_bytes() == snapshot.data
    old_file(path, b"next file")
    with pytest.raises(FileExistsError):
        files.move_input(files.read_stable(path), archive, archive_date=date(2026, 11, 16))
    assert path.read_bytes() == b"next file"
    error = files.move_input(files.read_stable(path), tmp_path / "error")
    assert error.read_bytes() == b"next file"


def test_cs_passthrough_is_byte_identical(tmp_path, folders, caplog):
    staging, output = folders
    path = old_file(tmp_path / "us_cs_orders1.csv", b"arbitrary CS layout\r\n")
    snapshot = files.read_stable(path)
    target = files.passthrough_cs(snapshot, "US", output, staging)
    assert target.name == path.name
    assert target.read_bytes() == snapshot.data
    assert path.exists()  # Archive only after durable tracking in the future runner.
    assert "CS export found" in caplog.text


def test_settings_require_confirmed_paths_and_do_not_create_directories(tmp_path):
    with pytest.raises(ValueError, match="HELD_DIR"):
        FileSettings.from_env("US", {})
    env = {
        "LW_PIPELINE_US_HELD_DIR": str(tmp_path / "held"),
        "LW_PIPELINE_US_ERROR_DIR": str(tmp_path / "error"),
    }
    settings = FileSettings.from_env("US", env)
    assert settings.input_dir == Path("/data/us/new_orders")
    assert list(tmp_path.iterdir()) == []
    env["LW_PIPELINE_US_TEMP_DIR"] = "/data/us/orders/tmp"
    with pytest.raises(ValueError, match="non-overlapping"):
        FileSettings.from_env("US", env)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux flock integration")
def test_flock_excludes_second_run_and_releases(tmp_path):
    lock = tmp_path / "pipeline.lock"
    with files.run_lock(lock):
        with pytest.raises(files.RunLockedError):
            with files.run_lock(lock):
                pytest.fail("Second lock must not enter")
    with files.run_lock(lock):
        assert lock.exists()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux symlink support")
def test_symlinks_are_not_inputs(tmp_path):
    target = old_file(tmp_path / "actual.csv")
    link = tmp_path / "us_ajc_orders1.csv"
    link.symlink_to(target)
    assert files.pickup_files(tmp_path, "US") == ()
    with pytest.raises(files.UnstableFileError):
        files.read_stable(link)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux permissions")
def test_publish_uses_destination_group(folders):
    staging, output = folders
    destination = files.atomic_publish(export(), output / "orders.csv", staging)
    assert destination.stat().st_gid == output.stat().st_gid
    assert destination.stat().st_mode & 0o777 == 0o660
