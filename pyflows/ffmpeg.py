"""FFmpeg command builder and executor."""

import logging
import os
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
# FFmpeg option values: strings like "128k" or ints like 22
type FfmpegOptValue = str | int | float

from pyflows.config import HardwareConfig

from pyflows.logging_utils import log_event

log = logging.getLogger(__name__)

DEFAULT_STALL_TIMEOUT = 300  # Kill if no progress for 5 minutes
STARTUP_TIMEOUT = 600  # Kill if ffmpeg never starts producing progress (10 minutes)
STDERR_TAIL_BYTES = 1000
_OUT_TIME_US_PREFIX = b"out_time_us="
_SPEED_PREFIX = b"speed="

_active_proc: subprocess.Popen | None = None  # type: ignore[type-arg]
_active_proc_lock = threading.Lock()


@dataclass
class EncodeProgress:
    """Snapshot of real-time encode progress."""

    out_time_us: int = 0
    speed: float = 0.0
    file_path: str = ""


_current_progress = EncodeProgress()
_progress_data_lock = threading.Lock()


def get_current_progress() -> EncodeProgress:
    """Return a copy of the current encode progress."""
    with _progress_data_lock:
        return EncodeProgress(
            out_time_us=_current_progress.out_time_us,
            speed=_current_progress.speed,
            file_path=_current_progress.file_path,
        )


def _set_progress(file_path: str, out_time_us: int, speed: float) -> None:
    with _progress_data_lock:
        _current_progress.file_path = file_path
        _current_progress.out_time_us = out_time_us
        _current_progress.speed = speed


def _clear_progress() -> None:
    with _progress_data_lock:
        _current_progress.file_path = ""
        _current_progress.out_time_us = 0
        _current_progress.speed = 0.0


# Default codecs where VAAPI hardware decode is confirmed reliable on AMD/Mesa.
# These use Pipeline 1 (HW decode + HW encode): fast full-GPU path.
DEFAULT_VAAPI_HW_DECODE_CODECS: frozenset[str] = frozenset({"hevc", "av1", "vp9"})

# Default AMD-specific env var: disables the unstable EFC (Explicit Frame Copy)
# feature in Mesa. Jellyfin sets this for all AMD VAAPI sessions.
DEFAULT_VAAPI_ENV = {"AMD_DEBUG": "noefc"}

# Backwards-compatible alias used in tests and older imports.
VAAPI_HW_DECODE_CODECS = DEFAULT_VAAPI_HW_DECODE_CODECS


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
        """Apply runtime-configurable hardware/VAAPI settings."""
        self._vaapi_name = hardware.vaapi.device
        self._vaapi_hw_decode_codecs = frozenset(codec.lower() for codec in hardware.vaapi.hw_decode_codecs)
        self._vaapi_upload_filter = hardware.vaapi.upload_filter
        self._vaapi_env = dict(hardware.env)

    def set_input_codec(self, codec: str) -> None:
        """Set the input video codec to select the correct VAAPI pipeline."""
        self._input_codec = codec

    def set_ffmpeg_path(self, path: str) -> None:
        self._ffmpeg_path = path

    def add_input(self, path: str, **kwargs: FfmpegOptValue) -> None:
        self._inputs.append((path, kwargs))

    def map_stream(self, spec: str) -> int:
        """Map a stream and return its global output index."""
        stream_type = spec.split(":")[1]  # v, a, or s
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
        """Return True if the input codec supports reliable VAAPI HW decode."""
        return self._input_codec in self._vaapi_hw_decode_codecs if self._input_codec else False

    def build(self) -> list[str]:
        """Build the complete ffmpeg argument list."""
        args = [self._ffmpeg_path] + self._global_opts

        if self._vaapi_device:
            hw_decode = self._use_hw_decode()
            # Common: initialise the VAAPI device with a named alias
            args += ["-init_hw_device", f"vaapi={self._vaapi_name}:{self._vaapi_device}"]

            if hw_decode:
                # Pipeline 1: hardware decode keeps frames on the GPU.
                # -hwaccel_output_format vaapi ensures the decoder outputs
                # VAAPI surfaces that hevc_vaapi can consume directly.
                args += [
                    "-hwaccel", "vaapi",
                    "-hwaccel_output_format", "vaapi",
                    "-hwaccel_device", self._vaapi_name,
                ]
            else:
                # Pipeline 2: software decode, GPU encode.
                # -filter_hw_device tells the filter graph which VAAPI device
                # hwupload_vaapi should target (required — without it the
                # filter cannot locate the device and silently stalls).
                args += ["-filter_hw_device", self._vaapi_name]

        # Inputs
        for path, kwargs in self._inputs:
            for k, v in kwargs.items():
                args += [f"-{k}", str(v)]
            args += ["-i", path]

        # Stream mappings
        for spec, _, _ in self._maps:
            args += ["-map", spec]

        # Video filter
        if self._vaapi_device and not self._use_hw_decode():
            # Pipeline 2: convert decoded frames to NV12 then upload to VAAPI.
            # format=nv12 ensures a pixel format the VAAPI encoder accepts.
            # hwupload_vaapi uploads from system memory to the GPU surface
            # pointed to by -filter_hw_device.
            args += ["-vf", self._vaapi_upload_filter]
        # Pipeline 1: no filter — frames are already on the GPU.

        # Per-stream codecs (using per-type indices)
        for idx, (codec, opts) in sorted(self._codecs.items()):
            _, stype, type_idx = self._maps[idx]
            args += [f"-c:{stype}:{type_idx}", codec]
            for k, v in opts.items():
                args += [f"-{k}:{stype}:{type_idx}", str(v)]

        # Metadata (using per-type indices)
        for idx, key, value in self._metadata:
            _, stype, type_idx = self._maps[idx]
            args += [f"-metadata:s:{stype}:{type_idx}", f"{key}={value}"]

        # Dispositions (using per-type indices)
        for idx, value in sorted(self._dispositions.items()):
            _, stype, type_idx = self._maps[idx]
            args += [f"-disposition:{stype}:{type_idx}", value]

        # Output
        args.append(self._output)

        return args

    def run(self, stall_timeout: int = DEFAULT_STALL_TIMEOUT) -> subprocess.CompletedProcess[str]:
        """Execute the ffmpeg command with stall detection.

        Uses ``-progress pipe:1`` to monitor ffmpeg's ``out_time_us`` output.
        If encoding progress doesn't advance for *stall_timeout* seconds,
        the process is terminated (SIGTERM, then SIGKILL after 10s).

        This adapts to any file length or encode speed — short files finish
        quickly, slow CPU encodes are allowed as long as they keep progressing,
        and hung processes are killed in minutes rather than hours.
        """
        args = self.build()

        # Inject -progress pipe:1 -nostats before the output path.
        # This makes ffmpeg write machine-readable progress to stdout.
        progress_args = list(args)
        output_idx = len(progress_args) - 1  # last arg is output path
        progress_args[output_idx:output_idx] = ["-progress", "pipe:1", "-nostats"]

        log_event(log, logging.INFO, "ffmpeg_run", "Running ffmpeg",
                  command=" ".join(args), stall_timeout=stall_timeout)

        env = {**os.environ, **self._vaapi_env} if self._vaapi_device else None

        with tempfile.NamedTemporaryFile(
            mode="w+b", suffix=".log", prefix="pyflows-ffmpeg-", delete=True
        ) as stderr_file:
            proc = subprocess.Popen(
                progress_args,
                stdout=subprocess.PIPE,
                stderr=stderr_file,
                env=env,
            )

            global _active_proc
            with _active_proc_lock:
                _active_proc = proc

            input_path = self._inputs[0][0] if self._inputs else ""
            _set_progress(input_path, 0, 0.0)

            # Track progress from stdout in a background thread.
            # Stall clock starts only after the first progress line is received,
            # so ffmpeg's initial input analysis phase doesn't trigger a false stall.
            last_progress_time: float | None = None  # None = no progress yet
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
                                _set_progress(input_path, value, current_speed)
                            except ValueError:
                                pass
                        elif line.startswith(_SPEED_PREFIX):
                            try:
                                raw = line[len(_SPEED_PREFIX):].strip().rstrip(b"x")
                                current_speed = float(raw)
                            except ValueError:
                                pass
                except (OSError, ValueError):
                    pass  # stdout closed after process kill

            reader = threading.Thread(target=_read_progress, daemon=True, name="ffmpeg-progress")
            reader.start()

            # Poll for stall or completion
            start_time = time.monotonic()
            while proc.poll() is None:
                time.sleep(5)
                with progress_lock:
                    if last_progress_time is None:
                        # No progress line received yet (ffmpeg still analyzing input).
                        if time.monotonic() - start_time > STARTUP_TIMEOUT:
                            stalled.set()
                            log_event(log, logging.ERROR, "ffmpeg_startup_timeout",
                                      "ffmpeg never started producing progress — sending SIGTERM",
                                      start_timeout=STARTUP_TIMEOUT)
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
                    reason = f"[STARTUP TIMEOUT: no progress output for {STARTUP_TIMEOUT}s]"
                else:
                    reason = f"[STALL detected: no progress for {stall_timeout}s]"
                with _active_proc_lock:
                    _active_proc = None
                _clear_progress()
                return subprocess.CompletedProcess(
                    args=args, returncode=-1, stdout="",
                    stderr=f"{reason} {tail}",
                )

            pos = stderr_file.tell()
            stderr_file.seek(max(0, pos - STDERR_TAIL_BYTES))
            stderr_tail = stderr_file.read().decode("utf-8", errors="replace")

        with _active_proc_lock:
            _active_proc = None
        _clear_progress()
        return subprocess.CompletedProcess(
            args=args, returncode=proc.returncode, stdout="", stderr=stderr_tail,
        )


def terminate_active_encode() -> None:
    """Terminate the active ffmpeg process, if any.

    Called during daemon shutdown so SIGTERM is not blocked waiting for
    a long-running encode to finish on its own.
    """
    with _active_proc_lock:
        proc = _active_proc
    if proc is not None:
        try:
            proc.terminate()
        except OSError:
            pass
