"""Concurrency lock + atomic staged generation (spec sections 14-15).

Generation must be failure-safe:

- never write directly into the final tier directory while generation is
  still in progress (a mid-generation crash/kill must never leave a
  corrupt, partially-generated PRODUCTION tier behind);
- never let two concurrent generations against the same output root race
  each other (both consuming disk space the pre-flight only accounted for
  once, or one clobbering the other's files).

Approach: pre-flight -> acquire a directory-based lock on the output root
-> generate + validate into a hidden staging directory on the SAME
filesystem as the final tier -> only after a full PASS, atomically rename
staging -> final (displacing any previous tier only at the very end, and
only after the replacement is fully generated and validated). On any
failure, the staging directory is removed and the previous, still-valid
tier (if any) is left completely untouched.
"""
from __future__ import annotations

import json
import os
import shutil
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional


class LockHeldError(Exception):
    pass


def _pid_alive(pid: int) -> Optional[bool]:
    """True/False if determinable, None if this platform can't tell us.

    POSIX only: ``os.kill(pid, 0)`` is a documented-safe existence probe
    (never actually signals the target). On Windows, ``os.kill(pid, sig)``
    for a non-CTRL_* signal opens a real process handle and calls
    ``TerminateProcess(handle, sig)`` -- i.e. it can ACTUALLY KILL the
    target process (including, catastrophically, the calling process
    itself if `pid` happens to be its own PID, as can occur when a lock
    file records the current process's own PID and this check runs against
    it). Never call os.kill for a liveness probe on Windows; fall back to
    the age-based staleness check there instead."""
    if os.name != "posix":
        return None
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # process exists, we just can't signal it
    except OSError:
        return None


def _lock_is_stale(lock_dir: Path, max_age_seconds: int = 6 * 3600) -> bool:
    info_path = lock_dir / "info.json"
    pid = None
    try:
        info = json.loads(info_path.read_text(encoding="utf-8"))
        pid = info.get("pid")
    except (OSError, ValueError):
        pass
    if pid is not None:
        alive = _pid_alive(pid)
        if alive is True:
            return False
        if alive is False:
            return True
    # Can't determine liveness (e.g. no PID recorded, or platform can't
    # signal-check) -- fall back to a conservative age-based staleness
    # check so an abandoned lock from a killed process doesn't block
    # generation forever.
    try:
        age = time.time() - lock_dir.stat().st_mtime
    except OSError:
        return True
    return age > max_age_seconds


@contextmanager
def generation_lock(output_root: Path, *, tier: str):
    """Directory-based mutual-exclusion lock for generation against a given
    output root. Raises LockHeldError if another (apparently still-alive)
    generation already holds it."""
    output_root.mkdir(parents=True, exist_ok=True)
    lock_dir = output_root / ".workload04_generator.lock"
    try:
        os.mkdir(lock_dir)
    except FileExistsError:
        if _lock_is_stale(lock_dir):
            shutil.rmtree(lock_dir, ignore_errors=True)
            os.mkdir(lock_dir)
        else:
            raise LockHeldError(
                f"Another workload04_generator generation appears to already be running "
                f"against output root {output_root} (lock directory: {lock_dir}). Refusing to "
                f"start a concurrent generation against the same output root -- this would "
                f"corrupt disk-space accounting and could race with an in-progress tier write. "
                f"If you are certain no other generation process is actually running, remove "
                f"{lock_dir} manually and retry."
            )
    (lock_dir / "info.json").write_text(
        json.dumps({"pid": os.getpid(), "tier": tier, "started_at": time.time()}),
        encoding="utf-8",
    )
    try:
        yield lock_dir
    finally:
        shutil.rmtree(lock_dir, ignore_errors=True)


def staging_dir_for(out_dir: Path) -> Path:
    """Hidden staging directory on the SAME filesystem/parent as `out_dir`
    (never a different filesystem -- the final rename must be atomic)."""
    return out_dir.parent / f".{out_dir.name}.staging.{os.getpid()}"


@contextmanager
def staged_generation(out_dir: Path):
    """Context manager yielding a staging directory to generate into.
    On success (`commit()` called), atomically replaces `out_dir` with the
    staging directory's contents. On any exception, or if `commit()` is
    never called, the staging directory is removed and `out_dir` (if it
    already existed) is left completely untouched."""
    staging = staging_dir_for(out_dir)
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)

    state = {"committed": False}

    def commit():
        state["committed"] = True

    try:
        yield staging, commit
        if state["committed"]:
            _atomic_replace(out_dir, staging)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def _atomic_replace(out_dir: Path, staging: Path) -> None:
    """Swap `staging` into `out_dir`'s place. Only the final two renames
    are performed once both sides are known-ready (staging fully generated
    and validated) -- the old tier is never deleted before the replacement
    is confirmed complete, and a crash between the two renames leaves the
    old tier moved aside but recoverable rather than silently destroyed."""
    trash = out_dir.parent / f".{out_dir.name}.trash.{os.getpid()}"
    shutil.rmtree(trash, ignore_errors=True)
    had_previous = out_dir.exists()
    if had_previous:
        out_dir.rename(trash)
    try:
        staging.rename(out_dir)
    except OSError:
        # Roll back: restore the previous tier so we never end up with
        # neither a valid old nor new tier on disk.
        if had_previous and trash.exists() and not out_dir.exists():
            trash.rename(out_dir)
        raise
    finally:
        shutil.rmtree(trash, ignore_errors=True)
