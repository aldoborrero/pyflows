"""ffprobe wrapper for analyzing media file streams."""

import json
import subprocess
from dataclasses import dataclass, field


@dataclass
class StreamInfo:
    index: int
    codec_type: str
    codec: str
    language: str = ""
    title: str = ""
    channels: int = 0
    width: int = 0
    height: int = 0
    is_default: bool = False


@dataclass
class ProbeResult:
    video: StreamInfo | None = None
    audio: list[StreamInfo] = field(default_factory=list)
    subtitles: list[StreamInfo] = field(default_factory=list)


def parse_probe_output(raw_json: str) -> ProbeResult:
    """Parse ffprobe JSON output into a ProbeResult."""
    data = json.loads(raw_json)
    result = ProbeResult()

    for stream in data.get("streams", []):
        tags = stream.get("tags", {})
        disposition = stream.get("disposition", {})
        info = StreamInfo(
            index=stream["index"],
            codec_type=stream["codec_type"],
            codec=stream.get("codec_name", ""),
            language=tags.get("language", ""),
            title=tags.get("title", ""),
            channels=stream.get("channels", 0),
            width=stream.get("width", 0),
            height=stream.get("height", 0),
            is_default=bool(disposition.get("default", 0)),
        )

        if info.codec_type == "video" and result.video is None:
            result.video = info
        elif info.codec_type == "audio":
            result.audio.append(info)
        elif info.codec_type == "subtitle":
            result.subtitles.append(info)

    return result


def probe_file(path: str, ffprobe_path: str = "ffprobe") -> ProbeResult:
    """Run ffprobe on a file and return parsed results."""
    cmd = [
        ffprobe_path, "-v", "quiet",
        "-print_format", "json",
        "-show_streams",
        path,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return parse_probe_output(proc.stdout)
