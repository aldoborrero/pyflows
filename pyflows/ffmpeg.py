"""FFmpeg command builder and executor."""

import logging
import os
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Union

FfmpegOptValue = Union[str, int, float]

from pyflows.config import HardwareConfig

from pyflows.logging_utils import log_event

log = logging.getLogger(__name__)

DEFAULT_STALL_TIMEOUT = 300
STARTUP_TIMEOUT = 600
STDERR_TAIL_BYTES = 1000
_OUT_TIME_US_PREFIX = b"out_time_us="
_SPEED_PREFIX = b"speed="

DEFAULT_VAAPI_HW_DECODE_CODECS: frozenset[str] = frozenset({"hevc", "av1", "vp9"})
DEFAULT_VAAPI_ENV = {"AMD_DEBUG": "noefc"}
VAAPI_HW_DECODE_CODECS = DEFAULT_VAAPI_HW_DECODE_CODECS


@dataclass
class EncodeProgress:
    out_time_us: int = 0
    speed: float = 0.0
    file_path: str = ""


class ProcessRegistry:
    """Thread-safe registry of active FFmpeg processes keyed by token."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._procs: dict[str, subprocess.Popen] = {}  # type: ignore[type-arg]
        self._closed = False

    def register(self, key: str, proc: subprocess.Popen) -> None:  # type: ignore[type-arg]
        with self._lock:
            if self._closed:
                try:
                    proc.terminate()
                    proc.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    try:
                        proc.kill()
                        proc.wait()
                    except OSError:
                        pass
                return
            self._procs[key] = proc

    def unregister(self, key: str) -> None:
        with self._lock:
            self._procs.pop(key, None)

    def terminate_all(self) -> None:
        with self._lock:
            self._closed = True
            procs = list(self._procs.values())
        for proc in procs:
            try:
                proc.terminate()
            except OSError:
                pass
        for proc in procs:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                    proc.wait()
                except OSError:
                    pass
            except OSError:
                pass


class ProgressRegistry:
    """Thread-safe registry of encode progress keyed by token."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[str, EncodeProgress] = {}

    def update(self, key: str, out_time_us: int, speed: float, file_path: str = "") -> None:
        with self._lock:
            self._data[key] = EncodeProgress(out_time_us=out_time_us, speed=speed, file_path=file_path)

    def get(self, key: str) -> EncodeProgress | None:
        with self._lock:
            p = self._data.get(key)
            if p is None:
                return None
            return EncodeProgress(out_time_us=p.out_time_us, speed=p.speed, file_path=p.file_path)

    def get_any_active(self) -> EncodeProgress:
        with self._lock:
            for p in self._data.values():
                return EncodeProgress(out_time_us=p.out_time_us, speed=p.speed, file_path=p.file_path)
        return EncodeProgress()

    def remove(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)


_process_registry = ProcessRegistry()
_progress_registry = ProgressRegistry()


def get_current_progress() -> EncodeProgress:
    return _progress_registry.get_any_active()


def terminate_active_encode() -> None:
    _process_registry.terminate_all()


@dataclass
class FFmpegCommand:
    """Builds an ffmpeg command with per-stream codec and metadata options.

    Two VAAPI pipelines are supported, selected by set_input_codec():

    Pipeline 1 — HW decode + HW encode (hevc, av1, vp9 inputs):
        -init_hw_device vaapi=va:<device>
        -hwaccel vaapi -hwaccel_output_format vaapi -hwaccel_device va
        -i <input>
        [no video filter — frames stay on GPU]
        -c:v hevc_vaapi

    Pipeline 2 — SW decode + HW encode (h264 and all other inputs):
        -init_hw_device vaapi=va:<device>
        -filter_hw_device va
        -i <input>
        -vf format=nv12,hwupload_vaapi
        -c:v hevc_vaapi

    Pipeline 2 is the safe default when no input codec is set.
    """

    _inputs: list[tuple[str, dict[str, FfmpegOptValue]]] = field(default_factory=list)
    _maps: list[tuple[str, str, int]] = field(default_factory=list)
    _codecs: dict[int, tuple[str, dict[str, FfmpegOptValue]]] = field(default_factory=dict)
    _metadata: list[tuple[int, str, str]] = field(default_factory=list)
    _dispositions: dict[int, str] = field(default_factory=dict)
    _global_opts: list[str] = field(default_factory=lambda: ["-y", "-nostdin"])
    _vaapi_device: str | None = None
    _vaapi_name: str = "va"
    _vaapi_hw_decode_codecs: frozenset[str] = DEFAULT_VAAPI_HW_DECODE_CODECS
    _vaapi_upload_filter: str = "format=nv12,hwupload_vaapi"
    _vaapi_env: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_VAAPI_ENV))
    _input_codec: str | None = None
    _ffmpeg_path: str = "ffmpeg"
    _output: str = ""

    def set_vaapi_device(self, device: str) -> None:
        self._vaapi_device = device

    def configure_hardware(self, hardware: HardwareConfig) -> None:
        self._vaapi_name = hardware.vaapi.device
        self._vaapi_hw_decode_codecs = frozenset(codec.lower() for codec in hardware.vaapi.hw_decode_codecs)
        self._vaapi_upload_filter = hardware.vaapi.upload_filter
        self._vaapi_env = dict(hardware.env)

    def set_input_codec(self, codec: str) -> None:
        self._input_codec = codec

    def set_ffmpeg_path(self, path: str) -> None:
        self._ffmpeg_path = path

    def add_input(self, path: str, **kwargs: FfmpegOptValue) -> None:
        self._inputs.append((path, kwargs))

    def map_stream(self, spec: str) -> int:
        stream_type = spec.split(":")[1]
        type_idx = sum(1 for _, t, _ in self._maps if t == stream_type)
        self._maps.append((spec, stream_type, type_idx))
        return len(self._maps) - 1

    def set_codec(self, stream_idx: int, codec: str, **opts: FfmpegOptValue) -> None:
        self._codecs[stream_idx] = (codec, opts)

    def set_metadata(self, stream_idx: int, key: str, value: str) -> None:
        self._metadata.append((stream_idx, key, value))

    def set_disposition(self, stream_idx: int, value: str) -> None:
        self._dispositions[stream_idx] = value

    def set_output(self, path: str) -> None:
        self._output = path

    def _use_hw_decode(self) -> bool:
        return self._input_codec in self._vaapi_hw_decode_codecs if self._input_codec else False

    def build(self) -> list[str]:
        args = [self._ffmpeg_path] + self._global_opts

        if self._vaapi_device:
            hw_decode = self._use_hw_decode()
            args += ["-init_hw_device", f"vaapi={self._vaapi_name}:{self._vaapi_device}"]

            if hw_decode:
                args += [
                    "-hwaccel", "vaapi",
                    "-hwaccel_output_format", "vaapi",
                    "-hwaccel_device", self._vaapi_name,
                ]
            else:
                args += ["-filter_hw_device", self._vaapi_name]

        for path, kwargs in self._inputs:
            for k, v in kwargs.items():
                args += [f"-{k}", str(v)]
            args += ["-i", path]

        for spec, _, _ in self._maps:
            args += ["-map", spec]

        if self._vaapi_device and not self._use_hw_decode():
            args += ["-vf", self._vaapi_upload_filter]

        for idx, (codec, opts) in sorted(self._codecs.items()):
            _, stype, type_idx = self._maps[idx]
            args += [f"-c:{stype}:{type_idx}", codec]
            for k, v in opts.items():
                args += [f"-{k}:{stype}:{type_idx}", str(v)]

        for idx, key, value in self._metadata:
            _, stype, type_idx = self._maps[idx]
            args += [f"-metadata:s:{stype}:{type_idx}", f"{key}={value}"]

        for idx, value in sorted(self._dispositions.items()):
            _, stype, type_idx = self._maps[idx]
            args += [f"-disposition:{stype}:{type_idx}", value]

        args.append(self._output)

        return args

    def run(self, stall_timeout: int = DEFAULT_STALL_TIMEOUT,
            startup_timeout: int = STARTUP_TIMEOUT,
            registry_key: str = "") -> subprocess.CompletedProcess[str]:
        """Execute the ffmpeg command with stall detection."""
        args = self.build()

        progress_args = list(args)
        output_idx = len(progress_args) - 1
        progress_args[output_idx:output_idx] = ["-progress", "pipe:1", "-nostats"]

        log_event(log, logging.INFO, "ffmpeg_run", "Running ffmpeg",
                  command=" ".join(args), stall_timeout=stall_timeout)

        env = {**os.environ, **self._vaapi_env} if self._vaapi_device else None

        input_path = self._inputs[0][0] if self._inputs else ""
        reg_key = registry_key or input_path

        with tempfile.NamedTemporaryFile(
            mode="w+b", suffix=".log", prefix="pyflows-ffmpeg-", delete=True
        ) as stderr_file:
            proc = subprocess.Popen(
                progress_args,
                stdout=subprocess.PIPE,
                stderr=stderr_file,
                env=env,
            )

            _process_registry.register(reg_key, proc)
            _progress_registry.update(reg_key, 0, 0.0, file_path=input_path)

            last_progress_time: float | None = None
            last_out_time_us: int = -1
            progress_lock = threading.Lock()
            stalled = threading.Event()

            def _read_progress() -> None:
                nonlocal last_progress_time, last_out_time_us
                current_speed = 0.0
                assert proc.stdout is not None
                try:
                    for line in proc.stdout:
                        if line.startswith(_OUT_TIME_US_PREFIX):
                            try:
                                value = int(line[len(_OUT_TIME_US_PREFIX):].strip())
                                with progress_lock:
                                    if value > last_out_time_us:
                                        last_out_time_us = value
                                        last_progress_time = time.monotonic()
                                _progress_registry.update(reg_key, value, current_speed, file_path=input_path)
                            except ValueError:
                                pass
                        elif line.startswith(_SPEED_PREFIX):
                            try:
                                raw = line[len(_SPEED_PREFIX):].strip().rstrip(b"x")
                                current_speed = float(raw)
                            except ValueError:
                                pass
                except (OSError, ValueError):
                    pass

            reader = threading.Thread(target=_read_progress, daemon=True, name="ffmpeg-progress")
            reader.start()

            start_time = time.monotonic()
            while proc.poll() is None:
                time.sleep(5)
                with progress_lock:
                    if last_progress_time is None:
                        if time.monotonic() - start_time > startup_timeout:
                            stalled.set()
                            log_event(log, logging.ERROR, "ffmpeg_startup_timeout",
                                      "ffmpeg never started producing progress — sending SIGTERM",
                                      start_timeout=startup_timeout)
                        else:
                            continue
                    else:
                        elapsed_since_progress = time.monotonic() - last_progress_time
                        if elapsed_since_progress > stall_timeout:
                            stalled.set()
                            log_event(log, logging.ERROR, "ffmpeg_stall",
                                      "ffmpeg stalled, no progress — sending SIGTERM",
                                      stall_seconds=int(elapsed_since_progress),
                                      last_out_time_us=last_out_time_us,
                                      stall_timeout=stall_timeout)
                if stalled.is_set():
                    proc.terminate()
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        log_event(log, logging.WARNING, "ffmpeg_kill",
                                  "ffmpeg did not exit after SIGTERM, sending SIGKILL")
                        proc.kill()
                        proc.wait()
                    break

            reader.join(timeout=5)

            if stalled.is_set():
                stderr_file.seek(max(0, stderr_file.tell() - STDERR_TAIL_BYTES))
                tail = stderr_file.read().decode("utf-8", errors="replace")
                if last_progress_time is None:
                    reason = f"[STARTUP TIMEOUT: no progress output for {startup_timeout}s]"
                else:
                    reason = f"[STALL detected: no progress for {stall_timeout}s]"
                _process_registry.unregister(reg_key)
                _progress_registry.remove(reg_key)
                return subprocess.CompletedProcess(
                    args=args, returncode=-1, stdout="",
                    stderr=f"{reason} {tail}",
                )

            pos = stderr_file.tell()
            stderr_file.seek(max(0, pos - STDERR_TAIL_BYTES))
            stderr_tail = stderr_file.read().decode("utf-8", errors="replace")

        _process_registry.unregister(reg_key)
        _progress_registry.remove(reg_key)
        return subprocess.CompletedProcess(
            args=args, returncode=proc.returncode, stdout="", stderr=stderr_tail,
        )
