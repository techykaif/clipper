"""Conservative disk-space guardrails for media-heavy operations.

The reserve protects the user's volume from a growing live buffer, VOD downloads and
Remotion's temporary render output. It never deletes user media automatically.
"""
import math
import os
import shutil
from pathlib import Path

GIB = 1024 ** 3
MIN_FREE_ENV = "CLIPPER_MIN_FREE_GB"
DEFAULT_MIN_FREE_GB = 2.0


class StorageSafetyError(RuntimeError):
    """Free space could not be checked safely or the configured policy is invalid."""


class InsufficientDiskSpace(StorageSafetyError):
    """An operation would cross Clipper's configured free-space reserve."""


def minimum_free_bytes() -> int:
    """Return the configured free-space reserve in bytes (2 GiB by default)."""
    raw = os.environ.get(MIN_FREE_ENV, "").strip()
    if not raw:
        gb = DEFAULT_MIN_FREE_GB
    else:
        try:
            gb = float(raw)
        except (TypeError, ValueError) as exc:
            raise StorageSafetyError(f"{MIN_FREE_ENV} must be a number of GiB.") from exc
    if not math.isfinite(gb) or gb < 0 or gb > 1024:
        raise StorageSafetyError(f"{MIN_FREE_ENV} must be between 0 and 1024 GiB.")
    return int(gb * GIB)


def snapshot(path) -> dict:
    """Return volume capacity and whether the configured reserve has been crossed."""
    target = Path(path)
    try:
        usage = shutil.disk_usage(str(target))
    except OSError as exc:
        raise StorageSafetyError(f"Could not inspect free disk space at {target}: {exc}") from exc
    reserve = minimum_free_bytes()
    total, used, free = int(usage.total), int(usage.used), int(usage.free)
    return {
        "path": str(target),
        "total_bytes": total,
        "used_bytes": used,
        "free_bytes": free,
        "used_percent": (100.0 * used / total) if total > 0 else 0.0,
        "reserve_bytes": reserve,
        "below_reserve": free < reserve,
    }


def require_free_space(path, purpose: str) -> dict:
    """Fail closed before media work, without deleting or truncating existing files."""
    state = snapshot(path)
    if state["below_reserve"]:
        free_gib = state["free_bytes"] / GIB
        reserve_gib = state["reserve_bytes"] / GIB
        task = " ".join(str(purpose).split()) or "starting media work"
        raise InsufficientDiskSpace(
            f"Low disk space: {free_gib:.1f} GiB free; Clipper's safety reserve is "
            f"{reserve_gib:.1f} GiB. Stopping before {task}. Free up disk space, "
            f"or set {MIN_FREE_ENV} to a different reserve before launching Clipper."
        )
    return state
