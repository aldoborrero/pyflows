"""UI route handlers for pages, partials, SSE, actions, and static files."""

import time
import urllib.parse
from http.server import BaseHTTPRequestHandler
from typing import TYPE_CHECKING

from pyflows.db import FileDB

if TYPE_CHECKING:
    from pyflows.config import PyflowsConfig
    from pyflows.ui import UIRenderer


from typing import Union
JsonResponse = dict[str, Union[str, int, None]]


class UIHandlerMixin:
    """Mixin providing /ui/* route handling for BaseHTTPRequestHandler subclasses.

    Expects the host class to have: config, ui_renderer, _respond, _respond_html, wfile, headers, path, send_response, send_header, end_headers.
    """

    config: "PyflowsConfig"
    ui_renderer: "UIRenderer | None"
    path: str
    headers: object
    wfile: object


    def route_ui_get(self) -> bool:
        if self.path == "/ui" or self.path == "/ui/":
            self._serve_page("dashboard")
        elif self.path.startswith("/ui/queue"):
            self._serve_page("queue")
        elif self.path.startswith("/ui/history"):
            self._serve_page("history")
        elif self.path.startswith("/ui/libraries"):
            self._serve_page("libraries")
        elif self.path.startswith("/ui/settings"):
            self._serve_page("settings")
        elif self.path.startswith("/ui/files/"):
            self._serve_file_detail()
        elif self.path.startswith("/ui/partials/file-detail/"):
            self._serve_file_detail_partial()
        elif self.path == "/ui/events":
            self._serve_sse()
        elif self.path.startswith("/ui/partials/queue-table"):
            self._serve_partial("queue-table")
        elif self.path.startswith("/ui/partials/history-table"):
            self._serve_partial("history-table")
        elif self.path.startswith("/ui/static/"):
            self._serve_static()
        else:
            return False
        return True

    def route_ui_post(self, body: bytes) -> bool:
        if self.path == "/ui/api/retry":
            self._handle_retry(body)
        elif self.path == "/ui/api/retry-all":
            self._handle_retry_all(body)
        elif self.path == "/ui/api/skip":
            self._handle_skip(body)
        elif self.path == "/ui/api/reencode":
            self._handle_reencode(body)
        elif self.path == "/ui/api/scan":
            self._handle_scan(body)
        elif self.path.startswith("/ui/api/pause/"):
            self._handle_pause(False)
        elif self.path.startswith("/ui/api/resume/"):
            self._handle_pause(True)
        else:
            return False
        return True

    def _serve_page(self, page: str) -> None:
        if self.ui_renderer is None:
            self._respond(404, {"error": "UI not enabled"})
            return
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if page == "dashboard":
            html = self.ui_renderer.render_dashboard()
        elif page == "queue":
            html = self.ui_renderer.render_queue(
                filter_val=params.get("filter", [""])[0],
                query=params.get("q", [""])[0],
                library=params.get("library", [""])[0],
            )
        elif page == "history":
            html = self.ui_renderer.render_history(
                status_filter=params.get("status", [""])[0],
                library_filter=params.get("library", [""])[0],
            )
        elif page == "libraries":
            html = self.ui_renderer.render_libraries()
        elif page == "settings":
            html = self.ui_renderer.render_settings()
        else:
            self._respond(404, {"error": "not found"})
            return
        self._respond_html(200, html)

    def _serve_partial(self, partial: str) -> None:
        if self.ui_renderer is None:
            self._respond(404, {"error": "UI not enabled"})
            return
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        offset = int(params.get("offset", ["0"])[0])
        if partial == "queue-table":
            html = self.ui_renderer.render_queue_partial(
                filter_val=params.get("filter_val", [""])[0],
                query=params.get("q", [""])[0],
                library=params.get("library", [""])[0],
                offset=offset,
            )
        elif partial == "history-table":
            html = self.ui_renderer.render_history_partial(
                status_filter=params.get("status_val", [""])[0],
                library_filter=params.get("library", [""])[0],
                offset=offset,
            )
        else:
            self._respond(404, {"error": "not found"})
            return
        self._respond_html(200, html)

    def _serve_file_detail(self) -> None:
        if self.ui_renderer is None:
            self._respond(404, {"error": "UI not enabled"})
            return
        try:
            file_id = int(self.path.split("/ui/files/")[1].split("?")[0])
        except (ValueError, IndexError):
            self._respond(404, {"error": "invalid file id"})
            return
        html = self.ui_renderer.render_file_detail(file_id)
        if html is None:
            self._respond(404, {"error": "file not found"})
            return
        self._respond_html(200, html)

    def _serve_file_detail_partial(self) -> None:
        if self.ui_renderer is None:
            self._respond(404, {"error": "UI not enabled"})
            return
        try:
            file_id = int(self.path.split("/ui/partials/file-detail/")[1].split("?")[0])
        except (ValueError, IndexError):
            self._respond(404, {"error": "invalid file id"})
            return
        html = self.ui_renderer.render_file_detail_partial(file_id)
        if html is None:
            self._respond(404, {"error": "file not found"})
            return
        self._respond_html(200, html)

    def _serve_static(self) -> None:
        if self.ui_renderer is None:
            self._respond(404, {"error": "UI not enabled"})
            return
        filename = self.path.split("/ui/static/", 1)[-1]
        result = self.ui_renderer.serve_static(filename)
        if result is None:
            self._respond(404, {"error": "not found"})
            return
        data, content_type = result
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "public, max-age=86400")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)  # type: ignore[union-attr]

    def _serve_sse(self) -> None:
        if self.ui_renderer is None:
            self._respond(404, {"error": "UI not enabled"})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        tick = 0
        try:
            while True:
                tick += 1
                if tick % 1 == 0:
                    html = self.ui_renderer.render_partial_encode_progress()
                    self._send_sse_event("encode-progress", html)
                if tick % 3 == 0:
                    html = self.ui_renderer.render_partial_status_bar()
                    self._send_sse_event("status-counts", html)
                if tick % 5 == 0:
                    self._send_sse_event("queue-summary", self.ui_renderer.render_partial_queue_preview())
                    self._send_sse_event("failed-update", self.ui_renderer.render_partial_failed_table())
                    self._send_sse_event("completions-update", self.ui_renderer.render_partial_recent_completions())
                time.sleep(2)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _send_sse_event(self, event: str, data: str) -> None:
        data_lines = "\n".join(f"data: {line}" for line in data.strip().splitlines())
        payload = f"event: {event}\n{data_lines}\n\n"
        self.wfile.write(payload.encode())  # type: ignore[union-attr]
        self.wfile.flush()  # type: ignore[union-attr]

    def _handle_retry(self, body: bytes) -> None:
        params = urllib.parse.parse_qs(body.decode())
        path = params.get("path", [""])[0]
        if not path:
            self._respond(400, {"error": "missing path"})
            return
        with FileDB(self.config.general.db_path) as db:
            if db.retry_failed(path):
                self._respond_html(200, "<tr><td colspan='5'>Retrying...</td></tr>")
            else:
                self._respond(404, {"error": "not found or not failed"})

    def _handle_retry_all(self, body: bytes) -> None:
        with FileDB(self.config.general.db_path) as db:
            count = db.retry_all_failed()
        if not self.ui_renderer:
            self._respond_html(200, "")
            return
        referer = getattr(self.headers, 'get', lambda *a: "")("Referer", "")  # type: ignore[arg-type]
        if "/ui/history" in referer:
            html = self.ui_renderer.render_history_partial()
        else:
            html = self.ui_renderer.render_partial_failed_table()
        self._respond_html(200, html)

    def _handle_skip(self, body: bytes) -> None:
        params = urllib.parse.parse_qs(body.decode())
        path = params.get("path", [""])[0]
        if not path:
            self._respond(400, {"error": "missing path"})
            return
        with FileDB(self.config.general.db_path) as db:
            if db.skip_file(path):
                self._respond_html(200, "")
            else:
                self._respond(404, {"error": "not found or not pending"})

    def _handle_reencode(self, body: bytes) -> None:
        params = urllib.parse.parse_qs(body.decode())
        path = params.get("path", [""])[0]
        if not path:
            self._respond(400, {"error": "missing path"})
            return
        with FileDB(self.config.general.db_path) as db:
            if db.reencode(path):
                self._respond_html(200, "<p>Re-queued for encoding</p>")
            else:
                self._respond(404, {"error": "not found or not eligible"})

    def _handle_scan(self, body: bytes) -> None:
        params = urllib.parse.parse_qs(body.decode())
        library = params.get("library", [""])[0]
        if not library:
            self._respond(400, {"error": "missing library"})
            return
        matching = [lib for lib in self.config.libraries if lib.name == library]
        if not matching:
            self._respond(404, {"error": "library not found"})
            return
        lib = matching[0]
        from pyflows.scanner import scan_library
        with FileDB(self.config.general.db_path) as db:
            scan_library(lib, db, ffprobe_path=self.config.general.ffprobe_path,
                         priority_codecs=self.config.resolved_priority_codecs())
        self._respond_html(200, "<p>Scan complete</p>")

    def _handle_pause(self, enabled: bool) -> None:
        from pyflows.tasks import set_pause_state, get_pause_state
        prefix = "/ui/api/resume/" if enabled else "/ui/api/pause/"
        component = self.path[len(prefix):]
        if not set_pause_state(component, enabled):
            self._respond(400, {"error": f"unknown component: {component}"})
            return
        state = get_pause_state()
        if self.ui_renderer:
            self._respond_html(200, self.ui_renderer.render_partial_pause_controls(state))
        else:
            self._respond(200, {"status": "ok", **{k: v for k, v in state.items()}})
