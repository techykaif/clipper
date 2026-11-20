"""Persistent wall-clock rolling-buffer inspection and safe clip extraction."""
import math
import os
import subprocess
import uuid
from pathlib import Path


class BufferCoverageError(RuntimeError):
    """A requested interval is not fully represented by saved media segments."""


def probe_duration(path, ffprobe="ffprobe", timeout=12):
    result = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, timeout=timeout,
    )
    if result.returncode != 0:
        raise BufferCoverageError((result.stderr or "ffprobe failed")[-500:])
    try:
        duration = float((result.stdout or "").strip().splitlines()[0])
    except (ValueError, IndexError):
        raise BufferCoverageError("ffprobe did not report a usable segment duration.")
    if not math.isfinite(duration) or duration <= 0 or duration > 60:
        raise BufferCoverageError(f"Invalid segment duration: {duration!r}")
    return duration


def segment_spans(buffer_dir, duration_reader=probe_duration):
    """Return probeable seg_<epoch>.ts files as wall-clock intervals."""
    root = Path(buffer_dir)
    spans = []
    if not root.is_dir():
        return spans
    for path in root.glob("seg_*.ts"):
        try:
            start = int(path.stem[4:])
            if start < 0 or not path.is_file() or path.stat().st_size <= 0:
                continue
            duration = float(duration_reader(path))
            if not math.isfinite(duration) or duration <= 0 or duration > 60:
                continue
            spans.append({"start": float(start), "end": float(start) + duration,
                          "duration": duration, "path": path})
        except (OSError, ValueError, TypeError, BufferCoverageError, subprocess.SubprocessError):
            continue
    return sorted(spans, key=lambda item: (item["start"], str(item["path"])))


def coverage_from_spans(spans, wall_start, wall_end, max_gap=0.75):
    """Check complete interval coverage; small filename timestamp quantisation is tolerated."""
    try:
        start, end, gap = float(wall_start), float(wall_end), float(max_gap)
    except (TypeError, ValueError):
        raise ValueError("Buffer times must be numeric.")
    if not all(math.isfinite(x) for x in (start, end, gap)) or end <= start or gap < 0:
        raise ValueError("Buffer interval must be finite, increasing and use a non-negative gap tolerance.")
    cursor = start
    selected = []
    for span in sorted(spans, key=lambda item: float(item["start"])):
        seg_start, seg_end = float(span["start"]), float(span["end"])
        if seg_end <= cursor:
            continue
        if seg_start > cursor + gap:
            break
        selected.append(span)
        cursor = max(cursor, seg_end)
        if cursor >= end:
            return {"covered": True, "requested_start": start, "requested_end": end,
                    "covered_until": end, "gap_seconds": 0.0, "segments": selected}
    missing = max(0.0, end - cursor)
    reason = (f"Saved footage ends at {cursor:.1f}; candidate needs footage through {end:.1f}."
              if cursor > start else "No saved segment covers the candidate start.")
    return {"covered": False, "requested_start": start, "requested_end": end,
            "covered_until": cursor, "gap_seconds": missing, "segments": selected, "reason": reason}


def coverage_report(buffer_dir, wall_start, wall_end, duration_reader=probe_duration, max_gap=0.75):
    spans = segment_spans(buffer_dir, duration_reader=duration_reader)
    return coverage_from_spans(spans, wall_start, wall_end, max_gap=max_gap)


def cut(buffer_dir, wall_start, wall_end, destination, *, ffmpeg="ffmpeg", timeout=180,
        duration_reader=probe_duration):
    """Extract only a continuously-covered interval and atomically promote a validated file."""
    dest = Path(destination)
    report = coverage_report(buffer_dir, wall_start, wall_end, duration_reader=duration_reader)
    if not report["covered"]:
        raise BufferCoverageError(report.get("reason") or "Saved footage does not cover this candidate.")
    selected = report["segments"]
    if not selected:
        raise BufferCoverageError("No saved media segments cover this candidate.")
    dest.parent.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    concat_file = dest.parent / f".concat-{token}.txt"
    temporary = dest.parent / f".{dest.stem}-{token}.tmp{dest.suffix or '.mp4'}"

    def escaped(path):
        value = str(Path(path).resolve())
        return value.replace("\\", "\\\\").replace("'", "'\\''")

    concat_file.write_text("".join(f"file '{escaped(item['path'])}'\n" for item in selected), encoding="utf-8")
    offset = max(0.0, float(wall_start) - float(selected[0]["start"]))
    try:
        try:
            result = subprocess.run(
                [ffmpeg, "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_file),
                 "-ss", f"{offset:.3f}", "-t", f"{float(wall_end) - float(wall_start):.3f}",
                 "-c:v", "libx264", "-crf", "14", "-preset", "medium", "-c:a", "aac", "-b:a", "320k",
                 "-movflags", "+faststart", str(temporary)],
                capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"ffmpeg timed out while cutting the rolling buffer ({timeout}-second limit).") from exc
        if result.returncode != 0 or not temporary.is_file() or temporary.stat().st_size <= 0:
            detail = (getattr(result, "stderr", "") or "ffmpeg produced no output")[-500:]
            raise RuntimeError("ffmpeg couldn't cut the rolling buffer: " + detail)
        actual = float(duration_reader(temporary))
        expected = float(wall_end) - float(wall_start)
        minimum = max(0.25, expected - max(1.5, expected * 0.12))
        if actual < minimum:
            raise RuntimeError(f"Extracted media is incomplete ({actual:.1f}s of {expected:.1f}s requested).")
        os.replace(temporary, dest)
        return dest
    finally:
        concat_file.unlink(missing_ok=True)
        temporary.unlink(missing_ok=True)
