"""N3: _rename_exclusive works on Linux (production) and macOS (developers) and never overwrites."""

import sys
from types import SimpleNamespace

import pytest

from lwsync.pipeline import files

supported = pytest.mark.skipif(
    not (sys.platform.startswith("linux") or sys.platform == "darwin"),
    reason="exclusive rename implemented for Linux and macOS",
)


@supported
def test_rename_moves_file(tmp_path):
    src, dst = tmp_path / "a.tmp", tmp_path / "a.csv"
    src.write_bytes(b"data")
    files._rename_exclusive(src, dst)
    assert dst.read_bytes() == b"data"
    assert not src.exists()


@supported
def test_rename_never_overwrites_existing_file(tmp_path):
    src, dst = tmp_path / "new.tmp", tmp_path / "out.csv"
    src.write_bytes(b"new")
    dst.write_bytes(b"already collected by 3PL")
    with pytest.raises(FileExistsError):
        files._rename_exclusive(src, dst)
    assert dst.read_bytes() == b"already collected by 3PL"
    assert src.read_bytes() == b"new"  # nothing lost; caller decides what to do


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS fallback path")
@pytest.mark.parametrize("collide", [False, True])
def test_macos_fallback_without_renamex(tmp_path, monkeypatch, collide):
    """Filesystems/OS builds without renamex_np use link+unlink, which also refuses to overwrite."""
    monkeypatch.setattr(files.ctypes, "CDLL", lambda *a, **k: SimpleNamespace())
    src, dst = tmp_path / "f.tmp", tmp_path / "f.csv"
    src.write_bytes(b"new")
    if collide:
        dst.write_bytes(b"old")
        with pytest.raises(FileExistsError):
            files._rename_exclusive(src, dst)
        assert dst.read_bytes() == b"old" and src.exists()
    else:
        files._rename_exclusive(src, dst)
        assert dst.read_bytes() == b"new" and not src.exists()
