"""Local production metrics aggregated from existing job artifacts."""
from pathlib import Path

from .common import JOBS, read_json


def snapshot(*, jobs_dir=None):
    root = Path(jobs_dir or JOBS)
    out = {
        "streams": 0, "candidates": 0, "clips": 0, "rendered": 0,
        "render_failed": 0, "review_keep": 0, "review_reject": 0,
        "review_pending": 0, "media_bytes": 0,
    }
    if not root.is_dir():
        return out
    for job_dir in root.iterdir():
        if not job_dir.is_dir() or not (job_dir / "job.json").is_file():
            continue
        out["streams"] += 1
        summary = read_json(job_dir / "clips.json", {}) or {}
        clip_records = summary.get("clips", []) if isinstance(summary, dict) else []
        out["clips"] += len(clip_records) if isinstance(clip_records, list) else 0
        queue = read_json(job_dir / "candidates.json", {}) or {}
        items = queue.get("items", []) if isinstance(queue, dict) else []
        if isinstance(items, list):
            out["candidates"] += len(items)
        clips_dir = job_dir / "clips"
        if not clips_dir.is_dir():
            continue
        for clip_dir in clips_dir.iterdir():
            if not clip_dir.is_dir():
                continue
            review = read_json(clip_dir / "review.json", {}) or {}
            verdict = review.get("verdict")
            if review.get("status") == "reviewed" and verdict == "KEEP":
                out["review_keep"] += 1
            elif review.get("status") == "reviewed" and verdict == "REJECT":
                out["review_reject"] += 1
            elif review.get("status") in ("needs_review", "error"):
                out["review_pending"] += 1
            render = read_json(clip_dir / "render.json", {}) or {}
            if render.get("state") == "completed" or (clip_dir / "final.mp4").is_file():
                out["rendered"] += 1
            elif render.get("state") == "failed":
                out["render_failed"] += 1
            for name in ("source.mp4", "final.mp4"):
                path = clip_dir / name
                try:
                    if path.is_file():
                        out["media_bytes"] += path.stat().st_size
                except OSError:
                    pass
    return out


def human_bytes(value):
    number = float(max(0, int(value or 0)))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if number < 1024 or unit == "TB":
            return f"{number:.0f} {unit}" if unit in ("B", "KB") else f"{number:.1f} {unit}"
        number /= 1024
    return f"{number:.1f} TB"
