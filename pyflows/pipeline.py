"""Encode pipeline: probe → decide → build → encode → verify → replace."""

import logging
import os
import shutil
import subprocess
import uuid
from pathlib import Path

from pyflows.audio import AudioAction, build_audio_plan
from pyflows.config import HardwareConfig, ProfileConfig
from pyflows.ffmpeg import FFmpegCommand
from pyflows.logging_utils import log_event
from pyflows.plan import CHANNEL_NAMES, LANGUAGE_NAMES, FilePlan, container_suffix, plan_file, plan_from_probe, select_default_audio_pos, select_default_subtitle_pos
from pyflows.probe import ProbeResult, StreamInfo, probe_file
from pyflows.subtitles import filter_subtitles

log = logging.getLogger(__name__)

# Codec to encoder mapping
VAAPI_ENCODERS = {"hevc": "hevc_vaapi", "av1": "av1_vaapi"}
CPU_ENCODERS = {"hevc": "libx265", "av1": "libsvtav1"}


class TrackTitle:
    @staticmethod
    def format(language: str, codec: str, channels: int) -> str:
        lang = LANGUAGE_NAMES.get(language, language.upper())
        ch = CHANNEL_NAMES.get(channels, f"{channels}ch")
        return f"{lang} / {codec.upper()} / {ch}"


def analyze_changes_detailed(
    probe: ProbeResult,
    profile: ProfileConfig,
    input_path: str,
) -> dict[str, list[str]]:
    """Return human-readable reasons describing which parts would change."""
    plan = plan_from_probe(input_path, probe, profile)
    grouped: dict[str, list[str]] = {
        "video": [],
        "audio": [],
        "subtitles": [],
        "container": [],
    }
    for reason in plan.reasons:
        grouped[reason.scope].append(reason.message)
    return grouped


def analyze_changes(probe: ProbeResult, profile: ProfileConfig, input_path: str) -> dict[str, bool]:
    """Return whether the current file would change under the configured profile."""
    detailed = analyze_changes_detailed(probe, profile, input_path)
    return {key: bool(value) for key, value in detailed.items()}


def should_skip(probe: ProbeResult, profile: ProfileConfig, input_path: str) -> bool:
    """Skip only when the full configured policy would make no changes."""
    return plan_from_probe(input_path, probe, profile).should_skip


def build_encode_command_from_plan(
    plan: FilePlan,
    profile: ProfileConfig,
    vaapi_device: str,
    use_cpu: bool = False,
    ffmpeg_path: str = "ffmpeg",
    hardware_config: HardwareConfig | None = None,
) -> FFmpegCommand:
    """Build the ffmpeg command from a FilePlan."""
    cmd = FFmpegCommand()
    cmd.set_ffmpeg_path(ffmpeg_path)

    use_vaapi = profile.video.encoder == "vaapi" and not use_cpu
    if hardware_config is not None:
        cmd.configure_hardware(hardware_config)
    if use_vaapi:
        cmd.set_vaapi_device(vaapi_device)
        if plan.source_probe.video:
            cmd.set_input_codec(plan.source_probe.video.codec)

    cmd.add_input(plan.output.input_path)

    # --- Video ---
    v_idx = cmd.map_stream("0:v:0")
    codec_name = profile.video.codec
    if use_vaapi:
        encoder = VAAPI_ENCODERS.get(codec_name, f"{codec_name}_vaapi")
        async_depth = hardware_config.vaapi.async_depth if hardware_config is not None else 4
        cmd.set_codec(v_idx, encoder, qp=profile.video.quality, async_depth=async_depth)
    else:
        encoder = CPU_ENCODERS.get(codec_name, f"lib{codec_name}")
        cmd.set_codec(v_idx, encoder, crf=profile.video.quality, preset="medium")
    cmd.set_metadata(v_idx, "title", "")

    # --- Audio ---
    for audio_item in plan.audio:
        assert audio_item.source_index is not None
        a_idx = cmd.map_stream(
            f"0:a:{_audio_input_index_by_source_index(plan.source_probe.audio, audio_item.source_index)}"
        )
        if audio_item.action == "copy":
            cmd.set_codec(a_idx, "copy")
        else:
            cmd.set_codec(
                a_idx,
                audio_item.target_codec,
                ac=audio_item.target_channels,
                b=f"{_default_audio_bitrate(profile, audio_item.language)}k",
            )
        cmd.set_metadata(a_idx, "title", audio_item.title_after)
        cmd.set_disposition(a_idx, "default" if audio_item.default else "0")

    # --- Subtitles ---
    for subtitle_item in plan.subtitles:
        assert subtitle_item.source_index is not None
        s_idx = cmd.map_stream(
            f"0:s:{_sub_input_index_by_source_index(plan.source_probe.subtitles, subtitle_item.source_index)}"
        )
        cmd.set_codec(s_idx, "copy")
        cmd.set_disposition(s_idx, "default" if subtitle_item.default else "0")

    cmd.set_output(plan.output.output_path)
    return cmd


def build_encode_command(
    input_path: str,
    output_path: str,
    probe: ProbeResult,
    profile: ProfileConfig,
    vaapi_device: str,
    use_cpu: bool = False,
    ffmpeg_path: str = "ffmpeg",
    hardware_config: HardwareConfig | None = None,
) -> FFmpegCommand:
    """Build the ffmpeg command from probe data and profile config."""
    plan = plan_from_probe(input_path, probe, profile)
    if output_path != plan.output.output_path:
        plan.output.output_path = output_path
    return build_encode_command_from_plan(
        plan,
        profile,
        vaapi_device,
        use_cpu=use_cpu,
        ffmpeg_path=ffmpeg_path,
        hardware_config=hardware_config,
    )


def _audio_input_index(all_audio: list[StreamInfo], stream: StreamInfo) -> int:
    """Get the audio-relative index (0:a:N) for a stream."""
    return _audio_input_index_by_source_index(all_audio, stream.index)


def _audio_input_index_by_source_index(all_audio: list[StreamInfo], source_index: int) -> int:
    """Get the audio-relative index (0:a:N) for a source stream index."""
    indices = [s.index for s in all_audio]
    try:
        return indices.index(source_index)
    except ValueError:
        raise ValueError(
            f"Audio stream index {source_index} not found in available audio streams: {indices}"
        ) from None


def _sub_input_index(all_subs: list[StreamInfo], stream: StreamInfo) -> int:
    """Get the subtitle-relative index (0:s:N) for a stream."""
    return _sub_input_index_by_source_index(all_subs, stream.index)


def _sub_input_index_by_source_index(all_subs: list[StreamInfo], source_index: int) -> int:
    """Get the subtitle-relative index (0:s:N) for a source stream index."""
    indices = [s.index for s in all_subs]
    try:
        return indices.index(source_index)
    except ValueError:
        raise ValueError(
            f"Subtitle stream index {source_index} not found in available subtitle streams: {indices}"
        ) from None


def _default_audio_bitrate(profile: ProfileConfig, language: str) -> int:
    return profile.audio.add_stereo.bitrate


def check_disk_space(temp_dir: str, input_size: int) -> bool:
    """Check that temp_dir has at least 2x input_size free."""
    stat = shutil.disk_usage(temp_dir)
    return stat.free >= input_size * 2


def verify_output(output_path: str) -> bool:
    """Verify the output file is valid using ffprobe."""
    if not Path(output_path).exists():
        return False
    if Path(output_path).stat().st_size == 0:
        return False
    try:
        probe_file(output_path)
        return True
    except subprocess.CalledProcessError:
        return False


def _container_suffix(profile: ProfileConfig) -> str:
    return container_suffix(profile)


def encode_file(
    input_path: str,
    profile: ProfileConfig,
    temp_dir: str,
    vaapi_device: str,
    ffmpeg_path: str = "ffmpeg",
    ffprobe_path: str = "ffprobe",
    hardware_config: HardwareConfig | None = None,
    stall_timeout: int = 300,
) -> tuple[bool, str, str]:
    """Run the full encode pipeline for a single file.

    Returns (success, error_message, final_output_path).
    """
    # Probe + plan
    try:
        plan = plan_file(input_path, profile, ffprobe_path=ffprobe_path)
    except subprocess.CalledProcessError as e:
        return False, f"ffprobe failed: {e}", input_path

    # Skip check
    if plan.should_skip:
        return True, "skipped", input_path

    # Disk space check
    input_size = Path(input_path).stat().st_size
    if not check_disk_space(temp_dir, input_size):
        return False, "Insufficient disk space", input_path

    # Build temp output path
    output_ext = _container_suffix(profile)
    output_path = str(Path(temp_dir) / f"{Path(input_path).stem}_{uuid.uuid4().hex[:8]}{output_ext}")

    # Try VAAPI first
    plan.output.output_path = output_path
    cmd = build_encode_command_from_plan(
        plan,
        profile,
        vaapi_device,
        use_cpu=False,
        ffmpeg_path=ffmpeg_path,
        hardware_config=hardware_config,
    )
    result = cmd.run(stall_timeout=stall_timeout)

    if result.returncode != 0:
        fallback_mode = profile.video.fallback.lower()
        if profile.video.encoder == "vaapi" and fallback_mode == "cpu":
            log_event(
                log,
                logging.WARNING,
                "vaapi_fallback",
                "VAAPI encode failed, trying CPU fallback",
                file_path=input_path,
            )
            # Clean failed output
            Path(output_path).unlink(missing_ok=True)

            # CPU fallback
            cmd = build_encode_command_from_plan(
                plan,
                profile,
                vaapi_device,
                use_cpu=True,
                ffmpeg_path=ffmpeg_path,
                hardware_config=hardware_config,
            )
            result = cmd.run(stall_timeout=stall_timeout)

            if result.returncode != 0:
                Path(output_path).unlink(missing_ok=True)
                return False, f"Both VAAPI and CPU encode failed: {result.stderr[-500:]}", input_path
        else:
            Path(output_path).unlink(missing_ok=True)
            return False, f"Encode failed: {result.stderr[-500:]}", input_path

    # Verify output
    if not verify_output(output_path):
        Path(output_path).unlink(missing_ok=True)
        return False, "Output validation failed", input_path

    final_path = output_path

    if profile.output.replace_original:
        target_path = str(Path(input_path).with_suffix(output_ext))
        if target_path != input_path and Path(target_path).exists():
            Path(output_path).unlink(missing_ok=True)
            return False, f"Target output path already exists: {target_path}", input_path

        target_dir = Path(target_path).parent
        tmp_path = str(target_dir / f".{Path(target_path).name}.tmp")
        try:
            shutil.copy2(output_path, tmp_path)
            os.replace(tmp_path, target_path)
        except OSError as exc:
            Path(tmp_path).unlink(missing_ok=True)
            Path(output_path).unlink(missing_ok=True)
            return False, f"Failed to replace original: {exc}", input_path
        except BaseException:
            Path(tmp_path).unlink(missing_ok=True)
            raise
        Path(output_path).unlink(missing_ok=True)
        if target_path != input_path and Path(input_path).exists():
            os.unlink(input_path)
        final_path = target_path
    else:
        log_event(log, logging.INFO, "output_written", "Output written without replacing original", file_path=output_path)

    return True, "", final_path
