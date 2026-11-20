"""Persistent live candidate queue and lightweight visual-quality scoring.

The signal detector finds interesting *times*. This module gives each time a durable
identity, prevents it being cut repeatedly after a worker restart, and records cheap
frame-level quality risks before expensive clip preparation.
"""
import math
import statistics
import time
from pathlib import Path

import cv2
import numpy as np

from .common import read_json, write_json

QUEUE_FILE = "candidates.json"
QUEUE_VERSION = 1
MAX_ITEMS = 250
ACTIVE_STATES = {"queued", "processing", "needs_review"}
TERMINAL_STATES = {"ready", "rejected", "filtered", "failed"}

# These are intentionally conservative risk indicators, not a semantic GTA scene classifier.
DARK_MEAN = 13.0
BLUR_VARIANCE = 10.0
LOW_MOTION_MEAN = 0.45
LOW_MOTION_FRACTION = 0.60


def _finite(value, default=0.0):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _peak(candidate):
    return _finite(candidate.get("peak", candidate.get("chat_peak", candidate.get("start", 0))))


def inspect_video(source, max_frames=9):
    """Sample a few small frames and return blur/brightness/motion risk indicators.

    OpenCV decode failures are fail-open: the clip is retained with an unavailable
    assessment rather than silently discarded.
    """
    capture = None
    try:
        capture = cv2.VideoCapture(str(source))
        if not capture.isOpened():
            return {"status": "unavailable", "reason": "Video could not be opened for frame inspection."}
        frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if frame_count < 3:
            return {"status": "unavailable", "reason": "Fewer than three video frames were available."}
        n = max(3, min(int(max_frames), 12))
        indexes = sorted({
            int(round(i * (frame_count - 1) / (n - 1)))
            for i in range(n)
        })
        grays, brightness, sharpness = [], [], []
        for index in indexes:
            capture.set(cv2.CAP_PROP_POS_FRAMES, index)
            ok, frame = capture.read()
            if not ok or frame is None:
                continue
            small = cv2.resize(frame, (320, 180), interpolation=cv2.INTER_AREA)
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
            grays.append(gray)
            brightness.append(float(np.mean(gray)))
            sharpness.append(float(cv2.Laplacian(gray, cv2.CV_64F).var()))
        if len(grays) < 3:
            return {"status": "unavailable", "reason": "Too few frames could be decoded for inspection."}
        motion = [float(cv2.absdiff(a, b).mean()) for a, b in zip(grays, grays[1:])]
        mean_brightness = float(statistics.median(brightness))
        blur = float(statistics.median(sharpness))
        motion_mean = float(statistics.median(motion)) if motion else 0.0
        low_motion_fraction = sum(value < LOW_MOTION_MEAN for value in motion) / max(1, len(motion))
        flags = []
        if mean_brightness < DARK_MEAN:
            flags.append("very_dark")
        if blur < BLUR_VARIANCE:
            flags.append("severely_blurred")
        if motion_mean < LOW_MOTION_MEAN and low_motion_fraction >= LOW_MOTION_FRACTION:
            flags.append("low_motion_or_static_scene")
        return {
            "status": "checked",
            "sampled_frames": len(grays),
            "brightness_median": round(mean_brightness, 2),
            "laplacian_variance_median": round(blur, 2),
            "frame_motion_median": round(motion_mean, 3),
            "low_motion_fraction": round(low_motion_fraction, 2),
            "flags": flags,
        }
    except Exception as exc:
        return {"status": "unavailable", "reason": f"Frame inspection failed: {type(exc).__name__}: {exc}"}
    finally:
        if capture is not None:
            try:
                capture.release()
            except Exception:
                pass


def apply_visual_quality(candidate, assessment):
    """Apply bounded visual-risk penalties while preserving the original signal score."""
    result = dict(candidate)
    signal_score = max(0, min(100, round(_finite(result.get("signal_score", result.get("score", 0))))))
    flags = list(assessment.get("flags") or []) if assessment.get("status") == "checked" else []
    penalties = {
        "very_dark": 8,
        "severely_blurred": 18,
        "low_motion_or_static_scene": 18,
    }
    penalty = min(35, sum(penalties.get(flag, 0) for flag in set(flags)))
    result["signal_score"] = signal_score
    result["quality_adjustment"] = -penalty
    result["score"] = max(0, signal_score - penalty)
    result["visual_quality"] = dict(assessment)
    result["quality_flags"] = flags
    if flags:
        labels = {
            "very_dark": "very dark footage",
            "severely_blurred": "severely blurred footage",
            "low_motion_or_static_scene": "a low-motion or static scene",
        }
        result["auto_reason"] = (str(result.get("auto_reason") or "").strip() + " "
                                 + "Visual quality caution: " + ", ".join(labels.get(x, x) for x in flags) + ".").strip()
    return result


def _bounded_record(candidate):
    """Keep queue records compact and JSON-safe; source paths and media are never stored here."""
    allowed = (
        "start", "end", "peak", "chat_peak", "chat_start", "chat_end", "score", "signal_score",
        "basis", "sub", "said", "hot_ref", "multiple", "peak_per_min", "avg_per_min", "z",
        "messages", "unique_chatters", "signals", "reaction_share", "top_words", "sample_chat",
        "auto_reason", "what", "why", "quality_adjustment", "quality_flags", "visual_quality",
        "filtered_reason", "error", "review_verdict", "recovery_note",
    )
    record = {key: candidate[key] for key in allowed if key in candidate}
    record["basis"] = list(record.get("basis") or [])[:3]
    record["said"] = [str(x)[:120] for x in (record.get("said") or [])[:6]]
    record["top_words"] = [str(x)[:80] for x in (record.get("top_words") or [])[:10]]
    record["sample_chat"] = [str(x)[:180] for x in (record.get("sample_chat") or [])[:12]]
    record["auto_reason"] = str(record.get("auto_reason") or "")[:1000]
    record["what"] = str(record.get("what") or "")[:500]
    record["why"] = str(record.get("why") or "")[:800]
    return record


class CandidateQueue:
    """Small file-backed queue. One live worker owns writes for a job directory."""

    def __init__(self, job_dir, first_id=1, max_items=MAX_ITEMS):
        self.path = Path(job_dir) / QUEUE_FILE
        self.max_items = max(25, int(max_items))
        payload = read_json(self.path, {}) or {}
        items = payload.get("items", []) if isinstance(payload, dict) else []
        self.items = [dict(item) for item in items if isinstance(item, dict) and _peak(item) >= 0]
        max_id = max((int(item.get("id") or 0) for item in self.items), default=0)
        self.next_id = max(int(payload.get("next_id") or 1) if isinstance(payload, dict) else 1,
                           int(first_id or 1), max_id + 1)
        # The live rolling buffer is not yet restart-persistent. Preserve unfinished
        # candidate records, but require review instead of cutting against a reset timeline.
        recovered = False
        for item in self.items:
            if item.get("queue_state") in ("queued", "processing"):
                item["queue_state"] = "needs_review"
                item["recovery_note"] = (
                    "The previous live worker stopped before extraction completed. "
                    "The rolling-buffer timeline is not restart-persistent; re-detect this moment in a new session."
                )
                recovered = True
        if recovered:
            self._save()

    def _save(self):
        if len(self.items) > self.max_items:
            active = [item for item in self.items if item.get("queue_state") in ACTIVE_STATES]
            terminal = [item for item in self.items if item.get("queue_state") not in ACTIVE_STATES]
            room = max(0, self.max_items - len(active))
            self.items = active[-self.max_items:] + terminal[-room:] if room else active[-self.max_items:]
            self.items.sort(key=_peak)
        write_json(self.path, {
            "version": QUEUE_VERSION,
            "next_id": self.next_id,
            "updated_at": time.time(),
            "items": self.items,
        })

    def seed(self, existing_clips):
        """Import clips from pre-queue live jobs so old moments are not cut again."""
        changed = False
        for clip in existing_clips or []:
            peak = _peak(clip)
            if any(abs(_peak(item) - peak) < 60.0 for item in self.items):
                continue
            record = _bounded_record(clip)
            try:
                item_id = int(clip.get("id") or self.next_id)
            except (TypeError, ValueError):
                item_id = self.next_id
            item_id = max(1, item_id)
            if any(int(item.get("id") or 0) == item_id for item in self.items):
                item_id = self.next_id
            self.next_id = max(self.next_id, item_id + 1)
            record.update({
                "id": item_id, "queue_state": "ready",
                "detected_at": time.time(), "updated_at": time.time(),
                "last_seen_at": time.time(), "observations": 1,
            })
            self.items.append(record)
            changed = True
        if changed:
            self._save()

    def discover(self, candidates, dedupe_window=60.0, minimum_score=0):
        """Persist eligible candidates and return only newly discovered queue entries."""
        accepted, changed = [], False
        ordered = sorted((c for c in candidates or [] if isinstance(c, dict)), key=_peak)
        now = time.time()
        for candidate in ordered:
            peak = _peak(candidate)
            if peak < 0 or _finite(candidate.get("score")) < minimum_score:
                continue
            match = min(self.items, key=lambda item: abs(_peak(item) - peak), default=None)
            if match is not None and abs(_peak(match) - peak) < max(1.0, float(dedupe_window)):
                match["last_seen_at"] = now
                match["observations"] = int(match.get("observations") or 1) + 1
                changed = True
                continue
            record = _bounded_record(candidate)
            record.update({
                "id": self.next_id,
                "queue_state": "queued",
                "detected_at": now,
                "updated_at": now,
                "last_seen_at": now,
                "observations": 1,
            })
            self.next_id += 1
            self.items.append(record)
            accepted.append(dict(record))
            changed = True
        if changed:
            self._save()
        return accepted

    def mark(self, candidate_id, state, **updates):
        if state not in ACTIVE_STATES | TERMINAL_STATES:
            raise ValueError(f"Unsupported candidate queue state: {state}")
        for item in self.items:
            if int(item.get("id") or 0) == int(candidate_id):
                item.update(_bounded_record({**item, **updates}))
                item["queue_state"] = state
                item["updated_at"] = time.time()
                self._save()
                return dict(item)
        raise KeyError(f"Candidate {candidate_id} is not in the queue.")

    def pending(self):
        """Return new work queued by this worker, in chronological order."""
        return [
            dict(item) for item in sorted(self.items, key=_peak)
            if item.get("queue_state") == "queued"
        ]

    def counts(self):
        result = {state: 0 for state in sorted(ACTIVE_STATES | TERMINAL_STATES)}
        for item in self.items:
            state = item.get("queue_state")
            if state in result:
                result[state] += 1
        return result

    def active_count(self):
        return sum(self.counts().get(state, 0) for state in ACTIVE_STATES)
