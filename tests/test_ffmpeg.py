"""Tests for FFmpeg command builder."""

from pyflows.config import HardwareConfig
from pyflows.ffmpeg import FFmpegCommand, VAAPI_HW_DECODE_CODECS


def test_basic_command():
    """Building a minimal command produces correct args."""
    cmd = FFmpegCommand()
    cmd.add_input("/input.mkv")
    cmd.set_output("/output.mkv")
    args = cmd.build()
    assert args[0] == "ffmpeg"
    assert "-i" in args
    assert "/input.mkv" in args
    assert args[-1] == "/output.mkv"


def test_map_streams():
    """Mapping streams adds -map arguments."""
    cmd = FFmpegCommand()
    cmd.add_input("/input.mkv")
    idx = cmd.map_stream("0:v:0")
    cmd.set_output("/output.mkv")
    args = cmd.build()
    assert "-map" in args
    assert "0:v:0" in args
    assert idx == 0


def test_per_stream_codec():
    """Per-stream codec options use per-type indices."""
    cmd = FFmpegCommand()
    cmd.add_input("/input.mkv")
    v_idx = cmd.map_stream("0:v:0")
    a0_idx = cmd.map_stream("0:a:0")
    a1_idx = cmd.map_stream("0:a:1")
    cmd.set_codec(v_idx, "hevc_vaapi", qp=22)
    cmd.set_codec(a0_idx, "copy")
    cmd.set_codec(a1_idx, "aac", ac=2, b="128k")
    cmd.set_output("/output.mkv")
    args = cmd.build()
    assert "-c:v:0" in args and "hevc_vaapi" in args
    assert "-c:a:0" in args and "copy" in args
    assert "-c:a:1" in args and "aac" in args
    assert "-ac:a:1" in args and "-b:a:1" in args


def test_metadata():
    """Stream metadata uses per-type indices."""
    cmd = FFmpegCommand()
    cmd.add_input("/input.mkv")
    v_idx = cmd.map_stream("0:v:0")
    a_idx = cmd.map_stream("0:a:0")
    cmd.set_metadata(a_idx, "title", "English / EAC3 / 5.1")
    cmd.set_output("/output.mkv")
    args = cmd.build()
    assert "-metadata:s:a:0" in args
    assert "title=English / EAC3 / 5.1" in args


def test_vaapi_pipeline1_hw_decode_codecs():
    """Pipeline 1: hevc/av1/vp9 use -hwaccel flags, no video filter."""
    for codec in VAAPI_HW_DECODE_CODECS:
        cmd = FFmpegCommand()
        cmd.set_vaapi_device("/dev/dri/renderD128")
        cmd.set_input_codec(codec)
        cmd.add_input("/input.mkv")
        cmd.map_stream("0:v:0")
        cmd.set_output("/output.mkv")
        args = cmd.build()

        assert "-init_hw_device" in args, f"missing init_hw_device for {codec}"
        assert "vaapi=va:/dev/dri/renderD128" in args

        # Pipeline 1: hardware decode flags present
        assert "-hwaccel" in args, f"missing -hwaccel for {codec}"
        assert "vaapi" in args
        assert "-hwaccel_output_format" in args
        assert "-hwaccel_device" in args

        # Pipeline 1: no upload filter needed
        assert "-vf" not in args, f"-vf must not be present for hw-decoded {codec}"
        assert "hwupload_vaapi" not in args
        assert "-filter_hw_device" not in args


def test_vaapi_pipeline2_sw_decode_codecs():
    """Pipeline 2: h264 and unknown codecs use -filter_hw_device + hwupload_vaapi."""
    for codec in ("h264", "mpeg4", "mpeg2video", "unknown"):
        cmd = FFmpegCommand()
        cmd.set_vaapi_device("/dev/dri/renderD128")
        cmd.set_input_codec(codec)
        cmd.add_input("/input.mkv")
        cmd.map_stream("0:v:0")
        cmd.set_output("/output.mkv")
        args = cmd.build()

        assert "-init_hw_device" in args
        assert "vaapi=va:/dev/dri/renderD128" in args

        # Pipeline 2: no hardware decode flags
        assert "-hwaccel" not in args, f"-hwaccel must not be present for sw-decoded {codec}"
        assert "-hwaccel_output_format" not in args
        assert "-hwaccel_device" not in args

        # Pipeline 2: filter_hw_device + upload filter
        assert "-filter_hw_device" in args, f"missing -filter_hw_device for {codec}"
        assert "va" in args
        assert "-vf" in args
        assert "format=nv12,hwupload_vaapi" in args


def test_vaapi_pipeline2_default_no_codec():
    """Pipeline 2 is the safe default when no input codec is set."""
    cmd = FFmpegCommand()
    cmd.set_vaapi_device("/dev/dri/renderD128")
    # No set_input_codec call
    cmd.add_input("/input.mkv")
    cmd.map_stream("0:v:0")
    cmd.set_output("/output.mkv")
    args = cmd.build()

    assert "-filter_hw_device" in args
    assert "format=nv12,hwupload_vaapi" in args
    assert "-hwaccel" not in args


def test_global_options():
    """Global options like -y and -nostdin are included."""
    cmd = FFmpegCommand()
    cmd.add_input("/input.mkv")
    cmd.set_output("/output.mkv")
    args = cmd.build()
    assert "-y" in args
    assert "-nostdin" in args


def test_ffmpeg_path():
    """Custom ffmpeg path is used in build."""
    cmd = FFmpegCommand()
    cmd.set_ffmpeg_path("/usr/lib/jellyfin-ffmpeg/ffmpeg")
    cmd.add_input("/input.mkv")
    cmd.set_output("/output.mkv")
    args = cmd.build()
    assert args[0] == "/usr/lib/jellyfin-ffmpeg/ffmpeg"


def test_configure_hardware_overrides_vaapi_defaults():
    """Hardware config overrides the VAAPI alias, upload filter, env and HW codecs."""
    cmd = FFmpegCommand()
    cmd.configure_hardware(
        HardwareConfig.model_validate({
            "acceleration": "vaapi",
            "env": {"AMD_DEBUG": "noefc", "LIBVA_DRIVER_NAME": "radeonsi"},
            "vaapi": {
                "device": "render",
                "hw_decode_codecs": ["vp9"],
                "sw_decode_codecs": ["h264"],
                "upload_filter": "format=nv12,hwupload_vaapi,scale_vaapi",
                "async_depth": 6,
                "use_hw_encode": True,
            },
        })
    )
    cmd.set_vaapi_device("/dev/dri/renderD128")
    cmd.set_input_codec("vp9")
    cmd.add_input("/input.mkv")
    v_idx = cmd.map_stream("0:v:0")
    cmd.set_codec(v_idx, "hevc_vaapi", qp=22, async_depth=6)
    cmd.set_output("/output.mkv")
    args = cmd.build()

    assert "vaapi=render:/dev/dri/renderD128" in args
    assert "-hwaccel_device" in args
    assert "render" in args
    assert "-async_depth:v:0" in args
    assert args[args.index("-async_depth:v:0") + 1] == "6"
