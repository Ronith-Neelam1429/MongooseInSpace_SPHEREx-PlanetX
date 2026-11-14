"""Survey worker loop: claim a patch, process it, record the result, repeat.

The patch processor is a callable `(PatchContext) -> summary dict` (see
ProcessPatch). While it runs, a heartbeat thread renews the patch's lease so
slow patches are not reclaimed by other workers. A processor exception fails
the patch (it is retried later); a database error stops the worker, and the
lease returns the patch to the queue.

Signals:
  * SIGINT (Ctrl-C): finish the current patch, then stop. A second SIGINT
    aborts the current patch and releases it back to the queue.
  * SIGTERM (hosting platforms stopping the process, usually with only a few
    seconds' grace): abort and release the current patch immediately.
  An abort interrupts only the processor. If the processor has already
  returned, its result is recorded first and the loop then stops.
"""
import logging
import signal
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import FrameType
from typing import Any

from planetx.survey.queue import (
    DEFAULT_LEASE_S,
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_RETRY_DELAY_S,
    LeaseLostError,
    Patch,
    PatchQueue,
)

log = logging.getLogger(__name__)

# How long to wait before asking again when no patch is claimable yet
# (others are in progress elsewhere, or failed patches are waiting to retry).
DEFAULT_IDLE_POLL_S = 30.0


class PatchAbandoned(Exception):
    """Raise from a processor to hand its patch back unprocessed.

    The patch returns to pending without counting a failed attempt. Use it
    when `ctx.should_stop()` turns true between slow steps.
    """


@dataclass(frozen=True)
class PatchContext:
    """What a processor gets: the patch, plus flags to check between slow steps."""

    patch: Patch
    _abort: threading.Event = field(repr=False)
    _lease_lost: threading.Event = field(repr=False)

    def lease_lost(self) -> bool:
        """True if this worker lost (or could not renew) the lease: another worker may redo the patch."""
        return self._lease_lost.is_set()

    def should_stop(self) -> bool:
        """True if the processor should give up now (raise PatchAbandoned): the
        worker was told to abort, or the lease is lost."""
        return self._abort.is_set() or self._lease_lost.is_set()


ProcessPatch = Callable[[PatchContext], dict[str, Any]]
"""A patch processor: does the work for `ctx.patch` and returns a small JSON summary.

Processing is at-least-once: the same patch can be processed again after a
crash or a lost lease. Anything the processor writes outside the returned
summary (candidates, cached files) must therefore be idempotent, keyed by
(nside, ipix): replace that patch's previous output instead of appending.
"""


@dataclass
class RunResult:
    done: int = 0
    failed: int = 0  # failed attempts (the patch may still be retried)
    released: int = 0  # patches handed back unprocessed (PatchAbandoned)
    lease_lost: int = 0  # results discarded because another worker reclaimed the patch
    aborted: bool = False  # stopped mid-patch by a signal; that patch was released
    reason: str = ""  # why the loop stopped


class _StopFlag:
    """Signal handlers for the run loop. Installed only in the main thread.

    `stop`: stop after the current patch. `abort`: give up the current patch.
    KeyboardInterrupt is raised only while the processor runs (`in_processor`),
    so a finished result is never thrown away by a late signal.
    """

    def __init__(self) -> None:
        self.stop = threading.Event()
        self.abort = threading.Event()
        self.in_processor = False
        self._previous: dict[int, Any] = {}

    def install(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            return
        for sig in (signal.SIGINT, signal.SIGTERM):
            self._previous[sig] = signal.signal(sig, self._handle)

    def restore(self) -> None:
        for sig, handler in self._previous.items():
            # signal.signal() returns None for handlers not installed from Python.
            signal.signal(sig, signal.SIG_DFL if handler is None else handler)
        self._previous.clear()

    def _handle(self, signum: int, frame: FrameType | None) -> None:
        name = signal.Signals(signum).name
        if signum == signal.SIGINT and not self.stop.is_set():
            log.warning("SIGINT: finishing the current patch, then stopping (Ctrl-C again to abort it)")
            self.stop.set()
            return
        self.stop.set()
        self.abort.set()
        if self.in_processor:
            log.warning("%s: aborting the current patch", name)
            raise KeyboardInterrupt
        log.warning("%s: stopping after recording the current patch", name)


def _open_heartbeat_queue(queue: PatchQueue) -> PatchQueue:
    """A second connection for the heartbeat thread (sqlite3 connections are per thread)."""
    return PatchQueue(queue.path, worker_id=queue.worker_id, lease_s=queue.lease_s, clock=queue.clock)


@contextmanager
def _heartbeat(queue: PatchQueue, patch: Patch) -> Iterator[threading.Event]:
    """Renew the lease on `patch` from a background thread until the block exits.

    Yields an Event that is set if the lease is lost: another worker took the
    patch, or renewals kept failing until the lease would run out before the
    next try. Failed renewals (e.g. "database is locked") are logged and retried.
    """
    stop = threading.Event()
    lease_lost = threading.Event()
    interval = queue.lease_s / 5

    def beat() -> None:
        expires = queue.clock() + queue.lease_s  # the claim happened just before
        own: PatchQueue | None = None
        try:
            while not stop.wait(interval):
                try:
                    if own is None:
                        own = _open_heartbeat_queue(queue)
                    own.renew(patch)
                    expires = queue.clock() + queue.lease_s
                except LeaseLostError:
                    log.warning("lost the lease on patch %d; another worker may redo it", patch.ipix)
                    lease_lost.set()
                    return
                except Exception:
                    log.exception("lease renewal for patch %d failed; retrying", patch.ipix)
                    if queue.clock() + interval >= expires:
                        log.error("lease on patch %d runs out before the next renewal; treating it as lost",
                                  patch.ipix)
                        lease_lost.set()
                        return
        finally:
            if own is not None:
                own.close()

    thread = threading.Thread(target=beat, name=f"lease-{patch.ipix}", daemon=True)
    thread.start()
    try:
        yield lease_lost
    finally:
        stop.set()
        thread.join()


def run(
    process_patch: ProcessPatch,
    db_path: Path | str,
    *,
    max_patches: int | None = None,
    lease_s: float = DEFAULT_LEASE_S,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    retry_delay_s: float = DEFAULT_RETRY_DELAY_S,
    idle_poll_s: float = DEFAULT_IDLE_POLL_S,
    worker_id: str | None = None,
) -> RunResult:
    """Process patches from the queue at `db_path` until it is drained or stopped.

    Stops after `max_patches` claimed patches if given. Raises
    FileNotFoundError if the database does not exist (it is never created
    here) and RuntimeError if the queue has not been initialised.
    """
    valid_max = isinstance(max_patches, int) and not isinstance(max_patches, bool) and max_patches >= 1
    if max_patches is not None and not valid_max:
        raise ValueError(f"max_patches must be an int >= 1, got {max_patches!r}")
    if not idle_poll_s > 0:
        raise ValueError(f"idle_poll_s must be > 0, got {idle_poll_s!r}")
    result = RunResult()
    flags = _StopFlag()
    with PatchQueue(
        db_path, worker_id=worker_id, lease_s=lease_s, max_attempts=max_attempts, retry_delay_s=retry_delay_s
    ) as queue:
        stats = queue.stats()
        if stats.total == 0:
            raise RuntimeError(f"survey queue at {db_path} is empty; run `python -m planetx.survey init` first")
        log.info("worker %s starting on %s (nside %s, %d/%d done)",
                 queue.worker_id, db_path, stats.nside, stats.counts["done"], stats.total)
        flags.install()
        try:
            claimed = 0
            while True:
                if flags.stop.is_set():
                    result.reason = "stopped by signal"
                    break
                if max_patches is not None and claimed >= max_patches:
                    result.reason = f"reached max_patches={max_patches}"
                    break
                patch = queue.claim_next()
                if patch is None:
                    stats = queue.stats()
                    if stats.counts["pending"] == 0 and stats.counts["in_progress"] == 0:
                        result.reason = "queue drained"
                        break
                    log.info("nothing claimable yet (%d pending, %d in progress elsewhere); waiting %.0fs",
                             stats.counts["pending"], stats.counts["in_progress"], idle_poll_s)
                    flags.stop.wait(idle_poll_s)
                    continue
                claimed += 1
                if not _process_one(queue, patch, process_patch, result, flags):
                    result.reason = "aborted by signal"
                    break
        except KeyboardInterrupt:
            # Only reachable when our handlers are not installed (run() off the
            # main thread). A patch claimed in that instant is recovered when
            # its lease expires.
            result.aborted = True
            result.reason = "aborted by signal"
        finally:
            flags.restore()
    log.info("worker stopping: %s (done %d, failed attempts %d)", result.reason, result.done, result.failed)
    return result


def _process_one(
    queue: PatchQueue, patch: Patch, process_patch: ProcessPatch, result: RunResult, flags: _StopFlag
) -> bool:
    """Process one claimed patch and record the outcome. Returns False if aborted."""
    started = time.monotonic()
    log.info("patch rank %d (ipix %d, attempt %d): processing", patch.rank, patch.ipix, patch.attempts + 1)
    try:
        with _heartbeat(queue, patch) as lease_lost:
            ctx = PatchContext(patch, flags.abort, lease_lost)
            flags.in_processor = True
            try:
                summary = process_patch(ctx)
            finally:
                flags.in_processor = False
    except KeyboardInterrupt:
        _release(queue, patch)
        result.aborted = True
        return False
    except PatchAbandoned as exc:
        log.warning("patch rank %d (ipix %d) abandoned by the processor: %s", patch.rank, patch.ipix, exc)
        if _release(queue, patch):
            result.released += 1
        return True
    except Exception as exc:
        log.exception("patch rank %d (ipix %d) failed", patch.rank, patch.ipix)
        _fail(queue, patch, f"{type(exc).__name__}: {exc}", result)
        return True

    try:
        queue.complete(patch, summary)
    except (TypeError, ValueError) as exc:
        log.error("patch rank %d (ipix %d): processor returned an invalid summary: %s", patch.rank, patch.ipix, exc)
        _fail(queue, patch, f"invalid summary: {exc}", result)
        return True
    except LeaseLostError as exc:
        log.warning("%s; result discarded", exc)
        result.lease_lost += 1
        return True
    result.done += 1
    stats = queue.stats()
    log.info("patch rank %d (ipix %d): done in %.1fs %s [survey %d/%d done, %d failed]",
             patch.rank, patch.ipix, time.monotonic() - started, summary,
             stats.counts["done"], stats.total, stats.counts["failed"])
    return True


def _fail(queue: PatchQueue, patch: Patch, error: str, result: RunResult) -> None:
    try:
        status = queue.fail(patch, error)
    except LeaseLostError as exc:
        log.warning("%s; failure not recorded", exc)
        result.lease_lost += 1
        return
    result.failed += 1
    log.warning("patch rank %d (ipix %d) is now %s", patch.rank, patch.ipix, status)


def _release(queue: PatchQueue, patch: Patch) -> bool:
    try:
        queue.release(patch)
    except LeaseLostError as exc:
        log.warning("%s; nothing to release", exc)
        return False
    log.warning("patch rank %d (ipix %d) released back to the queue", patch.rank, patch.ipix)
    return True


def dry_run(ctx: PatchContext) -> dict[str, Any]:
    """Trivial processor for exercising the loop: no network, records the patch centre."""
    ra, dec = ctx.patch.center
    return {"dry_run": True, "ra": round(ra, 6), "dec": round(dec, 6)}


# Processors selectable from the CLI. The real motion-detection processor goes here.
PROCESSORS: dict[str, ProcessPatch] = {"dry-run": dry_run}
