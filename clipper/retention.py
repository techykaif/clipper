"""Opt-in cleanup for Clipper temporary cache and stale diagnostic logs.

Finished clips, source media, candidate metadata, the review ledger and saved buffers are
never part of this cleanup policy.
"""
import os
import time
from pathlib import Path

from .common import JOBS, TMP

SAFE_JOB_LOGS = {"live.log", "record.log", "chat.log", "error.log"}
SAFE_TMP_SUFFIXES = {".tmp", ".part", ".log", ".cache", ".jsonl"}


def _inside(path, root):
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except (OSError, ValueError):
        return False


def plan_cleanup(*, tmp_days=14, logs_days=30, tmp_dir=None, jobs_dir=None, now=None):
    """Return a dry-run list; no files are removed by this function."""
    try:
        tmp_age, log_age = float(tmp_days), float(logs_days)
    except (TypeError, ValueError) as exc:
        raise ValueError("Retention ages must be numeric day counts.") from exc
    if not (0 <= tmp_age <= 3650 and 0 <= log_age <= 3650):
        raise ValueError("Retention ages must be between 0 and 3650 days.")
    stamp = time.time() if now is None else float(now)
    if not (stamp >= 0 and stamp < float("inf")):
        raise ValueError("Cleanup timestamp must be finite and non-negative.")
    tmp_root, jobs_root = Path(tmp_dir or TMP), Path(jobs_dir or JOBS)
    candidates = []
    if tmp_root.is_dir():
        cutoff = stamp - tmp_age * 86400
        for root, dirs, files in os.walk(tmp_root, followlinks=False):
            dirs[:] = [d for d in dirs if not (Path(root) / d).is_symlink()]
            for name in files:
                path = Path(root) / name
                try:
                    if path.is_symlink() or not _inside(path, tmp_root):
                        continue
                    stat = path.stat()
                    if stat.st_mtime <= cutoff and path.suffix.lower() in SAFE_TMP_SUFFIXES:
                        candidates.append({"path": str(path), "size": stat.st_size, "category": "temporary cache"})
                except OSError:
                    continue
    if jobs_root.is_dir():
        cutoff = stamp - log_age * 86400
        for job_dir in jobs_root.iterdir():
            if not job_dir.is_dir() or not (job_dir / "job.json").is_file():
                continue
            try:
                job = __import__("json").loads((job_dir / "job.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            live_info = {}
            try:
                live_info = __import__("json").loads((job_dir / "live.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
            if job.get("live") is True and not live_info.get("ended"):
                continue
            for name in SAFE_JOB_LOGS:
                path = job_dir / name
                try:
                    if path.is_file() and not path.is_symlink() and path.stat().st_mtime <= cutoff:
                        candidates.append({"path": str(path), "size": path.stat().st_size, "category": "old diagnostic log"})
                except OSError:
                    continue
    candidates.sort(key=lambda item: (item["category"], item["path"]))
    return {
        "items": candidates,
        "count": len(candidates),
        "bytes": sum(int(item["size"]) for item in candidates),
        "tmp_days": tmp_age,
        "logs_days": log_age,
    }


def apply_cleanup(plan, *, tmp_dir=None, jobs_dir=None):
    """Delete only paths present in a generated plan and still inside approved roots."""
    tmp_root, jobs_root = Path(tmp_dir or TMP), Path(jobs_dir or JOBS)
    deleted, skipped = [], []
    for item in (plan or {}).get("items", []):
        path = Path(str(item.get("path") or ""))
        category = item.get("category")
        allowed = ((_inside(path, tmp_root) and category == "temporary cache")
                   or (_inside(path, jobs_root) and category == "old diagnostic log"
                       and path.name in SAFE_JOB_LOGS))
        if not allowed or path.is_symlink() or not path.is_file():
            skipped.append(str(path))
            continue
        try:
            path.unlink()
            deleted.append(str(path))
        except OSError:
            skipped.append(str(path))
    # Remove empty cache directories, never recurse-delete directories containing files.
    if tmp_root.is_dir():
        for root, dirs, _ in os.walk(tmp_root, topdown=False, followlinks=False):
            for name in dirs:
                path = Path(root) / name
                try:
                    if not path.is_symlink():
                        path.rmdir()
                except OSError:
                    pass
    return {"deleted": deleted, "skipped": skipped,
            "bytes": sum(int(item.get("size") or 0) for item in (plan or {}).get("items", [])
                         if str(item.get("path")) in deleted)}


def human_bytes(value):
    number = float(max(0, int(value or 0)))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if number < 1024 or unit == "TB":
            return f"{number:.0f} {unit}" if unit == "B" else f"{number:.1f} {unit}"
        number /= 1024
    return f"{number:.1f} TB"
