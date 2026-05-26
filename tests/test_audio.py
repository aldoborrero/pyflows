# nix/packages/pyflows/tests/test_audio.py
"""Tests for audio track selection and stereo creation."""

from pyflows.audio import build_audio_plan, AudioAction
from pyflows.probe import StreamInfo
from pyflows.config import AudioConfig, StereoConfig


def _make_audio(index, codec, channels, language, title=""):
    return StreamInfo(index=index, codec_type="audio", codec=codec,
                      channels=channels, language=language, title=title)


def _make_config(**kwargs):
    defaults = dict(
        keep_languages=["eng", "spa", "jpn"],
        default_language="eng",
        priority=["eng", "spa", "jpn"],
        remove_commentary=True,
        add_stereo=StereoConfig(codec="aac", bitrate=128, channels=2, languages=["eng", "spa", "jpn"]),
        preserve_surround=True,
    )
    defaults.update(kwargs)
    return AudioConfig(**defaults)


def test_keep_matching_languages():
    """Only streams matching keep_languages are kept."""
    streams = [
        _make_audio(1, "eac3", 6, "eng"),
        _make_audio(2, "aac", 2, "fre"),
        _make_audio(3, "aac", 2, "spa"),
    ]
    plan = build_audio_plan(streams, _make_config())
    languages = [a.stream.language for a in plan if a.action == "copy"]
    assert "fre" not in languages
    assert "eng" in languages
    assert "spa" in languages


def test_remove_commentary():
    """Commentary tracks are removed."""
    streams = [
        _make_audio(1, "eac3", 6, "eng"),
        _make_audio(2, "eac3", 6, "eng", title="Director Commentary"),
    ]
    plan = build_audio_plan(streams, _make_config())
    assert len([a for a in plan if a.action == "copy"]) == 1


def test_add_stereo_for_surround():
    """AAC stereo track added for each surround source."""
    streams = [_make_audio(1, "eac3", 6, "eng")]
    plan = build_audio_plan(streams, _make_config())
    stereo = [a for a in plan if a.action == "encode"]
    assert len(stereo) == 1
    assert stereo[0].codec == "aac"
    assert stereo[0].channels == 2


def test_no_stereo_for_already_stereo():
    """No AAC stereo added if source is already stereo."""
    streams = [_make_audio(1, "aac", 2, "eng")]
    plan = build_audio_plan(streams, _make_config())
    stereo = [a for a in plan if a.action == "encode"]
    assert len(stereo) == 0


def test_preserve_surround():
    """Original surround track is kept alongside stereo copy."""
    streams = [_make_audio(1, "eac3", 6, "eng")]
    plan = build_audio_plan(streams, _make_config())
    copies = [a for a in plan if a.action == "copy"]
    encodes = [a for a in plan if a.action == "encode"]
    assert len(copies) == 1  # original kept
    assert len(encodes) == 1  # stereo added


def test_disable_preserve_surround_keeps_only_stereo_derivative():
    """When preserve_surround is false, the original surround track is not copied."""
    streams = [_make_audio(1, "eac3", 6, "eng")]
    plan = build_audio_plan(streams, _make_config(preserve_surround=False))
    copies = [a for a in plan if a.action == "copy"]
    encodes = [a for a in plan if a.action == "encode"]
    assert len(copies) == 0
    assert len(encodes) == 1


def test_priority_ordering():
    """Tracks are ordered by priority (e.g., jpn first for anime)."""
    streams = [
        _make_audio(1, "eac3", 6, "eng"),
        _make_audio(2, "aac", 6, "jpn"),
    ]
    config = _make_config(priority=["jpn", "eng", "spa"])
    plan = build_audio_plan(streams, config)
    copy_langs = [a.stream.language for a in plan if a.action == "copy"]
    assert copy_langs[0] == "jpn"
    assert copy_langs[1] == "eng"


def test_default_language_prefers_matching_track_for_first_default_candidate():
    """Plan ordering keeps the configured default language first when priority matches it."""
    streams = [
        _make_audio(1, "eac3", 6, "eng"),
        _make_audio(2, "aac", 6, "jpn"),
    ]
    config = _make_config(default_language="jpn", priority=["jpn", "eng", "spa"])
    plan = build_audio_plan(streams, config)
    first = plan[0]
    assert first.stream.language == "jpn"


def test_stereo_after_original_per_language():
    """Stereo copy comes right after its original, grouped by language."""
    streams = [
        _make_audio(1, "eac3", 6, "eng"),
        _make_audio(2, "dts", 6, "jpn"),
    ]
    config = _make_config(priority=["eng", "jpn"])
    plan = build_audio_plan(streams, config)
    actions = [(a.stream.language, a.action) for a in plan]
    assert actions == [("eng", "copy"), ("eng", "encode"), ("jpn", "copy"), ("jpn", "encode")]
