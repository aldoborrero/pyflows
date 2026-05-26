"""Configuration models and YAML loader for pyflows."""

import os
import re
from pathlib import Path

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, Field, model_validator


type ConfigValue = str | dict[str, "ConfigValue"] | list["ConfigValue"] | int | float | bool | None


def expand_env_vars(value: ConfigValue) -> ConfigValue:
    """Recursively expand ${VAR} references from environment."""
    if isinstance(value, str):
        def replace_env(match: re.Match[str]) -> str:
            return os.environ.get(match.group(1), match.group(0))
        return re.sub(r"\$\{([^}]+)\}", replace_env, value)
    elif isinstance(value, dict):
        return {k: expand_env_vars(v) for k, v in value.items()}
    elif isinstance(value, list):
        return [expand_env_vars(item) for item in value]
    return value


# --- Pydantic Models ---

from typing import Literal

DaemonMode = Literal["daemon", "webhook"]


class GeneralConfig(BaseModel):
    mode: DaemonMode = "daemon"
    temp_dir: str
    log_level: str = "info"
    log_output: str = "stdout"
    log_format: str = "text"
    workers: int = 1
    db_path: str
    vaapi_device: str = "/dev/dri/renderD128"
    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"
    settle_time: int = 60
    watcher_event_debounce_seconds: int = 5
    stable_for_seconds: int = 30
    ignore_suffixes: list[str] = [".part", ".tmp", ".partial", ".!qB"]
    max_retries: int = 3
    retry_backoff_seconds: int = 300
    stall_timeout: int = 300  # Kill ffmpeg if no progress for this many seconds
    metrics_port: int = 9327


class VideoConfig(BaseModel):
    codec: str  # hevc, av1
    bit_depth: int = 10
    encoder: str = "vaapi"
    quality: int = 22
    fallback: str = "cpu"
    skip_codecs: list[str] = []


class StereoConfig(BaseModel):
    codec: str = "aac"
    bitrate: int = 128
    channels: int = 2
    languages: list[str] = []


class AudioConfig(BaseModel):
    keep_languages: list[str] = Field(default_factory=list)
    default_language: str = "eng"
    priority: list[str] = Field(default_factory=list)
    remove_commentary: bool = True
    add_stereo: StereoConfig = Field(default_factory=StereoConfig)
    preserve_surround: bool = True


class SubtitleConfig(BaseModel):
    keep_languages: list[str] = Field(default_factory=list)
    default_language: str = ""
    remove_formats: list[str] = Field(default_factory=list)
    remove_commentary: bool = True


class OutputConfig(BaseModel):
    container: str = "mkv"
    replace_original: bool = True


class ProfileConfig(BaseModel):
    video: VideoConfig
    audio: AudioConfig
    subtitles: SubtitleConfig
    output: OutputConfig


class LibraryConfig(BaseModel):
    name: str
    path: str
    profile: str
    scan_interval: int = 3600
    extensions: list[str] = ["mkv", "mp4", "avi"]


class WebhookConfig(BaseModel):
    enabled: bool = False
    port: int = 9328
    path_mappings: dict[str, str] = Field(default_factory=dict)


class ArrInstanceConfig(BaseModel):
    url: str
    api_key: str


class ArrNotifyConfig(BaseModel):
    sonarr: list[ArrInstanceConfig] = Field(default_factory=list)
    radarr: list[ArrInstanceConfig] = Field(default_factory=list)


class NtfyConfig(BaseModel):
    url: str
    on_failure: bool = True
    on_success: bool = False


class JellyfinConfig(BaseModel):
    url: str = ""
    api_key: str = ""
    on_success: bool = False
    refresh_path: bool = True


class NotificationsConfig(BaseModel):
    ntfy: NtfyConfig | None = None
    jellyfin: JellyfinConfig | None = None
    arr: ArrNotifyConfig | None = None


class VaapiConfig(BaseModel):
    device: str = "va"
    hw_decode_codecs: list[str] = Field(default_factory=lambda: ["hevc", "av1", "vp9"])
    sw_decode_codecs: list[str] = Field(default_factory=lambda: ["h264"])
    upload_filter: str = "format=nv12,hwupload_vaapi"
    async_depth: int = 4
    use_hw_encode: bool = True


class HardwareConfig(BaseModel):
    acceleration: str = "vaapi"
    env: dict[str, str] = Field(default_factory=lambda: {"AMD_DEBUG": "noefc"})
    vaapi: VaapiConfig = Field(default_factory=VaapiConfig)


class QueueConfig(BaseModel):
    priority_codecs: list[str] = Field(default_factory=list)


class PyflowsConfig(BaseModel):
    general: GeneralConfig
    profiles: dict[str, ProfileConfig]
    libraries: list[LibraryConfig] = Field(default_factory=list)
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)
    hardware: HardwareConfig = Field(default_factory=HardwareConfig)
    queue: QueueConfig = Field(default_factory=QueueConfig)
    webhook: WebhookConfig | None = None

    def resolved_priority_codecs(self) -> list[str]:
        """Return queue priority codecs, falling back to hw decode codecs."""
        return self.queue.priority_codecs or self.hardware.vaapi.hw_decode_codecs

    @model_validator(mode="after")
    def _validate_library_profiles(self) -> "PyflowsConfig":
        for lib in self.libraries:
            if lib.profile not in self.profiles:
                raise ValueError(
                    f"Library '{lib.name}' references profile '{lib.profile}' "
                    f"which does not exist. Available profiles: "
                    f"{list(self.profiles.keys())}"
                )
        return self


def load_config(path: Path) -> PyflowsConfig:
    """Load and validate config from a YAML file."""
    with open(path) as f:
        raw: ConfigValue = yaml.safe_load(f)
    raw = expand_env_vars(raw)
    return PyflowsConfig.model_validate(raw)
