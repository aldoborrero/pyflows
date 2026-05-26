# pyflows

Media library transcoder with VAAPI hardware encoding. Automatically scans, analyzes, and re-encodes your media library to a consistent format with configurable profiles.

## Features

- **VAAPI hardware encoding** — uses GPU acceleration for fast HEVC/H.265 transcoding with CPU fallback
- **Profile-based processing** — define separate profiles for TV, movies, anime, etc. with per-profile video, audio, and subtitle rules
- **Smart analysis** — checks codec, bitrate, audio tracks, and subtitles before deciding what to transcode
- **Audio normalization** — keeps preferred languages, removes commentary, adds stereo downmix tracks
- **Subtitle cleanup** — removes image-based formats (PGS, DVD), keeps text subs in configured languages
- **Webhook integration** — receives Sonarr/Radarr import notifications and processes new files automatically
- **Stall detection** — monitors FFmpeg progress in real-time, fails only when encoding actually stalls (no fixed timeouts)
- **SQLite tracking** — records encode history, avoids reprocessing, tracks failures
- **Notifications** — ntfy push notifications and Jellyfin library refresh on completion

## Installation

### Nix (recommended)

```bash
# Run directly
nix run github:aldoborrero/pyflows -- --help

# Or add as a flake input
{
  inputs.pyflows.url = "github:aldoborrero/pyflows";
}
```

### NixOS module

```nix
{
  imports = [ inputs.pyflows.nixosModules.pyflows ];

  services.pyflows = {
    enable = true;
    settings = {
      general = {
        mode = "daemon";
        vaapi_device = "/dev/dri/renderD128";
        workers = 1;
      };
      profiles.tv = {
        video = {
          codec = "hevc";
          encoder = "vaapi";
          quality = 22;
        };
        audio = {
          keep_languages = [ "eng" "spa" ];
          remove_commentary = true;
        };
      };
      libraries = [{
        path = "/mnt/media/tv";
        profile = "tv";
      }];
    };
  };
}
```

### Development

```bash
git clone https://github.com/aldoborrero/pyflows
cd pyflows
direnv allow   # or: nix develop
```

## Usage

```bash
# Run the daemon (scanner + webhook server)
pyflows run --config config.yaml

# Scan library and process pending files
pyflows scan --config config.yaml

# Check what would happen to a single file (dry run)
pyflows check /path/to/video.mkv --profile tv --config config.yaml

# Show the encoding plan for a file
pyflows plan /path/to/video.mkv --profile tv --config config.yaml

# Encode a single file
pyflows encode /path/to/video.mkv --profile tv --config config.yaml

# Show processing status
pyflows status --config config.yaml

# Show encode history
pyflows history --config config.yaml
```

## Configuration

pyflows uses a YAML config file. Example:

```yaml
general:
  mode: daemon          # "daemon" (watcher+scanner+webhook) or "webhook" (webhook only)
  temp_dir: /tmp/pyflows
  log_level: info
  workers: 1
  db_path: /var/lib/pyflows/pyflows.db
  vaapi_device: /dev/dri/renderD128
  settle_time: 60       # seconds to wait after file modification before processing

profiles:
  tv:
    video:
      codec: hevc
      bit_depth: 10
      encoder: vaapi      # "vaapi" or "cpu"
      quality: 22          # CRF value (lower = better quality, larger file)
      fallback: cpu        # fall back to CPU if VAAPI fails
      skip_codecs: [hevc]  # don't re-encode files already in these codecs
    audio:
      keep_languages: [eng, spa, jpn]
      default_language: eng
      priority: [eng, spa, jpn]
      remove_commentary: true
      add_stereo:
        codec: aac
        bitrate: 128
        channels: 2
        languages: [eng, spa, jpn]
      preserve_surround: true
    subtitles:
      keep_languages: [eng, spa, jpn]
      remove_formats: [pgs, dvd_subtitle, hdmv_pgs_subtitle]
      remove_commentary: true
    output:
      container: mkv
      replace_original: true

libraries:
  - path: /mnt/media/tv
    profile: tv
  - path: /mnt/media/anime
    profile: anime

notifications:
  ntfy:
    url: https://ntfy.example.com
    topic: pyflows
  jellyfin:
    url: http://jellyfin:8096
    api_key: ${JELLYFIN_API_KEY}    # environment variable expansion
```

Environment variables can be referenced as `${VAR_NAME}` anywhere in the config.

## Architecture

```
pyflows
├── scanner      — Walks library directories, finds files needing processing
├── probe        — FFprobe wrapper, extracts stream metadata
├── plan         — Analyzes streams, builds an encoding plan per profile rules
├── pipeline     — Orchestrates the encode: temp file → ffmpeg → replace original
├── ffmpeg       — Builds ffmpeg commands, monitors progress, detects stalls
├── audio        — Audio stream selection, stereo downmix logic
├── subtitles    — Subtitle filtering and cleanup
├── db           — SQLite encode history and state tracking
├── webhook      — HTTP server for Sonarr/Radarr import notifications
├── notify       — ntfy push notifications, Jellyfin library refresh
├── tasks        — Huey task queue for background processing
├── config       — Pydantic models, YAML loader with env var expansion
└── metrics      — Prometheus metrics endpoint
```

## License

MIT
