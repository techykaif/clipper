"""analyze <url>: chat -> spikes -> download only the spike parts -> hook, words, face crop."""
import os
import traceback
from pathlib import Path

import psutil

from . import ai_review, candidates, captions, chat, download, face, hotspot, library, render, services, signals, spikes, stt, subs
from .common import job_dir, read_json, set_status, write_json, fmt_t
from .platforms import detect

PLATFORM_NAME = {"twitch": "Twitch", "youtube": "YouTube", "kick": "Kick"}


class Stopped(RuntimeError):
    pass


def _service_id(jd):
    return f"analysis:{Path(jd).name}"


def _service_identity():
    process = psutil.Process(os.getpid())
    return process.pid, process.create_time()


def _service_heartbeat(jd):
    try:
        pid, created = _service_identity()
        services.heartbeat(_service_id(jd), pid, created, jobs_dir=Path(jd).parent)
    except Exception:
        pass


def analyze(url, top_n=10, cookies_browser=None, chat_file=None, skip_download=False, basis=("chat",), keywords="",
            min_score=85, antigravity_review=False):
    """basis: any of "chat", "audio", "speech" (see signals.py)."""
    platform, vid = detect(url)
    jd = job_dir(platform, vid)
    basis = [b for b in ("chat", "audio", "speech") if b in (basis or ())] or ["chat"]
    write_json(jd / "job.json", {"url": url, "platform": platform, "id": vid, "cookies_browser": cookies_browser,
                                 "basis": basis, "keywords": keywords, "min_score": min_score,
                                 "antigravity_review": bool(antigravity_review)})
    (jd / "stop").unlink(missing_ok=True)
    service_recorded = False
    try:
        pid, created = _service_identity()
        services.register(
            _service_id(jd), "analysis", jd.name, pid, created,
            state="running", message="Analyzing past stream", jobs_dir=jd.parent,
            metadata={"platform": platform, "source_id": vid},
        )
        service_recorded = True
    except Exception:
        # Analysis remains usable even if the optional health ledger is damaged.
        pass
    try:
        _run(url, platform, vid, jd, top_n, cookies_browser, chat_file, skip_download, basis, keywords, min_score,
             bool(antigravity_review))
    except Exception as e:
        set_status(jd, "error", 0, "", f"{e}")
        (jd / "error.log").write_text(traceback.format_exc())
        if service_recorded:
            try:
                pid, created = _service_identity()
                services.finish(_service_id(jd), pid, created,
                                state="stopped" if isinstance(e, Stopped) else "failed",
                                message=str(e), jobs_dir=jd.parent)
            except Exception:
                pass
        raise
    else:
        if service_recorded:
            try:
                pid, created = _service_identity()
                services.finish(_service_id(jd), pid, created, state="completed",
                                message="Analysis completed", jobs_dir=jd.parent)
            except Exception:
                pass
    return jd


def _step(jd, stage, pct, msg=""):
    """Status update + the place a Stop from the dashboard takes effect."""
    if (jd / "stop").exists():
        raise Stopped("Stopped by you.")
    _service_heartbeat(jd)
    set_status(jd, stage, pct, msg)


def _run(url, platform, vid, jd: Path, top_n, cookies_browser, chat_file, skip_download, basis, keywords, min_score,
         antigravity_review=False):
    prog = lambda pct, msg: _step(jd, "chat", pct, msg)
    msgs = []
    if (jd / "chat.json").exists() and not chat_file:
        msgs, meta = read_json(jd / "chat.json"), read_json(jd / "meta.json")
        prog(30, "Chat already fetched")
    elif "chat" in basis or chat_file:
        msgs, meta = chat.fetch(platform, url, vid, jd, prog, cookies_browser=cookies_browser, chat_file=chat_file)
        write_json(jd / "chat.json", msgs)
        write_json(jd / "meta.json", meta)
    else:
        prog(2, "Reading video info")
        meta = chat.MODS[platform].meta_of(url, cookies_browser)
        write_json(jd / "meta.json", meta)
    if not meta.get("duration"):
        raise RuntimeError("Couldn't tell how long this stream is.")

    # Published streams are never listened to or transcribed: "what's said" comes from the platform's own
    # auto-generated subtitles (YouTube); loud-voice detection is for live streams only (live.py).
    notes = []
    if "audio" in basis:
        basis = [b for b in basis if b != "audio"]
        notes.append("Loud voice works on live streams only, so it was skipped.")
    _step(jd, "captions", 31, "Getting the stream's subtitles")
    words = captions.fetch(url, jd, platform, cookies_browser)
    if "speech" in basis and not words:
        basis = [b for b in basis if b != "speech"]
        notes.append(f"{PLATFORM_NAME.get(platform, platform)} has no subtitles for this stream, so what's said was skipped.")
    if not basis:
        raise RuntimeError(" ".join(notes) + " Pick chat spikes for this stream.")
    if notes:
        j = read_json(jd / "job.json", {}) or {}
        write_json(jd / "job.json", {**j, "basis_used": basis, "notes": notes})
    db = None

    names = ", ".join(signals.NAMES[b].lower() for b in basis)
    _step(jd, "spikes", 32, f"Finding moments by {names}")
    if "chat" in basis and not msgs:
        raise RuntimeError("No chat to find spikes in. Untick chat spikes or add a chat file.")
    res = signals.find(basis, meta["duration"], msgs=msgs, db=db, words=words, keywords=keywords, top_n=top_n,
                       min_score=min_score)
    old = {c["id"]: c for c in (read_json(jd / "clips.json", {}) or {}).get("clips", [])}
    for c in res["clips"]:
        c["vod_time"] = fmt_t(c["start"])
        prev = old.get(c["id"])           # keep Claude's notes if the same moment is re-analysed
        if prev and abs(prev["chat_peak"] - c["chat_peak"]) < 1:
            c["what"], c["why"] = prev.get("what", ""), prev.get("why", "")
        else:
            c["what"], c["why"] = "", ""
    summary = {"url": url, "platform": platform, "id": vid, "title": meta.get("title"), "channel": meta.get("channel"),
               "duration": meta["duration"], "messages": len(msgs), "avg_per_min": res.get("avg_per_min", 0),
               "bin": res["bin"], "basis": basis, "keywords": keywords, "notes": notes, "min_score": min_score,
               "below": res.get("below"), "clips": res["clips"]}
    if msgs:
        write_json(jd / "rates.json", {"bin": res["bin"], "avg": res["avg"], "counts": res["counts"]})
    else:
        (jd / "rates.json").unlink(missing_ok=True)
    write_json(jd / "clips.json", summary)
    if not res["clips"]:
        b = res.get("below")
        set_status(jd, "done", 100, (f"No clip scored {min_score} or more. The best was {b['score']} at {fmt_t(b['start'])}. "
                                     f"Lower the minimum score to get it.") if b else f"No moments found by {names} in this stream.")
        return
    if skip_download:
        set_status(jd, "done", 100, "Spikes found (download skipped)")
        return

    n = len(res["clips"])

    def review_candidate(c, progress):
        if not antigravity_review:
            return
        cd = jd / "clips" / str(c["id"])
        existing = read_json(cd / "review.json", {}) or {}
        if existing:
            c["review"] = existing
        else:
            _step(jd, "review", progress, f"Antigravity reviewing clip {c['id']}/{n}")
            c["review"] = ai_review.review_clip(jd, c)
        if c["review"].get("status") == "reviewed" and c["review"].get("verdict") == "REJECT":
            library.discard_rejected_clip(jd.name, c["id"])
            summary["clips"] = [item for item in summary.get("clips", []) if int(item.get("id", -1)) != int(c["id"])]
        write_json(jd / "clips.json", summary)

    for i, c in enumerate(res["clips"]):
        base = 35 + 65 * i / n
        step = 65 / n
        cd = jd / "clips" / str(c["id"])
        cd.mkdir(parents=True, exist_ok=True)
        if (cd / "source.mp4").exists() and (read_json(cd / "clip.json") or {}).get("start") == c["start"]:
            saved_meta = read_json(cd / "clip.json", {}) or {}
            assessment = saved_meta.get("visual_quality") or candidates.inspect_video(cd / "source.mp4")
            c.update(candidates.apply_visual_quality(c, assessment))
            saved_meta.update({
                "score": c.get("score"), "signal_score": c.get("signal_score"),
                "quality_adjustment": c.get("quality_adjustment", 0),
                "quality_flags": c.get("quality_flags", []), "visual_quality": c.get("visual_quality"),
            })
            write_json(cd / "clip.json", saved_meta)
            review_candidate(c, base + step * 0.95)
            continue
        for stale in ("edit.json", "final.mp4", "final.srt", "props.json", "fx.json", "brief.md"):
            (cd / stale).unlink(missing_ok=True)
        _step(jd, "download", base, f"Clip {c['id']}/{n}: downloading {fmt_t(c['start'])}-{fmt_t(c['end'])} (best quality)")
        src, info = download.section(url, c["start"], c["end"], cd, cookies_browser)
        prepare_clip(cd, c, src, info, lambda f, m: _step(jd, "prepare", base + step * f, f"Clip {c['id']}/{n}: {m}"),
                     captions_words=words)
        review_candidate(c, base + step * 0.95)
    # prepare_clip enriches each candidate with a visual-quality adjustment; persist the final scores.
    write_json(jd / "clips.json", summary)
    rejected = n - len(summary.get("clips", [])) if antigravity_review else 0
    kept = len(summary.get("clips", []))
    set_status(jd, "done", 100, f"{kept} clips kept; {rejected} rejected and moved to Trash" if antigravity_review else f"{kept} clips ready")


def prepare_clip(cd: Path, c, src, info, progress=lambda frac, msg: None, captions_words=None, visual_quality=None):
    """After source.mp4 exists: visual quality, hot moment, word timings, face crop and edit metadata."""
    if visual_quality is None:
        progress(0.35, "checking visual quality")
        visual_quality = candidates.inspect_video(src)
    c.update(candidates.apply_visual_quality(c, visual_quality))
    progress(0.5, "finding the hot moment")
    hook = hotspot.find(src, c["start"], c.get("hot_ref", c["chat_peak"]), info["duration"])
    try:
        if captions_words:                 # the platform's own subtitles, cut to this clip
            progress(0.6, "subtitles from the stream's captions")
            write_json(cd / "words.json", captions.clip_words(captions_words, c["start"], c["start"] + info["duration"]))
        else:                              # no platform subtitles (Twitch, Kick, live): transcribe just this clip
            progress(0.6, "transcribing this clip for subtitles")
            subs.words(src, cd / "words.json")
    except Exception as e:  # subtitles are optional; the clip still works
        write_json(cd / "words.json", [])
        (cd / "subs_error.log").write_text(str(e))
    progress(0.85, "finding the face cam")
    rects = face.suggest(src, info)
    clip_meta = {"id": c["id"], "start": c["start"], "end": c["end"], "info": info, "hook": list(hook),
                 "face_found": rects["found"], "score": c.get("score"),
                 "signal_score": c.get("signal_score"), "quality_adjustment": c.get("quality_adjustment", 0),
                 "quality_flags": c.get("quality_flags", []), "visual_quality": c.get("visual_quality")}
    write_json(cd / "clip.json", clip_meta)
    if not (cd / "edit.json").exists():
        write_json(cd / "edit.json", render.default_edit(clip_meta, info, rects))
    return clip_meta
