# nix/packages/pyflows/pyflows/db.py
"""SQLite file tracking database with partial file hashing."""

import enum
import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import TracebackType
from typing import TypedDict


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _utcnow_iso() -> str:
    return _utcnow().isoformat()


class FileRecord(TypedDict, total=False):
    id: int
    path: str
    library: str
    profile: str
    file_hash: str
    size: int
    status: str
    video_codec: str | None
    output_codec: str | None
    output_size: int | None
    error: str | None
    retry_count: int
    next_retry_at: str | None
    hold_until: str | None
    last_seen_at: str | None
    last_observed_size: int | None
    last_observed_mtime: str | None
    arr_source: str | None
    arr_id: int | None
    created_at: str
    started_at: str | None
    completed_at: str | None


def _row_to_record(row: sqlite3.Row) -> FileRecord:
    return dict(row)  # type: ignore[return-value]


def _rows_to_records(rows: list[sqlite3.Row]) -> list[FileRecord]:
    return [_row_to_record(r) for r in rows]


class FileStatus(enum.StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


VALID_TRANSITIONS: dict[FileStatus, set[FileStatus]] = {
    FileStatus.PENDING: {FileStatus.PROCESSING, FileStatus.COMPLETED, FileStatus.FAILED, FileStatus.SKIPPED},
    FileStatus.PROCESSING: {FileStatus.COMPLETED, FileStatus.FAILED, FileStatus.SKIPPED, FileStatus.PENDING},
    FileStatus.COMPLETED: {FileStatus.PENDING},
    FileStatus.FAILED: {FileStatus.PENDING},
    FileStatus.SKIPPED: {FileStatus.PENDING},
}


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
CREATE INDEX IF NOT EXISTS idx_files_library_status ON files(library, status);
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
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA cache_size=-32000")
            conn.execute("PRAGMA temp_store=MEMORY")
            conn.execute("PRAGMA mmap_size=134217728")
            conn.executescript(SCHEMA)
            self.conn = conn
            self._migrate_schema()
        except BaseException:
            conn.close()
            raise

    def upsert(self, path: str, library: str, profile: str,
               file_hash: str, size: int, video_codec: str = "",
               failed_retry_hours: int = 24) -> bool:
        """Insert or update a file record. Returns True if the file needs processing."""
        existing = self.get(path)
        now_dt = _utcnow()
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

        if existing["file_hash"] == file_hash and existing["status"] == FileStatus.FAILED:
            completed_at = existing["completed_at"]
            if completed_at:
                failed_age = now_dt - datetime.fromisoformat(completed_at)
                if failed_age >= timedelta(hours=failed_retry_hours):
                    self.conn.execute(
                        "UPDATE files SET status=?, error=NULL, retry_count=0, next_retry_at=NULL, "
                        "started_at=NULL, completed_at=NULL WHERE path=?",
                        (FileStatus.PENDING, path),
                    )
                    self.conn.commit()
                    return True
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

    def get(self, path: str) -> FileRecord | None:
        cur = self.conn.execute("SELECT * FROM files WHERE path=?", (path,))
        row = cur.fetchone()
        return _row_to_record(row) if row else None

    def get_by_status(self, status: FileStatus, limit: int = 100) -> list[FileRecord]:
        cur = self.conn.execute(
            "SELECT * FROM files WHERE status=? ORDER BY created_at LIMIT ?",
            (status, limit),
        )
        return _rows_to_records(cur.fetchall())

    def count_by_status(self, status: FileStatus) -> int:
        cur = self.conn.execute("SELECT count(*) FROM files WHERE status=?", (status,))
        return cur.fetchone()[0]

    def get_next_pending(
        self,
        now: datetime | None = None,
        priority_codecs: list[str] | None = None,
    ) -> FileRecord | None:
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
        row = cur.fetchone()
        return _row_to_record(row) if row else None

    def update_status(self, path: str, status: FileStatus, error: str = "",
                      output_codec: str = "", output_size: int = 0) -> None:
        now = _utcnow_iso()
        current = self.get(path)
        if current is not None:
            current_status = FileStatus(current["status"])
            if status not in VALID_TRANSITIONS.get(current_status, set()):
                raise ValueError(
                    f"Invalid status transition: {current_status} -> {status} for {path}"
                )
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

    def claim_for_processing(self, path: str) -> bool:
        """Atomically claim a pending file for processing.

        Returns True if the claim succeeded (this caller should encode it).
        Returns False if another worker already claimed it.
        """
        cur = self.conn.execute(
            "UPDATE files SET status=?, started_at=?, next_retry_at=NULL "
            "WHERE path=? AND status=?",
            (FileStatus.PROCESSING, _utcnow_iso(), path, FileStatus.PENDING),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def update_video_codec(self, path: str, codec: str) -> None:
        """Store the probed video codec without touching any other field."""
        self.conn.execute(
            "UPDATE files SET video_codec=? WHERE path=?",
            (codec, path),
        )
        self.conn.commit()

    def get_pending_without_codec(self) -> list[FileRecord]:
        """Return pending files whose video_codec has not been populated yet.

        Used at startup to backfill codec data for files that were queued
        before codec probing was introduced, so get_next_pending() can
        order them correctly from the first encode onwards.
        """
        cur = self.conn.execute(
            "SELECT path FROM files WHERE status = 'pending' "
            "AND (video_codec IS NULL OR video_codec = '')",
        )
        return _rows_to_records(cur.fetchall())

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

    def retry_failed(self, path: str) -> bool:
        """Reset a specific failed file to pending for re-processing."""
        cur = self.conn.execute(
            "UPDATE files SET status=?, error=NULL, retry_count=0, next_retry_at=NULL, "
            "started_at=NULL, completed_at=NULL WHERE path=? AND status=?",
            (FileStatus.PENDING, path, FileStatus.FAILED),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def retry_all_failed(self) -> int:
        """Reset all failed files to pending for re-processing."""
        cur = self.conn.execute(
            "UPDATE files SET status=?, error=NULL, retry_count=0, next_retry_at=NULL, "
            "started_at=NULL, completed_at=NULL WHERE status=?",
            (FileStatus.PENDING, FileStatus.FAILED),
        )
        self.conn.commit()
        return cur.rowcount

    def delete_failed(self) -> int:
        """Delete all failed file records."""
        cur = self.conn.execute("DELETE FROM files WHERE status=?", (FileStatus.FAILED,))
        self.conn.commit()
        return cur.rowcount

    def delete_file(self, path: str) -> bool:
        """Delete a single file record."""
        cur = self.conn.execute("DELETE FROM files WHERE path=?", (path,))
        self.conn.commit()
        return cur.rowcount > 0

    def reencode(self, path: str) -> bool:
        """Reset a completed or skipped file to pending for re-encoding."""
        cur = self.conn.execute(
            "UPDATE files SET status=?, error=NULL, retry_count=0, next_retry_at=NULL, "
            "output_codec=NULL, output_size=NULL, started_at=NULL, completed_at=NULL "
            "WHERE path=? AND status IN (?, ?)",
            (FileStatus.PENDING, path, FileStatus.COMPLETED, FileStatus.SKIPPED),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def get_history(self, limit: int = 100) -> list[FileRecord]:
        """Return recent completed/failed/skipped records sorted by completed_at DESC."""
        cur = self.conn.execute(
            "SELECT * FROM files WHERE status IN (?, ?, ?) "
            "ORDER BY completed_at DESC LIMIT ?",
            (FileStatus.COMPLETED, FileStatus.FAILED, FileStatus.SKIPPED, limit),
        )
        return _rows_to_records(cur.fetchall())

    def all_status_counts(self) -> dict[str, int]:
        counts = {s.value: 0 for s in FileStatus}
        for row in self.conn.execute("SELECT status, count(*) FROM files GROUP BY status"):
            counts[row[0]] = row[1]
        return counts

    def aggregate_space_saved(self) -> int:
        row = self.conn.execute(
            "SELECT coalesce(sum(size - output_size), 0) FROM files WHERE status=? AND output_size IS NOT NULL",
            (FileStatus.COMPLETED,),
        ).fetchone()
        return row[0]

    def status_counts_by_library(self) -> list[dict[str, object]]:
        rows = self.conn.execute(
            "SELECT library, status, count(*) as cnt, "
            "coalesce(sum(size), 0) as total_size, "
            "coalesce(sum(case when status='completed' and output_size is not null then size - output_size else 0 end), 0) as saved "
            "FROM files WHERE library IS NOT NULL GROUP BY library, status"
        ).fetchall()
        libs: dict[str, dict[str, object]] = {}
        for row in rows:
            lib = str(row[0])
            if lib not in libs:
                libs[lib] = {"library": lib, "pending": 0, "processing": 0, "completed": 0, "failed": 0, "skipped": 0, "total_size": 0, "saved": 0}
            libs[lib][row[1]] = row[2]
            libs[lib]["total_size"] = int(libs[lib]["total_size"]) + row[3]
            libs[lib]["saved"] = int(libs[lib]["saved"]) + row[4]
        return list(libs.values())

    def get_by_id(self, file_id: int) -> FileRecord | None:
        cur = self.conn.execute("SELECT * FROM files WHERE id=?", (file_id,))
        row = cur.fetchone()
        return _row_to_record(row) if row else None

    def get_last_scan(self, library: str) -> str | None:
        cur = self.conn.execute("SELECT last_scan_at FROM library_scans WHERE library=?", (library,))
        row = cur.fetchone()
        return row[0] if row else None

    def search_files(
        self,
        status: str | None = None,
        library: str | None = None,
        query: str | None = None,
        has_hold: bool | None = None,
        has_retry: bool | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> list[FileRecord]:
        conditions = []
        params: list[object] = []
        if status:
            conditions.append("status=?")
            params.append(status)
        if library:
            conditions.append("library=?")
            params.append(library)
        if query:
            conditions.append("path LIKE ?")
            params.append(f"%{query}%")
        if has_hold is True:
            conditions.append("hold_until IS NOT NULL AND hold_until != ''")
        if has_retry is True:
            conditions.append("next_retry_at IS NOT NULL AND next_retry_at != ''")
        where = " AND ".join(conditions) if conditions else "1=1"
        sql = f"SELECT * FROM files WHERE {where} ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        return _rows_to_records(self.conn.execute(sql, params).fetchall())

    def search_history(
        self,
        status: str | None = None,
        library: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> list[FileRecord]:
        conditions = ["status IN ('completed', 'failed', 'skipped')"]
        params: list[object] = []
        if status:
            conditions.append("status=?")
            params.append(status)
        if library:
            conditions.append("library=?")
            params.append(library)
        where = " AND ".join(conditions)
        sql = f"SELECT * FROM files WHERE {where} ORDER BY completed_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        return _rows_to_records(self.conn.execute(sql, params).fetchall())

    def history_stats(self, status: str | None = None, library: str | None = None) -> dict[str, object]:
        conditions = ["status IN ('completed', 'failed', 'skipped')"]
        params: list[object] = []
        if status:
            conditions.append("status=?")
            params.append(status)
        if library:
            conditions.append("library=?")
            params.append(library)
        where = " AND ".join(conditions)
        row = self.conn.execute(
            f"SELECT count(*) as total, "
            f"sum(case when status='completed' then 1 else 0 end) as completed, "
            f"sum(case when status='failed' then 1 else 0 end) as failed, "
            f"coalesce(sum(case when status='completed' and output_size is not null then size - output_size else 0 end), 0) as saved "
            f"FROM files WHERE {where}", params
        ).fetchone()
        total = row[0] or 0
        completed = row[1] or 0
        failed = row[2] or 0
        return {
            "total": total,
            "completed": completed,
            "failed": failed,
            "skipped": total - completed - failed,
            "success_rate": round(completed / max(completed + failed, 1) * 100, 1),
            "saved": row[3] or 0,
        }

    def skip_file(self, path: str) -> bool:
        cur = self.conn.execute(
            "UPDATE files SET status=?, completed_at=? WHERE path=? AND status=?",
            (FileStatus.SKIPPED, _utcnow_iso(), path, FileStatus.PENDING),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def get_libraries(self) -> list[str]:
        return [row[0] for row in self.conn.execute(
            "SELECT DISTINCT library FROM files WHERE library IS NOT NULL ORDER BY library"
        )]

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
        return _utcnow() - last_scan >= timedelta(seconds=interval_seconds)

    def record_library_scan(self, library: str) -> None:
        """Persist the latest scan timestamp for a library."""
        now = _utcnow_iso()
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
        now = _utcnow_iso()
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
            if existing["status"] == FileStatus.PROCESSING:
                return
            self.conn.execute(
                "UPDATE files SET library=?, profile=?, size=?, hold_until=?, last_seen_at=?, last_observed_size=?, last_observed_mtime=? "
                "WHERE path=? AND status != ?",
                (
                    library,
                    profile,
                    size,
                    hold_until.isoformat(),
                    now,
                    size,
                    observed_mtime.isoformat(),
                    path,
                    FileStatus.PROCESSING,
                ),
            )
        self.conn.commit()

    def list_ready_held_files(self, now: datetime) -> list[FileRecord]:
        cur = self.conn.execute(
            "SELECT * FROM files WHERE hold_until IS NOT NULL AND hold_until != '' AND hold_until <= ? ORDER BY hold_until ASC",
            (now.isoformat(),),
        )
        return _rows_to_records(cur.fetchall())

    def clear_hold(self, path: str) -> None:
        self.conn.execute(
            "UPDATE files SET hold_until=NULL, last_seen_at=NULL, last_observed_size=NULL, last_observed_mtime=NULL WHERE path=?",
            (path,),
        )
        self.conn.commit()

    def set_arr_metadata(self, path: str, source: str, arr_id: int | None) -> None:
        """Store arr source and ID for rescan callback after encoding."""
        cur = self.conn.execute(
            "UPDATE files SET arr_source=?, arr_id=? WHERE path=?",
            (source, arr_id, path),
        )
        if cur.rowcount == 0:
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
