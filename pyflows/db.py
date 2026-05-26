# nix/packages/pyflows/pyflows/db.py
"""SQLite file tracking database with partial file hashing."""

import enum
import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import TracebackType


class FileStatus(enum.StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    library TEXT NOT NULL,
    profile TEXT NOT NULL,
    file_hash TEXT NOT NULL,
    size INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    video_codec TEXT,
    output_codec TEXT,
    output_size INTEGER,
    error TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0,
    next_retry_at TEXT,
    hold_until TEXT,
    last_seen_at TEXT,
    last_observed_size INTEGER,
    last_observed_mtime TEXT,
    arr_source TEXT,
    arr_id INTEGER,
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_files_hash ON files(file_hash);
CREATE INDEX IF NOT EXISTS idx_files_status ON files(status);
CREATE TABLE IF NOT EXISTS library_scans (
    library TEXT PRIMARY KEY,
    last_scan_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS arr_metadata (
    path TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    arr_id INTEGER
);
"""


class FileDB:
    def __init__(self, db_path: str):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            self.conn = conn
            self._migrate_schema()
        except BaseException:
            conn.close()
            raise

    def upsert(self, path: str, library: str, profile: str,
               file_hash: str, size: int, video_codec: str = "") -> bool:
        """Insert or update a file record. Returns True if the file needs processing."""
        existing = self.get(path)
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()

        if existing is None:
            self.conn.execute(
                "INSERT INTO files (path, library, profile, file_hash, size, video_codec, status, retry_count, next_retry_at, hold_until, last_seen_at, last_observed_size, last_observed_mtime, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (path, library, profile, file_hash, size, video_codec, FileStatus.PENDING, 0, None, None, None, None, None, now),
            )
            self.conn.commit()
            return True

        if existing["file_hash"] == file_hash and existing["status"] in (FileStatus.COMPLETED, FileStatus.SKIPPED):
            return False

        hold_until = existing["hold_until"]
        hold_ready = hold_until is None or now_dt >= datetime.fromisoformat(hold_until)

        if existing["file_hash"] != file_hash:
            self.conn.execute(
                "UPDATE files SET file_hash=?, size=?, video_codec=?, status=?, error=NULL, retry_count=0, next_retry_at=NULL, "
                "last_seen_at=NULL, last_observed_size=NULL, last_observed_mtime=NULL, started_at=NULL, completed_at=NULL WHERE path=?",
                (file_hash, size, video_codec, FileStatus.PENDING, path),
            )
            self.conn.commit()
            return hold_ready

        if existing["status"] == FileStatus.PENDING:
            if not hold_ready:
                return False
            next_retry_at = existing["next_retry_at"]
            if next_retry_at:
                return now_dt >= datetime.fromisoformat(next_retry_at)
            return True

        return False

    def get(self, path: str) -> sqlite3.Row | None:
        cur = self.conn.execute("SELECT * FROM files WHERE path=?", (path,))
        row: sqlite3.Row | None = cur.fetchone()
        return row

    def get_by_status(self, status: str, limit: int = 100) -> list[sqlite3.Row]:
        cur = self.conn.execute(
            "SELECT * FROM files WHERE status=? ORDER BY created_at LIMIT ?",
            (status, limit),
        )
        return cur.fetchall()

    def get_next_pending(
        self,
        now: datetime | None = None,
        priority_codecs: list[str] | None = None,
    ) -> sqlite3.Row | None:
        """Return the highest-priority pending file ready for encoding.

        Within each tier files are ordered by created_at (FIFO).
        Files with next_retry_at in the future or hold_until in the future
        are excluded.
        """
        if now is None:
            now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        priority_codecs = [codec.lower() for codec in (priority_codecs or [])]

        if priority_codecs:
            placeholders = ",".join("?" for _ in priority_codecs)
            query = f"""
                SELECT * FROM files
                WHERE status = 'pending'
                  AND (next_retry_at IS NULL OR next_retry_at <= ?)
                  AND (hold_until   IS NULL OR hold_until   <= ?)
                ORDER BY
                  CASE WHEN lower(coalesce(video_codec, '')) IN ({placeholders}) THEN 0 ELSE 1 END,
                  created_at
                LIMIT 1
            """
            params: tuple[str, ...] = (now_iso, now_iso, *priority_codecs)
        else:
            query = """
                SELECT * FROM files
                WHERE status = 'pending'
                  AND (next_retry_at IS NULL OR next_retry_at <= ?)
                  AND (hold_until   IS NULL OR hold_until   <= ?)
                ORDER BY created_at
                LIMIT 1
            """
            params = (now_iso, now_iso)

        cur = self.conn.execute(query, params)
        return cur.fetchone()

    def update_status(self, path: str, status: str, error: str = "",
                      output_codec: str = "", output_size: int = 0) -> None:
        now = datetime.now(timezone.utc).isoformat()
        if status == FileStatus.PROCESSING:
            self.conn.execute("UPDATE files SET status=?, started_at=?, next_retry_at=NULL WHERE path=?",
                              (status, now, path))
        elif status in (FileStatus.COMPLETED, FileStatus.SKIPPED):
            self.conn.execute(
                "UPDATE files SET status=?, output_codec=?, output_size=?, completed_at=?, retry_count=0, next_retry_at=NULL, hold_until=NULL WHERE path=?",
                (status, output_codec, output_size, now, path))
        elif status == FileStatus.FAILED:
            self.conn.execute("UPDATE files SET status=?, error=?, completed_at=?, next_retry_at=NULL WHERE path=?",
                              (status, error, now, path))
        else:
            self.conn.execute("UPDATE files SET status=? WHERE path=?", (status, path))
        self.conn.commit()

    def update_video_codec(self, path: str, codec: str) -> None:
        """Store the probed video codec without touching any other field."""
        self.conn.execute(
            "UPDATE files SET video_codec=? WHERE path=?",
            (codec, path),
        )
        self.conn.commit()

    def get_pending_without_codec(self) -> list[sqlite3.Row]:
        """Return pending files whose video_codec has not been populated yet.

        Used at startup to backfill codec data for files that were queued
        before codec probing was introduced, so get_next_pending() can
        order them correctly from the first encode onwards.
        """
        cur = self.conn.execute(
            "SELECT path FROM files WHERE status = 'pending' "
            "AND (video_codec IS NULL OR video_codec = '')",
        )
        return cur.fetchall()

    def update_hash(self, path: str, file_hash: str, size: int) -> None:
        """Update the file hash and size after encoding (prevents watcher re-queue)."""
        self.conn.execute(
            "UPDATE files SET file_hash=?, size=? WHERE path=?",
            (file_hash, size, path),
        )
        self.conn.commit()

    def rename_path(self, old_path: str, new_path: str) -> None:
        """Rename the tracked path after container/extension changes."""
        self.conn.execute(
            "UPDATE files SET path=? WHERE path=?",
            (new_path, old_path),
        )
        self.conn.commit()

    def schedule_retry(self, path: str, error: str, retry_count: int, next_retry_at: datetime) -> None:
        """Schedule a retry without marking the file as terminally failed."""
        self.conn.execute(
            "UPDATE files SET status=?, error=?, retry_count=?, next_retry_at=?, started_at=NULL, completed_at=NULL WHERE path=?",
            (FileStatus.PENDING, error, retry_count, next_retry_at.isoformat(), path),
        )
        self.conn.commit()

    def get_retry_info(self, path: str) -> tuple[int, datetime | None]:
        cur = self.conn.execute("SELECT retry_count, next_retry_at FROM files WHERE path=?", (path,))
        row: sqlite3.Row | None = cur.fetchone()
        if row is None:
            return 0, None
        next_retry_at = row["next_retry_at"]
        return int(row["retry_count"] or 0), (datetime.fromisoformat(next_retry_at) if next_retry_at else None)

    def reset_processing(self) -> int:
        """Reset any 'processing' files back to 'pending' (crash recovery)."""
        cur = self.conn.execute(
            "UPDATE files SET status=?, started_at=NULL WHERE status=?",
            (FileStatus.PENDING, FileStatus.PROCESSING),
        )
        self.conn.commit()
        return cur.rowcount

    def get_history(self, limit: int = 100) -> list[sqlite3.Row]:
        """Return recent completed/failed/skipped records sorted by completed_at DESC."""
        cur = self.conn.execute(
            "SELECT * FROM files WHERE status IN (?, ?, ?) "
            "ORDER BY completed_at DESC LIMIT ?",
            (FileStatus.COMPLETED, FileStatus.FAILED, FileStatus.SKIPPED, limit),
        )
        return cur.fetchall()

    def should_scan_library(self, library: str, interval_seconds: int) -> bool:
        """Return True when a library scan is due based on its last scan timestamp."""
        if interval_seconds <= 0:
            return True
        cur = self.conn.execute(
            "SELECT last_scan_at FROM library_scans WHERE library=?",
            (library,),
        )
        row: sqlite3.Row | None = cur.fetchone()
        if row is None:
            return True
        last_scan = datetime.fromisoformat(row["last_scan_at"])
        now = datetime.now(timezone.utc)
        return now - last_scan >= timedelta(seconds=interval_seconds)

    def record_library_scan(self, library: str) -> None:
        """Persist the latest scan timestamp for a library."""
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            "INSERT INTO library_scans (library, last_scan_at) VALUES (?, ?) "
            "ON CONFLICT(library) DO UPDATE SET last_scan_at=excluded.last_scan_at",
            (library, now),
        )
        self.conn.commit()

    def record_file_event(
        self,
        path: str,
        library: str,
        profile: str,
        size: int,
        observed_mtime: datetime,
        hold_until: datetime,
    ) -> None:
        """Persist watcher/scan event state so processing can be deferred until stable."""
        now = datetime.now(timezone.utc).isoformat()
        existing = self.get(path)
        if existing is None:
            self.conn.execute(
                "INSERT INTO files (path, library, profile, file_hash, size, status, hold_until, last_seen_at, last_observed_size, last_observed_mtime, retry_count, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    path,
                    library,
                    profile,
                    "",
                    size,
                    FileStatus.PENDING,
                    hold_until.isoformat(),
                    now,
                    size,
                    observed_mtime.isoformat(),
                    0,
                    now,
                ),
            )
        else:
            self.conn.execute(
                "UPDATE files SET library=?, profile=?, size=?, hold_until=?, last_seen_at=?, last_observed_size=?, last_observed_mtime=? WHERE path=?",
                (
                    library,
                    profile,
                    size,
                    hold_until.isoformat(),
                    now,
                    size,
                    observed_mtime.isoformat(),
                    path,
                ),
            )
        self.conn.commit()

    def list_ready_held_files(self, now: datetime) -> list[sqlite3.Row]:
        cur = self.conn.execute(
            "SELECT * FROM files WHERE hold_until IS NOT NULL AND hold_until != '' AND hold_until <= ? ORDER BY hold_until ASC",
            (now.isoformat(),),
        )
        return cur.fetchall()

    def clear_hold(self, path: str) -> None:
        self.conn.execute(
            "UPDATE files SET hold_until=NULL, last_seen_at=NULL, last_observed_size=NULL, last_observed_mtime=NULL WHERE path=?",
            (path,),
        )
        self.conn.commit()

    def set_arr_metadata(self, path: str, source: str, arr_id: int | None) -> None:
        """Store arr source and ID for rescan callback after encoding."""
        self.conn.execute(
            "UPDATE files SET arr_source=?, arr_id=? WHERE path=?",
            (source, arr_id, path),
        )
        # If no row exists yet (file queued directly via webhook before scan),
        # the UPDATE is a no-op. The metadata table handles this case.
        if self.conn.total_changes == 0:
            self.conn.execute(
                "INSERT OR REPLACE INTO arr_metadata (path, source, arr_id) VALUES (?, ?, ?)",
                (path, source, arr_id),
            )
        self.conn.commit()

    def get_arr_metadata(self, path: str) -> tuple[str | None, int | None]:
        """Get arr source and ID for a file path."""
        cur = self.conn.execute("SELECT arr_source, arr_id FROM files WHERE path=?", (path,))
        row = cur.fetchone()
        if row and row["arr_source"]:
            return row["arr_source"], row["arr_id"]
        # Fallback to metadata table
        cur = self.conn.execute("SELECT source, arr_id FROM arr_metadata WHERE path=?", (path,))
        row = cur.fetchone()
        if row:
            return row["source"], row["arr_id"]
        return None, None

    def _migrate_schema(self) -> None:
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(files)").fetchall()}
        if "retry_count" not in columns:
            self.conn.execute("ALTER TABLE files ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0")
        if "next_retry_at" not in columns:
            self.conn.execute("ALTER TABLE files ADD COLUMN next_retry_at TEXT")
        if "hold_until" not in columns:
            self.conn.execute("ALTER TABLE files ADD COLUMN hold_until TEXT")
        if "last_seen_at" not in columns:
            self.conn.execute("ALTER TABLE files ADD COLUMN last_seen_at TEXT")
        if "last_observed_size" not in columns:
            self.conn.execute("ALTER TABLE files ADD COLUMN last_observed_size INTEGER")
        if "last_observed_mtime" not in columns:
            self.conn.execute("ALTER TABLE files ADD COLUMN last_observed_mtime TEXT")
        if "arr_source" not in columns:
            self.conn.execute("ALTER TABLE files ADD COLUMN arr_source TEXT")
        if "arr_id" not in columns:
            self.conn.execute("ALTER TABLE files ADD COLUMN arr_id INTEGER")
        # Separate table for webhook metadata (in case file isn't in DB yet)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS arr_metadata (
                path TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                arr_id INTEGER
            )
        """)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "FileDB":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.close()


HASH_CHUNK = 65536  # 64KB


def compute_file_hash(path: str) -> str:
    """Compute partial hash: SHA-256 of first 64KB + last 64KB + file size."""
    p = Path(path)
    size = p.stat().st_size
    h = hashlib.sha256()
    h.update(str(size).encode())

    with open(p, "rb") as f:
        h.update(f.read(HASH_CHUNK))
        f.seek(max(0, size - HASH_CHUNK))
        h.update(f.read(HASH_CHUNK))

    return h.hexdigest()
