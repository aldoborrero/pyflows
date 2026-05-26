# nix/packages/pyflows/pyflows/notify.py
"""Notification dispatching for ntfy, Jellyfin, and Sonarr/Radarr."""

import json
import logging
import urllib.error
import urllib.request
import urllib.parse
from pathlib import Path

from pyflows.config import NotificationsConfig
from pyflows.logging_utils import log_event

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, config: NotificationsConfig):
        self.config = config

    def on_failure(self, file_path: str, error: str) -> None:
        if self.config.ntfy and self.config.ntfy.on_failure:
            self._send_ntfy(
                title="pyflows - Encode Failed",
                body=f"Failed to process: {Path(file_path).name}\nError: {error}",
                priority="high",
                tags="warning",
            )

    def on_success(self, file_path: str, arr_source: str | None = None, arr_id: int | None = None) -> None:
        if self.config.ntfy and self.config.ntfy.on_success:
            self._send_ntfy(
                title="pyflows - Encode Complete",
                body=f"Processed: {Path(file_path).name}",
                priority="low",
                tags="white_check_mark",
            )
        if self.config.jellyfin and self.config.jellyfin.on_success:
            self._refresh_jellyfin(file_path)
        if arr_source and arr_id and self.config.arr:
            self._rescan_arr(arr_source, arr_id)

    def _send_ntfy(self, title: str, body: str, priority: str = "default", tags: str = "") -> None:
        ntfy = self.config.ntfy
        assert ntfy is not None
        try:
            req = urllib.request.Request(
                ntfy.url,
                data=body.encode(),
                headers={
                    "Title": title,
                    "Priority": priority,
                    "Tags": tags,
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10):
                pass
        except (urllib.error.URLError, OSError) as e:
            log_event(log, logging.WARNING, "ntfy_failed", "ntfy notification failed", reason=str(e), url=ntfy.url)

    def _refresh_jellyfin(self, file_path: str) -> None:
        jf = self.config.jellyfin
        assert jf is not None
        try:
            url = f"{jf.url}/Library/Refresh"
            if jf.refresh_path:
                parent = str(Path(file_path).parent)
                url += f"?path={urllib.parse.quote(parent)}"
            req = urllib.request.Request(
                url,
                headers={"X-Emby-Token": jf.api_key},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10):
                pass
        except (urllib.error.URLError, OSError) as e:
            log_event(log, logging.WARNING, "jellyfin_refresh_failed", "Jellyfin notification failed", reason=str(e), url=jf.url)

    def _rescan_arr(self, source: str, arr_id: int) -> None:
        """Tell Sonarr/Radarr to rescan so it picks up the new file metadata."""
        arr_config = self.config.arr
        if arr_config is None:
            return

        if source == "sonarr":
            instances = arr_config.sonarr
            command = {"name": "RescanSeries", "seriesId": arr_id}
            api_version = "v3"
        elif source == "radarr":
            instances = arr_config.radarr
            command = {"name": "RescanMovie", "movieId": arr_id}
            api_version = "v3"
        else:
            return

        for instance in instances:
            try:
                url = f"{instance.url}/api/{api_version}/command"
                data = json.dumps(command).encode()
                req = urllib.request.Request(
                    url,
                    data=data,
                    headers={
                        "X-Api-Key": instance.api_key,
                        "Content-Type": "application/json",
                    },
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=10):
                    pass
                log_event(log, logging.INFO, "arr_rescan_sent",
                          "Sent rescan command to arr",
                          source=source, arr_id=arr_id, url=instance.url)
                return  # Only need to hit one matching instance
            except (urllib.error.URLError, OSError) as e:
                log_event(log, logging.WARNING, "arr_rescan_failed",
                          "Failed to send rescan to arr",
                          source=source, arr_id=arr_id, url=instance.url, reason=str(e))
