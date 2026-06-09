# Concurrency Control Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Separate pyflows' control plane (scanning, maintenance, dispatch) from its data plane (encoding workers) and add admission controls for GPU and per-library concurrency.

**Architecture:** Replace the current "Huey does everything" model with dedicated threads for scanning, maintenance, and dispatch. A central dispatcher owns all scheduling decisions: it claims pending files with a unique lease token, checks capacity (worker slots, GPU slots, per-library limits), and enqueues Huey encode tasks. Workers verify lease ownership before encoding. Multiple FFmpeg processes are tracked in a registry for clean shutdown.

**Tech Stack:** Python threading, SQLite (existing WAL mode), Huey (encode-only), `threading.BoundedSemaphore` (GPU slots), UUID fencing tokens

**Terminology:** Tokens in this plan are *fencing tokens* (attempt identifiers), not leases. There is no expiry or heartbeat. Stale attempts are recovered at startup via `reset_processing()`. This is appropriate for a single-daemon, single-host system.

---

## Key Design Decisions

These decisions address specific race conditions and failure modes identified during plan review.

### 1. Dispatch lease tokens (prevents stale Huey task replay)

**Problem:** After restart, `reset_processing()` returns files to PENDING, but old durable Huey tasks survive in `huey.db`. A replayed old task plus a fresh dispatch can both see `status=processing` and double-encode.

**Solution:** Add a `dispatch_token` column to the `files` table. The dispatcher generates a UUID, stores it when claiming, and passes it to the Huey task. The worker atomically verifies `WHERE path=? AND dispatch_token=?` before encoding. A stale Huey task carries an old token that won't match after `reset_processing()` clears the token.

### 2. Two-phase claim: dispatch_token + atomic worker start (prevents all duplicate encoding)

**Problem:** A single `verify_claim()` SELECT is not enough. If Huey delivers the same task twice concurrently, both workers pass the read-only verify before either finishes. Additionally, stale Huey tasks after restart carry old tokens.

**Solution:** Two-phase ownership model:
- **Dispatcher phase:** `claim_with_token(path, token)` sets `status=processing`, `dispatch_token=token`, `dispatched_at=now`, `started_at=NULL`.
- **Worker phase:** Atomic `start_encode(path, token)` does `UPDATE ... SET started_at=? WHERE path=? AND dispatch_token=? AND started_at IS NULL`. Only one worker succeeds (rowcount=1). The second gets rowcount=0 and returns.

This eliminates both stale-task replay and concurrent-delivery races.

### 3. Token-guarded terminal operations

**Problem:** After a worker verifies ownership once, later status updates use path only. A manual reset or redispatch during encoding could let an old worker overwrite the newer attempt's state.

**Solution:** All terminal operations (complete, fail, skip, retry, rename, release) require the matching `dispatch_token`. Methods: `complete_with_token(path, token, ...)`, `fail_with_token(path, token, error)`, etc. If the token doesn't match, the operation is a no-op (returns False).

### 4. Multi-process FFmpeg registry (prevents orphaned processes)

**Problem:** `_ActiveProcess` stores a single `subprocess.Popen`. With multiple workers, each overwrite loses the previous reference.

**Solution:** Replace with `ProcessRegistry` and `ProgressRegistry` — thread-safe dicts keyed by dispatch token (not file path, to avoid collisions during resets). `terminate_all()` on shutdown.

### 5. Claim rollback on enqueue failure

**Problem:** If the dispatcher claims then Huey enqueueing fails, the file is stuck in PROCESSING.

**Solution:** try/except around enqueue; on failure, `release_claim(path, token)` rolls back to PENDING.

### 6. Pause-aware workers

**Problem:** Dispatcher claims and enqueues just before encoding is paused. Worker returns, file stuck PROCESSING.

**Solution:** Worker calls `release_claim(path, token)` when paused, returning file to PENDING.

### 7. GPU capacity in the dispatcher (not workers)

**Problem:** GPU semaphore in workers blocks them while CPU-only work is queued.

**Solution:** The dispatcher persists `needs_gpu` when claiming. The value is computed per-file using `_needs_gpu(video_codec, profile_name, config)` which checks the file's specific profile's `skip_codecs` and `encoder`. GPU filtering is NOT done in SQL (cross-profile skip_codecs aggregation would be incorrect). Instead the dispatcher:

1. Fetches the next candidate via standard priority query.
2. Evaluates `_needs_gpu()` using the file's own profile.
3. If `needs_gpu=True` and `gpu_processing_count() >= gpu_slots`, skips this candidate and tries the next.
4. If `needs_gpu=False`, dispatches regardless of GPU state.

The worker BoundedSemaphore remains as a safety net but should never block under normal dispatch.

### 8. Per-library limits checked per-claim, not per-batch

**Problem:** One-time library count query before a batch can overcommit.

**Solution:** Re-query `SELECT library, count(*) FROM files WHERE status='processing' GROUP BY library` before each claim within the dispatch loop.

### 9. Clean thread shutdown with join

**Problem:** Daemon threads are never joined.

**Solution:** Store all thread references. On shutdown: `shutdown.set()`, join each with timeout, then cleanup.

### 10. Configuration validation

**Problem:** `BoundedSemaphore(0)` blocks forever. Invalid intervals cause unexpected behavior.

**Solution:** Pydantic validators: `encode_workers >= 1` (after resolution), `gpu_slots >= 1`, `scan_check_interval >= 1`, `max_concurrent >= 0`. All early errors at config load.

---

## File Structure

| File | Role | Change |
|------|------|--------|
| `pyflows/tasks.py` | Daemon orchestration | Major: remove periodic tasks, add scanner/maintenance/dispatcher threads, simplify `_encode_file`, add lease verification |
| `pyflows/config.py` | Configuration | Add `scan_check_interval`, `encode_workers`, `gpu_slots`, per-library `max_concurrent` |
| `pyflows/db.py` | Database | Add `dispatch_token`+`dispatched_at`+`needs_gpu` columns, two-phase claim methods, token-guarded terminal ops, library counts, exclusion query |
| `pyflows/pipeline.py` | Encode pipeline | Accept optional `gpu_semaphore` parameter |
| `pyflows/ffmpeg.py` | FFmpeg process management | Replace singletons with `ProcessRegistry`+`ProgressRegistry` keyed by dispatch token |
| `pyflows/webhook.py` | Webhook handler | Write DB records instead of calling `encode_task`; remove `encode_task` parameter |
| `tests/test_tasks.py` | Task tests | Rewrite for dispatcher architecture |
| `tests/test_db.py` | DB tests | Add lease token and library exclusion tests |

---

## Phase 1: Foundations (lease tokens, process registry, thread separation)

### Task 1: Config fields

**Files:**
- Modify: `pyflows/config.py`

- [ ] **Step 1: Add concurrency fields to GeneralConfig**

```python
scan_check_interval: int = 60
encode_workers: int = 0  # 0 = use 'workers' value
gpu_slots: int = 2

@model_validator(mode="after")
def _resolve_and_validate_concurrency(self) -> "GeneralConfig":
    if self.encode_workers == 0:
        object.__setattr__(self, "encode_workers", self.workers)
    if self.encode_workers < 1:
        raise ValueError("encode_workers must be >= 1")
    if self.gpu_slots < 1:
        raise ValueError("gpu_slots must be >= 1")
    if self.scan_check_interval < 1:
        raise ValueError("scan_check_interval must be >= 1")
    return self
```

- [ ] **Step 2: Add max_concurrent to LibraryConfig**

```python
max_concurrent: int = 0  # 0 = unlimited

@field_validator("max_concurrent")
@classmethod
def _validate_max_concurrent(cls, v: int) -> int:
    if v < 0:
        raise ValueError("max_concurrent must be >= 0")
    return v
```

- [ ] **Step 3: Build and verify**

Run: `nix build .#packages.x86_64-linux.default`

- [ ] **Step 4: Commit**

```bash
git commit -m "feat(config): add concurrency control fields"
```

---

### Task 2: Dispatch lease tokens in DB

**Files:**
- Modify: `pyflows/db.py`
- Test: `tests/test_db.py`

- [ ] **Step 1: Add columns to schema migration**

In `_migrate_schema()`, add:
```python
if "dispatch_token" not in columns:
    self.conn.execute("ALTER TABLE files ADD COLUMN dispatch_token TEXT")
if "dispatched_at" not in columns:
    self.conn.execute("ALTER TABLE files ADD COLUMN dispatched_at TEXT")
if "needs_gpu" not in columns:
    self.conn.execute("ALTER TABLE files ADD COLUMN needs_gpu INTEGER DEFAULT 0")
```

Update `FileRecord` TypedDict to include `dispatch_token: str | None`, `dispatched_at: str | None`, `needs_gpu: int`.

- [ ] **Step 2: Add claim_with_token (dispatcher phase)**

```python
def claim_with_token(self, path: str, token: str, needs_gpu: bool = False) -> bool:
    cur = self.conn.execute(
        "UPDATE files SET status=?, dispatched_at=?, next_retry_at=NULL, "
        "dispatch_token=?, needs_gpu=?, started_at=NULL "
        "WHERE path=? AND status=?",
        (FileStatus.PROCESSING, _utcnow_iso(), token, int(needs_gpu),
         path, FileStatus.PENDING),
    )
    self.conn.commit()
    return cur.rowcount > 0
```

Note: `started_at=NULL` — set by dispatcher but not "started". The worker sets `started_at` atomically.

- [ ] **Step 3: Add start_encode (worker phase — atomic)**

```python
def start_encode(self, path: str, token: str) -> bool:
    cur = self.conn.execute(
        "UPDATE files SET started_at=? "
        "WHERE path=? AND dispatch_token=? AND status=? AND started_at IS NULL",
        (_utcnow_iso(), path, token, FileStatus.PROCESSING),
    )
    self.conn.commit()
    return cur.rowcount > 0
```

This is the critical atomic gate. Requires: correct token AND status=PROCESSING AND started_at IS NULL. Two concurrent deliveries: only one succeeds.

- [ ] **Step 4: Add release_claim**

```python
def release_claim(self, path: str, token: str) -> bool:
    cur = self.conn.execute(
        "UPDATE files SET status=?, dispatch_token=NULL, dispatched_at=NULL, "
        "started_at=NULL, needs_gpu=0 "
        "WHERE path=? AND dispatch_token=?",
        (FileStatus.PENDING, path, token),
    )
    self.conn.commit()
    return cur.rowcount > 0
```

- [ ] **Step 5: Add token-guarded terminal methods**

```python
def complete_with_token(self, path: str, token: str,
                        output_codec: str = "", output_size: int = 0) -> bool:
    cur = self.conn.execute(
        "UPDATE files SET status=?, output_codec=?, output_size=?, completed_at=?, "
        "retry_count=0, next_retry_at=NULL, hold_until=NULL, "
        "dispatch_token=NULL, dispatched_at=NULL, needs_gpu=0, "
        "commit_temp_path=NULL, commit_target_path=NULL "
        "WHERE path=? AND dispatch_token=? AND status='committing'",
        (FileStatus.COMPLETED, output_codec, output_size, _utcnow_iso(), path, token),
    )
    self.conn.commit()
    return cur.rowcount > 0

def fail_with_token(self, path: str, token: str, error: str = "") -> bool:
    cur = self.conn.execute(
        "UPDATE files SET status=?, error=?, completed_at=?, next_retry_at=NULL, "
        "dispatch_token=NULL, dispatched_at=NULL, needs_gpu=0 "
        "WHERE path=? AND dispatch_token=?",
        (FileStatus.FAILED, error, _utcnow_iso(), path, token),
    )
    self.conn.commit()
    return cur.rowcount > 0

def skip_with_token(self, path: str, token: str) -> bool:
    cur = self.conn.execute(
        "UPDATE files SET status=?, completed_at=?, "
        "dispatch_token=NULL, dispatched_at=NULL, needs_gpu=0 "
        "WHERE path=? AND dispatch_token=? AND status=?",
        (FileStatus.SKIPPED, _utcnow_iso(), path, token, FileStatus.PROCESSING),
    )
    self.conn.commit()
    return cur.rowcount > 0
```

- [ ] **Step 6: Add retry_with_token and rename_with_token**

```python
def retry_with_token(self, path: str, token: str, error: str,
                     retry_count: int, next_retry_at: datetime) -> bool:
    cur = self.conn.execute(
        "UPDATE files SET status=?, error=?, retry_count=?, next_retry_at=?, "
        "started_at=NULL, completed_at=NULL, dispatch_token=NULL, "
        "dispatched_at=NULL, needs_gpu=0 "
        "WHERE path=? AND dispatch_token=?",
        (FileStatus.PENDING, error, retry_count, next_retry_at.isoformat(),
         path, token),
    )
    self.conn.commit()
    return cur.rowcount > 0

def rename_with_token(self, old_path: str, new_path: str, token: str) -> bool:
    cur = self.conn.execute(
        "UPDATE files SET path=? WHERE path=? AND dispatch_token=?",
        (new_path, old_path, token),
    )
    self.conn.commit()
    return cur.rowcount > 0

def transition_to_committing(self, path: str, token: str) -> bool:
    cur = self.conn.execute(
        "UPDATE files SET status='committing' "
        "WHERE path=? AND dispatch_token=? AND status=?",
        (path, token, FileStatus.PROCESSING),
    )
    self.conn.commit()
    return cur.rowcount > 0
```

- [ ] **Step 7: Add capacity counting methods (include COMMITTING)**

```python
def count_by_statuses(self, statuses: tuple[str, ...]) -> int:
    placeholders = ",".join("?" for _ in statuses)
    row = self.conn.execute(
        f"SELECT count(*) FROM files WHERE status IN ({placeholders})", statuses
    ).fetchone()
    return row[0]

def library_active_counts(self, statuses: tuple[str, ...]) -> dict[str, int]:
    placeholders = ",".join("?" for _ in statuses)
    rows = self.conn.execute(
        f"SELECT library, count(*) FROM files WHERE status IN ({placeholders}) GROUP BY library",
        statuses,
    ).fetchall()
    return {str(row[0]): row[1] for row in rows}

def gpu_active_count(self, statuses: tuple[str, ...]) -> int:
    placeholders = ",".join("?" for _ in statuses)
    row = self.conn.execute(
        f"SELECT count(*) FROM files WHERE status IN ({placeholders}) AND needs_gpu=1",
        statuses,
    ).fetchone()
    return row[0]

def get_pending_batch(
    self,
    exclude_libraries: set[str] | None = None,
    priority_codecs: list[str] | None = None,
    limit: int = 20,
) -> list[FileRecord]:
    """Fetch a batch of pending files in priority order for the dispatcher."""
    # Same query as get_next_pending but returns multiple rows
    # and supports library exclusion
    ...  # implementation mirrors get_next_pending with LIMIT=limit
```

- [ ] **Step 8: Add get_next_pending_excluding_libraries**

Replaces both `get_next_pending` and `get_next_pending_excluding_libraries` for dispatch. Accepts `exclude_libraries`, `exclude_gpu`, and `priority_codecs`. Builds conditions dynamically:

```python
def get_next_dispatchable(
    self,
    exclude_libraries: set[str] | None = None,
    exclude_gpu: bool = False,
    priority_codecs: list[str] | None = None,
    config: "PyflowsConfig | None" = None,
    now: datetime | None = None,
) -> FileRecord | None:
    if now is None:
        now = _utcnow()
    now_iso = now.isoformat()

    conditions = [
        "status = 'pending'",
        "(next_retry_at IS NULL OR next_retry_at <= ?)",
        "(hold_until IS NULL OR hold_until <= ?)",
    ]
    params: list[object] = [now_iso, now_iso]

    if exclude_libraries:
        placeholders = ",".join("?" for _ in exclude_libraries)
        conditions.append(f"library NOT IN ({placeholders})")
        params.extend(exclude_libraries)

    if exclude_gpu and config is not None:
        # Exclude files that would need GPU encoding.
        # A file needs GPU if its video_codec is NOT in the profile's skip_codecs
        # AND the profile's encoder is "vaapi".
        # Since we can't join to config in SQL, we use a simpler heuristic:
        # exclude files whose video_codec is NOT in any profile's skip_codecs.
        # This is conservative (may skip some CPU-only files) but safe.
        all_skip_codecs = set()
        for p in config.profiles.values():
            all_skip_codecs.update(p.video.skip_codecs)
        if all_skip_codecs:
            skip_placeholders = ",".join("?" for _ in all_skip_codecs)
            conditions.append(
                f"(lower(coalesce(video_codec, '')) IN ({skip_placeholders}))"
            )
            params.extend(c.lower() for c in all_skip_codecs)
        else:
            # No skip_codecs defined = all files would need GPU = skip all
            return None

    where = " AND ".join(conditions)
    # ... priority ordering same as get_next_pending ...
```

- [ ] **Step 8: Add retry_with_token and rename_with_token**

```python
def retry_with_token(self, path: str, token: str, error: str,
                     retry_count: int, next_retry_at: datetime) -> bool:
    cur = self.conn.execute(
        "UPDATE files SET status=?, error=?, retry_count=?, next_retry_at=?, "
        "started_at=NULL, completed_at=NULL, dispatch_token=NULL, "
        "dispatched_at=NULL, needs_gpu=0 "
        "WHERE path=? AND dispatch_token=?",
        (FileStatus.PENDING, error, retry_count, next_retry_at.isoformat(),
         path, token),
    )
    self.conn.commit()
    return cur.rowcount > 0

def rename_with_token(self, old_path: str, new_path: str, token: str) -> bool:
    cur = self.conn.execute(
        "UPDATE files SET path=? WHERE path=? AND dispatch_token=?",
        (new_path, old_path, token),
    )
    self.conn.commit()
    return cur.rowcount > 0
```

- [ ] **Step 8: Update reset_processing to clear all dispatch fields**

```python
def reset_processing(self) -> int:
    cur = self.conn.execute(
        "UPDATE files SET status=?, started_at=NULL, dispatched_at=NULL, "
        "dispatch_token=NULL, needs_gpu=0 WHERE status=?",
        (FileStatus.PENDING, FileStatus.PROCESSING),
    )
    self.conn.commit()
    return cur.rowcount
```

- [ ] **Step 9: Write tests**

```python
def test_two_phase_claim(tmp_path):
    with FileDB(str(tmp_path / "test.db")) as db:
        db.upsert("/media/test.mkv", "Movies", "movie", "abc", 1000, "h264")
        assert db.claim_with_token("/media/test.mkv", "tok-1") is True
        # Worker phase: only one start succeeds
        assert db.start_encode("/media/test.mkv", "tok-1") is True
        assert db.start_encode("/media/test.mkv", "tok-1") is False  # duplicate delivery

def test_stale_token_rejected_after_reset(tmp_path):
    with FileDB(str(tmp_path / "test.db")) as db:
        db.upsert("/media/test.mkv", "Movies", "movie", "abc", 1000, "h264")
        db.claim_with_token("/media/test.mkv", "old-token")
        db.reset_processing()
        assert db.start_encode("/media/test.mkv", "old-token") is False

def test_token_guarded_complete(tmp_path):
    with FileDB(str(tmp_path / "test.db")) as db:
        db.upsert("/media/test.mkv", "Movies", "movie", "abc", 1000, "h264")
        db.claim_with_token("/media/test.mkv", "tok-1")
        db.start_encode("/media/test.mkv", "tok-1")
        assert db.complete_with_token("/media/test.mkv", "tok-1", output_codec="hevc") is True
        assert db.complete_with_token("/media/test.mkv", "wrong-tok") is False

def test_release_claim(tmp_path):
    with FileDB(str(tmp_path / "test.db")) as db:
        db.upsert("/media/test.mkv", "Movies", "movie", "abc", 1000, "h264")
        db.claim_with_token("/media/test.mkv", "tok-1")
        assert db.release_claim("/media/test.mkv", "tok-1") is True
        record = db.get("/media/test.mkv")
        assert record["status"] == "pending"
        assert record["dispatch_token"] is None

def test_gpu_processing_count(tmp_path):
    with FileDB(str(tmp_path / "test.db")) as db:
        db.upsert("/media/a.mkv", "Movies", "movie", "a", 1000, "h264")
        db.upsert("/media/b.mkv", "Movies", "movie", "b", 2000, "hevc")
        db.claim_with_token("/media/a.mkv", "tok-1", needs_gpu=True)
        db.claim_with_token("/media/b.mkv", "tok-2", needs_gpu=False)
        assert db.gpu_processing_count() == 1
```

- [ ] **Step 9: Build and verify**

Run: `nix build .#packages.x86_64-linux.default`

- [ ] **Step 10: Commit**

```bash
git commit -m "feat(db): add dispatch lease tokens for safe multi-worker"
```

---

### Task 3: Multi-process FFmpeg registry

**Files:**
- Modify: `pyflows/ffmpeg.py`

- [ ] **Step 1: Replace _ActiveProcess singleton with ProcessRegistry**

```python
class ProcessRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._procs: dict[str, subprocess.Popen] = {}  # type: ignore[type-arg]

    def register(self, key: str, proc: subprocess.Popen) -> None:  # type: ignore[type-arg]
        with self._lock:
            self._procs[key] = proc

    def unregister(self, key: str) -> None:
        with self._lock:
            self._procs.pop(key, None)

    def terminate_all(self) -> None:
        with self._lock:
            procs = list(self._procs.values())
        for proc in procs:
            try:
                proc.terminate()
            except OSError:
                pass

_process_registry = ProcessRegistry()
```

- [ ] **Step 2: Replace ProgressTracker singleton with ProgressRegistry**

```python
class ProgressRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[str, EncodeProgress] = {}

    def update(self, key: str, out_time_us: int, speed: float) -> None:
        with self._lock:
            self._data[key] = EncodeProgress(out_time_us=out_time_us, speed=speed, file_path=key)

    def get(self, key: str) -> EncodeProgress | None:
        with self._lock:
            return self._data.get(key)

    def get_any_active(self) -> EncodeProgress:
        with self._lock:
            for p in self._data.values():
                return EncodeProgress(out_time_us=p.out_time_us, speed=p.speed, file_path=p.file_path)
        return EncodeProgress()

    def remove(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)

_progress_registry = ProgressRegistry()
```

- [ ] **Step 3: Update FFmpegCommand.run to accept and use a registry key**

Add optional `registry_key: str = ""` parameter to `run()`. When set, register/unregister with that key (the dispatch token). Update `_read_progress` to use `_progress_registry.update(registry_key, ...)`. Unregister in finally block. If no key provided, fall back to input file path for backward compatibility.

- [ ] **Step 4: Update terminate_active_encode**

```python
def terminate_active_encode() -> None:
    _process_registry.terminate_all()
```

- [ ] **Step 5: Update get_current_progress**

```python
def get_current_progress() -> EncodeProgress:
    return _progress_registry.get_any_active()
```

- [ ] **Step 6: Build and verify existing tests pass**

Run: `nix build .#packages.x86_64-linux.default`

- [ ] **Step 7: Commit**

```bash
git commit -m "feat(ffmpeg): multi-process registry for concurrent workers"
```

---

### Task 4: Separate scanner and maintenance from Huey

**Files:**
- Modify: `pyflows/tasks.py`

- [ ] **Step 1: Remove periodic tasks from init_huey**

Remove `scan_all` and `release_held_files` periodic task registrations. Remove `crontab` import and `EVERY_MINUTE` constant. Only keep the `encode` task, which now accepts a `dispatch_token` parameter:

```python
@huey.task()
def encode(file_path: str, profile_name: str, dispatch_token: str = "") -> None:
    _encode_file(file_path, profile_name, dispatch_token)
```

- [ ] **Step 2: Add scanner and maintenance thread loops**

```python
def _scanner_loop(shutdown: threading.Event) -> None:
    state = _get_state()
    config = state.config
    log_event(log, logging.INFO, "scanner_started", "Scanner thread started")
    while not shutdown.is_set():
        try:
            if state.scanning_enabled.is_set():
                with FileDB(config.general.db_path) as db:
                    for lib in config.libraries:
                        if not shutdown.is_set():
                            _scan_library_if_due(db, lib, respect_schedule=True)
        except Exception as exc:
            log_event(log, logging.ERROR, "scanner_error",
                      "Scanner iteration failed", error=str(exc))
        shutdown.wait(config.general.scan_check_interval)

def _maintenance_loop(shutdown: threading.Event) -> None:
    state = _get_state()
    config = state.config
    log_event(log, logging.INFO, "maintenance_started", "Maintenance thread started")
    # One-time codec backfill
    with FileDB(config.general.db_path) as db:
        rows = db.get_pending_without_codec()
        for row in rows:
            codec = _probe_codec(str(row["path"]), config.general.ffprobe_path)
            if codec:
                db.update_video_codec(str(row["path"]), codec)
    while not shutdown.is_set():
        try:
            _release_held_files()
        except Exception as exc:
            log_event(log, logging.ERROR, "maintenance_error",
                      "Maintenance iteration failed", error=str(exc))
        shutdown.wait(30)
```

- [ ] **Step 3: Remove encode_task calls from scanner and maintenance**

In `_scan_library_if_due()`, remove the loop calling `state.encode_task`. In `_release_held_files()`, remove the `state.encode_task` call. Both now only write DB records.

- [ ] **Step 4: Remove _select_best_file**

Delete the function entirely — the dispatcher handles priority.

- [ ] **Step 5: Build (expect test failures)**

Run: `nix build .#packages.x86_64-linux.default`

- [ ] **Step 6: Commit**

```bash
git commit -m "refactor(tasks): move scanner and maintenance to dedicated threads"
```

---

### Task 5: Implement the dispatcher

**Files:**
- Modify: `pyflows/tasks.py`

- [ ] **Step 1: Write _dispatch_once (testable single iteration)**

```python
def _needs_gpu(video_codec: str, profile_name: str, config: PyflowsConfig) -> bool:
    """Determine if encoding this file will use GPU."""
    if profile_name not in config.profiles:
        return False
    profile = config.profiles[profile_name]
    if profile.video.encoder != "vaapi":
        return False
    if video_codec in profile.video.skip_codecs:
        return False  # video will be copied, not encoded
    return True

def _dispatch_once() -> int:
    """Run one dispatch iteration. Returns number of files dispatched."""
    state = _get_state()
    config = state.config
    dispatched = 0

    if not state.encoding_enabled.is_set():
        return 0

    lib_limits = {lib.name: lib.max_concurrent for lib in config.libraries
                  if lib.max_concurrent > 0}

    with FileDB(config.general.db_path) as db:
        active_statuses = ("processing", "committing")
        active = db.count_by_statuses(active_statuses)
        capacity = config.general.encode_workers - active

        for _ in range(max(0, capacity)):
            # Re-query counts each iteration (per-claim, not per-batch)
            # Count both PROCESSING and COMMITTING for capacity
            lib_counts = db.library_active_counts(active_statuses) if lib_limits else {}
            gpu_active = db.gpu_active_count(active_statuses)

            saturated = {name for name, limit in lib_limits.items()
                         if lib_counts.get(name, 0) >= limit}

            # Fetch a batch of candidates in priority order
            candidates = db.get_pending_batch(
                exclude_libraries=saturated or None,
                priority_codecs=config.resolved_priority_codecs(),
                limit=20,
            )

            if not candidates:
                break

            # Find the first candidate that passes GPU admission
            claimed = False
            for candidate in candidates:
                path = str(candidate["path"])
                profile = str(candidate["profile"])
                codec = str(candidate.get("video_codec", ""))
                needs_gpu = _needs_gpu(codec, profile, config)

                if needs_gpu and gpu_active >= config.general.gpu_slots:
                    continue  # skip GPU file, try next (may be CPU-only)

            token = uuid.uuid4().hex

                token = uuid.uuid4().hex
                if not db.claim_with_token(path, token, needs_gpu=needs_gpu):
                    continue

                try:
                    state.encode_task(path, profile, token)
                    dispatched += 1
                    claimed = True
                    break  # claimed one, re-enter outer loop for fresh counts
                except Exception as exc:
                    db.release_claim(path, token)
                    log_event(log, logging.ERROR, "dispatch_enqueue_failed",
                              "Failed to enqueue encode task, releasing claim",
                              file_path=path, error=str(exc))

            if not claimed:
                break  # no dispatchable candidate found in batch

    return dispatched
```

def _dispatcher_loop(shutdown: threading.Event) -> None:
    state = _get_state()
    log_event(log, logging.INFO, "dispatcher_started", "Dispatcher thread started")
    while not shutdown.is_set():
        try:
            _dispatch_once()
        except Exception as exc:
            log_event(log, logging.ERROR, "dispatcher_error",
                      "Dispatcher iteration failed", error=str(exc))
        shutdown.wait(5)
```

- [ ] **Step 2: Rewrite _encode_file with two-phase claim and token-guarded ops**

All early returns either release the claim or fail with token. No path leaves a file stuck in PROCESSING.

```python
def _encode_file(file_path: str, profile_name: str, dispatch_token: str = "") -> None:
    state = _get_state()
    config = state.config

    # Reject tasks without a fencing token (stale pre-upgrade Huey tasks)
    if not dispatch_token:
        log_event(log, logging.WARNING, "encode_no_token",
                  "Rejecting encode task without fencing token", file_path=file_path)
        return

    # Pause handling: release claim and return
    if not state.encoding_enabled.is_set():
        with FileDB(config.general.db_path) as db:
                db.release_claim(file_path, dispatch_token)
        return

    if profile_name not in config.profiles:
        with FileDB(config.general.db_path) as db:
            db.fail_with_token(file_path, dispatch_token, error=f"Unknown profile: {profile_name}")
        return

    # Atomic worker start (prevents duplicate delivery)
    with FileDB(config.general.db_path) as db:
        if not db.start_encode(file_path, dispatch_token):
                log_event(log, logging.DEBUG, "encode_start_failed",
                          "Could not start encode (stale token or duplicate delivery)",
                          file_path=file_path)
                return

    if not Path(file_path).exists():
        with FileDB(config.general.db_path) as db:
            db.fail_with_token(file_path, dispatch_token, error="File no longer exists")
        return

    profile = config.profiles[profile_name]
    notifier = Notifier(config.notifications)

    # Pre-encode hooks
    if config.hooks.pre_encode:
        if not run_hooks(config.hooks.pre_encode, "pre_encode", file_path,
                       profile=profile_name, timeout=config.hooks.timeout):
            with FileDB(config.general.db_path) as db:
                db.fail_with_token(file_path, dispatch_token, error="pre_encode hook failed")
            return

    # Encode (DB connection NOT held during FFmpeg)
    # Pass dispatch_token as registry_key so ProcessRegistry/ProgressRegistry use it
    try:
        result = encode_file(
            input_path=file_path, profile=profile,
            temp_dir=config.general.temp_dir,
            vaapi_device=config.general.vaapi_device,
            ffmpeg_path=config.general.ffmpeg_path,
            ffprobe_path=config.general.ffprobe_path,
            hardware_config=config.hardware,
            stall_timeout=config.general.stall_timeout,
            startup_timeout=config.general.startup_timeout,
            gpu_semaphore=state.gpu_semaphore,
            registry_key=dispatch_token,
            replace_original=False,
        )
    except Exception as exc:
        with FileDB(config.general.db_path) as db:
            db.fail_with_token(file_path, dispatch_token, error=str(exc))
        log_event(log, logging.ERROR, "encode_exception",
                  "Unexpected exception during encode",
                  file_path=file_path, error=str(exc))
        return

    # Handle result — all ops are token-guarded
    with FileDB(config.general.db_path) as db:
        if result.status == EncodeStatus.SKIPPED:
            db.skip_with_token(file_path, dispatch_token)
            run_hooks(config.hooks.on_skip, "on_skip", file_path,
                      profile=profile_name, status="skipped",
                      timeout=config.hooks.timeout)
        elif result.status == EncodeStatus.COMPLETED:
            _handle_encode_success(db, file_path, result.final_path,
                                   profile_name, profile, notifier, config,
                                   dispatch_token)
        else:
            _handle_encode_failure(db, file_path, result.error, result.transient,
                                   profile_name, config, notifier, dispatch_token)
```

Note: `_handle_encode_success` and `_handle_encode_failure` must accept `dispatch_token` and use token-guarded methods.
- `db.update_status(COMPLETED)` → `db.complete_with_token(path, token, ...)`
- `db.update_status(FAILED)` → `db.fail_with_token(path, token, ...)`
- `db.schedule_retry(...)` → `db.retry_with_token(path, token, ...)`
- `db.rename_path(old, new)` → `db.rename_with_token(old, new, token)`

**Critical: fenced filesystem commit via COMMITTING state.**

A token check followed by a file rename has a TOCTOU race. The solution:

1. `encode_file(replace_original=False)` returns temp output path without replacing the original.
2. `_handle_encode_success` atomically transitions to COMMITTING: `UPDATE SET status='committing' WHERE path=? AND dispatch_token=? AND status='processing'`. If this fails, the worker abandons the temp file.
3. Only after COMMITTING succeeds does the worker perform the filesystem rename.
4. After rename, transition to COMPLETED via `complete_with_token`.
5. `reset_processing()` does NOT reset COMMITTING files. A separate `reset_committing()` handles abandoned commits at startup.

Add `COMMITTING = "committing"` to `FileStatus`. Update `VALID_TRANSITIONS`:
```python
FileStatus.PROCESSING: {..., FileStatus.COMMITTING},
FileStatus.COMMITTING: {FileStatus.COMPLETED, FileStatus.FAILED},
```

- [ ] **Step 3: Add GPU semaphore to DaemonState**

```python
@dataclass
class DaemonState:
    config: PyflowsConfig
    huey: SqliteHuey
    encode_task: Callable[..., object]
    scanning_enabled: threading.Event = field(default_factory=threading.Event)
    encoding_enabled: threading.Event = field(default_factory=threading.Event)
    watcher_enabled: threading.Event = field(default_factory=threading.Event)
    gpu_semaphore: threading.BoundedSemaphore = field(init=False)

    def __post_init__(self) -> None:
        self.scanning_enabled.set()
        self.encoding_enabled.set()
        self.watcher_enabled.set()
        self.gpu_semaphore = threading.BoundedSemaphore(self.config.general.gpu_slots)
```

- [ ] **Step 4: Commit**

```bash
git commit -m "feat(tasks): add dispatcher with lease tokens, pause-safe workers"
```

---

### Task 6: Wire everything into start_daemon

**Files:**
- Modify: `pyflows/tasks.py`, `pyflows/webhook.py`

- [ ] **Step 1: Rewrite start_daemon**

Key changes:
- Remove synchronous initial scan
- Remove codec backfill (moved to maintenance thread)
- Start scanner, maintenance, dispatcher as named threads (not daemon — join on shutdown)
- Store all thread references
- Remove `encode_task` from `_MediaFileHandler` init (dead parameter)
- Huey consumer uses `config.general.encode_workers` threads

- [ ] **Step 2: Update webhook to write DB records only**

Remove `encode_task` parameter from `start_webhook_server()`. In `_queue_encode()`, replace `self.encode_task(local_path, profile)` with `db.upsert(...)`.

- [ ] **Step 3: Clean shutdown with join**

```python
try:
    shutdown.wait()
finally:
    shutdown.set()
    terminate_active_encode()  # SIGTERM all active FFmpeg processes
    consumer.stop()
    for t in [scanner_thread, maintenance_thread, dispatcher_thread]:
        if t is not None:
            t.join(timeout=10)
    if handler is not None:
        handler.stop()
    if observer is not None:
        observer.stop()
        observer.join(timeout=10)
    if webhook_server is not None:
        webhook_server.shutdown()
    if metrics_stop is not None:
        metrics_stop.set()
    log_event(log, logging.INFO, "daemon_stopped", "pyflows daemon stopped")
```

Update `ProcessRegistry.terminate_all()` to SIGTERM, wait, then SIGKILL:
```python
def terminate_all(self) -> None:
    with self._lock:
        procs = list(self._procs.values())
    for proc in procs:
        try:
            proc.terminate()
        except OSError:
            pass
    for proc in procs:
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()  # reap zombie
```

- [ ] **Step 4: Build and fix issues**

Run: `nix build .#packages.x86_64-linux.default`

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(tasks): wire dispatcher architecture into start_daemon"
```

---

### Task 7: Pipeline changes (GPU semaphore, registry key, deferred replacement)

**Files:**
- Modify: `pyflows/pipeline.py`

- [ ] **Step 1: Add all new parameters to encode_file**

```python
def encode_file(
    ...,
    gpu_semaphore: threading.BoundedSemaphore | None = None,
    registry_key: str = "",
    replace_original: bool = True,
) -> EncodeResult:
```

When `replace_original=False`, skip file replacement logic and return `EncodeResult` with `final_path` set to the temp output file. The caller handles the fenced COMMITTING transition.

Pass `registry_key` to `FFmpegCommand.run(registry_key=registry_key)` so ProcessRegistry/ProgressRegistry use the fencing token.

- [ ] **Step 2: Acquire/release around VAAPI encode with try/finally**

```python
    gpu_acquired = False
    try:
        if use_vaapi and gpu_semaphore is not None:
            gpu_semaphore.acquire()
            gpu_acquired = True

        result = cmd.run(...)

        if result.returncode != 0:
            if gpu_acquired:
                gpu_semaphore.release()
                gpu_acquired = False
            # CPU fallback (no semaphore needed)
            ...
    finally:
        if gpu_acquired:
            gpu_semaphore.release()
```

Where `use_vaapi` is determined by `profile.video.encoder == "vaapi"` AND `plan.video.action != "copy"`.

- [ ] **Step 3: Build and verify**

Run: `nix build .#packages.x86_64-linux.default`

- [ ] **Step 4: Commit**

```bash
git commit -m "feat(pipeline): GPU semaphore for VAAPI admission control"
```

---

### Task 8: Tests

**Files:**
- Modify: `tests/test_tasks.py`, `tests/test_db.py`

- [ ] **Step 1: Update existing tests for new architecture**

- Remove `_select_best_file` tests (function deleted)
- Update `test_release_held_files` to assert DB state only (no encode_task call)
- Update DaemonState mocking to include `gpu_semaphore`

- [ ] **Step 2: Add dispatcher tests using _dispatch_once**

```python
def test_dispatch_once_claims_and_enqueues(tmp_config):
    # Insert pending file, run _dispatch_once, verify claimed + dispatched

def test_dispatch_once_respects_capacity(tmp_config):
    # Set encode_workers=1, 1 already processing, verify nothing dispatched

def test_dispatch_once_rollback_on_enqueue_failure(tmp_config):
    # Mock encode_task to raise, verify file returned to PENDING

def test_dispatch_once_skips_saturated_library(tmp_config):
    # Library max_concurrent=1, 1 processing, verify different library dispatched

def test_dispatch_once_per_library_count_per_claim(tmp_config):
    # Library max_concurrent=1, 2 pending, verify only 1 dispatched per iteration

def test_dispatch_once_gpu_capacity(tmp_config):
    # gpu_slots=1, 1 GPU file processing, verify GPU files skipped but CPU files dispatched

def test_dispatch_once_needs_gpu_detection(tmp_config):
    # hevc file with skip_codecs=[hevc] → needs_gpu=False (copy)
    # h264 file with encoder=vaapi → needs_gpu=True
```

- [ ] **Step 3: Add two-phase claim and token lifecycle tests**

```python
def test_stale_huey_task_rejected(tmp_config):
    # Claim with token A, reset_processing, call start_encode with token A → False

def test_duplicate_delivery_second_start_fails(tmp_config):
    # Claim file, start_encode twice with same token → first True, second False

def test_worker_releases_claim_when_paused(tmp_config):
    # Claim file, clear encoding_enabled, call _encode_file, verify file back to PENDING

def test_unknown_profile_fails_with_token(tmp_config):
    # Claim file with bad profile, call _encode_file, verify file FAILED

def test_missing_file_fails_with_token(tmp_config):
    # Claim non-existent file, call _encode_file, verify file FAILED

def test_token_guarded_complete_rejects_wrong_token(tmp_config):
    # Complete with wrong token → returns False, record unchanged
```

- [ ] **Step 4: Build and verify all tests pass**

Run: `nix build .#packages.x86_64-linux.default`

- [ ] **Step 5: Commit**

```bash
git commit -m "test: add dispatcher, fencing token, and admission control tests"
```

---

### Task 9: Crash recovery

**Files:**
- Modify: `pyflows/tasks.py`, `pyflows/db.py`

- [ ] **Step 1: Add commit metadata columns and reset_committing to db.py**

Add `commit_temp_path` and `commit_target_path` columns to track the filesystem commit state. These are set when transitioning to COMMITTING and cleared on completion.

In `transition_to_committing`:
```python
def transition_to_committing(self, path: str, token: str,
                              temp_path: str, target_path: str) -> bool:
    cur = self.conn.execute(
        "UPDATE files SET status='committing', "
        "commit_temp_path=?, commit_target_path=? "
        "WHERE path=? AND dispatch_token=? AND status=?",
        (temp_path, target_path, path, token, FileStatus.PROCESSING),
    )
    self.conn.commit()
    return cur.rowcount > 0
```

`reset_committing` reconciles filesystem state:
```python
def reset_committing(self) -> int:
    rows = self.conn.execute(
        "SELECT path, commit_temp_path, commit_target_path FROM files "
        "WHERE status='committing'"
    ).fetchall()
    count = 0
    for row in rows:
        path = row[0]
        target = row[2]
        # If target exists, the rename succeeded before crash → mark completed
        if target and Path(target).exists():
            self.conn.execute(
                "UPDATE files SET status=?, completed_at=?, "
                "commit_temp_path=NULL, commit_target_path=NULL, "
                "dispatch_token=NULL, dispatched_at=NULL, needs_gpu=0 "
                "WHERE path=?",
                (FileStatus.COMPLETED, _utcnow_iso(), path),
            )
        else:
            # Rename didn't happen → mark failed, clean temp
            temp = row[1]
            if temp and Path(temp).exists():
                Path(temp).unlink(missing_ok=True)
            self.conn.execute(
                "UPDATE files SET status=?, error='interrupted during commit', "
                "commit_temp_path=NULL, commit_target_path=NULL, "
                "dispatch_token=NULL, dispatched_at=NULL, needs_gpu=0 "
                "WHERE path=?",
                (FileStatus.FAILED, path),
            )
        count += 1
    self.conn.commit()
    return count
```

- [ ] **Step 2: Update reset_processing to clear dispatch fields**

```python
def reset_processing(self) -> int:
    cur = self.conn.execute(
        "UPDATE files SET status=?, started_at=NULL, dispatched_at=NULL, "
        "dispatch_token=NULL, needs_gpu=0 WHERE status=?",
        (FileStatus.PENDING, FileStatus.PROCESSING),
    )
    self.conn.commit()
    return cur.rowcount
```

- [ ] **Step 3: Call both in start_daemon startup sequence**

```python
with FileDB(config.general.db_path) as db:
    reset = db.reset_processing()
    if reset > 0:
        log_event(log, logging.INFO, "processing_reset", count=reset)
    committed = db.reset_committing()
    if committed > 0:
        log_event(log, logging.WARNING, "committing_reset", count=committed)
```

Stale Huey tasks from previous runs are harmless — `start_encode()` rejects them (token cleared by reset).

- [ ] **Step 4: Commit**

```bash
git commit -m "feat(tasks): crash recovery for COMMITTING state"
```

---

## Verification

1. `nix build` — all tests pass
2. Two-phase claim: `start_encode` atomic gate prevents duplicate delivery
3. Token-guarded terminal ops: stale workers can't overwrite newer attempt's state
4. `_dispatch_once` directly testable — all edge cases covered including GPU capacity
5. GPU dispatch: GPU files skipped when `gpu_slots` reached; CPU files dispatch freely
6. Per-library limits checked per-claim (no batch overcommit)
7. Pause releases claimed files back to PENDING via `release_claim`
8. Enqueue failure rolls back claim
9. All early returns in `_encode_file` either `release_claim` or `fail_with_token`
10. `terminate_active_encode()` kills ALL active FFmpeg processes via registry
11. Clean shutdown joins all threads with timeout
12. Config validation: `gpu_slots>=1`, `scan_check_interval>=1` enforced at load
13. Backward compatible: `workers=1, gpu_slots=2, max_concurrent=0` behaves identically
14. Deploy to rhea container with `encode_workers: 2, gpu_slots: 1`, verify concurrent encoding

---

## Risks and Mitigations

| Risk | Mitigation |
|------|-----------|
| Stale Huey task replay | `start_encode(path, token)` atomic gate — `WHERE started_at IS NULL` |
| Duplicate Huey delivery | Same atomic gate — second delivery gets rowcount=0 |
| Stale worker overwrites new attempt | All terminal ops require matching `dispatch_token` |
| Orphaned FFmpeg processes | ProcessRegistry keyed by dispatch token; `terminate_all()` on shutdown |
| Claim stuck on enqueue failure | try/except rolls back via `release_claim()` |
| Early return leaves file stuck | Every return path calls `release_claim` or `fail_with_token` |
| Pause leaves file processing | Worker calls `release_claim()` when paused |
| GPU worker starvation | Dispatcher checks `gpu_processing_count()` before dispatching GPU jobs |
| Per-library overcommit | Re-query `library_processing_counts()` before each claim |
| Thread shutdown unclean | All threads joined with timeout on SIGTERM |
| Invalid config | Pydantic validators: `gpu_slots>=1`, `scan_check_interval>=1` |
| SQLite contention | WAL mode + busy_timeout=5000 (existing) |
