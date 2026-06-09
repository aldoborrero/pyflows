# Concurrency Control Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace Huey with a central dispatcher + ThreadPoolExecutor architecture. SQLite is the sole queue authority. Scanning and maintenance run in dedicated threads. Admission controls gate GPU and per-library concurrency.

**Architecture:**
```
Watcher thread ───────┐
Scanner thread ───────┼──> SQLite files table (PENDING records)
Webhook handler ──────┘
                             |
                      Dispatcher thread
                      (polls DB, checks capacity, claims with fencing token)
                             |
                      ThreadPoolExecutor(max_workers=encode_workers)
                             |
                      FFmpeg subprocesses
```

SQLite is the single durable queue. The dispatcher is the only component that transitions files from PENDING to PROCESSING. Workers verify ownership via fencing tokens. On restart, `reset_processing()` returns incomplete work to PENDING; the dispatcher resubmits.

**Tech Stack:** Python threading, `concurrent.futures.ThreadPoolExecutor`, SQLite (WAL), `threading.BoundedSemaphore` (GPU safety net), UUID fencing tokens

**Terminology:** Tokens are *fencing tokens* — attempt identifiers with no expiry. Stale attempts are recovered at startup. Appropriate for a single-daemon system.

---

## Design Decisions

### 1. Remove Huey, use ThreadPoolExecutor

Huey creates a second persistent queue (`huey.db`) alongside the `files` table, causing stale task replay, duplicate delivery, and synchronization complexity. `ThreadPoolExecutor` eliminates all of this — submissions are in-memory, lost on restart (harmless, since startup recovery re-queues), and duplicate delivery is impossible.

### 2. Fencing tokens

The dispatcher generates a UUID per dispatch and stores it in `dispatch_token`. The worker atomically claims the token via `start_encode(path, token)` (`UPDATE ... WHERE started_at IS NULL`). All terminal operations require the matching token. This protects against: manual resets during encoding, late worker completion after redispatch, and filesystem commit ownership.

### 3. Two-phase filesystem commit

Encode produces a temp file. Before replacing the original, the worker transitions to COMMITTING (storing temp and target paths). Only then does the filesystem rename happen. On crash, `reset_committing()` reconciles: checks if the target file has the expected output size (stored in DB) to determine whether the rename completed.

### 4. GPU admission in the dispatcher

The dispatcher fetches a batch of candidates and evaluates `_needs_gpu()` per-file (using each file's own profile). GPU files are skipped when `gpu_slots` is reached; CPU-only files behind them are dispatched freely. A `BoundedSemaphore` in the worker is a safety net but should never block under normal dispatch.

### 5. Per-library limits from DB

No in-memory counters. The dispatcher queries `SELECT library, count(*) FROM files WHERE status IN ('processing','committing') GROUP BY library` before each claim.

### 6. Capacity includes COMMITTING

All capacity checks (total workers, GPU slots, per-library) count both PROCESSING and COMMITTING statuses.

---

## File Structure

| File | Change |
|------|--------|
| `pyflows/tasks.py` | Major rewrite: remove Huey, add ThreadPoolExecutor, dispatcher, scanner/maintenance threads |
| `pyflows/config.py` | Add `scan_check_interval`, `encode_workers`, `gpu_slots`; add `max_concurrent` to LibraryConfig; remove Huey-specific config |
| `pyflows/db.py` | Add `dispatch_token`, `dispatched_at`, `needs_gpu`, `commit_temp_path`, `commit_target_path` columns; fencing token methods; batch query; capacity counts |
| `pyflows/pipeline.py` | Add `gpu_semaphore`, `registry_key`, `replace_original` parameters |
| `pyflows/ffmpeg.py` | Replace singletons with `ProcessRegistry` + `ProgressRegistry` keyed by token |
| `pyflows/webhook.py` | Write DB records only; remove `encode_task` parameter |
| `pyproject.toml` | Remove `huey` dependency |
| `nix/package.nix` | Remove `huey` from Nix deps |
| `tests/test_tasks.py` | Rewrite for executor-based architecture |
| `tests/test_db.py` | Add fencing token and capacity tests |

---

## Task 1: Config changes

**Files:** `pyflows/config.py`

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
- [ ] **Step 4: Commit** `feat(config): add concurrency control fields`

---

## Task 2: DB schema and fencing token methods

**Files:** `pyflows/db.py`, `tests/test_db.py`

- [ ] **Step 1: Add columns in _migrate_schema**

```python
if "dispatch_token" not in columns:
    self.conn.execute("ALTER TABLE files ADD COLUMN dispatch_token TEXT")
if "dispatched_at" not in columns:
    self.conn.execute("ALTER TABLE files ADD COLUMN dispatched_at TEXT")
if "needs_gpu" not in columns:
    self.conn.execute("ALTER TABLE files ADD COLUMN needs_gpu INTEGER DEFAULT 0")
if "commit_temp_path" not in columns:
    self.conn.execute("ALTER TABLE files ADD COLUMN commit_temp_path TEXT")
if "commit_target_path" not in columns:
    self.conn.execute("ALTER TABLE files ADD COLUMN commit_target_path TEXT")
if "expected_output_size" not in columns:
    self.conn.execute("ALTER TABLE files ADD COLUMN expected_output_size INTEGER")
```

Update `FileRecord` TypedDict accordingly. Add `COMMITTING = "committing"` to `FileStatus`. Update `VALID_TRANSITIONS`:
```python
FileStatus.PROCESSING: {FileStatus.FAILED, FileStatus.SKIPPED, FileStatus.COMMITTING, FileStatus.PENDING},
FileStatus.COMMITTING: {FileStatus.COMPLETED, FileStatus.FAILED},
```

- [ ] **Step 2: Dispatcher claim method**

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

- [ ] **Step 3: Worker start gate**

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

- [ ] **Step 4: Release claim**

```python
def release_claim(self, path: str, token: str) -> bool:
    cur = self.conn.execute(
        "UPDATE files SET status=?, dispatch_token=NULL, dispatched_at=NULL, "
        "started_at=NULL, needs_gpu=0 "
        "WHERE path=? AND dispatch_token=? AND status=?",
        (FileStatus.PENDING, path, token, FileStatus.PROCESSING),
    )
    self.conn.commit()
    return cur.rowcount > 0
```

- [ ] **Step 5: Token-guarded terminal methods**

Each enforces the required source status:

```python
def transition_to_committing(self, path: str, token: str,
                              temp_path: str, target_path: str,
                              expected_size: int) -> bool:
    # requires status=PROCESSING
    ...WHERE path=? AND dispatch_token=? AND status='processing'

def complete_with_token(self, path: str, token: str, ...) -> bool:
    # requires status=COMMITTING
    # clears dispatch_token, dispatched_at, needs_gpu, commit_*
    ...WHERE path=? AND dispatch_token=? AND status='committing'

def fail_with_token(self, path: str, token: str, error: str) -> bool:
    # allows PROCESSING or COMMITTING
    ...WHERE path=? AND dispatch_token=? AND status IN ('processing','committing')

def skip_with_token(self, path: str, token: str) -> bool:
    # requires PROCESSING
    ...WHERE path=? AND dispatch_token=? AND status='processing'

def retry_with_token(self, path: str, token: str, error: str,
                     retry_count: int, next_retry_at: datetime) -> bool:
    # requires PROCESSING
    ...WHERE path=? AND dispatch_token=? AND status='processing'

def rename_with_token(self, old_path: str, new_path: str, token: str) -> bool:
    ...WHERE path=? AND dispatch_token=? AND status='committing'
```

- [ ] **Step 6: Capacity counting methods**

```python
def count_active(self) -> int:
    return self.conn.execute(
        "SELECT count(*) FROM files WHERE status IN ('processing','committing')"
    ).fetchone()[0]

def library_active_counts(self) -> dict[str, int]:
    rows = self.conn.execute(
        "SELECT library, count(*) FROM files "
        "WHERE status IN ('processing','committing') GROUP BY library"
    ).fetchall()
    return {str(r[0]): r[1] for r in rows}

def gpu_active_count(self) -> int:
    return self.conn.execute(
        "SELECT count(*) FROM files "
        "WHERE status IN ('processing','committing') AND needs_gpu=1"
    ).fetchone()[0]

def get_pending_batch(self, exclude_libraries: set[str] | None = None,
                      priority_codecs: list[str] | None = None,
                      limit: int = 20) -> list[FileRecord]:
    # Like get_next_pending but LIMIT=limit, optional library exclusion
    ...
```

- [ ] **Step 7: Crash recovery methods**

```python
def reset_processing(self) -> int:
    cur = self.conn.execute(
        "UPDATE files SET status=?, started_at=NULL, dispatched_at=NULL, "
        "dispatch_token=NULL, needs_gpu=0 WHERE status=?",
        (FileStatus.PENDING, FileStatus.PROCESSING),
    )
    self.conn.commit()
    return cur.rowcount

def reset_committing(self) -> int:
    rows = self.conn.execute(
        "SELECT path, commit_temp_path, commit_target_path, expected_output_size "
        "FROM files WHERE status='committing'"
    ).fetchall()
    count = 0
    for row in rows:
        path, temp, target, expected_size = row[0], row[1], row[2], row[3]
        # Check if target has the expected output size (rename succeeded)
        target_ok = (target and Path(target).exists() and
                     expected_size and Path(target).stat().st_size == expected_size)
        if target_ok:
            # Rename succeeded — update path to target if different (extension change)
            final_path = target if target != path else path
            self.conn.execute(
                "UPDATE files SET status=?, path=?, completed_at=?, "
                "commit_temp_path=NULL, commit_target_path=NULL, "
                "dispatch_token=NULL, dispatched_at=NULL, needs_gpu=0 WHERE path=?",
                (FileStatus.COMPLETED, final_path, _utcnow_iso(), path))
            # Clean up old original if extension changed and it still exists
            if target != path and Path(path).exists():
                Path(path).unlink(missing_ok=True)
        else:
            if temp and Path(temp).exists():
                Path(temp).unlink(missing_ok=True)
            self.conn.execute(
                "UPDATE files SET status=?, error='interrupted during commit', "
                "commit_temp_path=NULL, commit_target_path=NULL, "
                "dispatch_token=NULL WHERE path=?",
                (FileStatus.FAILED, path))
        count += 1
    self.conn.commit()
    return count
```

- [ ] **Step 8: Tests**

```python
def test_two_phase_claim(tmp_path): ...
def test_start_encode_rejects_duplicate(tmp_path): ...
def test_stale_token_after_reset(tmp_path): ...
def test_complete_requires_committing(tmp_path): ...
def test_skip_requires_processing(tmp_path): ...
def test_release_requires_processing(tmp_path): ...
def test_reset_committing_target_exists(tmp_path): ...
def test_reset_committing_target_missing(tmp_path): ...
def test_gpu_active_count_includes_committing(tmp_path): ...
def test_get_pending_batch_excludes_libraries(tmp_path): ...
```

- [ ] **Step 9: Build and verify**
- [ ] **Step 10: Commit** `feat(db): fencing tokens, COMMITTING state, capacity queries`

---

## Task 3: Multi-process FFmpeg registry

**Files:** `pyflows/ffmpeg.py`

- [ ] **Step 1: Replace _ActiveProcess with ProcessRegistry**

```python
class ProcessRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._procs: dict[str, subprocess.Popen] = {}

    def register(self, key: str, proc: subprocess.Popen) -> None:
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
        for proc in procs:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

_process_registry = ProcessRegistry()
```

- [ ] **Step 2: Replace ProgressTracker with ProgressRegistry**

Same pattern — `dict[str, EncodeProgress]` keyed by token. `get_any_active()` returns the first entry for the UI.

- [ ] **Step 3: Update FFmpegCommand.run to accept registry_key**

Register/unregister in try/finally. Update `_read_progress` to use registry.

- [ ] **Step 4: Update terminate_active_encode**

```python
def terminate_active_encode() -> None:
    _process_registry.terminate_all()
```

- [ ] **Step 5: Build and verify**
- [ ] **Step 6: Commit** `feat(ffmpeg): multi-process registry keyed by fencing token`

---

## Task 4: Pipeline changes

**Files:** `pyflows/pipeline.py`

- [ ] **Step 1: Add parameters to encode_file**

```python
def encode_file(
    ...,
    gpu_semaphore: threading.BoundedSemaphore | None = None,
    registry_key: str = "",
    replace_original: bool = True,
) -> EncodeResult:
```

- [ ] **Step 2: GPU semaphore acquire/release with try/finally**

```python
gpu_acquired = False
try:
    if use_vaapi and gpu_semaphore is not None:
        gpu_semaphore.acquire()
        gpu_acquired = True
    result = cmd.run(registry_key=registry_key, ...)
    if result.returncode != 0 and gpu_acquired:
        gpu_semaphore.release()
        gpu_acquired = False
        # CPU fallback...
finally:
    if gpu_acquired:
        gpu_semaphore.release()
```

- [ ] **Step 3: Deferred replacement mode**

When `replace_original=False`, skip file move logic. Set `EncodeResult.final_path` to the temp output file. Caller handles the fenced commit.

- [ ] **Step 4: Pass registry_key to cmd.run()**
- [ ] **Step 5: Build and verify**
- [ ] **Step 6: Commit** `feat(pipeline): GPU semaphore, deferred replacement, registry key`

---

## Task 5: Rewrite tasks.py — remove Huey, add executor and threads

**Files:** `pyflows/tasks.py`

This is the largest task. The entire file is restructured.

- [ ] **Step 1: Remove Huey imports and references**

Remove: `from huey import SqliteHuey, crontab`
Remove: `EVERY_MINUTE` constant
Remove: `init_huey()` function

- [ ] **Step 2: Rewrite DaemonState**

```python
@dataclass
class DaemonState:
    config: PyflowsConfig
    executor: ThreadPoolExecutor
    scanning_enabled: threading.Event = field(default_factory=threading.Event)
    encoding_enabled: threading.Event = field(default_factory=threading.Event)
    watcher_enabled: threading.Event = field(default_factory=threading.Event)
    gpu_semaphore: threading.BoundedSemaphore = field(init=False)
    shutdown: threading.Event = field(default_factory=threading.Event)
    pending_futures: dict = field(default_factory=dict)  # Future -> (path, token)
    futures_lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        self.scanning_enabled.set()
        self.encoding_enabled.set()
        self.watcher_enabled.set()
        self.gpu_semaphore = threading.BoundedSemaphore(self.config.general.gpu_slots)
```

- [ ] **Step 3: Write _needs_gpu helper**

```python
def _needs_gpu(video_codec: str, profile_name: str, config: PyflowsConfig) -> bool:
    if profile_name not in config.profiles:
        return False
    profile = config.profiles[profile_name]
    if profile.video.encoder != "vaapi":
        return False
    if video_codec in profile.video.skip_codecs:
        return False
    return True
```

- [ ] **Step 4: Write _dispatch_once**

```python
def _future_done(future: Future, state: DaemonState) -> None:
    with state.futures_lock:
        state.pending_futures.pop(future, None)

def _dispatch_once() -> int:
    state = _get_state()
    config = state.config
    dispatched = 0

    if not state.encoding_enabled.is_set():
        return 0

    lib_limits = {lib.name: lib.max_concurrent for lib in config.libraries
                  if lib.max_concurrent > 0}

    with FileDB(config.general.db_path) as db:
        active = db.count_active()
        capacity = config.general.encode_workers - active

        for _ in range(max(0, capacity)):
            lib_counts = db.library_active_counts() if lib_limits else {}
            gpu_active = db.gpu_active_count()

            saturated = {name for name, limit in lib_limits.items()
                         if lib_counts.get(name, 0) >= limit}

            candidates = db.get_pending_batch(
                exclude_libraries=saturated or None,
                priority_codecs=config.resolved_priority_codecs(),
                limit=20)

            if not candidates:
                break

            claimed = False
            for candidate in candidates:
                path = str(candidate["path"])
                profile = str(candidate["profile"])
                codec = str(candidate.get("video_codec", ""))
                needs_gpu = _needs_gpu(codec, profile, config)

                if needs_gpu and gpu_active >= config.general.gpu_slots:
                    continue

                token = uuid.uuid4().hex
                if not db.claim_with_token(path, token, needs_gpu=needs_gpu):
                    continue

                try:
                    future = state.executor.submit(_encode_file, path, profile, token)
                    with state.futures_lock:
                        state.pending_futures[future] = (path, token)
                    future.add_done_callback(lambda f: _future_done(f, state))
                    dispatched += 1
                    claimed = True
                    break
                except Exception as exc:
                    db.release_claim(path, token)
                    log_event(log, logging.ERROR, "dispatch_submit_failed",
                              "Failed to submit encode", file_path=path, error=str(exc))

            if not claimed:
                break

    return dispatched
```

- [ ] **Step 5: Write _dispatcher_loop, _scanner_loop, _maintenance_loop**

Same as previous plan versions, using `shutdown.wait(interval)`.

- [ ] **Step 6: Write _encode_file**

```python
def _encode_file(file_path: str, profile_name: str, dispatch_token: str) -> None:
    state = _get_state()
    config = state.config

    if not dispatch_token:
        return

    if not state.encoding_enabled.is_set():
        with FileDB(config.general.db_path) as db:
            db.release_claim(file_path, dispatch_token)
        return

    if profile_name not in config.profiles:
        with FileDB(config.general.db_path) as db:
            db.fail_with_token(file_path, dispatch_token,
                               error=f"Unknown profile: {profile_name}")
        return

    with FileDB(config.general.db_path) as db:
        if not db.start_encode(file_path, dispatch_token):
            return

    if not Path(file_path).exists():
        with FileDB(config.general.db_path) as db:
            db.fail_with_token(file_path, dispatch_token, error="File not found")
        return

    profile = config.profiles[profile_name]
    notifier = Notifier(config.notifications)

    if config.hooks.pre_encode:
        if not run_hooks(config.hooks.pre_encode, "pre_encode", file_path,
                       profile=profile_name, timeout=config.hooks.timeout):
            with FileDB(config.general.db_path) as db:
                db.fail_with_token(file_path, dispatch_token, error="pre_encode hook failed")
            return

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
        return

    with FileDB(config.general.db_path) as db:
        if result.status == EncodeStatus.SKIPPED:
            db.skip_with_token(file_path, dispatch_token)
        elif result.status == EncodeStatus.COMPLETED:
            _handle_encode_success(db, file_path, result.final_path,
                                   profile_name, profile, notifier, config,
                                   dispatch_token)
        else:
            _handle_encode_failure(db, file_path, result.error, result.transient,
                                   profile_name, config, notifier, dispatch_token)
```

- [ ] **Step 7: Update _handle_encode_success with COMMITTING transition**

```python
def _handle_encode_success(db, file_path, temp_output_path,
                            profile_name, profile, notifier, config, token):
    output_ext = container_suffix(profile)
    target_path = str(Path(file_path).with_suffix(output_ext))
    output_size = Path(temp_output_path).stat().st_size

    # Fenced commit: transition to COMMITTING before filesystem changes
    if not db.transition_to_committing(file_path, token,
                                        temp_path=temp_output_path,
                                        target_path=target_path,
                                        expected_size=output_size):
        Path(temp_output_path).unlink(missing_ok=True)
        return

    # Filesystem rename (COMMITTING state protects this)
    try:
        os.rename(temp_output_path, target_path)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            db.fail_with_token(file_path, token, error=f"Rename failed: {exc}")
            Path(temp_output_path).unlink(missing_ok=True)
            return
        # Cross-device: stage beside target, fsync, replace
        staging = str(Path(target_path).parent / f".{Path(target_path).name}.tmp")
        try:
            shutil.copy2(temp_output_path, staging)
            os.replace(staging, target_path)
        except OSError as copy_exc:
            Path(staging).unlink(missing_ok=True)
            db.fail_with_token(file_path, token, error=f"Cross-device copy failed: {copy_exc}")
            Path(temp_output_path).unlink(missing_ok=True)
            return
        Path(temp_output_path).unlink(missing_ok=True)

    # Update path if extension changed
    final_path = target_path
    if target_path != file_path:
        if not db.rename_with_token(file_path, target_path, token):
            return  # lost ownership
        if Path(file_path).exists():
            os.unlink(file_path)

    new_hash = compute_file_hash(final_path)
    db.update_hash(final_path, new_hash, output_size)
    if not db.complete_with_token(final_path, token,
                                  output_codec=profile.video.codec,
                                  output_size=output_size):
        return  # lost ownership — do not notify

    # Only notify/hook after successful DB commit
    arr_source, arr_id = db.get_arr_metadata(final_path)
    notifier.on_success(final_path, arr_source=arr_source, arr_id=arr_id)
    run_hooks(config.hooks.post_encode, "post_encode", final_path,
              profile=profile_name, output_path=final_path, status="completed",
              timeout=config.hooks.timeout)
```

- [ ] **Step 8: Update _handle_encode_failure with token**

All calls use `db.fail_with_token` or `db.retry_with_token`.

- [ ] **Step 9: Rewrite start_daemon**

```python
def start_daemon(config: PyflowsConfig, metrics_stop: threading.Event | None = None) -> None:
    global _state
    executor = ThreadPoolExecutor(
        max_workers=config.general.encode_workers,
        thread_name_prefix="encode-worker",
    )
    _state = DaemonState(config=config, executor=executor)
    state = _state

    # Crash recovery
    with FileDB(config.general.db_path) as db:
        db.reset_processing()
        db.reset_committing()

    # Clean temp files
    ...

    # Start threads (not daemon — joined on shutdown)
    shutdown = state.shutdown
    webhook_server = start_webhook_server(config)

    threads = []
    maintenance = threading.Thread(target=_maintenance_loop, args=(shutdown,), name="maintenance")
    maintenance.start()
    threads.append(maintenance)

    dispatcher = threading.Thread(target=_dispatcher_loop, args=(shutdown,), name="dispatcher")
    dispatcher.start()
    threads.append(dispatcher)

    observer = None
    handler = None
    if config.general.mode == "daemon":
        observer = Observer()
        handler = _MediaFileHandler(config)
        for lib in config.libraries:
            if Path(lib.path).exists():
                observer.schedule(handler, lib.path, recursive=True)
        observer.start()

        scanner = threading.Thread(target=_scanner_loop, args=(shutdown,), name="scanner")
        scanner.start()
        threads.append(scanner)

    # Signal handling
    def signal_handler(sig, frame):
        log_event(log, logging.INFO, "shutdown_requested", "Shutting down")
        shutdown.set()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    log_event(log, logging.INFO, "daemon_started", "pyflows daemon started",
              mode=config.general.mode, encode_workers=config.general.encode_workers)

    # Block until shutdown
    shutdown.wait()

    # Ordered shutdown:
    # 1. Stop dispatcher first (prevents new submissions)
    # 2. Cancel unstarted futures and release their claims
    # 3. Terminate running FFmpeg processes
    # 4. Wait for workers to finish
    # 5. Join remaining threads and cleanup

    # Step 1: join dispatcher so no new work is submitted
    for t in threads:
        t.join(timeout=10)

    # Step 2: cancel unstarted futures and release their DB claims
    for future, (path, token) in list(state.pending_futures.items()):
        if future.cancel():
            with FileDB(config.general.db_path) as db:
                db.release_claim(path, token)
    state.pending_futures.clear()

    # Step 3: terminate running FFmpeg
    terminate_active_encode()

    # Step 4: wait for running workers to finish
    executor.shutdown(wait=True, cancel_futures=False)

    # Step 5: cleanup
    if handler:
        handler.stop()
    if observer:
        observer.stop()
        observer.join(timeout=10)
    if webhook_server:
        webhook_server.shutdown()
    if metrics_stop:
        metrics_stop.set()
    log_event(log, logging.INFO, "daemon_stopped", "pyflows daemon stopped")
```

- [ ] **Step 10: Remove _MediaFileHandler.encode_task parameter** (dead code)
- [ ] **Step 11: Build and verify**
- [ ] **Step 12: Commit** `feat(tasks): replace Huey with ThreadPoolExecutor and central dispatcher`

---

## Task 6: Update webhook

**Files:** `pyflows/webhook.py`

- [ ] **Step 1: Remove encode_task parameter from start_webhook_server**
- [ ] **Step 2: _queue_encode writes DB record only (upsert)**
- [ ] **Step 3: Build and verify**
- [ ] **Step 4: Commit** `refactor(webhook): write DB records only, remove encode_task`

---

## Task 7: Remove Huey dependency

**Files:** `pyproject.toml`, `nix/package.nix`

- [ ] **Step 1: Remove huey from pyproject.toml dependencies**
- [ ] **Step 2: Remove huey from nix/package.nix dependencies**
- [ ] **Step 3: Remove huey.db references from tasks.py**
- [ ] **Step 4: Build and verify**
- [ ] **Step 5: Commit** `chore: remove Huey dependency`

---

## Task 8: Tests

**Files:** `tests/test_tasks.py`, `tests/test_db.py`

- [ ] **Step 1: Update DaemonState mocking (no more Huey)**

```python
from concurrent.futures import ThreadPoolExecutor
tasks._state = DaemonState(
    config=config,
    executor=ThreadPoolExecutor(max_workers=1),
)
```

- [ ] **Step 2: Add _dispatch_once tests**

```python
def test_dispatch_once_claims_and_submits(tmp_config): ...
def test_dispatch_once_respects_capacity(tmp_config): ...
def test_dispatch_once_rollback_on_submit_failure(tmp_config): ...
def test_dispatch_once_skips_gpu_when_full(tmp_config): ...
def test_dispatch_once_reaches_cpu_behind_gpu(tmp_config): ...
def test_dispatch_once_per_library_limit(tmp_config): ...
def test_dispatch_once_paused_skips(tmp_config): ...
```

- [ ] **Step 3: Add worker lifecycle tests**

```python
def test_encode_file_rejects_missing_token(tmp_config): ...
def test_encode_file_releases_on_pause(tmp_config): ...
def test_encode_file_fails_unknown_profile(tmp_config): ...
def test_encode_file_start_encode_rejects_stale(tmp_config): ...
```

- [ ] **Step 4: Update existing tests for new architecture**
- [ ] **Step 5: Build and verify all tests pass**
- [ ] **Step 6: Commit** `test: rewrite tests for executor-based dispatcher`

---

## Verification

1. `nix build` — all tests pass
2. Fencing tokens prevent stale/duplicate encoding
3. COMMITTING crash recovery reconciles filesystem state
4. GPU files blocked when full; CPU files dispatch past them
5. Per-library limits enforced per-claim
6. Pause releases claims back to PENDING
7. Submit failure rolls back claim
8. Every early return in `_encode_file` either releases or fails with token
9. `terminate_active_encode()` kills ALL FFmpeg processes, waits, then kills
10. Clean ordered shutdown: stop dispatch → cancel futures → terminate FFmpeg → join threads
11. Backward compatible: default config behaves identically
12. No `huey.db` created; SQLite `files` table is the sole queue

---

## Risks and Mitigations

| Risk | Mitigation |
|------|-----------|
| Stale worker after reset | `start_encode` atomic gate + token-guarded terminal ops |
| Filesystem TOCTOU | COMMITTING transition before rename; `expected_output_size` for crash recovery |
| GPU starvation | Batch candidate fetch; CPU files dispatched past GPU-blocked files |
| Per-library overcommit | Counts re-queried before each claim |
| Orphaned FFmpeg | ProcessRegistry; SIGTERM → wait → SIGKILL → wait |
| Submit failure | try/except with `release_claim` rollback |
| Invalid config | Pydantic validators: encode_workers≥1, gpu_slots≥1, max_concurrent≥0, scan_check_interval≥1 |
| Restart lost submissions | `reset_processing()` returns to PENDING; dispatcher resubmits |
