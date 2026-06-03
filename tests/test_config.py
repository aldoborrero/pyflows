"""Tests for configuration loading and validation."""

import pytest  # type: ignore[import-not-found]
from pathlib import Path

from pyflows.config import load_config, PyflowsConfig


def test_load_minimal_config(tmp_config):
    """Loading a valid config file returns a PyflowsConfig."""
    config = load_config(tmp_config)
    assert isinstance(config, PyflowsConfig)
    assert config.general.workers == 1
    assert config.general.log_format == "text"
    assert config.general.max_retries == 3
    assert config.general.retry_backoff_seconds == 300
    assert config.general.watcher_event_debounce_seconds == 0
    assert config.general.stable_for_seconds == 0
    assert ".part" in config.general.ignore_suffixes
    assert "test" in config.profiles
    assert len(config.libraries) == 1


def test_profile_defaults(tmp_config):
    """Profile fields have correct values from YAML."""
    config = load_config(tmp_config)
    profile = config.profiles["test"]
    assert profile.video.codec == "hevc"
    assert profile.video.quality == 22
    assert profile.video.skip_codecs == ["hevc"]
    assert profile.audio.keep_languages == ["eng", "spa", "jpn"]
    assert profile.audio.add_stereo.bitrate == 128
    assert profile.audio.preserve_surround is True
    assert profile.subtitles.default_language == "eng"
    assert profile.subtitles.remove_formats == ["pgs", "dvd_subtitle"]


def test_library_references_valid_profile(tmp_config):
    """Library config references an existing profile name."""
    config = load_config(tmp_config)
    lib = config.libraries[0]
    assert lib.profile in config.profiles


def test_hardware_and_queue_defaults(tmp_config):
    """New hardware/queue sections default correctly when omitted from YAML."""
    config = load_config(tmp_config)
    assert config.hardware.env == {"AMD_DEBUG": "noefc"}
    assert config.hardware.vaapi.device == "va"
    assert config.hardware.vaapi.hw_decode_codecs == ["hevc", "av1", "vp9"]
    assert config.hardware.vaapi.sw_decode_codecs == ["h264"]
    assert config.hardware.vaapi.upload_filter == "format=nv12,hwupload_vaapi"
    assert config.hardware.vaapi.async_depth == 4
    assert config.queue.priority_codecs == []


def test_hardware_and_queue_overrides(tmp_path):
    """Explicit hardware/queue YAML overrides the new defaults."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text("""
general:
  temp_dir: /tmp
  db_path: /tmp/test.db
profiles:
  test:
    video: { codec: hevc, bit_depth: 10, encoder: vaapi, quality: 22, fallback: cpu, skip_codecs: [hevc] }
    audio: { keep_languages: [eng], default_language: eng, priority: [eng], remove_commentary: false, add_stereo: { codec: aac, bitrate: 128, channels: 2, languages: [eng] }, preserve_surround: true }
    subtitles: { keep_languages: [eng], default_language: eng, remove_formats: [], remove_commentary: false }
    output: { container: mkv, replace_original: true }
libraries: []
hardware:
  env:
    AMD_DEBUG: noefc
    LIBVA_DRIVER_NAME: radeonsi
  vaapi:
    device: render
    hw_decode_codecs: [hevc, vp9]
    sw_decode_codecs: [h264, mpeg2video]
    upload_filter: format=nv12,hwupload_vaapi,scale_vaapi
    async_depth: 6
queue:
  priority_codecs: [vp9, hevc]
""")
    config = load_config(config_file)
    assert config.hardware.env["LIBVA_DRIVER_NAME"] == "radeonsi"
    assert config.hardware.vaapi.device == "render"
    assert config.hardware.vaapi.hw_decode_codecs == ["hevc", "vp9"]
    assert config.hardware.vaapi.sw_decode_codecs == ["h264", "mpeg2video"]
    assert config.hardware.vaapi.upload_filter == "format=nv12,hwupload_vaapi,scale_vaapi"
    assert config.hardware.vaapi.async_depth == 6
    assert config.queue.priority_codecs == ["vp9", "hevc"]


def test_env_var_expansion(tmp_path, monkeypatch):
    """Environment variables in ${VAR} syntax are expanded."""
    monkeypatch.setenv("TEST_API_KEY", "secret123")
    config_file = tmp_path / "config.yaml"
    config_file.write_text("""
general:
  temp_dir: /tmp
  log_level: info
  log_output: stdout
  log_format: json
  workers: 1
  max_retries: 5
  retry_backoff_seconds: 60
  db_path: /tmp/test.db
  vaapi_device: /dev/dri/renderD128
  settle_time: 0
  watcher_event_debounce_seconds: 5
  stable_for_seconds: 30
  ignore_suffixes: [.part, .tmp]
profiles:
  test:
    video: { codec: hevc, bit_depth: 10, encoder: vaapi, quality: 22, fallback: cpu, skip_codecs: [hevc] }
    audio: { keep_languages: [eng], default_language: eng, priority: [eng], remove_commentary: false, add_stereo: { codec: aac, bitrate: 128, channels: 2, languages: [eng] }, preserve_surround: true }
    subtitles: { keep_languages: [eng], default_language: eng, remove_formats: [], remove_commentary: false }
    output: { container: mkv, replace_original: true }
libraries: []
notifications:
  ntfy:
    url: https://ntfy.example.com/test
    on_failure: true
    on_success: false
  jellyfin:
    url: http://localhost:8096
    api_key: ${TEST_API_KEY}
    on_success: true
    refresh_path: true
""")
    config = load_config(config_file)
    assert config.notifications.jellyfin.api_key == "secret123"


def test_invalid_config_missing_profiles(tmp_path):
    """Missing required fields raise ValidationError."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text("general:\n  temp_dir: /tmp\n")
    with pytest.raises(Exception):
        load_config(config_file)


def test_invalid_library_profile_reference(tmp_path):
    """Library referencing a non-existent profile raises ValidationError."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text("""
general:
  temp_dir: /tmp
  db_path: /tmp/test.db
profiles:
  movie:
    video: { codec: hevc, bit_depth: 10, encoder: vaapi, quality: 22, fallback: cpu, skip_codecs: [hevc] }
    audio: { keep_languages: [eng], default_language: eng, priority: [eng], remove_commentary: false, add_stereo: { codec: aac, bitrate: 128, channels: 2, languages: [eng] }, preserve_surround: true }
    subtitles: { keep_languages: [eng], default_language: eng, remove_formats: [], remove_commentary: false }
    output: { container: mkv, replace_original: true }
libraries:
  - name: Test
    path: /media
    profile: nonexistent
""")
    with pytest.raises(Exception, match="nonexistent"):
        load_config(config_file)
