"""Tests for library scanner and file discovery."""

from pathlib import Path
from pyflows.scanner import scan_library
from pyflows.config import LibraryConfig
from pyflows.db import FileDB


def test_scan_finds_matching_extensions(tmp_path):
    """Scanner finds files matching configured extensions."""
    media = tmp_path / "media"
    media.mkdir()
    (media / "movie.mkv").write_bytes(b"x" * 100000)
    (media / "show.mp4").write_bytes(b"y" * 100000)
    (media / "readme.txt").write_bytes(b"z" * 100)
    (media / "sub").mkdir()
    (media / "sub" / "nested.mkv").write_bytes(b"n" * 100000)

    lib = LibraryConfig(name="Test", path=str(media), profile="test",
                        scan_interval=3600, extensions=["mkv", "mp4"])
    db = FileDB(str(tmp_path / "test.db"))

    found = scan_library(lib, db)
    assert len(found) == 3  # movie.mkv, show.mp4, nested.mkv
    # Each entry is (path, profile, codec)
    for path, profile, codec in found:
        assert profile == "test"
        assert isinstance(codec, str)  # may be empty if ffprobe unavailable
    db.close()


def test_scan_skips_already_processed(tmp_path):
    """Scanner skips files already completed with same hash."""
    media = tmp_path / "media"
    media.mkdir()
    (media / "done.mkv").write_bytes(b"x" * 100000)

    lib = LibraryConfig(name="Test", path=str(media), profile="test",
                        scan_interval=3600, extensions=["mkv"])
    db = FileDB(str(tmp_path / "test.db"))

    # First scan finds it
    assert len(scan_library(lib, db)) == 1
    # Mark as completed
    db.update_status(str(media / "done.mkv"), "completed")
    # Second scan skips it
    assert len(scan_library(lib, db)) == 0
    db.close()
