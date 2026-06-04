"""Tests for webhook path mapping and library resolution logic."""

import pytest  # type: ignore[import-not-found]

from pyflows.config import (
    AudioConfig,
    GeneralConfig,
    LibraryConfig,
    OutputConfig,
    ProfileConfig,
    PyflowsConfig,
    SubtitleConfig,
    VideoConfig,
)
from pyflows.webhook import _map_path, _resolve_library


def _make_config(tmp_path, libraries: list[LibraryConfig]) -> PyflowsConfig:
    """Build a minimal PyflowsConfig for testing."""
    return PyflowsConfig(
        general=GeneralConfig(
            temp_dir=str(tmp_path / "tmp"),
            db_path=str(tmp_path / "test.db"),
        ),
        profiles={
            "tv": ProfileConfig(
                video=VideoConfig(codec="hevc"),
                audio=AudioConfig(),
                subtitles=SubtitleConfig(),
                output=OutputConfig(),
            ),
            "movies": ProfileConfig(
                video=VideoConfig(codec="hevc"),
                audio=AudioConfig(),
                subtitles=SubtitleConfig(),
                output=OutputConfig(),
            ),
        },
        libraries=libraries,
    )


class TestMapPath:
    def test_translates_prefix(self, tmp_path):
        """_map_path correctly translates a matching prefix to the local path."""
        local_dir = tmp_path / "media" / "tv"
        local_dir.mkdir(parents=True)
        (local_dir / "show").mkdir()
        (local_dir / "show" / "ep.mkv").write_bytes(b"x")

        result = _map_path(
            "/sonarr/tv/show/ep.mkv",
            {"/sonarr/tv": str(local_dir)},
        )
        assert result == str(local_dir / "show" / "ep.mkv")

    def test_rejects_traversal(self, tmp_path):
        """_map_path rejects path traversal attempts and returns the original."""
        local_dir = tmp_path / "media" / "tv"
        local_dir.mkdir(parents=True)

        original = "/sonarr/tv/../../etc/passwd"
        result = _map_path(
            original,
            {"/sonarr/tv": str(local_dir)},
        )
        assert result == original

    def test_no_matching_prefix(self):
        """_map_path returns the original path when no prefix matches."""
        original = "/unrelated/path/file.mkv"
        result = _map_path(
            original,
            {"/sonarr/tv": "/media/tv"},
        )
        assert result == original


class TestResolveLibrary:
    def test_matches_correct_profile(self, tmp_path):
        """_resolve_library returns the profile for a file under a library path."""
        tv_dir = tmp_path / "media" / "tv"
        movies_dir = tmp_path / "media" / "movies"
        tv_dir.mkdir(parents=True)
        movies_dir.mkdir(parents=True)

        config = _make_config(tmp_path, [
            LibraryConfig(name="TV", path=str(tv_dir), profile="tv"),
            LibraryConfig(name="Movies", path=str(movies_dir), profile="movies"),
        ])

        result = _resolve_library(str(tv_dir / "show" / "ep.mkv"), config)
        assert result == "tv"

        result = _resolve_library(str(movies_dir / "film.mkv"), config)
        assert result == "movies"

    def test_no_match(self, tmp_path):
        """_resolve_library returns None for a file outside all libraries."""
        tv_dir = tmp_path / "media" / "tv"
        tv_dir.mkdir(parents=True)

        config = _make_config(tmp_path, [
            LibraryConfig(name="TV", path=str(tv_dir), profile="tv"),
        ])

        result = _resolve_library("/some/other/path/file.mkv", config)
        assert result is None

    def test_no_prefix_confusion(self, tmp_path):
        """_resolve_library does not match /media/movies_4k against /media/movies."""
        movies_dir = tmp_path / "media" / "movies"
        movies_4k_dir = tmp_path / "media" / "movies_4k"
        movies_dir.mkdir(parents=True)
        movies_4k_dir.mkdir(parents=True)

        config = _make_config(tmp_path, [
            LibraryConfig(name="Movies", path=str(movies_dir), profile="movies"),
        ])

        result = _resolve_library(str(movies_4k_dir / "film.mkv"), config)
        assert result is None
