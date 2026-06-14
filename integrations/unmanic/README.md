# pyflows Transcode — Unmanic plugin

A thin [Unmanic](https://github.com/Unmanic/unmanic) plugin that drives the
**pyflows** transcode engine, so Unmanic's scanner/queue/worker produce the
exact same output as the pyflows daemon. It reuses pyflows' own
`plan_file()` and `build_encode_command()` — no duplicated logic.

Per file it:
1. picks the pyflows **profile** by which configured library the file lives under,
2. runs `plan_file()` to skip already-compliant files (`on_library_management_file_test`),
3. emits pyflows' exact ffmpeg command, with **VAAPI → CPU fallback** (`on_worker_process`).

Non-video files (`.nfo/.jpg/.srt/...`) are ignored.

## Install

1. Copy this directory into Unmanic's plugins dir as `pyflows_transcode`:
   ```
   <unmanic-config>/plugins/pyflows_transcode/
   ```
2. Make `pyflows` importable in Unmanic's Python — either:
   - `pip install <pyflows>` into the Unmanic environment, **or**
   - `pip install --target plugins/pyflows_transcode/site-packages <pyflows>`
     (the plugin adds a sibling `site-packages/` to `sys.path` if present).
3. Provide a pyflows `config.yaml`. By default the plugin reads
   `config.yaml` next to `plugin.py`; override with the env var
   `PYFLOWS_UNMANIC_CONFIG=/path/to/config.yaml`. Start from
   [`config.example.yaml`](config.example.yaml).
   **The library `path`s must match what Unmanic sees on disk.**
4. Register the plugin and add it to each library's flow under both
   **Library File Tests** and **Worker** stages (or insert
   `library_management.file_test` + `worker.process` rows into
   `librarypluginflow`).

Optional env: `PYFLOWS_FFMPEG`, `PYFLOWS_FFPROBE` (default: jellyfin-ffmpeg).

## Operational notes

- **Replace-original** is handled by Unmanic's post-processor (output container
  is mkv); the plugin writes to `file_out` and lets Unmanic move it back.
- **Cache:** point Unmanic's `cache_path` at the same disk as the media — a full
  HEVC temp is several GB and will fill a small root disk.
- **Shared GPU:** keep the worker count low (1–2). Unmanic ties concurrency to
  worker count, so that throttles concurrent VAAPI encodes.
- **Audio safety:** pyflows never drops *all* audio — if no track matches a
  profile's `keep_languages` (e.g. untagged `und` audio), it keeps the original
  tracks rather than emitting a video-only file.

## Not handled here

Sonarr/Radarr **rescan + rename** after transcode (so the *arr filenames /
mediainfo match the new codec) is a separate concern — wire it as an Unmanic
post-processor or via the *arr APIs. See the project docs.
