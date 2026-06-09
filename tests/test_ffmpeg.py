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


# --- Registry tests ---

from unittest.mock import MagicMock, patch
from pyflows.ffmpeg import ProcessRegistry, ProgressRegistry, EncodeProgress


def test_process_registry_register_unregister():
    reg = ProcessRegistry()
    proc = MagicMock()
    reg.register("tok-1", proc)
    reg.register("tok-2", MagicMock())
    reg.unregister("tok-1")
    reg.unregister("nonexistent")


def test_process_registry_terminate_all():
    reg = ProcessRegistry()
    p1 = MagicMock()
    p2 = MagicMock()
    p1.wait.return_value = 0
    p2.wait.side_effect = [subprocess.TimeoutExpired("ffmpeg", 10), None]
    reg.register("a", p1)
    reg.register("b", p2)
    reg.terminate_all()
    p1.terminate.assert_called_once()
    p2.terminate.assert_called_once()
    p2.kill.assert_called_once()


def test_process_registry_closed_terminates_late_registration():
    reg = ProcessRegistry()
    reg.terminate_all()
    late_proc = MagicMock()
    late_proc.wait.return_value = 0
    reg.register("late", late_proc)
    late_proc.terminate.assert_called_once()


def test_process_registry_terminate_handles_oserror():
    reg = ProcessRegistry()
    p = MagicMock()
    p.wait.side_effect = [subprocess.TimeoutExpired("ffmpeg", 10), None]
    p.kill.side_effect = OSError("already dead")
    reg.register("a", p)
    reg.terminate_all()


def test_progress_registry_multiple_entries():
    reg = ProgressRegistry()
    reg.update("tok-1", 1000, 2.0, file_path="/a.mkv")
    reg.update("tok-2", 5000, 3.5, file_path="/b.mkv")
    p1 = reg.get("tok-1")
    assert p1 is not None
    assert p1.out_time_us == 1000
    p2 = reg.get("tok-2")
    assert p2 is not None
    assert p2.speed == 3.5
    active = reg.get_any_active()
    assert active.file_path in ("/a.mkv", "/b.mkv")
    reg.remove("tok-1")
    assert reg.get("tok-1") is None
    assert reg.get("tok-2") is not None


def test_progress_registry_empty():
    reg = ProgressRegistry()
    assert reg.get("nonexistent") is None
    active = reg.get_any_active()
    assert active.out_time_us == 0
    assert active.file_path == ""


import subprocess


def test_gpu_semaphore_released_on_vaapi_failure(tmp_config, tmp_path):
    """GPU semaphore is released before CPU fallback starts."""
    import threading
    import json
    from pyflows.pipeline import encode_file
    from pyflows.config import load_config

    config = load_config(tmp_config)
    profile = config.profiles["test"]
    sem = threading.BoundedSemaphore(1)

    input_file = tmp_path / "media" / "movie.mkv"
    input_file.parent.mkdir(parents=True, exist_ok=True)
    input_file.write_bytes(b"\x00" * 5000)
    temp_dir = str(tmp_path / "temp")
    (tmp_path / "temp").mkdir()

    probe_json = json.dumps({"streams": [
        {"index": 0, "codec_type": "video", "codec_name": "h264", "tags": {}},
        {"index": 1, "codec_type": "audio", "codec_name": "aac", "channels": 2,
         "tags": {"language": "eng", "title": ""}},
    ]})

    import io

    call_count = 0

    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=probe_json, stderr="")

    class FakePopen:
        def __init__(self, args, **kwargs):
            nonlocal call_count
            call_count += 1
            self.args = args
            self._returncode = 1 if call_count == 1 else 0
            output_path = args[-1]
            if self._returncode == 0:
                from pathlib import Path
                Path(output_path).write_bytes(b"\x00" * 3000)
            self.stdout = io.BytesIO(b"out_time_us=1000000\n")
            self._poll_count = 0

        def poll(self):
            self._poll_count += 1
            return self._returncode if self._poll_count >= 2 else None

        @property
        def returncode(self):
            return self._returncode

        def terminate(self): pass
        def kill(self): pass
        def wait(self, timeout=None): return self._returncode

    with patch("subprocess.run", side_effect=fake_run), \
         patch("subprocess.Popen", FakePopen):
        result = encode_file(
            input_path=str(input_file), profile=profile,
            temp_dir=temp_dir, vaapi_device="/dev/dri/renderD128",
            gpu_semaphore=sem,
        )

    assert result.status.value == "completed"
    assert sem._value == 1


def test_replace_original_false_preserves_source(tmp_config, tmp_path):
    """replace_original=False returns temp path without touching original."""
    import json
    from pyflows.pipeline import encode_file
    from pyflows.config import load_config

    config = load_config(tmp_config)
    profile = config.profiles["test"]

    input_file = tmp_path / "media" / "movie.mkv"
    input_file.parent.mkdir(parents=True, exist_ok=True)
    input_file.write_bytes(b"\x00" * 5000)
    temp_dir = str(tmp_path / "temp")
    (tmp_path / "temp").mkdir()

    probe_json = json.dumps({"streams": [
        {"index": 0, "codec_type": "video", "codec_name": "h264", "tags": {}},
        {"index": 1, "codec_type": "audio", "codec_name": "aac", "channels": 2,
         "tags": {"language": "eng", "title": ""}},
    ]})

    import io

    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=probe_json, stderr="")

    class FakePopen:
        def __init__(self, args, **kwargs):
            self.args = args
            self._returncode = 0
            from pathlib import Path
            Path(args[-1]).write_bytes(b"\x00" * 3000)
            self.stdout = io.BytesIO(b"out_time_us=1000000\n")
            self._poll_count = 0

        def poll(self):
            self._poll_count += 1
            return self._returncode if self._poll_count >= 2 else None

        @property
        def returncode(self):
            return self._returncode

        def terminate(self): pass
        def kill(self): pass
        def wait(self, timeout=None): return self._returncode

    with patch("subprocess.run", side_effect=fake_run), \
         patch("subprocess.Popen", FakePopen):
        result = encode_file(
            input_path=str(input_file), profile=profile,
            temp_dir=temp_dir, vaapi_device="/dev/dri/renderD128",
            replace_original=False,
        )

    assert result.status.value == "completed"
    assert input_file.exists()
    assert result.final_path != str(input_file)
    assert result.final_path.startswith(temp_dir)
    from pathlib import Path
    assert Path(result.final_path).exists()
