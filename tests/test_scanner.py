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


def test_scan_defers_recently_modified_files(tmp_path):
    """Scanner defers files modified within the stable_for_seconds window."""
    media = tmp_path / "media"
    media.mkdir()
    (media / "new.mkv").write_bytes(b"x" * 100000)

    lib = LibraryConfig(name="Test", path=str(media), profile="test",
                        scan_interval=3600, extensions=["mkv"])
    db = FileDB(str(tmp_path / "test.db"))

    found = scan_library(lib, db, stable_for_seconds=3600)
    assert len(found) == 0

    record = db.get(str(media / "new.mkv"))
    assert record is not None
    assert record["hold_until"] is not None
    assert record["status"] == "pending"
    db.close()


def test_scan_processes_old_files_with_stable_for(tmp_path):
    """Scanner processes files older than the stable_for_seconds window."""
    import os
    import time

    media = tmp_path / "media"
    media.mkdir()
    old_file = media / "old.mkv"
    old_file.write_bytes(b"x" * 100000)
    # Backdate mtime to 2 hours ago
    old_mtime = time.time() - 7200
    os.utime(str(old_file), (old_mtime, old_mtime))

    lib = LibraryConfig(name="Test", path=str(media), profile="test",
                        scan_interval=3600, extensions=["mkv"])
    db = FileDB(str(tmp_path / "test.db"))

    found = scan_library(lib, db, stable_for_seconds=3600)
    assert len(found) == 1
    assert found[0][0] == str(old_file)

    record = db.get(str(old_file))
    assert record is not None
    assert record["hold_until"] is None
    db.close()


def test_scan_mixed_new_and_old_files(tmp_path):
    """Scanner defers recent files and processes old ones in the same scan."""
    import os
    import time

    media = tmp_path / "media"
    media.mkdir()

    # Recent file — should be deferred
    new_file = media / "new.mkv"
    new_file.write_bytes(b"x" * 100000)

    # Old file — should be processed
    old_file = media / "old.mkv"
    old_file.write_bytes(b"y" * 100000)
    old_mtime = time.time() - 7200
    os.utime(str(old_file), (old_mtime, old_mtime))

    lib = LibraryConfig(name="Test", path=str(media), profile="test",
                        scan_interval=3600, extensions=["mkv"])
    db = FileDB(str(tmp_path / "test.db"))

    found = scan_library(lib, db, stable_for_seconds=3600)

    # Only the old file should be queued
    assert len(found) == 1
    assert found[0][0] == str(old_file)

    # Old file: processed normally, no hold_until
    old_record = db.get(str(old_file))
    assert old_record is not None
    assert old_record["hold_until"] is None

    # New file: deferred with hold_until set
    new_record = db.get(str(new_file))
    assert new_record is not None
    assert new_record["hold_until"] is not None
    assert new_record["status"] == "pending"
    db.close()
