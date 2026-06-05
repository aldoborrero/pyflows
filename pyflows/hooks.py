"""Pre/post-encode shell hook execution."""

import logging
import os
import subprocess

from pyflows.logging_utils import log_event

log = logging.getLogger(__name__)


def run_hooks(
    commands: list[str],
    phase: str,
    file_path: str,
    profile: str = "",
    output_path: str = "",
    status: str = "",
    error: str = "",
    timeout: int = 300,
) -> bool:
    if not commands:
        return True
    env = {
        **os.environ,
        "PYFLOWS_FILE_PATH": file_path,
        "PYFLOWS_PROFILE": profile,
        "PYFLOWS_OUTPUT_PATH": output_path,
        "PYFLOWS_STATUS": status,
        "PYFLOWS_ERROR": error,
        "PYFLOWS_PHASE": phase,
    }
    for cmd in commands:
        try:
            subprocess.run(cmd, shell=True, env=env, check=True, timeout=timeout)
        except subprocess.CalledProcessError as exc:
            log_event(log, logging.WARNING, "hook_failed",
                      f"{phase} hook failed", command=cmd, returncode=exc.returncode)
            return False
        except subprocess.TimeoutExpired:
            log_event(log, logging.WARNING, "hook_timeout",
                      f"{phase} hook timed out", command=cmd)
            return False
    return True
