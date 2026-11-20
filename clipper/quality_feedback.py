"""Human feedback ledger for calibrating candidate quality without auto-changing thresholds."""
import json
import math
import os
import time
from contextlib import contextmanager
from pathlib import Path

from .common import JOBS

FEEDBACK_FILE = ".quality-feedback.json"
LOCK_FILE = ".quality-feedback.guard"
LABELS = {"keep", "reject", "unsure"}
MAX_RECORDS = 2000


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


def _load(path):
    if not path.exists():
        return {"version": 1, "items": []}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise RuntimeError(f"Quality feedback ledger is unreadable; refusing to overwrite it: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("version") != 1 or not isinstance(payload.get("items"), list):
        raise RuntimeError("Quality feedback ledger has an invalid structure; refusing to overwrite it.")
    return payload


def record(job_key, candidate, label, reason="", *, jobs_dir=None, now=None):
    """Upsert one feedback label per stream/candidate; never deletes or changes candidate media."""
    key = str(job_key or "").strip()
    if not key or Path(key).name != key or key in (".", ".."):
        raise ValueError("A safe stream key is required.")
    label = str(label or "").strip().lower()
    if label not in LABELS:
        raise ValueError(f"Feedback label must be one of {sorted(LABELS)}.")
    if not isinstance(candidate, dict):
        raise ValueError("Candidate details are required.")
    try:
        cid = int(candidate.get("id"))
        score = float(candidate.get("score"))
    except (TypeError, ValueError) as exc:
        raise ValueError("Candidate ID and score must be numeric.") from exc
    if cid <= 0 or not math.isfinite(score):
        raise ValueError("Candidate ID and score are invalid.")
    score = max(0, min(100, round(score)))
    stamp = time.time() if now is None else float(now)
    if not math.isfinite(stamp):
        raise ValueError("Feedback timestamp must be finite.")
    item = {
        "job_key": key, "candidate_id": cid, "label": label, "score": score,
        "signal_score": candidate.get("signal_score"), "review_verdict": (candidate.get("review") or {}).get("verdict"),
        "platform": str(candidate.get("platform") or "")[:40],
        "reason": " ".join(str(reason).split())[:300], "updated_at": stamp,
    }
    path = Path(jobs_dir or JOBS) / FEEDBACK_FILE
    with _guard(path.with_name(LOCK_FILE)):
        payload = _load(path)
        items = payload["items"]
        identity = (key, cid)
        found = next((i for i, old in enumerate(items)
                      if (old.get("job_key"), int(old.get("candidate_id", 0))) == identity), None)
        if found is None:
            items.append(item)
        else:
            items[found] = item
        payload["items"] = items[-MAX_RECORDS:]
        temporary = path.with_name(path.name + ".part")
        try:
            temporary.write_text(json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8")
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    return item


def calibration(*, jobs_dir=None, min_samples=20, min_above=5, target_precision=0.80):
    """Return a threshold suggestion from explicit good/bad labels; never apply it automatically."""
    path = Path(jobs_dir or JOBS) / FEEDBACK_FILE
    payload = _load(path)
    labelled = [x for x in payload["items"] if x.get("label") in ("keep", "reject")]
    keep = sum(x["label"] == "keep" for x in labelled)
    reject = len(labelled) - keep
    result = {"samples": len(labelled), "keep": keep, "reject": reject,
              "suggested_min_score": None, "precision": None, "ready": False}
    if len(labelled) < int(min_samples) or keep == 0 or reject == 0:
        return result
    for threshold in range(0, 101):
        above = [x for x in labelled if float(x.get("score", 0)) >= threshold]
        if len(above) < int(min_above):
            continue
        precision = sum(x["label"] == "keep" for x in above) / len(above)
        if precision >= float(target_precision):
            result.update({"suggested_min_score": threshold, "precision": round(precision, 3), "ready": True})
            break
    return result


def summary_text(*, jobs_dir=None):
    data = calibration(jobs_dir=jobs_dir)
    if not data["samples"]:
        return "No labelled quality feedback yet."
    text = (f"{data['samples']} labelled clips · {data['keep']} good · {data['reject']} poor")
    if data["ready"]:
        text += (f" · observed precision {data['precision']:.0%} above score "
                 f"{data['suggested_min_score']}; suggestion only, threshold unchanged")
    elif data["samples"] < 20:
        text += f" · need {20 - data['samples']} more labels before calibration"
    else:
        text += " · no reliable 80% precision threshold found yet"
    return text
