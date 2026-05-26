# nix/packages/pyflows/tests/test_subtitles.py
"""Tests for subtitle filtering logic."""

from pyflows.subtitles import filter_subtitles
from pyflows.probe import StreamInfo
from pyflows.config import SubtitleConfig


def _make_sub(index, codec, language, title=""):
    return StreamInfo(index=index, codec_type="subtitle", codec=codec,
                      language=language, title=title)


def _make_config(**kwargs):
    defaults = dict(
        keep_languages=["eng", "spa", "jpn"],
        remove_formats=["pgs", "dvd_subtitle", "hdmv_pgs_subtitle"],
        remove_commentary=True,
    )
    defaults.update(kwargs)
    return SubtitleConfig(**defaults)


def test_keep_matching_languages():
    """Only subtitles matching keep_languages are kept."""
    subs = [
        _make_sub(3, "subrip", "eng"),
        _make_sub(4, "subrip", "fre"),
        _make_sub(5, "subrip", "spa"),
    ]
    result = filter_subtitles(subs, _make_config())
    langs = [s.language for s in result]
    assert "eng" in langs
    assert "spa" in langs
    assert "fre" not in langs


def test_remove_pgs_format():
    """PGS bitmap subtitles are removed."""
    subs = [
        _make_sub(3, "subrip", "eng"),
        _make_sub(4, "hdmv_pgs_subtitle", "eng"),
    ]
    result = filter_subtitles(subs, _make_config())
    assert len(result) == 1
    assert result[0].codec == "subrip"


def test_remove_dvd_subtitle():
    """VobSub subtitles are removed."""
    subs = [
        _make_sub(3, "subrip", "eng"),
        _make_sub(4, "dvd_subtitle", "eng"),
    ]
    result = filter_subtitles(subs, _make_config())
    assert len(result) == 1


def test_remove_commentary_subtitles():
    """Commentary subtitles are removed."""
    subs = [
        _make_sub(3, "subrip", "eng", title="English"),
        _make_sub(4, "subrip", "eng", title="English Commentary"),
    ]
    result = filter_subtitles(subs, _make_config())
    assert len(result) == 1
    assert "Commentary" not in result[0].title
