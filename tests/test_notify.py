# nix/packages/pyflows/tests/test_notify.py
"""Tests for notification dispatching."""

import urllib.error
from unittest.mock import patch, MagicMock
from pyflows.notify import Notifier
from pyflows.config import NtfyConfig, JellyfinConfig, NotificationsConfig, ArrNotifyConfig, ArrInstanceConfig


def test_ntfy_failure_notification():
    """ntfy is called on failure when configured."""
    config = NotificationsConfig(
        ntfy=NtfyConfig(url="https://ntfy.example.com/test", on_failure=True),
    )
    notifier = Notifier(config)
    with patch("pyflows.notify.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = MagicMock(status=200)
        notifier.on_failure("/media/test.mkv", "VAAPI failed")
        mock_urlopen.assert_called_once()
        req = mock_urlopen.call_args[0][0]
        assert "ntfy.example.com" in req.full_url
        assert b"test.mkv" in req.data


def test_jellyfin_success_notification():
    """Jellyfin refresh is called on success when configured."""
    config = NotificationsConfig(
        jellyfin=JellyfinConfig(url="http://localhost:8096", api_key="key123",
                                on_success=True, refresh_path=True),
    )
    notifier = Notifier(config)
    with patch("pyflows.notify.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = MagicMock(status=204)
        notifier.on_success("/media/movies/test.mkv")
        mock_urlopen.assert_called_once()


def test_no_notification_when_disabled():
    """No HTTP call when notifications are disabled."""
    config = NotificationsConfig()
    notifier = Notifier(config)
    with patch("pyflows.notify.urllib.request.urlopen") as mock_urlopen:
        notifier.on_failure("/media/test.mkv", "error")
        notifier.on_success("/media/test.mkv")
        mock_urlopen.assert_not_called()


def test_rescan_sonarr_sends_command():
    """Sonarr rescan sends RescanSeries command with correct seriesId."""
    config = NotificationsConfig(
        arr=ArrNotifyConfig(
            sonarr=[ArrInstanceConfig(url="http://sonarr:8989", api_key="key1")],
        ),
    )
    notifier = Notifier(config)
    with patch("pyflows.notify.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = MagicMock()
        notifier.on_success("/media/test.mkv", arr_source="sonarr", arr_id=42)
        mock_urlopen.assert_called_once()
        req = mock_urlopen.call_args[0][0]
        assert "sonarr:8989/api/v3/command" in req.full_url
        import json
        body = json.loads(req.data)
        assert body["name"] == "RescanSeries"
        assert body["seriesId"] == 42


def test_rescan_radarr_sends_command():
    """Radarr rescan sends RescanMovie command with correct movieId."""
    config = NotificationsConfig(
        arr=ArrNotifyConfig(
            radarr=[ArrInstanceConfig(url="http://radarr:7878", api_key="key1")],
        ),
    )
    notifier = Notifier(config)
    with patch("pyflows.notify.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = MagicMock()
        notifier.on_success("/media/test.mkv", arr_source="radarr", arr_id=42)
        mock_urlopen.assert_called_once()
        req = mock_urlopen.call_args[0][0]
        assert "radarr:7878/api/v3/command" in req.full_url
        import json
        body = json.loads(req.data)
        assert body["name"] == "RescanMovie"
        assert body["movieId"] == 42


def test_rescan_first_success_stops():
    """Only the first successful Sonarr instance is contacted."""
    config = NotificationsConfig(
        arr=ArrNotifyConfig(
            sonarr=[
                ArrInstanceConfig(url="http://sonarr1:8989", api_key="key1"),
                ArrInstanceConfig(url="http://sonarr2:8989", api_key="key2"),
            ],
        ),
    )
    notifier = Notifier(config)
    with patch("pyflows.notify.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = MagicMock()
        notifier.on_success("/media/test.mkv", arr_source="sonarr", arr_id=10)
        mock_urlopen.assert_called_once()


def test_rescan_first_failure_tries_second():
    """When the first instance fails, the second instance is tried."""
    config = NotificationsConfig(
        arr=ArrNotifyConfig(
            sonarr=[
                ArrInstanceConfig(url="http://sonarr1:8989", api_key="key1"),
                ArrInstanceConfig(url="http://sonarr2:8989", api_key="key2"),
            ],
        ),
    )
    notifier = Notifier(config)
    with patch("pyflows.notify.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.side_effect = [
            urllib.error.URLError("connection refused"),
            MagicMock(),
        ]
        notifier.on_success("/media/test.mkv", arr_source="sonarr", arr_id=10)
        assert mock_urlopen.call_count == 2


def test_rescan_unknown_source_ignored():
    """Unknown arr source is silently ignored with no HTTP calls."""
    config = NotificationsConfig(
        arr=ArrNotifyConfig(
            sonarr=[ArrInstanceConfig(url="http://sonarr:8989", api_key="key1")],
        ),
    )
    notifier = Notifier(config)
    with patch("pyflows.notify.urllib.request.urlopen") as mock_urlopen:
        notifier.on_success("/media/test.mkv", arr_source="lidarr", arr_id=42)
        mock_urlopen.assert_not_called()
