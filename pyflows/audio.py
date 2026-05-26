# nix/packages/pyflows/pyflows/audio.py
"""Audio track selection, filtering, and stereo track creation."""

import re
from dataclasses import dataclass

from pyflows.config import AudioConfig
from pyflows.probe import StreamInfo

COMMENTARY_PATTERN = re.compile(r"commentary|director|description", re.IGNORECASE)


@dataclass
class AudioAction:
    stream: StreamInfo
    action: str  # "copy" or "encode"
    codec: str = ""
    channels: int = 0
    bitrate: int = 0
    source_index: int = 0  # input stream index for encode source


def build_audio_plan(streams: list[StreamInfo], config: AudioConfig) -> list[AudioAction]:
    """Build an ordered list of audio actions from probe data and config.

    Returns a list of AudioAction describing each output audio track:
    copy (passthrough original) or encode (new AAC stereo).
    """
    # 1. Filter to keep_languages
    kept = [s for s in streams if s.language in config.keep_languages]

    # 2. Remove commentary
    if config.remove_commentary:
        kept = [s for s in kept if not COMMENTARY_PATTERN.search(s.title)]

    # 3. Sort by priority
    priority_map = {lang: i for i, lang in enumerate(config.priority)}
    kept.sort(key=lambda s: (priority_map.get(s.language, 999), s.index))

    # 4. Build actions: for each language group, original then stereo copy
    actions: list[AudioAction] = []
    stereo_languages = set(config.add_stereo.languages)

    stereo_present_by_language = {
        language
        for language in stereo_languages
        if any(s.language == language and s.channels <= config.add_stereo.channels for s in kept)
    }

    for stream in kept:
        keep_original = stream.channels <= 2 or config.preserve_surround
        if keep_original:
            actions.append(AudioAction(stream=stream, action="copy"))

        # Add stereo if surround and language matches, unless one is already present
        if (
            stream.channels > config.add_stereo.channels
            and stream.language in stereo_languages
            and stream.language not in stereo_present_by_language
        ):
            actions.append(AudioAction(
                stream=stream,
                action="encode",
                codec=config.add_stereo.codec,
                channels=config.add_stereo.channels,
                bitrate=config.add_stereo.bitrate,
                source_index=stream.index,
            ))
            stereo_present_by_language.add(stream.language)

    return actions
