"""pyflows monitoring UI — server-side renderer."""

from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from pyflows.config import PyflowsConfig
from pyflows.db import FileDB, FileStatus
from pyflows.ffmpeg import get_current_progress


def _human_size(size_bytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size_bytes) < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024  # type: ignore[assignment]
    return f"{size_bytes:.1f} PB"


def _relative_time(iso_str: str | None) -> str:
    if not iso_str:
        return "—"
    try:
        dt = datetime.fromisoformat(iso_str)
        delta = datetime.now(timezone.utc) - dt
        seconds = int(delta.total_seconds())
        if seconds < 0:
            return "just now"
        if seconds < 60:
            return f"{seconds}s ago"
        minutes = seconds // 60
        if minutes < 60:
            return f"{minutes}m ago"
        hours = minutes // 60
        if hours < 24:
            return f"{hours}h ago"
        days = hours // 24
        return f"{days}d ago"
    except (ValueError, TypeError):
        return "—"


class UIRenderer:
    def __init__(self, config: PyflowsConfig) -> None:
        self.config = config
        template_dir = Path(__file__).parent / "templates"
        self.env = Environment(
            loader=FileSystemLoader(str(template_dir)),
            autoescape=True,
        )
        self.env.filters["human_size"] = _human_size
        self.env.filters["relative_time"] = _relative_time

    def render_dashboard(self) -> str:
        with FileDB(self.config.general.db_path) as db:
            counts = db.all_status_counts()
            saved = db.aggregate_space_saved()
            pending = db.get_by_status(FileStatus.PENDING, limit=20)
            failed = db.get_by_status(FileStatus.FAILED)
            history = db.get_history(limit=10)
        progress = get_current_progress()
        processing = None
        if progress.file_path:
            with FileDB(self.config.general.db_path) as db:
                processing = db.get(progress.file_path)
        tmpl = self.env.get_template("dashboard.html")
        return tmpl.render(
            counts=counts, saved=saved, pending=pending,
            failed=failed, history=history, progress=progress,
            processing=processing,
        )

    def render_partial_status_bar(self) -> str:
        with FileDB(self.config.general.db_path) as db:
            counts = db.all_status_counts()
            saved = db.aggregate_space_saved()
        tmpl = self.env.get_template("partials/status_bar.html")
        return tmpl.render(counts=counts, saved=saved)

    def render_partial_encode_progress(self) -> str:
        progress = get_current_progress()
        processing = None
        if progress.file_path:
            with FileDB(self.config.general.db_path) as db:
                processing = db.get(progress.file_path)
        tmpl = self.env.get_template("partials/encode_progress.html")
        return tmpl.render(progress=progress, processing=processing)

    def render_partial_queue_preview(self) -> str:
        with FileDB(self.config.general.db_path) as db:
            pending = db.get_by_status(FileStatus.PENDING, limit=20)
        tmpl = self.env.get_template("partials/queue_preview.html")
        return tmpl.render(pending=pending)

    def render_partial_failed_table(self) -> str:
        with FileDB(self.config.general.db_path) as db:
            failed = db.get_by_status(FileStatus.FAILED)
        tmpl = self.env.get_template("partials/failed_table.html")
        return tmpl.render(failed=failed)

    def render_partial_recent_completions(self) -> str:
        with FileDB(self.config.general.db_path) as db:
            history = db.get_history(limit=10)
        tmpl = self.env.get_template("partials/recent_completions.html")
        return tmpl.render(history=history)

    def serve_static(self, filename: str) -> tuple[bytes, str] | None:
        static_dir = Path(__file__).parent / "static"
        safe_name = Path(filename).name
        file_path = static_dir / safe_name
        if not file_path.exists() or not file_path.is_relative_to(static_dir):
            return None
        content_types = {".js": "application/javascript", ".css": "text/css"}
        ct = content_types.get(file_path.suffix, "application/octet-stream")
        return file_path.read_bytes(), ct
