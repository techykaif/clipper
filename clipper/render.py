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
import wave
from pathlib import Path

from .common import REMOTION, read_json, tool, write_json
from . import subs

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
    if math.isnan(v):
        raise FxError(f"{where}: {k} is not a number")
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


def render(jd: Path, n: int, progress=lambda pct, msg: None):
    cd = jd / "clips" / str(n)
    review = read_json(cd / "review.json", {}) or {}
    if review.get("status") == "reviewed" and review.get("verdict") == "REJECT":
        raise RuntimeError("Antigravity rejected this candidate; it has been removed from the library.")
    p = props_for(jd, n)
    write_json(cd / "props.json", p)
    out = cd / "final.mp4"
    cmd = [tool("npx"), "remotion", "render", "src/index.ts", "ClipShort", str(out), f"--props={cd / 'props.json'}",
           "--codec=h264", "--crf=16", "--pixel-format=yuv420p", "--color-space=bt709", "--audio-bitrate=320k",
           "--timeout=120000", "--log=info"]
    proc = subprocess.Popen(cmd, cwd=REMOTION, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=chrome_env())
    tail = []
    for line in _lines(proc.stdout):           # Remotion redraws progress with \r, not \n
        tail = (tail + [line])[-30:]
        m = re.search(r"Rendered (\d+)/(\d+)", line)
        if m:
            progress(100 * int(m.group(1)) / int(m.group(2)), "Rendering")
        elif "Encoded" in line or "Stitch" in line:
            progress(99, "Encoding")
    if proc.wait() != 0:
        raise RuntimeError("Remotion render failed:\n" + "\n".join(tail[-10:]))
    if p["subsEnabled"] and p["words"]:
        subs.srt(p["words"], timeline(p), cd / "final.srt")
    return out
