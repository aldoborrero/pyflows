#!/usr/bin/env python3
"""
pyflows Transcode — an Unmanic plugin.

A thin adapter that reuses pyflows' own planning and ffmpeg command builder, so
Unmanic produces *exactly* the same output as the pyflows daemon: VAAPI HEVC
(with CPU fallback), language keep/drop, AAC stereo downmix, commentary removal,
stream reorder, and image-subtitle removal — all driven by the same pyflows
config and the same code (`pyflows.plan` / `pyflows.pipeline`).

Per file it: picks the matching profile (by which configured pyflows *library*
the file lives under), runs pyflows' `plan_file()` to decide skip-vs-process,
and on the worker stage hands Unmanic the exact ffmpeg command from
`pyflows.build_encode_command().build()`.

Configuration (environment variables):
  PYFLOWS_UNMANIC_CONFIG  Path to the pyflows ``config.yaml`` (default:
                          ``config.yaml`` next to this plugin). The config's
                          ``libraries`` (path + profile) drive per-file profile
                          selection, so each library ``path`` must match what
                          Unmanic sees on disk.
  PYFLOWS_FFMPEG          ffmpeg binary  (default: jellyfin-ffmpeg).
  PYFLOWS_FFPROBE         ffprobe binary (default: jellyfin-ffmpeg).

Dependency: the ``pyflows`` package must be importable in Unmanic's Python —
``pip install`` it into the Unmanic environment, or drop it into a sibling
``site-packages/`` directory (added to ``sys.path`` automatically if present).
"""

import logging
import os
import shlex
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
# Optional bundled deps (e.g. a pinned pyflows) — used only if present.
_SITE = os.path.join(_HERE, "site-packages")
if os.path.isdir(_SITE) and _SITE not in sys.path:
    sys.path.insert(0, _SITE)

from unmanic.libs.unplugins.settings import PluginSettings  # noqa: E402

from pyflows.config import load_config  # noqa: E402
from pyflows.pipeline import build_encode_command  # noqa: E402
from pyflows.plan import plan_file  # noqa: E402
from pyflows.probe import probe_file  # noqa: E402

logger = logging.getLogger("Unmanic.Plugin.pyflows_transcode")

CONFIG_PATH = os.environ.get("PYFLOWS_UNMANIC_CONFIG") or os.path.join(_HERE, "config.yaml")
FFMPEG = os.environ.get("PYFLOWS_FFMPEG", "/usr/lib/jellyfin-ffmpeg/ffmpeg")
FFPROBE = os.environ.get("PYFLOWS_FFPROBE", "/usr/lib/jellyfin-ffmpeg/ffprobe")

# Only ever act on real video files. Without this the Unmanic scanner queues
# every sidecar/artwork file (.nfo/.jpg/.srt/...), wasting effort and failing.
VIDEO_EXTENSIONS = {
    ".mkv", ".mp4", ".avi", ".m4v", ".mov", ".ts", ".m2ts",
    ".wmv", ".flv", ".webm", ".mpg", ".mpeg", ".mpv", ".3gp",
}

_CFG = None


class Settings(PluginSettings):
    settings = {}


def _is_video(path):
    return os.path.splitext(path or "")[1].lower() in VIDEO_EXTENSIONS


def _cfg():
    global _CFG
    if _CFG is None:
        _CFG = load_config(CONFIG_PATH)
    return _CFG


def _profile_for_path(cfg, path):
    """Pick the pyflows profile for a file by the configured library it lives
    under (longest matching ``library.path`` wins)."""
    p = path or ""
    best = None
    for lib in cfg.libraries:
        base = lib.path.rstrip("/")
        if p == base or p.startswith(base + "/"):
            if best is None or len(base) > len(best.path.rstrip("/")):
                best = lib
    if best is None:
        return None, None
    return cfg.profiles.get(best.profile), best.profile


def on_library_management_file_test(data):
    abspath = data.get("path")
    if not _is_video(abspath):
        return data
    try:
        cfg = _cfg()
        profile, name = _profile_for_path(cfg, abspath)
        if profile is None:
            return data
        plan = plan_file(abspath, profile, ffprobe_path=FFPROBE)
        if not plan.should_skip:
            data["add_file_to_pending_tasks"] = True
            logger.info("Queue '%s' (profile=%s): %s", abspath, name,
                        "; ".join(r.message for r in plan.reasons[:4]))
    except Exception:
        logger.exception("file_test failed for '%s'", abspath)
    return data


def on_worker_process(data):
    data["exec_command"] = []
    data["repeat"] = False

    file_in = data.get("file_in")
    file_out = data.get("file_out")
    original = data.get("original_file_path") or file_in
    if not _is_video(original):
        return data
    try:
        cfg = _cfg()
        profile, name = _profile_for_path(cfg, original)
        if profile is None:
            return data

        probe = probe_file(file_in, ffprobe_path=FFPROBE)
        plan = plan_file(file_in, profile, ffprobe_path=FFPROBE)
        if plan.should_skip:
            return data

        # pyflows applies VAAPI env (e.g. AMD_DEBUG=noefc) to the process;
        # Unmanic runs the command directly, so prepend `env VAR=VAL ...`.
        env_prefix = ["/usr/bin/env"] + [f"{k}={v}" for k, v in cfg.hardware.env.items()]

        def build(use_cpu):
            cmd = build_encode_command(
                file_in, file_out, probe, profile,
                vaapi_device=cfg.general.vaapi_device,
                use_cpu=use_cpu, ffmpeg_path=FFMPEG,
                hardware_config=cfg.hardware,
            )
            return env_prefix + cmd.build()

        vaapi_cmd = build(use_cpu=False)
        if profile.video.encoder == "vaapi" and profile.video.fallback.lower() == "cpu":
            cpu_cmd = build(use_cpu=True)

            def q(args):
                return " ".join(shlex.quote(a) for a in args)

            # VAAPI first; on non-zero exit fall back to CPU (libx265). Both
            # write file_out with -y, so a partial VAAPI output is overwritten.
            shell = "%s || { echo 'pyflows: VAAPI encode failed, falling back to CPU' >&2; %s ; }" % (
                q(vaapi_cmd), q(cpu_cmd))
            data["exec_command"] = ["/bin/sh", "-c", shell]
        else:
            data["exec_command"] = vaapi_cmd
        logger.info("Encoding '%s' (profile=%s)", original, name)
    except Exception:
        logger.exception("worker_process failed for '%s'", original)
        data["exec_command"] = []
    return data
