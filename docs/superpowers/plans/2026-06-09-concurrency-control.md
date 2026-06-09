# Concurrency Control Implementation Plan

**Goal:** Replace the current task queue with a central dispatcher and
`ThreadPoolExecutor`. SQLite remains the only durable queue and source of
truth. Scanning, maintenance, and encoding must not consume each other's
worker capacity.

## Architecture

```text
Watcher / scanner / webhook
            |
            v
    SQLite files table
       status=PENDING
            |
            v
   Central dispatcher thread
   - priority ordering
   - worker capacity
   - GPU capacity
   - per-library limits
   - atomic fencing-token claim
            |
            v
 ThreadPoolExecutor(encode_workers)
            |
            v
      FFmpeg subprocesses
```

The executor is intentionally not durable. If the process exits after a file
is claimed but before or during execution, startup recovery returns that file
to a dispatchable state. There must never be a second persistent task queue.

## Core Invariants

1. SQLite is the sole durable queue authority.
2. Only the dispatcher transitions `PENDING` files to `PROCESSING`.
3. Every processing attempt has a unique fencing token.
4. Worker state changes require ownership of that token.
5. Worker, GPU, and library capacity count both `PROCESSING` and `COMMITTING`.
6. Encoders write attempt-specific temporary output and never replace the
   source directly.
7. Filesystem replacement happens only after an atomic transition to
   `COMMITTING`.
8. Runtime reconciliation touches only stale `COMMITTING` records. Startup
   recovery may reconcile all of them because no workers are active yet.
9. Admission checking and executor submission are protected by one lock.
10. Shutdown disables admission before cancelling or terminating any work.

## State Machine

```text
PENDING
  |
  | dispatcher claim
  v
PROCESSING
  |  |  |  |
  |  |  |  +--> PENDING (retry or released unstarted claim)
  |  |  +-----> SKIPPED
  |  +--------> FAILED
  |
  | successful encode
  v
COMMITTING
  |  |
  |  +--------> FAILED
  v
COMPLETED
```

`dispatch_token` is an attempt identifier, not an expiring distributed lease.
This design assumes one PyFlows daemon per database.

## Schema

Add these columns to `files` through the existing idempotent migration:

```sql
ALTER TABLE files ADD COLUMN dispatch_token TEXT;
ALTER TABLE files ADD COLUMN dispatched_at TEXT;
ALTER TABLE files ADD COLUMN committing_at TEXT;
ALTER TABLE files ADD COLUMN needs_gpu INTEGER NOT NULL DEFAULT 0;
ALTER TABLE files ADD COLUMN commit_temp_path TEXT;
ALTER TABLE files ADD COLUMN commit_target_path TEXT;
ALTER TABLE files ADD COLUMN expected_output_size INTEGER;
ALTER TABLE files ADD COLUMN expected_output_hash TEXT;
```

Add `COMMITTING = "committing"` to `FileStatus`.

Allowed transitions:

```python
VALID_TRANSITIONS = {
    FileStatus.PENDING: {FileStatus.PROCESSING},
    FileStatus.PROCESSING: {
        FileStatus.PENDING,
        FileStatus.COMMITTING,
        FileStatus.SKIPPED,
        FileStatus.FAILED,
    },
    FileStatus.COMMITTING: {
        FileStatus.COMPLETED,
        FileStatus.FAILED,
    },
}
```

Add indexes supporting dispatcher and recovery queries:

```sql
CREATE INDEX IF NOT EXISTS idx_files_dispatch
ON files(status, next_retry_at, hold_until);

CREATE INDEX IF NOT EXISTS idx_files_active_library
ON files(status, library);

CREATE INDEX IF NOT EXISTS idx_files_committing_at
ON files(status, committing_at);

CREATE UNIQUE INDEX IF NOT EXISTS idx_files_dispatch_token
ON files(dispatch_token)
WHERE dispatch_token IS NOT NULL;
```

## Database API

All methods commit their transaction and return whether they changed a row.
Use parameterized SQL throughout.

### Claim And Start

```python
def claim_with_token(
    self,
    path: str,
    token: str,
    *,
    needs_gpu: bool,
) -> bool:
    cur = self.conn.execute(
        """
        UPDATE files
        SET status = ?,
            dispatch_token = ?,
            dispatched_at = ?,
            started_at = NULL,
            needs_gpu = ?,
            next_retry_at = NULL
        WHERE path = ? AND status = ?
        """,
        (
            FileStatus.PROCESSING,
            token,
            _utcnow_iso(),
            int(needs_gpu),
            path,
            FileStatus.PENDING,
        ),
    )
    self.conn.commit()
    return cur.rowcount == 1


def start_encode(self, path: str, token: str) -> bool:
    cur = self.conn.execute(
        """
        UPDATE files
        SET started_at = ?
        WHERE path = ?
          AND dispatch_token = ?
          AND status = ?
          AND started_at IS NULL
        """,
        (_utcnow_iso(), path, token, FileStatus.PROCESSING),
    )
    self.conn.commit()
    return cur.rowcount == 1
```

The start gate remains useful without durable task replay: it prevents
accidental duplicate submission and rejects stale workers after a manual
reset.

### Release, Retry, Skip, And Failure

`release_claim(path, token)` requires `PROCESSING` and returns the row to
`PENDING`, clearing all attempt fields.

`retry_with_token(path, token, error, retry_count, next_retry_at)` requires
`PROCESSING`, stores retry information, returns the row to `PENDING`, and
clears all attempt fields.

`skip_with_token(path, token)` requires `PROCESSING` and clears all attempt
fields.

Provide token-only finalization for last-resort exception handling:

```python
def fail_attempt(self, token: str, error: str) -> bool:
    cur = self.conn.execute(
        """
        UPDATE files
        SET status = ?,
            error = ?,
            completed_at = ?,
            dispatch_token = NULL,
            dispatched_at = NULL,
            committing_at = NULL,
            needs_gpu = 0,
            commit_temp_path = NULL,
            commit_target_path = NULL,
            expected_output_size = NULL,
            expected_output_hash = NULL
        WHERE dispatch_token = ?
          AND status IN (?, ?)
        """,
        (
            FileStatus.FAILED,
            error,
            _utcnow_iso(),
            token,
            FileStatus.PROCESSING,
            FileStatus.COMMITTING,
        ),
    )
    self.conn.commit()
    return cur.rowcount == 1
```

Normal failure paths may use a path-aware wrapper, but top-level exception
handling must use `fail_attempt()` because the row path may already have
changed.

### Commit Transition

Before changing the filesystem, persist everything needed for recovery:

```python
def transition_to_committing(
    self,
    path: str,
    token: str,
    *,
    temp_path: str,
    target_path: str,
    expected_size: int,
    expected_hash: str,
) -> bool:
    cur = self.conn.execute(
        """
        UPDATE files
        SET status = ?,
            committing_at = ?,
            commit_temp_path = ?,
            commit_target_path = ?,
            expected_output_size = ?,
            expected_output_hash = ?
        WHERE path = ?
          AND dispatch_token = ?
          AND status = ?
        """,
        (
            FileStatus.COMMITTING,
            _utcnow_iso(),
            temp_path,
            target_path,
            expected_size,
            expected_hash,
            path,
            token,
            FileStatus.PROCESSING,
        ),
    )
    self.conn.commit()
    return cur.rowcount == 1
```

Finalize the commit in one database update identified by token. Do not use a
separate path rename followed by completion:

```python
def complete_commit(
    self,
    token: str,
    *,
    final_path: str,
    output_codec: str,
    output_size: int,
    output_hash: str,
) -> bool:
    cur = self.conn.execute(
        """
        UPDATE files
        SET status = ?,
            path = ?,
            output_codec = ?,
            output_size = ?,
            hash = ?,
            completed_at = ?,
            retry_count = 0,
            next_retry_at = NULL,
            hold_until = NULL,
            dispatch_token = NULL,
            dispatched_at = NULL,
            committing_at = NULL,
            needs_gpu = 0,
            commit_temp_path = NULL,
            commit_target_path = NULL,
            expected_output_size = NULL,
            expected_output_hash = NULL
        WHERE dispatch_token = ? AND status = ?
        """,
        (
            FileStatus.COMPLETED,
            final_path,
            output_codec,
            output_size,
            output_hash,
            _utcnow_iso(),
            token,
            FileStatus.COMMITTING,
        ),
    )
    self.conn.commit()
    return cur.rowcount == 1
```

### Capacity Queries

The following methods count both active states:

```python
ACTIVE_STATUSES = (FileStatus.PROCESSING, FileStatus.COMMITTING)
```

- `count_active()`
- `library_active_counts()`
- `gpu_active_count()`

`get_pending_batch()` returns an ordered candidate list and supports excluding
saturated libraries. It must apply:

- `status = PENDING`
- `next_retry_at IS NULL OR next_retry_at <= now`
- `hold_until IS NULL OR hold_until <= now`
- configured priority ordering
- a configurable batch limit, initially 50

GPU eligibility is evaluated in Python per candidate because it depends on
that file's selected profile.

## Commit Protocol

`pipeline.encode_file(..., replace_original=False)` returns an
attempt-specific temporary output and never mutates the source.

The success handler performs:

1. Validate the temporary output.
2. Calculate output size and a strong output hash.
3. Call `transition_to_committing()`.
4. Install the output at the target path.
5. If the extension changed, remove the old source only after the target is
   durably installed.
6. Call `complete_commit()` using the token.
7. Send notifications and run post-encode hooks only after step 6 succeeds.

Use `os.replace()` for the final installation. If the temporary output is on
another filesystem:

1. Copy it to a token-specific staging file beside the target.
2. Flush and `fsync()` the staging file.
3. `os.replace(staging, target)`.
4. `fsync()` the target directory where supported.
5. Delete the original temporary output.

For a same-filesystem output:

1. `os.replace(temp, target)`.
2. `fsync()` the target directory where supported.

Directory sync should be a small helper that tolerates unsupported platforms
but logs unexpected failures.

If the extension changes, remove the old source and sync its directory before
calling `complete_commit()`. A crash before database completion remains
recoverable from the persisted commit metadata.

## Commit Recovery

Provide one reconciliation method with an optional cutoff:

```python
def reconcile_committing(self, *, older_than: datetime | None) -> int:
    ...
```

- At startup, call it with `older_than=None` because no workers are running.
- During maintenance, call it with `utcnow - committing_timeout`.
- The initial `committing_timeout` should be one hour and configurable.
- The query must include `dispatch_token` and must filter
  `committing_at < cutoff` when a cutoff is supplied.

For each eligible row:

1. If target size and hash match the persisted output metadata, treat the
   filesystem commit as successful.
2. Remove the old source when the extension changed and it still exists.
3. Sync the affected directory.
4. Complete the row atomically using its token.
5. If the target does not match, delete attempt-specific temp/staging files
   and fail the attempt using its token.

Do not reconcile fresh `COMMITTING` rows while workers may still own them.

## Dispatcher And Executor

### Daemon State

```python
@dataclass
class DaemonState:
    config: PyflowsConfig
    executor: ThreadPoolExecutor
    shutdown: threading.Event = field(default_factory=threading.Event)
    scanning_enabled: threading.Event = field(default_factory=threading.Event)
    encoding_enabled: threading.Event = field(default_factory=threading.Event)
    watcher_enabled: threading.Event = field(default_factory=threading.Event)
    gpu_semaphore: threading.BoundedSemaphore = field(init=False)
    admission_lock: threading.Lock = field(default_factory=threading.Lock)
    admission_enabled: bool = True
    futures_lock: threading.Lock = field(default_factory=threading.Lock)
    pending_futures: dict[Future[None], tuple[str, str]] = field(
        default_factory=dict
    )

    def __post_init__(self) -> None:
        self.scanning_enabled.set()
        self.encoding_enabled.set()
        self.watcher_enabled.set()
        self.gpu_semaphore = threading.BoundedSemaphore(
            self.config.general.gpu_slots
        )
```

### Future Completion

```python
def _future_done(future: Future[None], state: DaemonState) -> None:
    with state.futures_lock:
        entry = state.pending_futures.pop(future, None)

    if future.cancelled() or entry is None:
        return

    exc = future.exception()
    if exc is None:
        return

    _, token = entry
    try:
        with FileDB(state.config.general.db_path) as db:
            db.fail_attempt(token, f"Unhandled worker exception: {exc}")
    except Exception:
        log.exception("Failed to finalize crashed worker", extra={"token": token})
```

The worker entry point also wraps its complete lifecycle in `try/except` and
calls `fail_attempt(token, ...)` as a last resort. The callback is a second
line of defense.

### Dispatch Iteration

For each available worker slot:

1. Stop if shutdown has started.
2. Re-query total, GPU, and per-library active counts.
3. Fetch an ordered pending batch excluding saturated libraries.
4. Select the first candidate that passes GPU admission.
5. Atomically claim it with a new token.
6. Acquire `admission_lock`.
7. If admission is disabled, release the claim and stop.
8. Submit to the executor and register the future before adding its callback.
9. On submission failure, release the claim.
10. Release `admission_lock` and repeat with fresh capacity counts.

The claim occurs before `admission_lock` to avoid holding the lock during
SQLite work. Shutdown is still safe because a dispatcher that claims after
the signal must acquire the lock, observe disabled admission, and release the
claim without submitting.

## Worker Lifecycle

The worker:

1. Rejects an empty token.
2. Releases its claim if encoding is paused before start.
3. Validates the profile.
4. Calls `start_encode(path, token)`.
5. Validates that the input still exists.
6. Runs pre-encode hooks.
7. Calls the pipeline with:
   - `replace_original=False`
   - `registry_key=token`
   - the GPU safety semaphore
8. Handles skip, retry, failure, or commit.
9. Uses `fail_attempt(token, ...)` for any unhandled exception.

Every return after a successful dispatcher claim must perform or attempt a
token-owned state transition.

## GPU Admission

`_needs_gpu(codec, profile_name, config)` returns true only when:

- the profile exists;
- its video encoder is VAAPI; and
- the planned video action is not copy.

Codec membership in `skip_codecs` is an initial approximation before the
pipeline builds its full plan. The worker semaphore remains mandatory as a
safety net if dispatcher prediction differs from the actual plan.

The dispatcher skips GPU candidates when slots are full and continues through
the batch so CPU-only work is not blocked behind them.

## Scanner And Maintenance

Use dedicated named threads with interruptible waits:

- Scanner: scans due libraries and writes `PENDING` records only.
- Maintenance: releases held files, performs codec backfill, and reconciles
  only stale commits.
- Dispatcher: performs admission and executor submission only.

These threads do not perform encoding and are joined during shutdown.

## Process And Progress Registries

Replace the single active process and progress objects with thread-safe maps
keyed by fencing token.

`ProcessRegistry.terminate_all()`:

1. Snapshot processes under the lock.
2. Send terminate to all.
3. Wait up to the configured grace period.
4. Kill remaining processes.
5. Wait again to reap them.

`FFmpegCommand.run()` registers immediately after process creation and
unregisters in `finally`.

## Shutdown

Shutdown ordering:

1. Set the shutdown event.
2. Acquire `admission_lock` and set `admission_enabled=False`.
3. Stop watcher and webhook producers.
4. Join dispatcher, scanner, and maintenance threads.
5. Snapshot `pending_futures` under `futures_lock`.
6. Cancel futures that have not started and release their claims.
7. Terminate all registered FFmpeg processes.
8. Call `executor.shutdown(wait=True, cancel_futures=False)`.
9. Join watcher/webhook resources and stop metrics.

Control-plane threads use interruptible waits and should exit promptly. A
thread still alive after the join deadline is a shutdown error, not a normal
warning. Admission remains disabled, so it cannot submit new executor work.

## Startup Recovery

Before starting producers, dispatcher, or executor work:

1. Reconcile all `COMMITTING` rows.
2. Reset remaining `PROCESSING` rows to `PENDING`, clearing attempt fields.
3. Clean only unreferenced, attempt-specific temporary files.
4. Start maintenance, dispatcher, scanner, watcher, and webhook services.

Commit reconciliation must run before generic temp cleanup.

## Configuration

Add:

```yaml
general:
  encode_workers: 1
  gpu_slots: 1
  scan_check_interval: 60
  committing_timeout: 3600

libraries:
  - name: Movies
    max_concurrent: 1
```

Validation:

- `encode_workers >= 1`
- `gpu_slots >= 1`
- `scan_check_interval >= 1`
- `committing_timeout >= 60`
- `max_concurrent >= 0`, where zero means unlimited

Keep the existing `workers` setting as a deprecated alias for one release if
backward compatibility is required.

## Implementation Tasks

### Task 1: Configuration

- Add and validate concurrency settings.
- Resolve the deprecated `workers` alias.
- Update example configuration and documentation.

### Task 2: Database

- Add schema fields and indexes.
- Add `COMMITTING`.
- Implement token-owned claim, start, retry, skip, release, failure, commit,
  capacity, batch-selection, and reconciliation methods.
- Remove path-only processing terminal operations from worker call sites.

### Task 3: Pipeline And FFmpeg

- Add deferred replacement mode.
- Add process/progress registries keyed by token.
- Add GPU semaphore handling around actual VAAPI execution.
- Add durable target installation and directory-sync helpers.

### Task 4: Orchestration

- Remove the external task queue integration.
- Add executor, dispatcher, scanner, and maintenance threads.
- Add admission and future registries.
- Convert watcher and webhook paths to database writes only.
- Implement ordered startup and shutdown.

### Task 5: Dependencies

- Remove the obsolete task queue dependency from Python and Nix manifests.
- Remove its database and configuration references.

### Task 6: Tests

Implement deterministic tests for:

- atomic claim and worker start;
- duplicate start rejection;
- stale token rejection after reset;
- token-only failure after a path change;
- retry scheduling and backoff eligibility;
- worker, GPU, and per-library capacity;
- CPU dispatch behind GPU-blocked candidates;
- submit failure claim rollback;
- cancelled future claim release;
- callback handling for cancelled and failed futures;
- admission disabled during a concurrent dispatch;
- pause before worker start;
- same-filesystem durable replacement;
- cross-filesystem staged replacement;
- extension-changing commit;
- crash before replacement;
- crash after replacement but before database completion;
- stale-only runtime reconciliation;
- startup reconciliation of all commits;
- commit metadata cleanup on every terminal path;
- termination and reaping of multiple FFmpeg processes;
- shutdown with queued and running work.

Use fault injection around filesystem replacement and database finalization.

## Verification

1. Run the full test suite in the Nix development shell.
2. Verify no obsolete task queue database is created.
3. Run with one worker and confirm existing behavior.
4. Run with two workers and one GPU slot:
   - one GPU encode at a time;
   - CPU-only work may run concurrently;
   - per-library limits are respected.
5. Send termination with queued and active jobs:
   - no new submission after admission closes;
   - queued claims return to `PENDING`;
   - FFmpeg processes terminate and are reaped;
   - startup recovery finds no irrecoverable rows.

## Non-Goals

- Multiple dispatcher processes sharing one database.
- Distributed workers or remote nodes.
- Expiring leases and heartbeats.
- Weighted resource scheduling beyond worker, GPU, and per-library limits.
- Preserving in-memory executor submissions across restart.

Those features require a distributed ownership model and should not be added
implicitly to this single-daemon design.
