"""Cross-process usage budget for optional Antigravity candidate reviews."""
import json
import math
import os
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from .common import JOBS

BUDGET_FILE = ".ai-review-budget.json"
LOCK_FILE = ".ai-review-budget.guard"
DEFAULT_DAILY_LIMIT = 20
DEFAULT_JOB_LIMIT = 10


class ReviewBudgetError(RuntimeError):
    """The local review budget could not be read or updated safely."""


def _limit(name, default):
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ReviewBudgetError(f"{name} must be a whole number between 0 and 10000.") from exc
    if value < 0 or value > 10000:
        raise ReviewBudgetError(f"{name} must be between 0 and 10000.")
    return value


def limits():
    return {
        "daily": _limit("CLIPPER_AI_REVIEW_DAILY_LIMIT", DEFAULT_DAILY_LIMIT),
        "job": _limit("CLIPPER_AI_REVIEW_JOB_LIMIT", DEFAULT_JOB_LIMIT),
    }


@contextmanager
def _guard(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as lock:
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


def reserve(job_key, candidate_id, *, jobs_dir=None, now=None):
    """Atomically reserve one attempt before invoking a model; duplicate IDs are idempotent."""
    key = str(job_key or "").strip()
    if not key or Path(key).name != key or key in (".", ".."):
        raise ValueError("A safe job key is required for review-budget accounting.")
    try:
        cid = int(candidate_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("A numeric candidate ID is required.") from exc
    if cid <= 0:
        raise ValueError("A positive candidate ID is required.")
    configured = limits()
    root = Path(jobs_dir or JOBS)
    file_path, lock_path = root / BUDGET_FILE, root / LOCK_FILE
    stamp = time.time() if now is None else float(now)
    if not math.isfinite(stamp):
        raise ValueError("Review-budget timestamp must be finite.")
    day = datetime.fromtimestamp(stamp).strftime("%Y-%m-%d")
    with _guard(lock_path):
        if file_path.exists():
            try:
                payload = json.loads(file_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, ValueError) as exc:
                raise ReviewBudgetError(f"Review budget ledger is unreadable; refusing to overwrite it: {exc}") from exc
            if not isinstance(payload, dict) or payload.get("version") != 1 or not isinstance(payload.get("days"), dict):
                raise ReviewBudgetError("Review budget ledger has an invalid structure; refusing to overwrite it.")
        else:
            payload = {"version": 1, "days": {}}
        records = payload["days"]
        current = records.get(day) or {"count": 0, "jobs": {}, "items": []}
        if not isinstance(current, dict) or not isinstance(current.get("jobs", {}), dict) or not isinstance(current.get("items", []), list):
            raise ReviewBudgetError("Review budget day entry is malformed; refusing to overwrite it.")
        items = current.setdefault("items", [])
        identity = f"{key}:{cid}"
        if identity in items:
            return {"allowed": True, "duplicate": True, "daily_used": int(current.get("count") or 0),
                    "daily_limit": configured["daily"], "job_used": int(current.get("jobs", {}).get(key, 0)),
                    "job_limit": configured["job"], "message": "This candidate already has a reserved review attempt."}
        daily_used = int(current.get("count") or 0)
        job_used = int(current.get("jobs", {}).get(key, 0))
        if daily_used >= configured["daily"] or job_used >= configured["job"]:
            reason = ("daily review budget exhausted" if daily_used >= configured["daily"]
                      else "this stream's review budget exhausted")
            return {"allowed": False, "duplicate": False, "daily_used": daily_used,
                    "daily_limit": configured["daily"], "job_used": job_used,
                    "job_limit": configured["job"], "message": reason}
        current["count"] = daily_used + 1
        current["jobs"][key] = job_used + 1
        items.append(identity)
        records[day] = current
        # Keep a short accounting history, not a transcript of user media.
        for old_day in sorted(records)[:-14]:
            records.pop(old_day, None)
        temporary = file_path.with_name(file_path.name + ".part")
        try:
            temporary.write_text(json.dumps(payload, indent=1), encoding="utf-8")
            os.replace(temporary, file_path)
        finally:
            temporary.unlink(missing_ok=True)
        return {"allowed": True, "duplicate": False, "daily_used": daily_used + 1,
                "daily_limit": configured["daily"], "job_used": job_used + 1,
                "job_limit": configured["job"], "message": "Review budget reserved."}
