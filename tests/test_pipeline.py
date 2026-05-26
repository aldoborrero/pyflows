"""Tests for the encode pipeline (probe->decide->build->encode->verify->replace)."""

import json
import subprocess
from unittest.mock import patch, MagicMock
from pathlib import Path

from pyflows.pipeline import analyze_changes, analyze_changes_detailed, build_encode_command, encode_file, should_skip, TrackTitle
from pyflows.probe import parse_probe_output
from pyflows.config import load_config


SAMPLE_PROBE = {
    "streams": [
        {"index": 0, "codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080, "tags": {}},
        {"index": 1, "codec_type": "audio", "codec_name": "eac3", "channels": 6, "tags": {"language": "eng", "title": ""}},
        {"index": 2, "codec_type": "audio", "codec_name": "aac", "channels": 2, "tags": {"language": "fre", "title": "French"}},
        {"index": 3, "codec_type": "subtitle", "codec_name": "subrip", "tags": {"language": "eng", "title": "English"}},
        {"index": 4, "codec_type": "subtitle", "codec_name": "hdmv_pgs_subtitle", "tags": {"language": "eng", "title": ""}},
        {"index": 5, "codec_type": "subtitle", "codec_name": "subrip", "tags": {"language": "spa", "title": "Spanish"}},
    ]
}


def test_should_skip_hevc(tmp_config):
    """Files already compliant with the full profile are skipped."""
    config = load_config(tmp_config)
    profile = config.profiles["test"].model_copy(deep=True)
    compliant_probe = parse_probe_output(json.dumps({"streams": [
        {"index": 0, "codec_type": "video", "codec_name": "hevc", "tags": {}},
        {
            "index": 1,
            "codec_type": "audio",
            "codec_name": "eac3",
            "channels": 6,
            "tags": {"language": "eng", "title": "English / EAC3 / 5.1"},
            "disposition": {"default": 1},
        },
        {
            "index": 2,
            "codec_type": "audio",
            "codec_name": "aac",
            "channels": 2,
            "tags": {"language": "eng", "title": "English / AAC / Stereo"},
            "disposition": {"default": 0},
        },
        {
            "index": 3,
            "codec_type": "audio",
            "codec_name": "aac",
            "channels": 2,
            "tags": {"language": "eng", "title": "English / AAC / Stereo"},
            "disposition": {"default": 0},
        },
        {
            "index": 4,
            "codec_type": "audio",
            "codec_name": "aac",
            "channels": 2,
            "tags": {"language": "eng", "title": "English / AAC / Stereo"},
            "disposition": {"default": 0},
        },
        {
            "index": 5,
            "codec_type": "subtitle",
            "codec_name": "subrip",
            "tags": {"language": "eng", "title": "English"},
            "disposition": {"default": 1},
        },
        {
            "index": 6,
            "codec_type": "subtitle",
            "codec_name": "subrip",
            "tags": {"language": "spa", "title": "Spanish"},
            "disposition": {"default": 0},
        },
    ]}))
    assert should_skip(compliant_probe, profile, "/media/test.mkv") is True


def test_should_not_skip_h264(tmp_config):
    """h264 files are not skipped."""
    config = load_config(tmp_config)
    profile = config.profiles["test"]
    probe = parse_probe_output(json.dumps(SAMPLE_PROBE))
    assert should_skip(probe, profile, "/media/test.mkv") is False


def test_analyze_changes_detects_audio_and_subtitle_policy_needs(tmp_config):
    """Even HEVC files are processed when audio/subtitle policy would change them."""
    config = load_config(tmp_config)
    profile = config.profiles["test"]
    hevc_with_bad_tracks = parse_probe_output(json.dumps({"streams": [
        {"index": 0, "codec_type": "video", "codec_name": "hevc", "tags": {}},
        {"index": 1, "codec_type": "audio", "codec_name": "eac3", "channels": 6, "tags": {"language": "eng", "title": ""}},
        {"index": 2, "codec_type": "subtitle", "codec_name": "hdmv_pgs_subtitle", "tags": {"language": "eng", "title": ""}},
    ]}))
    changes = analyze_changes(hevc_with_bad_tracks, profile, "/media/test.mkv")
    detailed = analyze_changes_detailed(hevc_with_bad_tracks, profile, "/media/test.mkv")
    assert changes["video"] is False
    assert changes["audio"] is True
    assert changes["subtitles"] is True
    assert any("audio track count would change" in reason for reason in detailed["audio"])
    assert any("subtitle track count would change" in reason for reason in detailed["subtitles"])
    assert should_skip(hevc_with_bad_tracks, profile, "/media/test.mkv") is False


def test_build_encode_command(tmp_config):
    """Build command produces valid ffmpeg args for an h264 file."""
    config = load_config(tmp_config)
    probe = parse_probe_output(json.dumps(SAMPLE_PROBE))
    profile = config.profiles["test"]

    cmd = build_encode_command(
        input_path="/media/test.mkv",
        output_path="/tmp/test.mkv",
        probe=probe,
        profile=profile,
        vaapi_device="/dev/dri/renderD128",
        use_cpu=False,
    )
    args = cmd.build()

    # Should have video encode
    assert "hevc_vaapi" in args
    # Should map eng audio, not fre
    # Should map eng and spa subs, not PGS
    assert "-map" in args
    # Should have output
    assert args[-1] == "/tmp/test.mkv"
    # Default language should be marked as default audio disposition
    assert "-disposition:a:0" in args
    assert args[args.index("-disposition:a:0") + 1] == "default"
    # Default subtitle language should also be marked
    assert "-disposition:s:0" in args
    assert args[args.index("-disposition:s:0") + 1] == "default"


def test_build_encode_command_cpu_fallback(tmp_config):
    """CPU fallback uses libx265 instead of hevc_vaapi."""
    config = load_config(tmp_config)
    probe = parse_probe_output(json.dumps(SAMPLE_PROBE))
    profile = config.profiles["test"]

    cmd = build_encode_command(
        input_path="/media/test.mkv",
        output_path="/tmp/test.mkv",
        probe=probe,
        profile=profile,
        vaapi_device="/dev/dri/renderD128",
        use_cpu=True,
    )
    args = cmd.build()
    assert "libx265" in args
    assert "hevc_vaapi" not in args


def test_build_encode_command_default_language_fallback(tmp_config):
    """If default_language is missing, the first kept audio and subtitle tracks become default."""
    config = load_config(tmp_config)
    probe = parse_probe_output(json.dumps(SAMPLE_PROBE))
    profile = config.profiles["test"].model_copy(deep=True)
    profile.audio.default_language = "ita"
    profile.subtitles.default_language = "ita"

    cmd = build_encode_command(
        input_path="/media/test.mkv",
        output_path="/tmp/test.mkv",
        probe=probe,
        profile=profile,
        vaapi_device="/dev/dri/renderD128",
        use_cpu=False,
    )
    args = cmd.build()

    assert "-disposition:a:0" in args
    assert args[args.index("-disposition:a:0") + 1] == "default"
    assert "-disposition:s:0" in args
    assert args[args.index("-disposition:s:0") + 1] == "default"


def test_build_encode_command_pipeline1_hevc(tmp_config):
    """hevc input uses Pipeline 1: -hwaccel flags, no video filter."""
    config = load_config(tmp_config)
    profile = config.profiles["test"]
    probe = parse_probe_output(json.dumps({"streams": [
        {"index": 0, "codec_type": "video", "codec_name": "hevc", "width": 1920, "height": 1080, "tags": {}},
        {"index": 1, "codec_type": "audio", "codec_name": "aac", "channels": 2, "tags": {"language": "eng", "title": ""}},
    ]}))
    cmd = build_encode_command("/media/test.mkv", "/tmp/test.mkv", probe, profile, "/dev/dri/renderD128")
    args = cmd.build()

    assert "hevc_vaapi" in args
    assert "-hwaccel" in args
    assert "-hwaccel_output_format" in args
    assert "-async_depth:v:0" in args
    assert args[args.index("-async_depth:v:0") + 1] == "4"
    assert "-vf" not in args
    assert "hwupload_vaapi" not in args
    assert "-filter_hw_device" not in args


def test_build_encode_command_pipeline2_h264(tmp_config):
    """h264 input uses Pipeline 2: -filter_hw_device + format=nv12,hwupload_vaapi."""
    config = load_config(tmp_config)
    profile = config.profiles["test"]
    probe = parse_probe_output(json.dumps(SAMPLE_PROBE))  # video codec = h264
    cmd = build_encode_command("/media/test.mkv", "/tmp/test.mkv", probe, profile, "/dev/dri/renderD128")
    args = cmd.build()

    assert "hevc_vaapi" in args
    assert "-filter_hw_device" in args
    assert "format=nv12,hwupload_vaapi" in args
    assert "-async_depth:v:0" in args
    assert args[args.index("-async_depth:v:0") + 1] == "4"
    assert "-hwaccel" not in args
    assert "-hwaccel_output_format" not in args


def test_track_title_formatting():
    """Track titles format as 'Language / Codec / Channels'."""
    assert TrackTitle.format("eng", "eac3", 6) == "English / EAC3 / 5.1"
    assert TrackTitle.format("jpn", "aac", 2) == "Japanese / AAC / Stereo"
    assert TrackTitle.format("spa", "dts", 8) == "Spanish / DTS / 7.1"


def test_encode_file_full_pipeline(tmp_config, tmp_path: Path) -> None:
    """End-to-end encode_file: VAAPI fails, CPU fallback succeeds, output replaced."""
    config = load_config(tmp_config)
    profile = config.profiles["test"]

    # Create a fake input file
    input_file = tmp_path / "media" / "movie.mkv"
    input_file.parent.mkdir(parents=True, exist_ok=True)
    input_file.write_bytes(b"\x00" * 5000)

    temp_dir = str(tmp_path / "temp")
    Path(temp_dir).mkdir(parents=True, exist_ok=True)

    probe_json = json.dumps(SAMPLE_PROBE)
    call_count = 0

    def fake_run(
        args: list[str],
        stdout: object = None,
        stderr: object = None,
        text: bool = False,
        check: bool = False,
        timeout: int | None = None,
        capture_output: bool = False,
        env: object = None,
    ) -> subprocess.CompletedProcess[str]:
        nonlocal call_count
        if args[0] == "ffprobe":
            return subprocess.CompletedProcess(args=args, returncode=0, stdout=probe_json, stderr="")
        if args[0] == "ffmpeg":
            call_count += 1
            # Find the output path (last arg)
            output_path = args[-1]
            if call_count == 1:
                # VAAPI fails
                return subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr="vaapi error")
            else:
                # CPU fallback succeeds — write a fake output file
                Path(output_path).write_bytes(b"\x00" * 3000)
                return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")
        return subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr="unknown")

    with patch("subprocess.run", side_effect=fake_run):
        success, error, final_path = encode_file(
            input_path=str(input_file),
            profile=profile,
            temp_dir=temp_dir,
            vaapi_device="/dev/dri/renderD128",
        )

    assert success is True
    assert error == ""
    assert final_path == str(input_file)
    # VAAPI failed once, CPU fallback ran, verify ran => ffmpeg called twice
    assert call_count == 2
    # Original file should have been replaced (same path, smaller size from CPU encode)
    assert input_file.stat().st_size == 3000


def test_encode_file_skip_already_encoded(tmp_config, tmp_path: Path) -> None:
    """encode_file returns skipped for files whose codec is in skip_codecs."""
    config = load_config(tmp_config)
    profile = config.profiles["test"]

    input_file = tmp_path / "media" / "movie.mkv"
    input_file.parent.mkdir(parents=True, exist_ok=True)
    input_file.write_bytes(b"\x00" * 5000)

    hevc_probe = {"streams": [
        {"index": 0, "codec_type": "video", "codec_name": "hevc", "tags": {}},
    ]}

    def fake_run(
        args: list[str], **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=args, returncode=0, stdout=json.dumps(hevc_probe), stderr="",
        )

    with patch("subprocess.run", side_effect=fake_run):
        success, error, final_path = encode_file(
            input_path=str(input_file),
            profile=profile,
            temp_dir=str(tmp_path / "temp"),
            vaapi_device="/dev/dri/renderD128",
        )

    assert success is True
    assert error == "skipped"
    assert final_path == str(input_file)


def test_encode_file_honors_output_container(tmp_config, tmp_path: Path) -> None:
    """Replacing the original can change the file extension to the configured container."""
    config = load_config(tmp_config)
    profile = config.profiles["test"]

    input_file = tmp_path / "media" / "movie.mp4"
    input_file.parent.mkdir(parents=True, exist_ok=True)
    input_file.write_bytes(b"\x00" * 5000)

    temp_dir = str(tmp_path / "temp")
    Path(temp_dir).mkdir(parents=True, exist_ok=True)

    probe_json = json.dumps(SAMPLE_PROBE)

    def fake_run(
        args: list[str],
        stdout: object = None,
        stderr: object = None,
        text: bool = False,
        check: bool = False,
        timeout: int | None = None,
        capture_output: bool = False,
        env: object = None,
    ) -> subprocess.CompletedProcess[str]:
        if args[0] == "ffprobe":
            return subprocess.CompletedProcess(args=args, returncode=0, stdout=probe_json, stderr="")
        if args[0] == "ffmpeg":
            output_path = args[-1]
            Path(output_path).write_bytes(b"\x00" * 3000)
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")
        return subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr="unknown")

    with patch("subprocess.run", side_effect=fake_run):
        success, error, final_path = encode_file(
            input_path=str(input_file),
            profile=profile,
            temp_dir=temp_dir,
            vaapi_device="/dev/dri/renderD128",
        )

    assert success is True
    assert error == ""
    assert final_path.endswith(".mkv")
    assert Path(final_path).exists()
    assert not input_file.exists()
