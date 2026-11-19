"""Delete streams and clips. Deleted files go to the system Trash or Recycle Bin (recoverable) when there is one,
else they are removed. Remotion's links (remotion/public/clips/<job>-<n>.mp4 and the <job>-<n>/ folder of memes and
sounds) are removed with them."""
import os
import shutil
import subprocess
import time
from pathlib import Path

from .common import JOBS, REMOTION, read_json, write_json

TRASH = Path.home() / ".Trash"


def can_trash() -> bool:
    return os.name == "nt" or TRASH.is_dir()


def _recycle(p: Path) -> bool:
    """Windows: send to the Recycle Bin through .NET (no extra package). The path goes in an env var, never quoted."""
    ps = ("Add-Type -AssemblyName Microsoft.VisualBasic; $p = $env:CLIPPER_RECYCLE; "
          "if (Test-Path -LiteralPath $p -PathType Container) { [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteDirectory("
          "$p, 'OnlyErrorDialogs', 'SendToRecycleBin') } else { [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile("
          "$p, 'OnlyErrorDialogs', 'SendToRecycleBin') }")
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True,
                       env={**os.environ, "CLIPPER_RECYCLE": str(p)})
    return r.returncode == 0 and not p.exists()


def size_of(p: Path) -> int:
    if p.is_file():
        return p.stat().st_size
    total = 0
    for root, _, files in os.walk(p):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def busy(key: str) -> str:
    """Why this stream can't be deleted right now, or ''."""
    jd = JOBS / key
    from . import live
    if live.running(jd):
        return "it's being watched live (stop it first)"
    st = read_json(jd / "status.json", {}) or {}
    if st.get("stage") not in (None, "", "done", "error") and not (read_json(jd / "job.json", {}) or {}).get("live"):
        if time.time() - (jd / "status.json").stat().st_mtime < 600:
            return "it's still being analysed"
    return ""


def _discard(p: Path, label: str):
    if not p.exists():
        return
    if os.name == "nt" and _recycle(p):
        return
    if TRASH.is_dir() and os.access(TRASH, os.W_OK):
        dest = TRASH / f"{label} {time.strftime('%H.%M.%S')}"
        shutil.move(str(p), str(dest))
    elif p.is_dir():
        shutil.rmtree(p)
    else:
        p.unlink()


def _unlink_public(key: str, n=None):
    pub = REMOTION / "public" / "clips"
    for f in pub.glob(f"{key}-{n if n is not None else '*'}.mp4"):
        f.unlink(missing_ok=True)
    for d in pub.glob(f"{key}-{n if n is not None else '*'}"):      # memes and sounds linked for effects
        if d.is_dir() and d.name[len(key) + 1:].isdigit():
            shutil.rmtree(d, ignore_errors=True)


def delete_streams(keys):
    done, skipped = [], []
    for k in keys:
        jd = JOBS / k
        if not jd.is_dir() or jd.parent != JOBS:
            continue
        why = busy(k)
        if why:
            skipped.append((k, why))
            continue
        _unlink_public(k)
        _discard(jd, f"Clipper stream {k}")
        done.append(k)
    return done, skipped


def delete_clips(key: str, ids):
    jd = JOBS / key
    why = busy(key)
    if why:
        return [], [(i, why) for i in ids]
    ids = {int(i) for i in ids}
    done = []
    for n in sorted(ids):
        cd = jd / "clips" / str(n)
        _unlink_public(key, n)
        if cd.exists():
            _discard(cd, f"Clipper {key} clip {n}")
        done.append(n)
    c = read_json(jd / "clips.json")
    if c:
        c["clips"] = [x for x in c.get("clips", []) if x["id"] not in ids]
        write_json(jd / "clips.json", c)
    return done, []



def discard_rejected_clip(key: str, clip_id: int) -> bool:
    """Remove a reviewed rejection from the library and send its files to the OS trash.

    This internal quality-gate action deliberately bypasses busy(): the VOD/live
    review worker owns the candidate while it commits the verdict.
    Only a persisted, validated REJECT verdict may use this path.
    """
    if not key or Path(key).name != key:
        raise ValueError("Invalid stream key.")
    jd = JOBS / key
    if not jd.is_dir() or jd.parent != JOBS:
        return False

    n = int(clip_id)
    cd = jd / "clips" / str(n)
    if cd.exists():
        review = read_json(cd / "review.json", {}) or {}
        if review.get("status") != "reviewed" or review.get("verdict") != "REJECT":
            raise ValueError("Only a completed Antigravity REJECT verdict can be discarded.")
        _unlink_public(key, n)
        _discard(cd, f"Clipper rejected {key} clip {n}")

    summary = read_json(jd / "clips.json", {}) or {}
    if summary:
        summary["clips"] = [c for c in summary.get("clips", []) if int(c.get("id", -1)) != n]
        write_json(jd / "clips.json", summary)
    return True
