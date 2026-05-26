"""Tests for ffprobe wrapper."""

import json
import pytest  # type: ignore[import-not-found]
from pyflows.probe import ProbeResult, parse_probe_output, StreamInfo


SAMPLE_PROBE = {
    "streams": [
        {"index": 0, "codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080, "tags": {}},
        {"index": 1, "codec_type": "audio", "codec_name": "eac3", "channels": 6, "tags": {"language": "eng", "title": ""}},
        {"index": 2, "codec_type": "audio", "codec_name": "aac", "channels": 2, "tags": {"language": "eng", "title": "Commentary"}},
        {"index": 3, "codec_type": "subtitle", "codec_name": "subrip", "tags": {"language": "eng", "title": "English"}},
        {"index": 4, "codec_type": "subtitle", "codec_name": "hdmv_pgs_subtitle", "tags": {"language": "eng", "title": "English PGS"}},
        {"index": 5, "codec_type": "subtitle", "codec_name": "subrip", "tags": {"language": "spa", "title": "Spanish"}},
        {"index": 6, "codec_type": "subtitle", "codec_name": "subrip", "tags": {"language": "fre", "title": "French"}},
    ]
}


def test_parse_probe_output():
    """Parsing probe JSON extracts streams correctly."""
    result = parse_probe_output(json.dumps(SAMPLE_PROBE))
    assert result.video.codec == "h264"
    assert len(result.audio) == 2
    assert len(result.subtitles) == 4


def test_video_stream_info():
    """Video stream has correct codec."""
    result = parse_probe_output(json.dumps(SAMPLE_PROBE))
    assert result.video.codec == "h264"
    assert result.video.index == 0


def test_audio_stream_info():
    """Audio streams have correct language and channels."""
    result = parse_probe_output(json.dumps(SAMPLE_PROBE))
    assert result.audio[0].codec == "eac3"
    assert result.audio[0].channels == 6
    assert result.audio[0].language == "eng"
    assert result.audio[1].title == "Commentary"


def test_parse_default_disposition():
    """Default dispositions are parsed from ffprobe output."""
    result = parse_probe_output(json.dumps({"streams": [
        {
            "index": 0,
            "codec_type": "audio",
            "codec_name": "aac",
            "channels": 2,
            "tags": {"language": "eng", "title": "English"},
            "disposition": {"default": 1},
        }
    ]}))
    assert result.audio[0].is_default is True


def test_subtitle_stream_info():
    """Subtitle streams include codec for format filtering."""
    result = parse_probe_output(json.dumps(SAMPLE_PROBE))
    pgs = [s for s in result.subtitles if s.codec == "hdmv_pgs_subtitle"]
    assert len(pgs) == 1
