"""Cross-process, persisted registry for Clipper background services.

The registry is an observability and recovery ledger, not a process supervisor:
it records exact process identity and heartbeats, classifies lost workers, and
never starts or kills a process during reconciliation.
"""
import json
import math
import os
import time
from contextlib import contextmanager
from pathlib import Path

import psutil

from .common import JOBS

REGISTRY_FILE = ".runtime-services.json"
LOCK_FILE = ".runtime-services.guard"
REGISTRY_VERSION = 1
MAX_RECORDS = 250
STALE_AFTER_S = 180
ACTIVE_STATES = {"starting", "running", "paused", "stopping", "queued", "rendering"}
TERMINAL_STATES = {"completed", "stopped", "failed", "crashed"}
VALID_STATES = ACTIVE_STATES | TERMINAL_STATES | {"stale", "unverified"}


class RuntimeRegistryError(RuntimeError):
    """The service registry cannot be safely read or updated."""


def _paths(jobs_dir=None):
    root = Path(jobs_dir or JOBS)
    return root / REGISTRY_FILE, root / LOCK_FILE


@contextmanager
def _guard(jobs_dir=None):
    root = Path(jobs_dir or JOBS)
    root.mkdir(parents=True, exist_ok=True)
    _, lock_path = _paths(root)
    with lock_path.open("a+b") as lock:
        lock.seek(0, os.SEEK_END)
        if lock.tell() == 0:
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _load(path):
    if not path.exists():
        return {"version": REGISTRY_VERSION, "updated_at": time.time(), "services": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise RuntimeRegistryError(f"Runtime registry is unreadable; refusing to overwrite it: {exc}") from exc
    if (not isinstance(payload, dict) or payload.get("version") != REGISTRY_VERSION
            or not isinstance(payload.get("services"), dict)):
        raise RuntimeRegistryError("Runtime registry has an unsupported or invalid structure; refusing to overwrite it.")
    return payload


def _save(path, payload):
    payload["updated_at"] = time.time()
    temporary = path.with_name(path.name + ".part")
    try:
        temporary.write_text(json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _identity_matches(record, pid, create_time):
    try:
        return (int(record.get("pid") or 0) == int(pid)
                and abs(float(record.get("create_time")) - float(create_time)) <= 2.0)
    except (TypeError, ValueError):
        return False


def _record_time(now):
    value = time.time() if now is None else float(now)
    if not math.isfinite(value):
        raise ValueError("Registry timestamps must be finite.")
    return value


def _trim(payload):
    records = payload["services"]
    if len(records) <= MAX_RECORDS:
        return
    terminal = sorted(
        ((key, value) for key, value in records.items() if value.get("state") in TERMINAL_STATES),
        key=lambda pair: float(pair[1].get("updated_at") or 0),
    )
    for key, _ in terminal[:max(0, len(records) - MAX_RECORDS)]:
        records.pop(key, None)


def register(service_id, service_type, job_key, pid, create_time, state="starting",
             message="", metadata=None, jobs_dir=None, now=None):
    """Create or refresh one service record without losing its original start time."""
    service_id = str(service_id or "").strip()
    service_type = str(service_type or "").strip()
    job_key = str(job_key or "").strip()
    if not service_id or not service_type or not job_key:
        raise ValueError("service_id, service_type and job_key are required.")
    if state not in VALID_STATES:
        raise ValueError(f"Unsupported runtime service state: {state}")
    pid, created = int(pid), float(create_time)
    if pid <= 0 or not math.isfinite(created):
        raise ValueError("A service requires a positive PID and finite process creation time.")
    stamp = _record_time(now)
    path, _ = _paths(jobs_dir)
    with _guard(jobs_dir):
        payload = _load(path)
        previous = payload["services"].get(service_id, {})
        same_process = _identity_matches(previous, pid, created)
        effective_state = previous.get("state") if same_process and previous.get("state") in ("paused", "stopping") else state
        record = {
            "id": service_id,
            "type": service_type,
            "job_key": job_key,
            "pid": pid,
            "create_time": created,
            "started_at": previous.get("started_at", stamp) if same_process else stamp,
            "heartbeat_at": stamp,
            "updated_at": stamp,
            "state": effective_state,
            "message": str(message or "")[:500],
            "metadata": dict(previous.get("metadata") or {}) if same_process else {},
        }
        if isinstance(metadata, dict):
            record["metadata"].update(metadata)
        payload["services"][service_id] = record
        _trim(payload)
        _save(path, payload)
        return dict(record)


def update(service_id, pid, create_time, state=None, message=None, jobs_dir=None, now=None):
    """Update state only when both PID and creation time still match the registered process."""
    if state is not None and state not in VALID_STATES:
        raise ValueError(f"Unsupported runtime service state: {state}")
    stamp = _record_time(now)
    path, _ = _paths(jobs_dir)
    with _guard(jobs_dir):
        payload = _load(path)
        record = payload["services"].get(str(service_id))
        if not isinstance(record, dict):
            raise RuntimeRegistryError(f"Runtime service {service_id!r} is not registered.")
        if not _identity_matches(record, pid, create_time):
            raise RuntimeRegistryError(f"Refusing to update {service_id!r}: process identity changed.")
        if record.get("state") in TERMINAL_STATES:
            raise RuntimeRegistryError(f"Runtime service {service_id!r} is already terminal.")
        if state is not None:
            record["state"] = state
        if message is not None:
            record["message"] = str(message)[:500]
        record["heartbeat_at"] = stamp
        record["updated_at"] = stamp
        _save(path, payload)
        return dict(record)


def heartbeat(service_id, pid, create_time, jobs_dir=None, now=None):
    """Persist proof of life and clear stale/unverified classifications without unpausing."""
    stamp = _record_time(now)
    path, _ = _paths(jobs_dir)
    with _guard(jobs_dir):
        payload = _load(path)
        record = payload["services"].get(str(service_id))
        if not isinstance(record, dict):
            raise RuntimeRegistryError(f"Runtime service {service_id!r} is not registered.")
        if not _identity_matches(record, pid, create_time):
            raise RuntimeRegistryError(f"Refusing to heartbeat {service_id!r}: process identity changed.")
        if record.get("state") in TERMINAL_STATES:
            raise RuntimeRegistryError(f"Runtime service {service_id!r} is already terminal.")
        if record.get("state") in ("starting", "stale", "unverified"):
            record["state"] = "running"
        record["heartbeat_at"] = stamp
        record["updated_at"] = stamp
        _save(path, payload)
        return dict(record)


def finish(service_id, pid, create_time, state="stopped", message="", jobs_dir=None, now=None):
    if state not in TERMINAL_STATES:
        raise ValueError(f"Finish state must be terminal, got {state!r}.")
    return update(service_id, pid, create_time, state=state, message=message,
                  jobs_dir=jobs_dir, now=now)


def annotate_recovery(service_id, pid, create_time, note, jobs_dir=None, now=None):
    """Attach a recovery note to a terminal record without changing its outcome."""
    stamp = _record_time(now)
    path, _ = _paths(jobs_dir)
    with _guard(jobs_dir):
        payload = _load(path)
        record = payload["services"].get(str(service_id))
        if not isinstance(record, dict) or not _identity_matches(record, pid, create_time):
            raise RuntimeRegistryError(f"Cannot annotate recovery for {service_id!r}: process identity does not match.")
        record["recovery_note"] = str(note or "")[:1000]
        record["recovered_at"] = stamp
        record["updated_at"] = stamp
        _save(path, payload)
        return dict(record)


def _process_alive(record):
    try:
        pid = int(record.get("pid") or 0)
        expected = float(record.get("create_time"))
        if pid <= 0 or not math.isfinite(expected):
            return None
        process = psutil.Process(pid)
        if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
            return False
        return abs(process.create_time() - expected) <= 2.0
    except psutil.AccessDenied:
        return None
    except (psutil.NoSuchProcess, TypeError, ValueError, OverflowError):
        return False


def snapshot(jobs_dir=None, stale_after=STALE_AFTER_S, now=None):
    """Reconcile registry records against OS process identity and recent heartbeats."""
    stamp = _record_time(now)
    try:
        stale_after = max(1.0, float(stale_after))
    except (TypeError, ValueError):
        stale_after = STALE_AFTER_S
    if not math.isfinite(stale_after):
        stale_after = STALE_AFTER_S
    path, _ = _paths(jobs_dir)
    with _guard(jobs_dir):
        payload = _load(path)
        records = payload["services"]
        for record in records.values():
            if not isinstance(record, dict) or record.get("state") in TERMINAL_STATES:
                continue
            alive = _process_alive(record)
            if alive is False:
                if record.get("state") == "stopping":
                    record["state"] = "stopped"
                    record["message"] = "Worker exited after a Stop request."
                else:
                    record["state"] = "crashed"
                    record["message"] = "Worker process exited without recording a terminal state."
                record["ended_at"] = stamp
                record["updated_at"] = stamp
                continue
            if alive is None:
                heartbeat_at = record.get("heartbeat_at", record.get("updated_at", stamp))
                try:
                    age = max(0.0, stamp - float(heartbeat_at))
                except (TypeError, ValueError):
                    age = stale_after + 1
                if age > stale_after and record.get("state") != "paused":
                    record["state"] = "unverified"
                    record["message"] = "Process identity could not be verified; no recovery action was taken."
                    record["updated_at"] = stamp
                continue
            if record.get("state") == "paused":
                continue
            heartbeat_at = record.get("heartbeat_at", record.get("updated_at", stamp))
            try:
                age = max(0.0, stamp - float(heartbeat_at))
            except (TypeError, ValueError):
                age = stale_after + 1
            if age > stale_after:
                record["state"] = "stale"
                record["message"] = "Process exists, but its service heartbeat is overdue."
                record["updated_at"] = stamp
        _trim(payload)
        _save(path, payload)
        services = {key: dict(value) for key, value in payload["services"].items()}
    counts = {state: 0 for state in sorted(VALID_STATES)}
    for record in services.values():
        state = record.get("state")
        if state in counts:
            counts[state] += 1
    counts["active"] = sum(counts.get(state, 0) for state in ("starting", "running", "paused", "stopping", "queued", "rendering"))
    counts["attention"] = counts.get("stale", 0) + counts.get("unverified", 0) + counts.get("crashed", 0) + counts.get("failed", 0)
    return {"version": REGISTRY_VERSION, "updated_at": stamp, "services": services, "counts": counts}
