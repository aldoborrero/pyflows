"""Planning models and logic for pyflows."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pyflows.audio import AudioAction, build_audio_plan
from pyflows.config import ProfileConfig
from pyflows.probe import ProbeResult, StreamInfo, probe_file
from pyflows.subtitles import filter_subtitles

PlanStatus = Literal["compliant", "changes_required", "unsupported"]
PlanScope = Literal["video", "audio", "subtitles", "container", "general"]
ReasonCode = Literal[
    "video_codec_mismatch",
    "container_mismatch",
    "audio_track_count_changed",
    "audio_track_changed",
    "subtitle_track_count_changed",
    "subtitle_track_changed",
    "no_video_stream",
]

LANGUAGE_NAMES = {
    "eng": "English",
    "spa": "Spanish",
    "jpn": "Japanese",
    "fre": "French",
    "ger": "German",
    "ita": "Italian",
    "por": "Portuguese",
    "chi": "Chinese",
    "kor": "Korean",
    "ara": "Arabic",
    "rus": "Russian",
    "dut": "Dutch",
}

CHANNEL_NAMES = {1: "Mono", 2: "Stereo", 6: "5.1", 8: "7.1"}


@dataclass
class PlanReason:
    code: ReasonCode
    message: str
    scope: PlanScope


@dataclass
class VideoPlan:
    source_index: int | None
    source_codec: str | None
    target_codec: str
    action: Literal["copy", "encode", "unsupported"]


@dataclass
class AudioTrackPlan:
    source_index: int | None
    language: str
    action: Literal["copy", "encode", "drop", "generate"]
    source_codec: str | None
    target_codec: str
    source_channels: int | None
    target_channels: int
    title_before: str | None
    title_after: str
    default: bool


@dataclass
class SubtitleTrackPlan:
    source_index: int | None
    language: str
    action: Literal["copy", "drop"]
    source_codec: str | None
    target_codec: str | None
    title_before: str | None
    title_after: str | None
    default: bool


@dataclass
class OutputPlan:
    input_path: str
    output_path: str
    replace_original: bool
    source_container: str | None
    target_container: str


@dataclass
class FilePlan:
    status: PlanStatus
    compliant: bool
    should_skip: bool
    source_probe: ProbeResult
    output: OutputPlan
    video: VideoPlan
    audio: list[AudioTrackPlan] = field(default_factory=list)
    subtitles: list[SubtitleTrackPlan] = field(default_factory=list)
    reasons: list[PlanReason] = field(default_factory=list)


def container_suffix(profile: ProfileConfig) -> str:
    container = profile.output.container.strip().lstrip(".")
    return f".{container}" if container else ".mkv"


def select_default_audio_pos(audio_plan: list[AudioAction], default_language: str) -> int | None:
    if not audio_plan:
        return None
    default_pos = next(
        (i for i, action in enumerate(audio_plan) if action.stream.language == default_language),
        None,
    )
    return 0 if default_pos is None else default_pos


def select_default_subtitle_pos(subtitles: list[StreamInfo], default_language: str) -> int | None:
    if not subtitles:
        return None
    if default_language:
        default_pos = next(
            (i for i, sub in enumerate(subtitles) if sub.language == default_language),
            None,
        )
        if default_pos is not None:
            return default_pos
    return 0


def _track_title(language: str, codec: str, channels: int) -> str:
    lang = LANGUAGE_NAMES.get(language, language.upper())
    ch = CHANNEL_NAMES.get(channels, f"{channels}ch")
    return f"{lang} / {codec.upper()} / {ch}"


def plan_from_probe(input_path: str, probe: ProbeResult, profile: ProfileConfig) -> FilePlan:
    output_ext = container_suffix(profile)
    output_path = str(Path(input_path).with_suffix(output_ext)) if profile.output.replace_original else f"<temp>{output_ext}"
    output = OutputPlan(
        input_path=input_path,
        output_path=output_path,
        replace_original=profile.output.replace_original,
        source_container=Path(input_path).suffix.lstrip(".") or None,
        target_container=profile.output.container,
    )

    if probe.video is None:
        return FilePlan(
            status="unsupported",
            compliant=False,
            should_skip=True,
            source_probe=probe,
            output=output,
            video=VideoPlan(source_index=None, source_codec=None, target_codec=profile.video.codec, action="unsupported"),
            reasons=[PlanReason(code="no_video_stream", message="no video stream present", scope="video")],
        )

    audio_plan = build_audio_plan(probe.audio, profile.audio)
    kept_subs = filter_subtitles(probe.subtitles, profile.subtitles)
    default_audio_pos = select_default_audio_pos(audio_plan, profile.audio.default_language)
    default_sub_pos = select_default_subtitle_pos(kept_subs, profile.subtitles.default_language)

    reasons: list[PlanReason] = []

    video_action: Literal["copy", "encode", "unsupported"] = "copy"
    if probe.video.codec not in profile.video.skip_codecs:
        video_action = "encode"
        reasons.append(
            PlanReason(
                code="video_codec_mismatch",
                message=f"video codec {probe.video.codec} not in skip_codecs {profile.video.skip_codecs}",
                scope="video",
            )
        )

    if Path(input_path).suffix.lower() != output_ext.lower():
        reasons.append(
            PlanReason(
                code="container_mismatch",
                message=f"extension {Path(input_path).suffix or '<none>'} differs from target {output_ext}",
                scope="container",
            )
        )

    audio_items: list[AudioTrackPlan] = []
    audio_by_index = {s.index: s for s in probe.audio}
    if len(probe.audio) != len(audio_plan):
        reasons.append(
            PlanReason(
                code="audio_track_count_changed",
                message=f"audio track count would change from {len(probe.audio)} to {len(audio_plan)}",
                scope="audio",
            )
        )
    for i, action in enumerate(audio_plan):
        expected_codec = action.stream.codec if action.action == "copy" else action.codec
        expected_channels = action.stream.channels if action.action == "copy" else action.channels
        expected_title = _track_title(action.stream.language, expected_codec, expected_channels)
        current = audio_by_index.get(action.stream.index)
        default = i == default_audio_pos
        if action.action == "copy":
            action_name: Literal["copy", "encode", "drop", "generate"] = "copy"
        elif action.action == "encode":
            action_name = "encode"
        else:
            action_name = "generate"
        audio_items.append(
            AudioTrackPlan(
                source_index=action.stream.index,
                language=action.stream.language,
                action=action_name,
                source_codec=current.codec if current else None,
                target_codec=expected_codec,
                source_channels=current.channels if current else None,
                target_channels=expected_channels,
                title_before=current.title if current else None,
                title_after=expected_title,
                default=default,
            )
        )
        if current is not None and (
            current.language != action.stream.language
            or current.codec != expected_codec
            or current.channels != expected_channels
            or current.title != expected_title
            or current.is_default != default
        ):
            reasons.append(
                PlanReason(
                    code="audio_track_changed",
                    message=f"audio track {i} would be updated to {expected_title}{' [default]' if default else ''}",
                    scope="audio",
                )
            )

    subtitle_items: list[SubtitleTrackPlan] = []
    sub_by_index = {s.index: s for s in probe.subtitles}
    if len(probe.subtitles) != len(kept_subs):
        reasons.append(
            PlanReason(
                code="subtitle_track_count_changed",
                message=f"subtitle track count would change from {len(probe.subtitles)} to {len(kept_subs)}",
                scope="subtitles",
            )
        )
    for i, sub in enumerate(kept_subs):
        current = sub_by_index.get(sub.index)
        default = i == default_sub_pos
        subtitle_items.append(
            SubtitleTrackPlan(
                source_index=sub.index,
                language=sub.language,
                action="copy",
                source_codec=current.codec if current else None,
                target_codec=sub.codec,
                title_before=current.title if current else None,
                title_after=sub.title,
                default=default,
            )
        )
        if current is not None and (
            current.language != sub.language
            or current.codec != sub.codec
            or current.title != sub.title
            or current.is_default != default
        ):
            reasons.append(
                PlanReason(
                    code="subtitle_track_changed",
                    message=f"subtitle track {i} would be updated to {sub.language or 'unknown'} / {sub.codec.upper()}{' [default]' if default else ''}",
                    scope="subtitles",
                )
            )

    compliant = len(reasons) == 0
    return FilePlan(
        status="compliant" if compliant else "changes_required",
        compliant=compliant,
        should_skip=compliant,
        source_probe=probe,
        output=output,
        video=VideoPlan(source_index=probe.video.index, source_codec=probe.video.codec, target_codec=profile.video.codec, action=video_action),
        audio=audio_items,
        subtitles=subtitle_items,
        reasons=reasons,
    )


def plan_file(input_path: str, profile: ProfileConfig, ffprobe_path: str = "ffprobe") -> FilePlan:
    return plan_from_probe(input_path, probe_file(input_path, ffprobe_path=ffprobe_path), profile)
