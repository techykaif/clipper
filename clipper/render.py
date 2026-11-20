"""Clip -> Remotion props -> final 1080x1920 Short + SRT.

edit.json holds the dashboard's settings (crops, layout, hook, subtitle style). fx.json, written by Claude (the
/short skill) or by hand, adds effects: colour grade, punch-in zooms, shakes, flashes, memes and sounds on top,
text pops, emphasised subtitle words and censoring. Both are optional beyond what the dashboard writes; see
normalize_fx for every field."""
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
import wave
from contextlib import contextmanager
from pathlib import Path

import psutil

from .common import REMOTION, read_json, tool, write_json
from . import services, subs

EDITABLE = ["trimStart", "trimEnd", "hookStart", "hookEnd", "hookEnabled", "layout", "split", "faceRect", "gameRect",
            "subsEnabled", "subColor", "subSize", "subY", "subUpper"]
GRADES = ("none", "warm", "cold")
BANDS = ("all", "face", "game")
MEDIA_VIDEO = (".mp4", ".webm", ".mov", ".m4v")
MEDIA_IMAGE = (".png", ".jpg", ".jpeg", ".webp", ".gif")
MEDIA_AUDIO = (".mp3", ".m4a", ".wav", ".ogg", ".aac")


class FxError(ValueError):
    """fx.json has a mistake; the message says which entry and why."""


def public_name(jd: Path, n: int) -> str:
    return f"clips/{jd.name}-{n}.mp4"


def _link(src: Path, dst: Path):
    """Hard link (same disk, no extra space); copy when linking isn't possible."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        try:
            if os.path.samefile(src, dst):
                return
        except OSError:
            pass
        dst.unlink()
    try:
        os.link(src, dst)
    except OSError:
        shutil.copyfile(src, dst)


def link_source(jd: Path, n: int) -> str:
    rel = public_name(jd, n)
    _link(jd / "clips" / str(n) / "source.mp4", REMOTION / "public" / rel)
    return rel


def default_edit(clip, info, rects):
    split = 0.4
    return {
        "trimStart": 0.0, "trimEnd": round(info["duration"], 2),
        "hookStart": clip["hook"][0], "hookEnd": clip["hook"][1], "hookEnabled": True,
        # face found -> face band on top by default (flip to bottom in the dashboard); none found -> full-frame gameplay
        "layout": "faceTop" if rects.get("found") else "noFace",
        "split": split, "faceRect": rects["face"], "gameRect": rects["game"],
        "subsEnabled": True, "subColor": "#FFFFFF", "subSize": 110, "subY": split, "subUpper": True,
    }


def timeline(p):
    segs, o = [], 0.0
    if p["hookEnabled"] and p["hookEnd"] > p["hookStart"]:
        segs.append((p["hookStart"], p["hookEnd"], 0.0))
        o = p["hookEnd"] - p["hookStart"]
    segs.append((p["trimStart"], min(p["trimEnd"], p["clipLength"]), o))
    return segs


def length(p) -> float:
    a, b, o = timeline(p)[-1]
    return o + (b - a)


# ---------------- fx.json ----------------
def _num(d, k, default, lo=None, hi=None, where=""):
    v = d.get(k, default)
    try:
        v = float(v)
    except (TypeError, ValueError):
        raise FxError(f"{where}: {k} must be a number, got {d.get(k)!r}")
    if not math.isfinite(v):
        raise FxError(f"{where}: {k} must be finite")
    if lo is not None:
        v = max(lo, v)
    if hi is not None:
        v = min(hi, v)
    return v


def _color(v, default, where):
    v = (v or default).strip()
    if not re.fullmatch(r"#[0-9A-Fa-f]{6}", v):
        raise FxError(f"{where}: colour must look like #FFD400, got {v!r}")
    return v.upper()


def _norm(w: str) -> str:
    return re.sub(r"[^\w']", "", w.lower())


def _matches(text: str, pats) -> bool:
    t = _norm(text)
    for p in pats:
        p = p.lower().strip()
        if not p:
            continue
        if p.endswith("*") and t.startswith(_norm(p[:-1])) and _norm(p[:-1]):
            return True
        if t == _norm(p):
            return True
    return False


def media_seconds(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True).stdout.strip()
    try:
        return float(out)
    except ValueError:
        return 0.0


def _media(cd: Path, jd: Path, n: int, src, i: int, kinds, where):
    """Resolve a meme/sound path (relative to the clip folder, or absolute), link it into remotion/public."""
    if not src:
        raise FxError(f"{where}: src is missing")
    f = Path(src)
    f = f if f.is_absolute() else cd / f
    if not f.is_file():
        raise FxError(f"{where}: file not found: {f}")
    if f.suffix.lower() not in kinds:
        raise FxError(f"{where}: {f.name} isn't one of {', '.join(kinds)}")
    rel = f"clips/{jd.name}-{n}/{i}-{re.sub(r'[^A-Za-z0-9._-]+', '-', f.name)}"
    _link(f, REMOTION / "public" / rel)
    return rel, f


def normalize_fx(jd: Path, n: int, p) -> dict:
    """Read clips/<n>/fx.json (all optional) into the exact shape the composition expects.

    Times: `at`/`dur` are seconds in the FINISHED Short (the hook, if on, plays first: a source moment s in the
    body appears at hookLength + s - trimStart). Censoring uses SOURCE clip seconds (words.json times), because the
    same word can play twice (hook + body).
    {
      "grade": "none" | "warm" | "cold",
      "zooms":   [{"at", "dur", "scale": 1.0-2.5, "x": 0-1, "y": 0-1, "band": "all"|"face"|"game", "ease": true}],
      "shakes":  [{"at", "dur", "strength": px}],
      "flashes": [{"at", "dur": 0.15, "color": "#FFFFFF"}],
      "overlays":[{"src": "memes/x.mp4|png", "at", "dur", "from": 0, "x": 0.5, "y": 0.5, "w": 0.6, "full": false,
                   "fit": "contain"|"cover", "volume": 1, "duck": 0.25, "rotate": 0}],
      "sounds":  [{"src": "memes/x.mp3", "at", "volume": 1, "duck": 1}],
      "texts":   [{"text", "at", "dur", "y": 0.12, "color": "#FFFFFF", "size": 90}],
      "censor":  {"words": ["fuck*", "shit"], "spans": [{"start", "end"}], "beep": true, "volume": 0.5},
      "emphasis":{"words": ["no", "way"], "color": "#FFD400", "scale": 1.25}
    }"""
    cd = jd / "clips" / str(n)
    raw = read_json(cd / "fx.json", {}) or {}
    if not isinstance(raw, dict):
        raise FxError("fx.json must be an object")
    L = length(p)
    fps = p["fps"]

    def items(k):
        v = raw.get(k, [])
        if not isinstance(v, list):
            raise FxError(f"{k} must be a list")
        for i, d in enumerate(v):
            if not isinstance(d, dict):
                raise FxError(f"{k}[{i}] must be an object")
            at = _num(d, "at", 0, 0, where=f"{k}[{i}]")
            if at >= L:
                continue                       # starts after the Short ends
            yield i, d, at, f"{k}[{i}]"

    def dur(d, at, default, where, lo=1 / fps):
        return min(_num(d, "dur", default, lo, where=where), L - at)

    grade = raw.get("grade", "none")
    if grade not in GRADES:
        raise FxError(f"grade must be one of {GRADES}, got {grade!r}")
    fx = {"grade": grade, "zooms": [], "shakes": [], "flashes": [], "overlays": [], "sounds": [], "texts": []}
    for i, d, at, w in items("zooms"):
        band = d.get("band", "all")
        if band not in BANDS:
            raise FxError(f"{w}: band must be one of {BANDS}")
        fx["zooms"].append({"at": at, "dur": dur(d, at, 1.2, w), "scale": _num(d, "scale", 1.25, 1, 2.5, w),
                            "x": _num(d, "x", 0.5, 0, 1, w), "y": _num(d, "y", 0.5, 0, 1, w), "band": band,
                            "ease": bool(d.get("ease", True))})
    for i, d, at, w in items("shakes"):
        fx["shakes"].append({"at": at, "dur": dur(d, at, 0.4, w), "strength": _num(d, "strength", 16, 0, 80, w)})
    for i, d, at, w in items("flashes"):
        fx["flashes"].append({"at": at, "dur": dur(d, at, 0.15, w), "color": _color(d.get("color"), "#FFFFFF", w)})
    for i, d, at, w in items("overlays"):
        rel, f = _media(cd, jd, n, d.get("src"), i, MEDIA_VIDEO + MEDIA_IMAGE, w)
        video = f.suffix.lower() in MEDIA_VIDEO
        start = _num(d, "from", 0, 0, where=w)
        own = max(1 / fps, media_seconds(f) - start) if video else 2.0
        fit = d.get("fit", "contain")
        if fit not in ("contain", "cover"):
            raise FxError(f"{w}: fit must be contain or cover")
        vol = _num(d, "volume", 1 if video else 0, 0, 2, w)
        fx["overlays"].append({"src": rel, "kind": "video" if video else "image", "at": at,
                               "dur": min(_num(d, "dur", own, 1 / fps, where=w), own if video else 1e9, L - at),
                               "from": start, "x": _num(d, "x", 0.5, 0, 1, w), "y": _num(d, "y", 0.5, 0, 1, w),
                               "w": _num(d, "w", 0.6, 0.05, 1, w), "full": bool(d.get("full", False)), "fit": fit,
                               "volume": vol, "duck": _num(d, "duck", 0.25 if vol > 0 else 1, 0, 1, w),
                               "rotate": _num(d, "rotate", 0, -45, 45, w)})
    for i, d, at, w in items("sounds"):
        rel, f = _media(cd, jd, n, d.get("src"), 100 + i, MEDIA_AUDIO + MEDIA_VIDEO, w)
        own = max(1 / fps, media_seconds(f))
        fx["sounds"].append({"src": rel, "at": at, "dur": min(_num(d, "dur", own, 1 / fps, where=w), own, L - at),
                             "volume": _num(d, "volume", 1, 0, 2, w), "duck": _num(d, "duck", 1, 0, 1, w)})
    for i, d, at, w in items("texts"):
        if not str(d.get("text", "")).strip():
            raise FxError(f"{w}: text is empty")
        fx["texts"].append({"text": str(d["text"]), "at": at, "dur": dur(d, at, 2.0, w),
                            "y": _num(d, "y", 0.12, 0.03, 0.97, w), "color": _color(d.get("color"), "#FFFFFF", w),
                            "size": _num(d, "size", 90, 30, 220, w)})

    cen = raw.get("censor") or {}
    if not isinstance(cen, dict):
        raise FxError("censor must be an object: {\"words\": [...], \"spans\": [...]}")
    spans = []
    for i, s in enumerate(cen.get("spans", [])):
        a, b = _num(s, "start", 0, 0, where=f"censor.spans[{i}]"), _num(s, "end", 0, 0, where=f"censor.spans[{i}]")
        if b > a:
            spans.append({"start": a, "end": b})
    pats = [str(x) for x in cen.get("words", [])]
    words = []
    emph = raw.get("emphasis") or {}
    epats = [str(x) for x in emph.get("words", [])]
    for wd in p["words"]:
        wd = dict(wd)
        if pats and _matches(wd["text"], pats):
            spans.append({"start": max(0.0, wd["start"] - 0.03), "end": wd["end"] + 0.03})
        if any(s["start"] < wd["end"] and wd["start"] < s["end"] for s in spans):
            t = wd["text"]
            letters = [i for i, ch in enumerate(t) if ch.isalnum()]
            if letters:                       # f*** : first letter kept, punctuation kept
                wd["text"] = "".join(ch if (i == letters[0] or not ch.isalnum()) else "*" for i, ch in enumerate(t))
        wd["emph"] = bool(epats) and _matches(wd["text"], epats)
        words.append(wd)
    p["words"] = words
    fx["censor"] = spans
    fx["beep"] = bool(cen.get("beep", True))
    fx["beepVolume"] = _num(cen, "volume", 0.5, 0, 2, "censor")
    # where each censored span plays in the finished Short (hook and body), for the beeps
    fx["censorOut"] = [{"at": o + max(s["start"], a) - a, "dur": min(s["end"], b) - max(s["start"], a)}
                       for a, b, o in timeline(p) for s in spans if s["start"] < b and a < s["end"]]
    fx["emphColor"] = _color(emph.get("color"), "#FFD400", "emphasis")
    fx["emphScale"] = _num(emph, "scale", 1.25, 1, 2, "emphasis")
    return fx


def beep_file() -> str:
    """A 1 kHz censor tone in remotion/public/sfx (made once, no download)."""
    rel = "sfx/beep-v2.wav"
    f = REMOTION / "public" / rel
    if not f.exists():
        import random
        f.parent.mkdir(parents=True, exist_ok=True)
        rate, secs = 48000, 3
        rnd = random.Random(7)
        # A perfectly periodic sine can look like MP3 frame headers to ffmpeg's WAV probe (Remotion's audio mix then
        # fails), so add a little dither. 5 ms fades at both ends avoid clicks.
        frames = []
        for i in range(rate * secs):
            env = min(1.0, i / 240, (rate * secs - i) / 240)
            frames.append(struct.pack("<h", int(env * 9000 * math.sin(2 * math.pi * 1000 * i / rate)) + rnd.randint(-60, 60)))
        with wave.open(str(f), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(b"".join(frames))
    return rel


def props_for(jd: Path, n: int):
    cd = jd / "clips" / str(n)
    meta = read_json(cd / "clip.json")
    edit = read_json(cd / "edit.json")
    words = read_json(cd / "words.json", [])
    info = meta["info"]
    p = {"src": link_source(jd, n), "srcWidth": info["width"], "srcHeight": info["height"],
         "fps": info["fps"], "clipLength": info["duration"], "words": words}
    p.update({k: edit[k] for k in EDITABLE})
    p["trimEnd"] = min(p["trimEnd"], info["duration"])
    p["fx"] = normalize_fx(jd, n, p)
    p["beepSrc"] = beep_file()
    return p


def chrome_env():
    env = dict(os.environ)
    if sys.platform == "darwin":
        # Chrome builds a fresh profile in TMPDIR at every launch; on a USB stick that took >25 s and Remotion gave
        # up connecting. Use the per-user temp for this subprocess (small, cleaned by Remotion).
        env["TMPDIR"] = subprocess.run(["getconf", "DARWIN_USER_TEMP_DIR"], capture_output=True,
                                       text=True).stdout.strip() or "/tmp"
        env.pop("TEMP", None)
        env.pop("TMP", None)
    elif os.name != "nt":
        env["TMPDIR"] = "/tmp"
        env.pop("TEMP", None)
        env.pop("TMP", None)
    return env


def _lines(stream):
    buf = b""
    while True:
        ch = stream.read1(4096) if hasattr(stream, "read1") else stream.read(4096)
        if not ch:
            break
        buf += ch
        parts = re.split(rb"[\r\n]", buf)
        buf = parts.pop()
        for p in parts:
            if p.strip():
                yield p.decode("utf-8", "replace").rstrip()
    if buf.strip():
        yield buf.decode("utf-8", "replace")


RENDER_WALL_TIMEOUT_S = 20 * 60
RENDER_QUEUE_TIMEOUT_S = 30 * 60
RENDER_LOCK_NAME = ".clipper-render.lock"
OUTPUT_WIDTH, OUTPUT_HEIGHT = 1080, 1920


@contextmanager
def _render_guard(progress=lambda pct, msg: None, timeout=RENDER_QUEUE_TIMEOUT_S):
    """Cross-process single-render gate shared by every Clipper dashboard instance."""
    REMOTION.mkdir(parents=True, exist_ok=True)
    lock_path = REMOTION / RENDER_LOCK_NAME
    lock_file = lock_path.open("a+b")
    try:
        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() == 0:
            lock_file.write(b"0")
            lock_file.flush()
        started = time.monotonic()
        acquired = False
        while not acquired:
            lock_file.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError:
                waited = time.monotonic() - started
                if waited >= timeout:
                    raise TimeoutError(f"Render queue wait exceeded {int(timeout)} seconds.")
                progress(0, f"Queued behind another render ({int(waited)}s waiting)")
                time.sleep(min(1.0, max(0.1, timeout - waited)))
        try:
            yield
        finally:
            lock_file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    finally:
        lock_file.close()


def _render_state(cd: Path, state: str, **updates):
    path = cd / "render.json"
    previous = read_json(path, {}) or {}
    record = {**previous, **updates, "state": state, "updated_at": time.time()}
    write_json(path, record)
    return record


def _fps_value(value):
    try:
        if isinstance(value, str) and "/" in value:
            a, b = value.split("/", 1)
            return float(a) / float(b)
        return float(value)
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0


def resource_profile():
    """Choose conservative Remotion tab concurrency and video cache from current device headroom."""
    try:
        logical = max(1, int(psutil.cpu_count(logical=True) or os.cpu_count() or 2))
        physical = max(1, int(psutil.cpu_count(logical=False) or logical))
        available = max(256 * 1024 * 1024, int(psutil.virtual_memory().available))
    except (psutil.Error, OSError, TypeError, ValueError, AttributeError):
        logical, physical, available = 2, 1, 2 * 1024 ** 3

    available_gb = available / (1024 ** 3)
    # Rendering gets one browser tab under memory pressure; otherwise cap at four and
    # never schedule more tabs than physical cores or roughly one per 2 GB available.
    concurrency = min(4, physical, max(1, int(available_gb // 2)))
    override = os.environ.get("CLIPPER_RENDER_CONCURRENCY", "").strip()
    if override:
        try:
            concurrency = max(1, min(logical, 4, int(override)))
        except ValueError:
            pass
    cache_bytes = int(min(512 * 1024 ** 2, max(96 * 1024 ** 2, available * 0.12)))
    return {
        "logical_threads": logical,
        "physical_cores": physical,
        "available_memory_bytes": available,
        "available_memory_gb": round(available_gb, 2),
        "concurrency": concurrency,
        "offthread_video_cache_bytes": cache_bytes,
    }


def _probe_output(path: Path):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        capture_output=True, text=True, timeout=45,
    )
    if result.returncode:
        raise RuntimeError("ffprobe could not inspect the rendered file: " + (result.stderr or "unknown error")[-500:])
    try:
        return json.loads(result.stdout)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("ffprobe returned invalid output metadata.") from exc


def validate_render_output(path: Path, expected_duration: float, expected_fps: float, probe=None):
    """Reject truncated, wrong-codec or non-vertical exports before promoting them to final.mp4."""
    path = Path(path)
    if not path.is_file() or path.stat().st_size < 1024:
        raise RuntimeError("Rendered output is missing or unexpectedly small.")
    data = probe if probe is not None else _probe_output(path)
    if not isinstance(data, dict):
        raise RuntimeError("Rendered output metadata is not an object.")
    streams = data.get("streams") or []
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    if not video:
        raise RuntimeError("Rendered output contains no video stream.")
    width, height = int(video.get("width") or 0), int(video.get("height") or 0)
    if (width, height) != (OUTPUT_WIDTH, OUTPUT_HEIGHT):
        raise RuntimeError(
            f"Rendered output is {width}x{height}; expected {OUTPUT_WIDTH}x{OUTPUT_HEIGHT} vertical video."
        )
    codec = str(video.get("codec_name") or "").lower()
    if codec != "h264":
        raise RuntimeError(f"Rendered output uses {codec or 'an unknown codec'}; expected H.264.")
    pix_fmt = str(video.get("pix_fmt") or "")
    if pix_fmt and pix_fmt != "yuv420p":
        raise RuntimeError(f"Rendered output pixel format is {pix_fmt}; expected yuv420p.")
    actual_duration = _finite_number((data.get("format") or {}).get("duration") or video.get("duration"))
    if actual_duration <= 0:
        raise RuntimeError("Rendered output has no valid duration.")
    tolerance = max(0.75, 2.0 / max(1.0, float(expected_fps or 30)))
    if expected_duration > 0 and abs(actual_duration - expected_duration) > tolerance:
        raise RuntimeError(
            f"Rendered duration is {actual_duration:.2f}s; expected about {expected_duration:.2f}s."
        )
    actual_fps = _fps_value(video.get("avg_frame_rate") or video.get("r_frame_rate"))
    if expected_fps > 0 and actual_fps > 0 and abs(actual_fps - expected_fps) > max(1.1, expected_fps * 0.05):
        raise RuntimeError(f"Rendered frame rate is {actual_fps:.3f} fps; expected {expected_fps:.3f} fps.")
    return {
        "width": width, "height": height, "codec": codec, "pixel_format": pix_fmt,
        "duration": round(actual_duration, 3), "fps": round(actual_fps, 3) if actual_fps else None,
        "bytes": path.stat().st_size,
    }


def _finite_number(value, default=0.0):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _stop_render_process(proc):
    """Terminate the Remotion process tree, including Chrome/Node children."""
    try:
        root = psutil.Process(proc.pid)
    except (psutil.NoSuchProcess, psutil.AccessDenied, AttributeError, TypeError):
        try:
            proc.terminate()
        except Exception:
            pass
        return
    try:
        children = root.children(recursive=True)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        children = []
    for child in reversed(children):
        try:
            child.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    try:
        root.terminate()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass
    _, alive = psutil.wait_procs(children + [root], timeout=8)
    for process in alive:
        try:
            process.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    if alive:
        psutil.wait_procs(alive, timeout=3)


def _run_remotion(cmd, progress, tail):
    """Stream render progress while enforcing a hard wall-clock ceiling."""
    popen_options = {
        "cwd": REMOTION, "stdout": subprocess.PIPE, "stderr": subprocess.STDOUT,
        "env": chrome_env(), "bufsize": 0,
    }
    if os.name == "nt":
        popen_options["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        popen_options["start_new_session"] = True
    proc = subprocess.Popen(cmd, **popen_options)
    finished = threading.Event()
    timed_out = threading.Event()

    def watchdog():
        if not finished.wait(RENDER_WALL_TIMEOUT_S):
            timed_out.set()
            _stop_render_process(proc)

    monitor = threading.Thread(target=watchdog, name="clipper-render-watchdog", daemon=True)
    monitor.start()
    return_code = None
    try:
        for line in _lines(proc.stdout):
            tail[:] = (tail + [line])[-30:]
            match = re.search(r"Rendered (\d+)/(\d+)", line)
            if match:
                progress(100 * int(match.group(1)) / max(1, int(match.group(2))), "Rendering")
            elif "Encoded" in line or "Stitch" in line:
                progress(99, "Encoding")
        return_code = proc.wait()
    finally:
        finished.set()
        monitor.join(timeout=2)
        if proc.stdout:
            try:
                proc.stdout.close()
            except Exception:
                pass
    if timed_out.is_set():
        raise TimeoutError(f"Remotion exceeded the {RENDER_WALL_TIMEOUT_S}-second render limit.")
    if return_code != 0:
        raise RuntimeError("Remotion render failed:\n" + "\n".join(tail[-10:]))


def render(jd: Path, n: int, progress=lambda pct, msg: None):
    """Render one Short through a persistent single-render queue and atomic output promotion."""
    cd = jd / "clips" / str(n)
    cd.mkdir(parents=True, exist_ok=True)
    review = read_json(cd / "review.json", {}) or {}
    if review.get("status") == "reviewed" and review.get("verdict") == "REJECT":
        raise RuntimeError("Antigravity rejected this candidate; it has been removed from the library.")

    p = props_for(jd, n)
    write_json(cd / "props.json", p)
    expected_duration = length(p)
    if not math.isfinite(expected_duration) or expected_duration <= 0:
        raise RuntimeError("The edit produces an empty or invalid timeline.")
    out = cd / "final.mp4"
    srt_out = cd / "final.srt"
    resources = resource_profile()
    # Root.tsx rounds the source FPS and caps Remotion's composition at 60 fps.
    expected_render_fps = min(60, max(1, round(float(p["fps"] or 30))))
    temp_out = cd / f".final-{os.getpid()}-{time.time_ns()}.rendering.mp4"
    temp_srt = cd / f".final-{os.getpid()}-{time.time_ns()}.rendering.srt"
    previous = read_json(cd / "render.json", {}) or {}
    attempts = int(previous.get("attempts") or 0) + 1
    _render_state(cd, "queued", attempts=attempts, queued_at=time.time(), error=None,
                  resource_profile=resources,
                  recovered_previous_run=previous.get("state") == "running")
    process = psutil.Process(os.getpid())
    service_id = f"render:{jd.name}:{n}:{process.pid}:{time.time_ns()}"
    service_created = process.create_time()
    service_recorded = False
    try:
        services.register(
            service_id, "render", jd.name, process.pid, service_created,
            state="queued", message="Waiting for the renderer", jobs_dir=jd.parent,
            metadata={"clip_id": int(n), "render_attempt": attempts},
        )
        service_recorded = True
    except Exception:
        pass

    def report_progress(pct, message):
        if service_recorded:
            try:
                services.heartbeat(service_id, process.pid, service_created, jobs_dir=jd.parent)
            except Exception:
                pass
        progress(pct, message)

    try:
        with _render_guard(report_progress):
            _render_state(cd, "running", attempts=attempts, started_at=time.time(), error=None,
                          resource_profile=resources)
            if service_recorded:
                try:
                    services.update(service_id, process.pid, service_created, state="rendering",
                                    message="Remotion rendering", jobs_dir=jd.parent)
                except Exception:
                    pass
            cmd = [
                tool("npx"), "remotion", "render", "src/index.ts", "ClipShort", str(temp_out),
                f"--props={cd / 'props.json'}", "--codec=h264", "--crf=16",
                "--pixel-format=yuv420p", "--color-space=bt709", "--audio-bitrate=320k",
                f"--concurrency={resources['concurrency']}",
                f"--offthreadvideo-cache-size-in-bytes={resources['offthread_video_cache_bytes']}",
                "--timeout=120000", "--log=info",
            ]
            tail = []
            report_progress(1, "Starting Remotion renderer")
            _run_remotion(cmd, report_progress, tail)
            report_progress(99, "Validating vertical export")
            validation = validate_render_output(temp_out, expected_duration, expected_render_fps)
            if p["subsEnabled"] and p["words"]:
                subs.srt(p["words"], timeline(p), temp_srt)
            temp_out.replace(out)
            if temp_srt.exists():
                temp_srt.replace(srt_out)
            else:
                srt_out.unlink(missing_ok=True)
            _render_state(cd, "completed", attempts=attempts, completed_at=time.time(),
                          output=str(out), output_bytes=validation["bytes"], output_info=validation,
                          error=None)
            report_progress(100, "Export validated and saved")
            if service_recorded:
                try:
                    services.finish(service_id, process.pid, service_created, state="completed",
                                    message="Vertical export validated and saved", jobs_dir=jd.parent)
                except Exception:
                    pass
            return out
    except Exception as exc:
        _render_state(cd, "failed", attempts=attempts, failed_at=time.time(), error=str(exc)[-1500:])
        if service_recorded:
            try:
                services.finish(service_id, process.pid, service_created, state="failed",
                                message=str(exc), jobs_dir=jd.parent)
            except Exception:
                pass
        raise
    finally:
        temp_out.unlink(missing_ok=True)
        temp_srt.unlink(missing_ok=True)

def recover_crashed_render(record, jobs_dir=None) -> bool:
    """Mark an interrupted render as failed while preserving the last known-good export."""
    if not isinstance(record, dict) or record.get("type") != "render" or record.get("state") != "crashed":
        return False
    if record.get("recovered_at"):
        return False
    job_key = str(record.get("job_key") or "")
    if not job_key or Path(job_key).name != job_key or job_key in (".", ".."):
        return False
    try:
        clip_id = int((record.get("metadata") or {}).get("clip_id"))
        attempts = int((record.get("metadata") or {}).get("render_attempt"))
        pid = int(record.get("pid"))
        created = float(record.get("create_time"))
    except (TypeError, ValueError):
        return False
    if clip_id <= 0 or attempts <= 0 or pid <= 0 or not math.isfinite(created):
        return False
    root = Path(jobs_dir or Path.cwd()) / job_key
    cd = root / "clips" / str(clip_id)
    manifest_path = cd / "render.json"
    manifest = read_json(manifest_path, {}) or {}
    if manifest.get("state") not in ("queued", "running") or int(manifest.get("attempts") or 0) != attempts:
        return False

    note = (
        "The Clipper process exited before this render completed. Any previous final.mp4 was left untouched; "
        "temporary output from the interrupted attempt was removed. Render this clip again to retry."
    )
    _render_state(
        cd, "failed", failed_at=time.time(), error=note,
        recovered_previous_run=True, recovery_at=time.time(),
        recovery_service_id=record.get("id"),
    )
    for pattern in (f".final-{pid}-*.rendering.mp4", f".final-{pid}-*.rendering.srt"):
        for temporary in cd.glob(pattern):
            temporary.unlink(missing_ok=True)
    try:
        services.annotate_recovery(
            str(record.get("id") or ""), pid, created, note, jobs_dir=jobs_dir,
        )
    except Exception:
        pass
    return True
