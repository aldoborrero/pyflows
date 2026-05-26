# nix/packages/pyflows/tests/test_notify.py
"""Tests for notification dispatching."""

from unittest.mock import patch, MagicMock
from pyflows.notify import Notifier
from pyflows.config import NtfyConfig, JellyfinConfig, NotificationsConfig


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
