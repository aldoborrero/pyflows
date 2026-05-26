"""Shared test fixtures for pyflows."""

import pytest  # type: ignore[import-not-found]
from pathlib import Path


@pytest.fixture
def tmp_config(tmp_path):
    """Create a minimal config file for testing."""
    config = tmp_path / "config.yaml"
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    config.write_text(
        f"""
general:
  temp_dir: {tmp_path}
  log_level: info
  log_output: stdout
  log_format: text
  workers: 1
  db_path: {tmp_path}/pyflows.db
  vaapi_device: /dev/dri/renderD128
  settle_time: 0
  watcher_event_debounce_seconds: 0
  stable_for_seconds: 0
  ignore_suffixes: [.part, .tmp, .partial, .!qB]
  max_retries: 3
  retry_backoff_seconds: 300

profiles:
  test:
    video:
      codec: hevc
      bit_depth: 10
      encoder: vaapi
      quality: 22
      fallback: cpu
      skip_codecs: [hevc]
    audio:
      keep_languages: [eng, spa, jpn]
      default_language: eng
      priority: [eng, spa, jpn]
      remove_commentary: true
      add_stereo:
        codec: aac
        bitrate: 128
        channels: 2
        languages: [eng, spa, jpn]
      preserve_surround: true
    subtitles:
      keep_languages: [eng, spa, jpn]
      default_language: eng
      remove_formats: [pgs, dvd_subtitle]
      remove_commentary: true
    output:
      container: mkv
      replace_original: true

libraries:
  - name: Test Library
    path: {media_dir}
    profile: test
    scan_interval: 3600
    extensions: [mkv, mp4]

notifications:
  ntfy:
    url: https://ntfy.example.com/test
    on_failure: true
    on_success: false
"""
    )
    return config
