"""Webhook HTTP server for Sonarr/Radarr import notifications."""

import json
import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Callable

from pyflows.config import PyflowsConfig, WebhookConfig
from pyflows.db import FileDB
from pyflows.logging_utils import log_event

log = logging.getLogger(__name__)

# Webhook JSON payloads are nested dicts with mixed value types
type WebhookPayload = dict[str, object]
type JsonResponse = dict[str, str | int | None]


def _map_path(path: str, mappings: dict[str, str]) -> str:
    """Translate an arr file path to the local pyflows path using prefix mappings."""
    for arr_prefix, local_prefix in mappings.items():
        if path.startswith(arr_prefix):
            mapped = local_prefix + path[len(arr_prefix):]
            # Resolve to prevent path traversal (e.g. ../../etc/passwd)
            resolved = str(Path(mapped).resolve())
            if not Path(resolved).is_relative_to(Path(local_prefix).resolve()):
                return path  # Reject traversal attempt
            return resolved
    return path


def _resolve_library(path: str, config: PyflowsConfig) -> str | None:
    """Find which library profile a file belongs to based on its path."""
    for lib in config.libraries:
        if Path(path).is_relative_to(lib.path):
            return lib.profile
    return None


class _WebhookHandler(BaseHTTPRequestHandler):
    config: PyflowsConfig
    webhook_config: WebhookConfig
    encode_task: Callable[[str, str], object]

    def do_POST(self) -> None:
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length > 1_048_576:
            self._respond(413, {"error": "request too large"})
            return
        body = self.rfile.read(content_length)

        if self.path == "/webhook/sonarr":
            self._handle_sonarr(body)
        elif self.path == "/webhook/radarr":
            self._handle_radarr(body)
        elif self.path == "/health":
            self._respond(200, {"status": "ok"})
        else:
            self._respond(404, {"error": "not found"})

    def do_GET(self) -> None:
        if self.path == "/health":
            self._respond(200, {"status": "ok"})
        else:
            self._respond(404, {"error": "not found"})

    def _handle_sonarr(self, body: bytes) -> None:
        try:
            payload: WebhookPayload = json.loads(body)
        except json.JSONDecodeError:
            self._respond(400, {"error": "invalid json"})
            return

        event_type = str(payload.get("eventType", ""))
        if event_type not in ("Download", "EpisodeFileDelete"):
            self._respond(200, {"status": "ignored", "reason": f"event type: {event_type}"})
            return

        if event_type == "EpisodeFileDelete":
            self._respond(200, {"status": "ignored", "reason": "delete event"})
            return

        episode_file = payload.get("episodeFile")
        if not isinstance(episode_file, dict):
            self._respond(400, {"error": "no file path in payload"})
            return

        file_path = str(episode_file.get("path", ""))
        if not file_path:
            file_path = str(episode_file.get("relativePath", ""))
            series = payload.get("series")
            series_path = str(series.get("path", "")) if isinstance(series, dict) else ""
            if file_path and series_path:
                file_path = f"{series_path}/{file_path}"

        if not file_path:
            self._respond(400, {"error": "no file path in payload"})
            return

        series = payload.get("series")
        series_id = int(series.get("id", 0)) if isinstance(series, dict) else None  # type: ignore[arg-type]
        self._queue_encode(file_path, "sonarr", series_id or None)

    def _handle_radarr(self, body: bytes) -> None:
        try:
            payload: WebhookPayload = json.loads(body)
        except json.JSONDecodeError:
            self._respond(400, {"error": "invalid json"})
            return

        event_type = str(payload.get("eventType", ""))
        if event_type not in ("Download", "MovieFileDelete"):
            self._respond(200, {"status": "ignored", "reason": f"event type: {event_type}"})
            return

        if event_type == "MovieFileDelete":
            self._respond(200, {"status": "ignored", "reason": "delete event"})
            return

        movie_file = payload.get("movieFile")
        if not isinstance(movie_file, dict):
            self._respond(400, {"error": "no file path in payload"})
            return

        file_path = str(movie_file.get("path", ""))
        if not file_path:
            relative = str(movie_file.get("relativePath", ""))
            movie = payload.get("movie")
            folder = str(movie.get("folderPath", "")) if isinstance(movie, dict) else ""
            if relative and folder:
                file_path = f"{folder}/{relative}"

        if not file_path:
            self._respond(400, {"error": "no file path in payload"})
            return

        movie = payload.get("movie")
        movie_id = int(movie.get("id", 0)) if isinstance(movie, dict) else None  # type: ignore[arg-type]
        self._queue_encode(file_path, "radarr", movie_id or None)

    def _queue_encode(self, arr_path: str, source: str, arr_id: int | None) -> None:
        """Map path, determine profile, and queue the encode task."""
        local_path = _map_path(arr_path, self.webhook_config.path_mappings)

        if not Path(local_path).exists():
            log_event(log, logging.WARNING, "webhook_file_not_found",
                      "Webhook file not found after path mapping",
                      arr_path=arr_path, local_path=local_path, source=source)
            self._respond(404, {"error": "file not found"})
            return

        profile = _resolve_library(local_path, self.config)
        if profile is None:
            log_event(log, logging.WARNING, "webhook_no_library",
                      "File path does not match any configured library",
                      local_path=local_path, source=source)
            self._respond(400, {"error": "no matching library"})
            return

        # Store arr metadata for rescan callback
        with FileDB(self.config.general.db_path) as db:
            db.set_arr_metadata(local_path, source, arr_id)

        self.encode_task(local_path, profile)
        log_event(log, logging.INFO, "webhook_queued",
                  "Queued file from webhook",
                  source=source, arr_path=arr_path, local_path=local_path,
                  profile=profile, arr_id=arr_id)
        self._respond(200, {"status": "queued", "profile": profile})

    def _respond(self, code: int, body: JsonResponse) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def log_message(self, format: str, *args: object) -> None:
        # Suppress default HTTP logging, we use our own
        pass


def start_webhook_server(config: PyflowsConfig, encode_task: Callable[[str, str], object]) -> HTTPServer | None:
    """Start the webhook HTTP server in a background thread. Returns the server or None if disabled."""
    if not config.webhook or not config.webhook.enabled:
        return None

    webhook_config = config.webhook

    class Handler(_WebhookHandler):
        pass

    Handler.config = config
    Handler.webhook_config = webhook_config
    Handler.encode_task = encode_task

    server = HTTPServer(("0.0.0.0", webhook_config.port), Handler)

    thread = threading.Thread(target=server.serve_forever, daemon=True, name="webhook-server")
    thread.start()

    log_event(log, logging.INFO, "webhook_started",
              "Webhook server started", port=webhook_config.port)
    return server
