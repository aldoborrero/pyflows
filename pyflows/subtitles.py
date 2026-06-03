# nix/packages/pyflows/pyflows/subtitles.py
"""Subtitle stream filtering by language, format, and title."""

from pyflows.config import SubtitleConfig
from pyflows.constants import COMMENTARY_PATTERN
from pyflows.probe import StreamInfo

# Map short format names to ffprobe codec names
FORMAT_ALIASES = {
    "pgs": {"hdmv_pgs_subtitle"},
    "dvd_subtitle": {"dvd_subtitle"},
    "hdmv_pgs_subtitle": {"hdmv_pgs_subtitle"},
}


def filter_subtitles(streams: list[StreamInfo], config: SubtitleConfig) -> list[StreamInfo]:
    """Filter subtitle streams according to config rules.

    Order: remove banned formats → keep matching languages → remove commentary.
    """
    # Expand format aliases
    banned_codecs: set[str] = set()
    for fmt in config.remove_formats:
        banned_codecs.update(FORMAT_ALIASES.get(fmt, {fmt}))

    result = []
    for s in streams:
        # Remove banned formats
        if s.codec in banned_codecs:
            continue
        # Keep only matching languages
        if config.keep_languages and s.language not in config.keep_languages:
            continue
        # Remove commentary
        if config.remove_commentary and COMMENTARY_PATTERN.search(s.title):
            continue
        result.append(s)

    return result
