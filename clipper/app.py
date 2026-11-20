"""Clipper dashboard (Gradio): find clips, review them, set up and render the vertical Short.
Run: ./clipper.sh serve  ->  http://localhost:8765"""
import html
import io
import subprocess
import threading
import time
from pathlib import Path

import gradio as gr
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from . import ai_review, library, live, pipeline, quality_feedback, render, runtime, signals, stt
from .common import CONFIG, JOBS, REMOTION, TMP, config, read_json, write_json, fmt_t, font as font_of, tool
from .platforms import detect

FACE_C, GAME_C = (63, 160, 255), (242, 162, 58)
LOCK = threading.Lock()          # one heavy job (analysis or render) at a time
STUDIO = {"proc": None}
PV_W, PV_H = 405, 720            # preview size (9:16)


# ---------------- data helpers ----------------
def esc(x) -> str:
    return html.escape(str(x if x is not None else ""))


def job_choices():
    out = []
    for jd in sorted([p for p in JOBS.iterdir() if (p / "job.json").exists()], key=lambda p: p.stat().st_mtime, reverse=True):
        c = read_json(jd / "clips.json", {}) or {}
        k = len(c.get("clips", []))
        out.append((f"{c.get('title') or jd.name} ({k} clip{'' if k == 1 else 's'})", jd.name))
    return out


def clips_of(key):
    return (read_json(JOBS / key / "clips.json", {}) or {}).get("clips", []) if key else []


def clip_choices(key):
    return [(f"Clip {c['id']} at {c['vod_time']} (score {c['score']})", c["id"]) for c in clips_of(key)]


def clip_status(key, c):
    cd = JOBS / key / "clips" / str(c["id"])
    review = read_json(cd / "review.json", {}) or {}
    render_state = read_json(cd / "render.json", {}) or {}
    if review.get("status") == "reviewed" and review.get("verdict") == "KEEP":
        return "ready", "Approved"
    if review.get("status") == "needs_review" or review.get("verdict") == "REVIEW":
        return "ready", "Needs review"
    if render_state.get("state") == "queued":
        return "wait", "Queued for rendering"
    if render_state.get("state") == "running":
        return "wait", "Rendering"
    if render_state.get("state") == "failed" and not (cd / "final.mp4").exists():
        return "wait", "Render failed"
    if (cd / "final.mp4").exists():
        return "rendered", "Rendered"
    if (cd / "clip.json").exists():
        return "ready", "Ready to edit"
    return "wait", "Downloading"


PLATFORM = {"twitch": "Twitch", "youtube": "YouTube", "kick": "Kick"}


def status_html(key):
    if not key:
        return ('<div class="np-status np-empty"><h2>No stream yet</h2>'
                '<p>Paste a past-stream link above and press <b>Find clips</b>. Clipper reads the chat replay, '
                'finds where it spiked and downloads only those parts.</p></div>')
    st = read_json(JOBS / key / "status.json", {}) or {}
    s = read_json(JOBS / key / "clips.json", {}) or {}
    job = read_json(JOBS / key / "job.json", {}) or {}
    title = esc(s.get("title") or key)
    plat = PLATFORM.get(s.get("platform") or job.get("platform"), "")
    sub = " on ".join(x for x in (esc(s.get("channel") or ""), plat) if x)
    head = f'<div class="np-st-title"><h2>{title}</h2>' + (f"<p>{sub}</p>" if sub else "") + "</div>"
    if st.get("error"):
        return (f'<div class="np-status">{head}<div class="np-note np-err"><b>This stream stopped.</b> '
                f'{esc(st["error"])}</div></div>')
    stage = (st.get("stage") or "").lower()
    if job.get("live"):
        on = live.running(JOBS / key)
        is_paused = live.paused(JOBS / key)
        ok = [c for c in s.get("clips", [])]
        stats = [("Paused at" if is_paused else ("Watched for" if not on else "Watching for"), fmt_t(s.get("duration") or 0)),
                 ("Chat messages", f"{s.get('messages', 0):,}"),
                 ("Average chat", f"{float(s.get('avg_per_min') or 0):.0f}<small> / min</small>"),
                 ("Clips so far" if on else "Clips", str(len(ok)))]
        dl = "".join(f"<div><dt>{a}</dt><dd>{b}</dd></div>" for a, b in stats)
        badge = ('<span class="np-live off">Paused</span>' if is_paused else
                 '<span class="np-live">Live</span>' if on else '<span class="np-live off">Ended</span>')
        head = head.replace("<h2>", f"<h2>{badge}", 1)
        return (f'<div class="np-status">{head}<dl class="np-stats">{dl}</dl>'
                f'<p class="np-livemsg">{esc(st.get("msg") or "")}</p></div>')
    if stage and stage != "done":
        pct = max(0.0, min(100.0, float(st.get("pct") or 0)))
        return (f'<div class="np-status">{head}<div class="np-run"><div class="np-run-line">'
                f'<b>{esc(stage.capitalize())}</b><span>{pct:.0f}%</span></div>'
                f'<div class="np-bar"><i style="width:{pct:.1f}%"></i></div>'
                f'<p>{esc(st.get("msg") or "")}</p></div></div>')
    stats = []
    basis = s.get("basis") or ["chat"]
    stats.append(("Looked for", "<span class='np-by'>" + " + ".join(signals.NAMES[x] for x in basis) + "</span>"))
    if s.get("min_score") is not None:
        stats.append(("Minimum score", str(s["min_score"])))
    if s.get("messages"):
        stats.append(("Chat messages", f"{s['messages']:,}"))
    if s.get("avg_per_min") is not None:
        stats.append(("Average chat", f"{s['avg_per_min']}<small> / min</small>"))
    if s.get("duration"):
        stats.append(("Stream length", fmt_t(s["duration"])))
    stats.append(("Clips found", str(len(s.get("clips", [])))))
    dl = "".join(f"<div><dt>{a}</dt><dd>{b}</dd></div>" for a, b in stats)
    notes = "".join(f'<p class="np-livemsg">{esc(n)}</p>' for n in s.get("notes") or [])
    b = s.get("below")
    if b and not s.get("clips"):
        notes += (f'<div class="np-note">No clip scored {s.get("min_score")} or more. The best moment scored '
                  f'<b>{b["score"]}</b> at {fmt_t(b["start"])} (found by {" + ".join(signals.NAMES[x] for x in b["basis"])}). '
                  f'Lower the minimum score and press Find clips again to get it.</div>')
    elif b:
        notes += (f'<p class="np-livemsg">{b["count"]} weaker moment{"" if b["count"] == 1 else "s"} left out '
                  f'(below {s.get("min_score")}).</p>')
    return f'<div class="np-status">{head}<dl class="np-stats">{dl}</dl>{notes}</div>'


def _tick_step(dur):
    for step in (30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 10800):
        if dur / step <= 8:
            return step
    return 14400


def _series_path(vals, top, cols_n):
    pts = [f"{(i + 0.5) / cols_n * 1000:.1f},{100 - v / top * 100:.2f}" for i, v in enumerate(vals)]
    return pts


def _down(vals, cols):
    n = len(vals)
    per = n / cols
    return [max(vals[int(i * per):max(int((i + 1) * per), int(i * per) + 1)]) for i in range(cols)]


def chart_html(key, sel=None):
    r = read_json(JOBS / key / "rates.json", {}) if key else None
    sg = read_json(JOBS / key / "signals.json", {}) if key else None
    words = read_json(JOBS / key / "transcript.json", []) if key else []
    has_chat = bool(r and r.get("counts"))
    has_voice = bool(sg and sg.get("db"))
    if not has_chat and not has_voice:
        return ('<div class="np-chart np-chart-empty"><p>Activity shows here once a stream has been read.</p></div>')
    b = float((r or sg)["bin"])
    n = max(len(r["counts"]) if has_chat else 0, len(sg["db"]) if has_voice else 0)
    dur = n * b
    cols = min(900, n)
    layers, lines, ylabs = [], "", ""
    if has_chat:
        counts = r["counts"]
        k = max(1, round(30 / b))                  # 30 s rolling window, shown as messages per minute
        pm, run = [], 0.0
        for i, c in enumerate(counts):
            run += c
            if i >= k:
                run -= counts[i - k]
            pm.append(run / (min(i + 1, k) * b) * 60)
        pm += [0.0] * (n - len(pm))
        ys = _down(pm, cols)
        avg = float(r["avg"]) * 60 / b
        top = max(max(ys), 2 * avg) * 1.08 or 1.0
        pts = _series_path(ys, top, cols)
        layers.append(f'<path class="area" d="M0,100 L{" L".join(pts)} L1000,100 Z"/><path class="trace" d="M{" L".join(pts)}"/>')
        ya, ys2 = 100 - avg / top * 100, 100 - 2 * avg / top * 100
        lines = (f'<line class="avg" x1="0" x2="1000" y1="{ya:.2f}" y2="{ya:.2f}"/>'
                 f'<line class="spk" x1="0" x2="1000" y1="{ys2:.2f}" y2="{ys2:.2f}"/>')
        ylabs = (f'<span class="spk" style="top:{ys2:.2f}%">Spike line {2 * avg:.0f}/min</span>'
                 f'<span class="avg" style="top:{ya:.2f}%">Average {avg:.0f}/min</span>')
    if has_voice:
        _, _, above = signals.audio_excite(sg["db"], n)
        v = [max(0.0, float(x)) for x in above]
        vs = _down(v, cols)
        vtop = max(max(vs), signals.AUDIO_DB * 1.6, 1.0)
        pts = _series_path(vs, vtop, cols)
        cls = "voice" if has_chat else "voice main"
        if not has_chat:
            layers.append(f'<path class="area voice-area" d="M0,100 L{" L".join(pts)} L1000,100 Z"/>')
        layers.append(f'<path class="{cls}" d="M{" L".join(pts)}"/>')
        yl = 100 - signals.AUDIO_DB / vtop * 100
        if not has_chat:
            lines += f'<line class="spk" x1="0" x2="1000" y1="{yl:.2f}" y2="{yl:.2f}"/>'
            ylabs += f'<span class="spk" style="top:{yl:.2f}%">Loud: +{signals.AUDIO_DB:.0f} dB</span>'
    ticks_said = ""
    if words:
        _, _, said = signals.speech_excite(words, n, (read_json(JOBS / key / "job.json", {}) or {}).get("keywords", ""))
        ticks_said = "".join(f'<i style="left:{(i + 0.5) / n * 100:.2f}%" title="{esc(", ".join(x))}"></i>'
                             for i, x in enumerate(said) if x)
    bands, flags = [], []
    for c in clips_of(key):
        x0 = max(0.0, c["start"] / dur * 1000)
        w = max(4.0, (c["end"] - c["start"]) / dur * 1000)
        on = " on" if sel is not None and c["id"] == sel else ""
        bands.append(f'<rect class="band{on}" x="{x0:.1f}" y="0" width="{w:.1f}" height="100"/>')
        mid = min(99.0, max(1.0, (x0 + w / 2) / 10))
        flags.append(f'<button class="np-flag{on}" data-clip="{c["id"]}" style="left:{mid:.2f}%" '
                     f'title="Clip {c["id"]} at {esc(c["vod_time"])}">{c["id"]}</button>')
    step = _tick_step(dur)
    ticks = "".join(f'<span style="left:{t / dur * 100:.2f}%">{fmt_t(t)}</span>'
                    for t in range(0, int(dur) + 1, step) if t / dur <= 0.97)
    legend = []
    if has_chat:
        legend.append('<span><i class="k-chat"></i>Chat messages per minute</span>')
    if has_voice:
        legend.append('<span><i class="k-voice"></i>Streamer\'s voice above their usual level</span>')
    if ticks_said:
        legend.append('<span><i class="k-said"></i>Reaction phrases said</span>')
    legend.append('<span><i class="k-clip"></i>Clips (click a number to open)</span>')
    live_job = (read_json(JOBS / key / "job.json", {}) or {}).get("live")
    when = "since Clipper started watching" if live_job else "across the whole stream"
    return f"""<div class="np-chart">
  <div class="np-flags">{''.join(flags)}</div>
  <div class="np-plotwrap">
    <div class="np-plot">
      <svg viewBox="0 0 1000 100" preserveAspectRatio="none" aria-hidden="true">
        {''.join(bands)}{''.join(layers)}{lines}
      </svg>
      <div class="np-said">{ticks_said}</div>
    </div>
    <div class="np-ylab">{ylabs}</div>
  </div>
  <div class="np-ticks">{ticks}</div>
  <div class="np-legend">{''.join(legend)}<span class="when">Times {when}</span></div>
</div>"""


def list_html(key, sel=None):
    cs = clips_of(key)
    if not key:
        return '<div class="np-list np-list-empty"><p>Open a stream from the list on the left to see its clips.</p></div>'
    if not cs:
        return ('<div class="np-list np-list-empty"><p>No clips yet. They appear here, best first, as soon as the '
                'chat has been read.</p></div>')
    rows = []
    for c in cs:
        cls, label = clip_status(key, c)
        on = " on" if sel is not None and c["id"] == sel else ""
        what = (f'<p class="what">{esc(c["what"])}</p>' if c.get("what")
                else '<p class="what none">Not described yet.</p>')
        rows.append(f"""<div class="np-row{on}">
  <label class="np-tick" title="Select clip {c['id']}"><input type="checkbox" class="np-pick" data-id="{c['id']}" aria-label="Select clip {c['id']}"></label>
  <button class="np-open" data-clip="{c['id']}" aria-label="Open clip {c['id']}">
    <span class="rank">{c['id']}</span>
    <span class="score"><b>{c['score']}</b><small>score</small></span>
    <span class="body">
      <span class="facts">{_row_facts(c)}</span>
      {what}
    </span>
    <span class="pill {cls}">{label}</span>
  </button>
</div>""")
    return f"""<div class="np-list">
  <div class="np-bulk"><label class="np-tick"><input type="checkbox" class="np-all" aria-label="Select all clips"></label>
    <span class="np-count">{len(cs)} clip{'' if len(cs) == 1 else 's'}, best first</span>
    <button class="np-bulk-del" disabled>Delete selected</button></div>
  {''.join(rows)}
</div>"""


def _basis_tags(c):
    tags = '<span class="by-plus">+</span>'.join(f'<span class="by by-{b}">{signals.NAMES[b]}</span>'
                                                  for b in c.get("basis", ["chat"]))
    return f'<span class="by-line"><span class="by-label">Found by</span>{tags}</span>'


def _row_facts(c):
    out = [_basis_tags(c)]
    if c.get("multiple"):
        out.append(f"<b>{c['multiple']}× chat</b>")
    out.append(f"<span>at {esc(c['vod_time'])}</span><span>{round(c['end'] - c['start'])} s</span>")
    if c.get("unique_chatters"):
        out.append(f"<span>{c['unique_chatters']} chatter{'' if c['unique_chatters'] == 1 else 's'}</span>")
    return "".join(out)


def sidebar_html(cur=None):
    jds = sorted([p for p in JOBS.iterdir() if (p / "job.json").exists()], key=lambda p: p.stat().st_mtime, reverse=True)
    if not jds:
        return ('<div class="sb"><div class="sb-head"><h3>Streams</h3></div><p class="sb-empty">No streams yet. Paste a '
                'stream link above and press Find clips.</p></div>')
    rows = []
    for jd in jds:
        c = read_json(jd / "clips.json", {}) or {}
        st = read_json(jd / "status.json", {}) or {}
        k = len(c.get("clips", []))
        on = " on" if jd.name == cur else ""
        if live.running(jd):
            tag = ('<span class="sb-tag run">Paused</span>' if live.paused(jd)
                   else '<span class="sb-tag live">Live</span>')
        elif st.get("error"):
            tag = '<span class="sb-tag err">Stopped</span>'
        elif st.get("stage") not in (None, "", "done"):
            tag = '<span class="sb-tag run">Working</span>'
        else:
            tag = ""
        plat = PLATFORM.get(c.get("platform") or (read_json(jd / "job.json", {}) or {}).get("platform"), "")
        meta = ", ".join(x for x in (plat, f"{k} clip{'' if k == 1 else 's'}", library.human(library.size_of(jd))) if x)
        rows.append(f"""<div class="sb-row{on}">
  <label class="np-tick"><input type="checkbox" class="np-pick" data-id="{esc(jd.name)}" aria-label="Select {esc(c.get('title') or jd.name)}"></label>
  <button class="sb-open" data-key="{esc(jd.name)}"><span class="t">{tag}{esc(c.get('title') or jd.name)}</span><span class="m">{meta}</span></button>
</div>""")
    return f"""<div class="sb">
  <div class="sb-head"><h3>Streams</h3><span>{len(jds)}</span></div>
  <div class="sb-list">{''.join(rows)}</div>
  <div class="np-bulk"><label class="np-tick"><input type="checkbox" class="np-all" aria-label="Select all streams"></label>
    <span class="np-count">Tick streams to delete them</span><button class="np-bulk-del" disabled>Delete selected</button></div>
</div>"""


REACT = {"laugh": "Laughing", "hype": "Hype", "shock": "Shock", "clip": "“Clip it”", "sad": "Sad"}


def card_html(key, n, note=""):
    c = next((x for x in clips_of(key) if x["id"] == n), None)
    if not c:
        return ('<div class="np-card np-card-empty"><p>Pick a clip from the list or a number on the chart to see '
                'why it was picked.</p></div>')
    why = esc(c.get("why")) if c.get("why") else f'{esc(c["auto_reason"])} <em>Automatic selection summary.</em>'
    what = esc(c["what"]) if c.get("what") else "<em>Not described yet.</em>"
    basis = c.get("basis", ["chat"])
    sub = c.get("sub") or {"chat": c["score"]}
    subs = "".join(f'<span class="chip sc"><b>{v}</b> {signals.NAMES[k]}</span>' for k, v in sub.items())
    if c.get("multiple"):
        headline = f"<b>{c['multiple']}×</b> the stream's average chat"
    else:
        headline = "Found by " + " and ".join(signals.NAMES[b].lower() for b in basis)
    facts = [("Starts at", esc(c["vod_time"])), ("Length", f"{round(c['end'] - c['start'])} s")]
    if c.get("messages"):
        facts = [("Peak chat", f"{c['peak_per_min']:.0f}/min"), ("Stream average", f"{c['avg_per_min']:.0f}/min"),
                 ("Chatters", c["unique_chatters"]), ("Messages", c["messages"])] + facts
    dl = "".join(f"<div><dt>{a}</dt><dd>{b}</dd></div>" for a, b in facts)
    sig = "".join(f'<span class="chip">{REACT.get(k, esc(k))} <b>{v}</b></span>' for k, v in c["signals"].items() if v)
    words = "".join(f'<span class="chip">{esc(w)}</span>' for w in c["top_words"][:8])
    said = "".join(f'<span class="chip said">“{esc(w)}”</span>' for w in c.get("said", []))
    extra = ""
    if said:
        extra += f"<h4>The streamer said</h4><div class='chips'>{said}</div>"
    if sig:
        extra += f"<h4>Chat reactions</h4><div class='chips'>{sig}</div>"
    if words:
        extra += f"<h4>Chat kept saying</h4><div class='chips'>{words}</div>"
    review = read_json(JOBS / key / "clips" / str(n) / "review.json", {}) or {}
    if review.get("status") == "reviewed":
        rcls = "np-err" if review.get("verdict") == "REJECT" else "np-ok"
        extra += (f"<div class='np-note {rcls}'><b>Antigravity: {esc(review.get('verdict'))}"
                  f" ({esc(review.get('score'))}/100).</b> {esc(review.get('summary'))}</div>")
    elif review.get("status") == "needs_review":
        extra += f"<div class='np-note'>Antigravity review needs attention: {esc(review.get('summary'))}</div>"
    return f"""<div class="np-card">
  <div class="np-card-top">
    <div class="np-score"><b>{c['score']}</b><span>/100</span></div>
    <div><p class="mult">{headline}</p><p class="muted">Clip {c['id']}</p></div>
  </div>
  <div class="np-foundby">{_basis_tags(c)}
  </div>
  <h4>Score by signal</h4><div class="chips">{subs}</div>
  <dl class="np-facts">{dl}</dl>
  <h4>What happens</h4><p>{what}</p>
  <h4>Why it could go viral</h4><p>{why}</p>
  {extra}
  {f'<p class="np-note">{note}</p>' if note else ''}
</div>"""


# ---------------- frames + preview ----------------
_FRAMES = {}


def grab(src: Path, t: float) -> Image.Image:
    k = (str(src), round(t, 1))
    if k not in _FRAMES:
        raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{max(0, t):.2f}", "-i", str(src), "-frames:v", "1",
                              "-vf", "scale=1280:-2", "-f", "image2pipe", "-vcodec", "png", "-"], capture_output=True).stdout
        _FRAMES.clear() if len(_FRAMES) > 40 else None
        _FRAMES[k] = Image.open(io.BytesIO(raw)).convert("RGB") if raw else Image.new("RGB", (1280, 720))
    return _FRAMES[k]


def aspect(info, layout, split, kind):
    band_h = 1920 if layout == "noFace" else 1920 * (split if kind == "face" else 1 - split)
    return (1080 / band_h) / (info["width"] / info["height"])


def make_rect(x, y, w, a):
    """[x,y,w,h] (normalised) with h from the band aspect, kept inside the frame."""
    h = w / a
    k = max(w, h, 1.0)
    w, h = w / k, h / k
    return [round(min(max(x, 0), 1 - w), 4), round(min(max(y, 0), 1 - h), 4), round(w, 4), round(h, 4)]


def cover(frame, rect, bw, bh):
    W, H = frame.size
    rx, ry, rw, rh = rect
    cw, ch = rw * W, rh * H
    s = max(bw / cw, bh / ch)
    sw, sh = bw / s, bh / s
    sx, sy = rx * W + (cw - sw) / 2, ry * H + (ch - sh) / 2
    return frame.crop((int(sx), int(sy), int(sx + sw), int(sy + sh))).resize((bw, bh), Image.LANCZOS)


def word_at(words, t):
    return next((w["text"] for w in words if w["start"] <= t < w["end"]), None)


def preview(frame, e, words, t):
    out = Image.new("RGB", (PV_W, PV_H), "black")
    if e["layout"] == "noFace":
        out.paste(cover(frame, e["gameRect"], PV_W, PV_H), (0, 0))
    else:
        fh = round(PV_H * e["split"])
        face, game = cover(frame, e["faceRect"], PV_W, fh), cover(frame, e["gameRect"], PV_W, PV_H - fh)
        if e["layout"] == "faceTop":
            out.paste(face, (0, 0)); out.paste(game, (0, fh))
        else:
            out.paste(game, (0, 0)); out.paste(face, (0, PV_H - fh))
    w = word_at(words, t) if e["subsEnabled"] else None
    if e["subsEnabled"] and not w:
        w = "preview"                     # show the style even between words
    if w:
        txt = w.upper() if e["subUpper"] else w
        size = int(e["subSize"] * PV_W / 1080)
        font = font_of("arial-black", size)
        cx, cy = PV_W // 2, int(PV_H * e["subY"])
        shadow = Image.new("RGBA", out.size, (0, 0, 0, 0))
        d = ImageDraw.Draw(shadow)
        d.text((cx, cy + size * 0.05), txt, font=font, fill=(0, 0, 0, 230), anchor="mm")
        shadow = shadow.filter(ImageFilter.GaussianBlur(size * 0.12))
        out = Image.alpha_composite(out.convert("RGBA"), shadow)
        d = ImageDraw.Draw(out)
        for dx, dy in ((0, 0.055), (0.035, 0.035), (-0.018, 0.018)):
            d.text((cx + size * dx, cy + size * dy), txt, font=font, fill=(0, 0, 0, 255), anchor="mm")
        d.text((cx, cy), txt, font=font, fill=e["subColor"], anchor="mm")
        out = out.convert("RGB")
    return out


def boxes(frame, e):
    img = frame.copy()
    d = ImageDraw.Draw(img)
    W, H = img.size
    kinds = [("gameRect", GAME_C, "GAMEPLAY")] + ([] if e["layout"] == "noFace" else [("faceRect", FACE_C, "FACE CAM")])
    for key, col, label in kinds:
        x, y, w, h = e[key]
        d.rectangle([x * W, y * H, (x + w) * W, (y + h) * H], outline=col, width=5)
        d.rectangle([x * W, max(0, y * H - 26), x * W + 120, max(26, y * H)], fill=col)
        d.text((x * W + 6, max(0, y * H - 24)), label, fill=(14, 14, 16), font=font_of("arial-black", 16))
    return img


# ---------------- actions ----------------
def save_antigravity_review(enabled):
    """Persist the local opt-in without overwriting other installer settings."""
    settings = config()
    settings["antigravity_review"] = bool(enabled)
    write_json(CONFIG, settings)


def start_analysis(url, top_n, cookies, chat_file, hours, basis, keywords, min_score, antigravity_review=False):
    basis = [b for b in ("chat", "audio", "speech") if b in (basis or [])]
    if not basis:
        raise gr.Error("Pick at least one way to find clips: chat spikes, loud voice or what's said.")
    try:
        target = live.probe(url or "")
    except live.NotLive as e:
        raise gr.Error(str(e))
    except Exception as e:
        raise gr.Error(f"Couldn't check that link: {e}")
    if target and "speech" in basis and not stt.available():
        raise gr.Error("Live streams are transcribed on your computer, which needs the speech-to-text model. "
                       "In a terminal run: clipper stt-download")
    if not target and "audio" in basis:
        raise gr.Error("Loud voice works on live streams only. Untick it for a past stream, or use a live broadcast.")
    if target:
        jd = live.new_job(target, hours=float(hours or 6), basis=basis, keywords=keywords or "",
                          min_score=int(min_score), antigravity_review=bool(antigravity_review))
        live.start_process(jd)
        gr.Info(f"Watching {target.get('channel') or target['id']} live. Clips appear as chat spikes.")
        return jd.name
    try:
        platform, vid = detect(url or "")
    except ValueError as e:
        raise gr.Error("Couldn't read that link. Use a past stream (Twitch VOD, YouTube stream, Kick VOD) or a "
                       "stream that's live now (Twitch channel link or live YouTube link).")
    key = f"{platform}-{vid}"
    jd = JOBS / key
    jd.mkdir(parents=True, exist_ok=True)
    write_json(jd / "job.json", {"url": url, "platform": platform, "id": vid,
                                  "antigravity_review": bool(antigravity_review)})
    write_json(jd / "status.json", {"stage": "queued", "pct": 0, "msg": "Waiting to start", "error": ""})
    cf = None
    if chat_file:
        src = Path(chat_file if isinstance(chat_file, str) else chat_file.name)
        cf = jd / ("chat_upload" + src.suffix.lower())
        cf.write_bytes(src.read_bytes())

    def work():
        with LOCK:
            try:
                pipeline.analyze(url, top_n=int(top_n), cookies_browser=cookies or None, chat_file=cf, basis=basis,
                                 keywords=keywords or "", min_score=int(min_score),
                                 antigravity_review=bool(antigravity_review))
            except Exception:
                pass                       # the error is in status.json
    threading.Thread(target=work, daemon=True).start()
    return key


def _running_past(key):
    jd = JOBS / key
    st = read_json(jd / "status.json", {}) or {}
    return (not (read_json(jd / "job.json", {}) or {}).get("live") and st.get("stage") not in (None, "", "done", "error")
            and not st.get("error") and time.time() - (jd / "status.json").stat().st_mtime < 900)


def _stop_btn(key):
    if not key or not (JOBS / key).exists():
        return gr.update(visible=False)
    if live.running(JOBS / key):
        return gr.update(visible=True, value="Stop watching")
    return gr.update(visible=_running_past(key), value="Stop")


def _pause_btn(key):
    visible = bool(key and (JOBS / key).exists() and
                   (read_json(JOBS / key / "job.json", {}) or {}).get("live") and
                   live.running(JOBS / key) and not live.paused(JOBS / key))
    return gr.update(visible=visible)


def _resume_btn(key):
    if not key or not (JOBS / key).exists() or not (read_json(JOBS / key / "job.json", {}) or {}).get("live"):
        return gr.update(visible=False)
    paused = live.paused(JOBS / key)
    recovered = (read_json(JOBS / key / "live.json", {}) or {}).get("recovery_state") == "interrupted" and not live.running(JOBS / key)
    return gr.update(visible=paused or recovered, value="Resume" if paused else "Recover & resume")


def _del_btn(key):
    return gr.update(visible=bool(key) and (JOBS / key).exists())


def refresh_job(key, sel=None):
    """Everything about the open stream (timer + after changes). Keeps the open clip."""
    return (status_html(key), chart_html(key, sel), list_html(key, sel), gr.update(choices=clip_choices(key)),
            _stop_btn(key), _pause_btn(key), _resume_btn(key), _del_btn(key), sidebar_html(key))


def switch_job(key):
    """Another stream opened: fresh view, no clip open."""
    return (status_html(key), chart_html(key), list_html(key), gr.update(choices=clip_choices(key), value=None),
            _stop_btn(key), _pause_btn(key), _resume_btn(key), _del_btn(key), sidebar_html(key))


def pause_live(key):
    if not key or not (JOBS / key).exists():
        raise gr.Error("Select an active live stream first.")
    try:
        live.pause(JOBS / key)
    except Exception as exc:
        raise gr.Error(str(exc))
    return status_html(key), gr.update(visible=False), gr.update(visible=True), sidebar_html(key)


def resume_live(key):
    if not key or not (JOBS / key).exists():
        raise gr.Error("Select an interrupted or paused live stream first.")
    try:
        if live.paused(JOBS / key):
            live.resume(JOBS / key)
        else:
            runtime.snapshot()
            live.start_process(JOBS / key, resume=True)
            gr.Info("Recovering the interrupted session. Only candidate intervals with complete saved footage will be re-extracted.")
    except Exception as exc:
        raise gr.Error(str(exc))
    return status_html(key), _pause_btn(key), _resume_btn(key), sidebar_html(key)


def stop_live(key):
    if key and live.running(JOBS / key):
        try:
            live.stop(JOBS / key)
        except Exception as exc:
            raise gr.Error(f"Could not stop the live worker cleanly: {exc}")
        gr.Info("Stopping. Clips already cut stay.")
    elif key:
        (JOBS / key / "stop").touch()        # pipeline._step picks this up at its next step
        gr.Info("Stopping after the current step.")
    return gr.update(visible=False), gr.update(visible=False), gr.update(visible=False), status_html(key)


_SETTLED = {}


def tick(key, sel):
    """Timer: refresh while a job runs; stay quiet once it's finished."""
    st = read_json(JOBS / key / "status.json", {}) if key else {}
    sig = ((st or {}).get("stage"), (st or {}).get("pct"), (st or {}).get("msg"),
           (JOBS / key / "clips.json").stat().st_mtime if key and (JOBS / key / "clips.json").exists() else 0)
    if not key or _SETTLED.get(key) == sig:
        return (gr.skip(),) * 9
    if sig[0] in ("done", "error"):
        _SETTLED[key] = sig
    return refresh_job(key, sel)


def first_load(key):
    _SETTLED.pop(key, None)
    return switch_job(key)


def _evt(evt):
    d = dict(getattr(evt, "_data", None) or {})
    for k in ("action", "id", "ids", "key"):
        if k not in d and hasattr(evt, k):
            d[k] = getattr(evt, k)
    return d


def _ask(kind, ids, key=None):
    """Fill the confirm dialog for deleting streams or clips."""
    if kind == "streams":
        size = sum(library.size_of(JOBS / k) for k in ids)
        names = [(read_json(JOBS / k / "clips.json", {}) or {}).get("title") or k for k in ids]
        head = f"Delete {len(ids)} stream{'' if len(ids) == 1 else 's'}?"
        body = ("Each goes with all its clips and Shorts. " if len(ids) > 1 else
                f"“{esc(names[0])}” goes with all its clips and Shorts. ")
    else:
        size = sum(library.size_of(JOBS / key / "clips" / str(i)) for i in ids)
        head = f"Delete {len(ids)} clip{'' if len(ids) == 1 else 's'}?"
        body = f"Clip{'' if len(ids) == 1 else 's'} {', '.join(str(i) for i in sorted(ids))}, with its Short and edits. "
    where = (("It goes" if len(ids) == 1 else "They go") + " to the Trash, so you can still get "
             + ("it" if len(ids) == 1 else "them") + " back.") if library.can_trash() else "This can't be undone."
    return (kind, list(ids), gr.update(visible=True),
            f'<h3>{head}</h3><p>{body}{library.human(size)} in all. {where}</p>')


def side_click(evt: gr.EventData):
    d = _evt(evt)
    if d.get("action") == "delete" and d.get("ids"):
        return (gr.skip(),) + _ask("streams", d["ids"])
    if d.get("action") == "open" and d.get("id"):
        return (d["id"],) + (gr.skip(),) * 4
    return (gr.skip(),) * 5


def list_click(key, evt: gr.EventData):
    d = _evt(evt)
    if d.get("action") == "delete" and d.get("ids") and key:
        return (gr.skip(),) + _ask("clips", [int(i) for i in d["ids"]], key)
    if d.get("action") == "open" and d.get("id") is not None:
        return (int(d["id"]),) + (gr.skip(),) * 4
    return (gr.skip(),) * 5


def ask_stream(key):
    if not key:
        raise gr.Error("Open a stream first.")
    return _ask("streams", [key])


def ask_clip(key, n):
    if not key or n is None:
        raise gr.Error("Open a clip first.")
    return _ask("clips", [int(n)], key)


def do_delete(kind, ids, key, n):
    """Confirmed: delete, then refresh everything that showed the deleted items."""
    hide = gr.update(visible=False)
    if kind == "streams":
        done, skipped = library.delete_streams(ids or [])
        left = [k for _, k in job_choices()]
        new = key if key in left else (left[0] if left else "")
        msgs = [f"Deleted {len(done)} stream{'' if len(done) == 1 else 's'}."] if done else []
    else:
        done, skipped = library.delete_clips(key, ids or [])
        new = key
        msgs = [f"Deleted {len(done)} clip{'' if len(done) == 1 else 's'}."] if done else []
    msgs += [f"Skipped {k}: {why}." for k, why in skipped]
    if done:
        gr.Info(" ".join(msgs))
    elif msgs:
        gr.Warning(" ".join(msgs))
    keep = n if kind == "clips" and n not in (done or []) else (n if new == key else None)
    return (hide, new, gr.update(choices=clip_choices(new), value=keep if new == key else None),
            *refresh_job(new, keep if new == key else None)[:3], sidebar_html(new), _del_btn(new))


def highlight(key, n):
    return chart_html(key, n), list_html(key, n)


def load_clip(key, n):
    """Fill the card + editor for one clip."""
    empty = [gr.update()] * 19 + [gr.update(value=None, visible=False)] * 2   # 19 editor fields + final video + files
    if not key or n is None:
        return [card_html(key, None), gr.update(value=None, visible=False)] + empty
    cd = JOBS / key / "clips" / str(n)
    if not (cd / "clip.json").exists():
        return [card_html(key, n, "This clip is still downloading. The editor opens when it is ready."),
                gr.update(value=None, visible=False)] + empty
    meta, e = read_json(cd / "clip.json"), read_json(cd / "edit.json")
    dur = meta["info"]["duration"]
    final = str(cd / "final.mp4") if (cd / "final.mp4").exists() else None
    files = [str(p) for p in (cd / "final.mp4", cd / "final.srt") if p.exists()] or None
    return [card_html(key, n), gr.update(value=str(cd / "source.mp4"), visible=True),
            gr.update(maximum=round(dur, 1), value=round(min(e["hookStart"] + 1, dur - 0.1), 1)),
            e["layout"], e["split"], e["faceRect"][0], e["faceRect"][1], e["faceRect"][2],
            e["gameRect"][0], e["gameRect"][1], e["gameRect"][2],
            e["hookEnabled"], e["hookStart"], e["hookEnd"], e["trimStart"], e["trimEnd"],
            e["subsEnabled"], e["subColor"], e["subSize"], e["subY"], e["subUpper"],
            gr.update(value=final, visible=bool(final)), gr.update(value=files, visible=bool(files))]


def apply_edit(key, n, t, layout, split, fx, fy, fw, gx, gy, gw, hook_on, hs, he, ts, te, subs_on, color, size, sy, upper):
    """Save settings, return the crop view and the 9:16 preview."""
    if not key or n is None:
        return None, None
    cd = JOBS / key / "clips" / str(n)
    if not (cd / "clip.json").exists():
        return None, None
    meta, e = read_json(cd / "clip.json"), read_json(cd / "edit.json")
    info, dur = meta["info"], meta["info"]["duration"]
    e.update({"layout": layout, "split": float(split),
              "faceRect": make_rect(fx, fy, fw, aspect(info, layout, split, "face")),
              "gameRect": make_rect(gx, gy, gw, aspect(info, layout, split, "game")),
              "hookEnabled": bool(hook_on), "hookStart": max(0.0, min(float(hs), dur)), "hookEnd": max(0.0, min(float(he), dur)),
              "trimStart": max(0.0, min(float(ts), dur)), "trimEnd": max(0.0, min(float(te), dur)),
              "subsEnabled": bool(subs_on), "subColor": _hex(color), "subSize": int(size), "subY": float(sy), "subUpper": bool(upper)})
    write_json(cd / "edit.json", e)
    frame = grab(cd / "source.mp4", float(t))
    return boxes(frame, e), preview(frame, e, read_json(cd / "words.json", []), float(t))


def _hex(c):
    c = (c or "#FFFFFF").strip()
    if c.startswith("rgb"):
        nums = [int(float(v)) for v in c[c.index("(") + 1:c.index(")")].split(",")[:3]]
        return "#%02X%02X%02X" % tuple(nums)
    return c.upper()


def snap_seam(layout, split):
    return round(1 - split, 2) if layout == "faceBottom" else round(split, 2) if layout == "faceTop" else 0.75


def do_render(key, n):
    if not key or n is None:
        raise gr.Error("Pick a clip first.")
    jd, cd = JOBS / key, JOBS / key / "clips" / str(n)
    review = read_json(cd / "review.json", {}) or {}
    if review.get("status") == "reviewed" and review.get("verdict") == "REJECT":
        yield ('<div class="np-note np-err"><b>Not promoted.</b> Antigravity rejected this candidate. '
               'The original clip is preserved in the library; review it again or remove its review record '
               'to override this decision.</div>'), gr.update(), gr.update()
        return
    state = {"pct": 0, "msg": "Waiting for the renderer", "err": None, "done": False}

    def work():
        with LOCK:
            try:
                render.render(jd, n, lambda p, m: state.update(pct=p, msg=m))
            except Exception as ex:
                state["err"] = str(ex)[-1500:]
            state["done"] = True
    threading.Thread(target=work, daemon=True).start()
    while not state["done"]:
        yield progress_html("Rendering", state["pct"], state["msg"]), gr.update(), gr.update()
        time.sleep(1)
    if state["err"]:
        yield (f'<div class="np-note np-err"><b>The render stopped.</b> The renderer said:<pre>{esc(state["err"])}</pre>'
               f'</div>'), gr.update(), gr.update()
        return
    files = [str(p) for p in (cd / "final.mp4", cd / "final.srt") if p.exists()]
    yield '<div class="np-note np-ok"><b>Rendered.</b> The Short and its subtitles are ready to download below.</div>', \
        gr.update(value=str(cd / "final.mp4"), visible=True), gr.update(value=files, visible=True)


def progress_html(label, pct, msg=""):
    pct = max(0.0, min(100.0, float(pct or 0)))
    return (f'<div class="np-run"><div class="np-run-line"><b>{esc(label)}</b><span>{pct:.0f}%</span></div>'
            f'<div class="np-bar"><i style="width:{pct:.1f}%"></i></div><p>{esc(msg)}</p></div>')


def open_studio(key, n):
    if not key or n is None:
        raise gr.Error("Pick a clip first.")
    cd = JOBS / key / "clips" / str(n)
    write_json(cd / "props.json", render.props_for(JOBS / key, n))
    if STUDIO["proc"] and STUDIO["proc"].poll() is None:
        STUDIO["proc"].terminate()
    STUDIO["proc"] = subprocess.Popen([tool("npx"), "remotion", "studio", "src/index.ts", f"--props={cd / 'props.json'}",
                                       "--port=3000"], cwd=REMOTION, env=render.chrome_env(), stdout=open(TMP / "studio.log", "w"),
                                      stderr=subprocess.STDOUT)
    return ('<div class="np-note">Remotion Studio is starting. It opens at '
            '<a href="http://localhost:3000/ClipShort" target="_blank">localhost:3000/ClipShort</a> in a few seconds.</div>')


# ---------------- Antigravity candidate review ----------------
def feedback_button_state(key, n):
    visible = bool(key and n is not None and any(int(c.get("id", -1)) == int(n) for c in clips_of(key)))
    return (gr.update(visible=visible), gr.update(visible=visible), gr.update(visible=visible))


def save_clip_feedback(key, n, label):
    if not key or n is None:
        raise gr.Error("Open a clip before recording quality feedback.")
    candidate = next((x for x in clips_of(key) if int(x.get("id", -1)) == int(n)), None)
    if not candidate:
        raise gr.Error("That clip is no longer in the active library.")
    try:
        quality_feedback.record(key, candidate, label)
    except Exception as exc:
        raise gr.Error(f"Could not save quality feedback: {exc}")
    return f"<div class='np-note'>Saved: <b>{esc(label.title())}</b>. {esc(quality_feedback.summary_text())}</div>"


def review_button_state(key, n):
    visible = bool(key and n is not None and (JOBS / key / "clips" / str(n) / "source.mp4").is_file())
    return gr.update(visible=visible, value="Review with Antigravity", interactive=True)


def review_start_state():
    return (
        gr.update(value="Reviewing with Antigravity…", interactive=False),
        "<div class='np-note'>Antigravity is checking the clip's sampled frames, transcript and chat. "
        "This can take a little while; please keep this dashboard open.</div>",
        gr.update(interactive=False),
    )


def review_with_antigravity(key, n):
    if not key or n is None:
        return "<div class='np-note np-err'>Open an extracted clip first.</div>", card_html(key, None), list_html(key, None), gr.update(interactive=True)
    try:
        candidate = next((x for x in clips_of(key) if x["id"] == int(n)), None)
        if not candidate:
            raise FileNotFoundError("That clip is no longer in the candidate list.")
        result = ai_review.review_clip(JOBS / key, candidate)
        verdict = result.get("verdict", "REVIEW")
        if result.get("status") == "reviewed" and verdict == "REJECT":
            library.discard_rejected_clip(key, int(n))
            return (
                f"<div class='np-note np-err'><b>Rejected and removed.</b> Antigravity scored this clip "
                f"{esc(result.get('score'))}/100. The clip folder was moved to the system Trash / Recycle Bin "
                "where available.</div>",
                card_html(key, None), list_html(key, None),
                gr.update(choices=clip_choices(key), value=None, interactive=True),
            )
        return "", card_html(key, int(n)), list_html(key, int(n)), gr.update(interactive=True)
    except Exception as exc:
        return (
            f"<div class='np-note np-err'><b>Review could not finish.</b> {esc(str(exc))} "
            "The candidate has not been deleted.</div>",
            card_html(key, int(n)), list_html(key, int(n)), gr.update(interactive=True),
        )


# ---------------- UI ----------------
# Look: cool light grey ground, white sheets, ink text, one "heat" colour for spikes, scores and deleting.
# Instrument Sans (SIL OFL, bundled in clipper/assets) throughout, condensed for big numbers. Nothing moves.
import base64 as _b64
_FONT = Path(__file__).with_name("assets") / "InstrumentSans.ttf"
FONT_FACE = (f"@font-face {{ font-family: 'Instrument Sans'; font-style: normal; font-weight: 400 700; "
             f"font-stretch: 75% 100%; font-display: block; "
             f"src: url(data:font/ttf;base64,{_b64.b64encode(_FONT.read_bytes()).decode()}) format('truetype'); }}"
             if _FONT.exists() else "")
HEAD = '<meta name="color-scheme" content="light">'
FORCE_LIGHT = "() => { document.body.classList.remove('dark'); document.documentElement.classList.remove('dark'); }"

INK, MUTED, RULE, SHEET, SUNK, GROUND, HEAT = "#181B21", "#646C79", "#D9DDE3", "#FFFFFF", "#F5F6F8", "#ECEEF1", "#E5432E"

THEME = gr.themes.Base(
    primary_hue=gr.themes.colors.slate, neutral_hue=gr.themes.colors.slate, radius_size=gr.themes.sizes.radius_sm,
    spacing_size=gr.themes.sizes.spacing_md, text_size=gr.themes.sizes.text_md,
    font=[gr.themes.Font(f) for f in ("Instrument Sans", "ui-sans-serif", "system-ui", "sans-serif")],
    font_mono=[gr.themes.Font(f) for f in ("ui-monospace", "SFMono-Regular", "Menlo", "monospace")],
).set(
    body_background_fill=GROUND, body_text_color=INK, body_text_color_subdued=MUTED,
    background_fill_primary=SHEET, background_fill_secondary=SUNK,
    border_color_primary=RULE, border_color_accent=INK, color_accent=INK, color_accent_soft=SUNK,
    block_background_fill=SHEET, block_border_color=RULE, block_border_width="1px", block_shadow="none",
    block_radius="10px", block_padding="14px", block_label_background_fill="transparent",
    block_label_text_color=MUTED, block_label_text_size="13px", block_label_text_weight="500",
    block_label_border_width="0px", block_label_padding="0", block_label_margin="0",
    block_title_text_color=INK, block_title_text_size="13px", block_title_text_weight="600",
    block_title_background_fill="transparent", block_title_padding="0",
    panel_background_fill=SHEET, panel_border_color=RULE, panel_border_width="1px",
    input_background_fill=SUNK, input_background_fill_focus=SHEET, input_border_color=RULE,
    input_border_color_focus=INK, input_border_width="1px", input_shadow="none", input_shadow_focus="none",
    input_radius="8px", input_padding="10px 12px", input_text_size="15px", input_placeholder_color="#9AA1AD",
    button_primary_background_fill=INK, button_primary_background_fill_hover="#2C313B",
    button_primary_text_color="#FFFFFF", button_primary_border_color=INK, button_primary_border_color_hover="#2C313B",
    button_secondary_background_fill=SHEET, button_secondary_background_fill_hover=SUNK,
    button_secondary_text_color=INK, button_secondary_border_color=RULE, button_secondary_border_color_hover="#B9BFC9",
    button_border_width="1px", button_large_radius="8px", button_medium_radius="8px", button_small_radius="6px",
    button_large_text_size="15px", button_large_text_weight="600", button_medium_text_weight="600",
    button_small_text_weight="500", button_primary_shadow="none", button_primary_shadow_hover="none",
    button_primary_shadow_active="none", button_secondary_shadow="none", button_secondary_shadow_hover="none",
    button_secondary_shadow_active="none",
    button_transition="none",
    slider_color=INK, checkbox_background_color=SHEET, checkbox_background_color_selected=INK,
    checkbox_border_color=RULE, checkbox_border_color_selected=INK, checkbox_border_color_focus=INK,
    checkbox_label_background_fill=SHEET, checkbox_label_background_fill_selected=SUNK,
    checkbox_label_border_color=RULE, checkbox_label_border_color_selected=INK, checkbox_label_text_color_selected=INK,
    checkbox_label_shadow="none", checkbox_shadow="none",
    table_border_color=RULE, table_even_background_fill=SHEET, table_odd_background_fill=SUNK,
    link_text_color=INK, link_text_color_hover=INK, link_text_color_visited=INK,
    shadow_drop="none", shadow_drop_lg="0 6px 24px rgba(24,27,33,.10)", shadow_spread="0px",
    layout_gap="14px", form_gap_width="0px",
    loader_color=MUTED, stat_background_fill=SUNK,
)


CSS = FONT_FACE + """
/* nothing moves */
*, *::before, *::after { animation: none !important; transition: none !important; scroll-behavior: auto !important; }
.eta-bar { display: none !important; }

:root { --ink:#181B21; --muted:#646C79; --faint:#8D94A0; --rule:#D9DDE3; --sheet:#FFFFFF; --sunk:#F5F6F8; --ground:#ECEEF1;
        --heat:#E5432E; --heat-ink:#B5321F; --heat-soft:#FCE6E2; --ok:#1E7A4A; --ok-soft:#E3F3EA; --wait:#8A6100; --wait-soft:#FBF1D9;
        --r-lg:12px; --r-md:8px; --r-sm:6px; }
body, .gradio-container { background: var(--ground) !important; font-feature-settings: "tnum" 1; }
.gradio-container { max-width: 1520px !important; margin: 0 auto !important; padding: 20px 24px 48px !important; }
footer { display: none !important; }
:focus-visible { outline: 2px solid var(--ink) !important; outline-offset: 2px; }
.html-container { padding: 0 !important; }
.np-hide { display: none !important; }
button, [role="button"], .np-open, .sb-open, label.np-tick { cursor: pointer; }

/* ---------- shell: sidebar + main ---------- */
#np-shell { gap: 20px; align-items: flex-start; flex-wrap: nowrap; }
#np-side { position: sticky; top: 16px; max-height: calc(100vh - 32px); overflow-y: auto; gap: 10px; flex-wrap: nowrap !important;
  background: var(--sheet); border: 1px solid var(--rule); border-radius: var(--r-lg); padding: 18px 16px 16px !important; }
#np-main { gap: 16px; min-width: 0; }
@media (max-width: 980px) {
  #np-shell { flex-wrap: wrap; }
  #np-side { position: static; max-height: none; width: 100% !important; flex: 1 1 100% !important; }
}

/* brand + find */
#np-brand h1 { font-size: 34px; line-height: 1; font-weight: 700; font-stretch: 75%; letter-spacing: -0.01em; margin: 0 0 6px; color: var(--ink); }
#np-brand p { margin: 0 0 4px; color: var(--muted); font-size: 14px; line-height: 1.45; }
#np-url input, #np-url textarea { font-size: 15px; height: 46px; background: var(--sunk) !important; border-color: #C9CED6 !important; }
#np-url input:focus { border-color: var(--ink) !important; background: var(--sheet) !important; }
#np-go { min-height: 46px; }
#np-opts { background: transparent !important; border: 0 !important; padding: 0 !important; }
#np-opts > button, #np-opts .label-wrap { padding: 2px 0 !important; justify-content: flex-start !important; gap: 6px; width: auto !important; }
#np-opts .label-wrap span, #np-opts > button span { font-size: 13.5px; color: var(--muted); font-weight: 500; }
#np-opts .icon { order: -1; margin: 0 !important; }

/* sidebar stream list */
.sb { border-top: 1px solid var(--rule); margin-top: 6px; padding-top: 14px; }
.sb-head { display: flex; align-items: baseline; justify-content: space-between; margin-bottom: 8px; }
.sb-head h3 { margin: 0; font-size: 15px; font-weight: 650; color: var(--ink); }
.sb-head span { font-size: 13px; color: var(--muted); }
.sb-empty { margin: 0; font-size: 14px; color: var(--muted); line-height: 1.5; }
.sb-list { display: flex; flex-direction: column; gap: 2px; }
.sb-row { display: flex; align-items: flex-start; gap: 4px; border-radius: var(--r-md); border-left: 3px solid transparent; padding: 2px 0; }
.sb-row:hover { background: var(--sunk); }
.sb-row.on { background: var(--sunk); border-left-color: var(--heat); }
.sb-row.picked { background: var(--heat-soft); }
.sb-open { all: unset; box-sizing: border-box; flex: 1; min-width: 0; padding: 8px 8px 8px 2px; display: flex; flex-direction: column; gap: 3px; }
.sb-open .t { font-size: 14px; font-weight: 600; color: var(--ink); line-height: 1.3; overflow: hidden; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow-wrap: anywhere; }
.sb-open .m { font-size: 12.5px; color: var(--muted); }
.sb-open:focus-visible { outline: 2px solid var(--ink); outline-offset: -2px; border-radius: var(--r-sm); }
.sb-tag { display: inline-block; font-size: 11.5px; font-weight: 650; padding: 1px 6px; border-radius: 4px; margin-right: 6px; vertical-align: 1px; }
.sb-tag.live { background: var(--heat); color: #fff; }
.sb-tag.err { background: var(--heat-soft); color: var(--heat-ink); }
.sb-tag.run { background: var(--wait-soft); color: var(--wait); }

/* checkboxes + bulk bar (sidebar and clip list) */
label.np-tick { display: grid; place-items: center; width: 36px; min-height: 36px; flex: none; }
.np-tick input { width: 17px; height: 17px; accent-color: var(--heat); margin: 0; }
.np-bulk { display: flex; align-items: center; gap: 8px; padding: 8px 10px 8px 0; }
.sb .np-bulk { border-top: 1px solid var(--rule); margin-top: 8px; padding-top: 10px; }
.np-count { flex: 1; font-size: 13.5px; color: var(--muted); }
.np-bulk-del { all: unset; font: 600 13.5px "Instrument Sans", sans-serif; color: #fff; background: var(--heat); padding: 7px 12px; border-radius: var(--r-sm); }
.np-bulk-del:disabled { background: var(--sunk); color: var(--faint); border: 1px solid var(--rule); padding: 6px 11px; cursor: default; }
.np-bulk-del:not(:disabled):hover { background: #C93422; }
.np-bulk-del:focus-visible { outline: 2px solid var(--ink); outline-offset: 2px; }

/* ---------- main: header ---------- */
#np-head { align-items: flex-start; gap: 16px; }
#np-actions { gap: 8px; align-items: stretch; }
.np-status { padding: 4px 2px 0; }
.np-status h2 { font-size: 28px; line-height: 1.15; font-weight: 700; font-stretch: 85%; margin: 0; color: var(--ink); overflow-wrap: anywhere; }
.np-st-title p { margin: 4px 0 0; color: var(--muted); font-size: 14px; }
.np-empty h2 { margin-bottom: 6px; }
.np-empty p { color: var(--muted); max-width: 62ch; margin: 0; font-size: 15px; line-height: 1.55; }
.np-stats { display: flex; flex-wrap: wrap; gap: 12px 0; margin: 16px 0 0; padding: 0; }
.np-stats > div { padding: 0 24px 0 0; margin-right: 24px; border-right: 1px solid var(--rule); }
.np-stats > div:last-child { border-right: 0; }
.np-stats dt { font-size: 13px; color: var(--muted); margin: 0 0 2px; }
.np-stats dd { margin: 0; font-size: 24px; font-weight: 650; font-stretch: 80%; color: var(--ink); }
.np-stats dd small { font-size: 14px; font-weight: 500; color: var(--muted); font-stretch: 100%; }
.np-live { display: inline-block; vertical-align: 5px; margin-right: 10px; font: 600 13px/1 "Instrument Sans", sans-serif; font-stretch: 100%; color: #fff; background: var(--heat); padding: 5px 8px 5px 20px; border-radius: 5px; position: relative; }
.np-live::before { content: ""; position: absolute; left: 8px; top: 50%; width: 6px; height: 6px; margin-top: -3px; border-radius: 50%; background: #fff; }
.np-live.off { background: var(--sunk); color: var(--muted); border: 1px solid var(--rule); }
.np-live.off::before { background: var(--muted); }
.np-livemsg { margin: 12px 0 0; font-size: 14px; color: var(--muted); }

/* progress (static) */
.np-run { margin-top: 16px; max-width: 560px; }
.np-run-line { display: flex; justify-content: space-between; font-size: 14px; margin-bottom: 6px; }
.np-run-line span { color: var(--muted); }
.np-bar { height: 6px; background: var(--sunk); border: 1px solid var(--rule); border-radius: 99px; overflow: hidden; }
.np-bar i { display: block; height: 100%; background: var(--ink); }
.np-run p { margin: 8px 0 0; color: var(--muted); font-size: 14px; }

/* notes */
.np-note { margin: 14px 0 0; padding: 10px 14px; border-radius: var(--r-md); background: var(--sunk); border: 1px solid var(--rule); font-size: 14px; line-height: 1.5; color: var(--ink); }
.np-note.np-err { background: var(--heat-soft); border-color: #F2B8AE; }
.np-note.np-ok { background: var(--ok-soft); border-color: #B5DEC6; }
.np-note pre { white-space: pre-wrap; font-size: 12px; margin: 8px 0 0; max-height: 220px; overflow: auto; }
.np-note a { color: var(--ink); text-decoration: underline; text-underline-offset: 2px; }

/* buttons */
.np-danger { background: var(--heat) !important; border-color: var(--heat) !important; color: #fff !important; }
.np-danger:hover { background: #C93422 !important; border-color: #C93422 !important; }
.np-danger-soft { color: var(--heat-ink) !important; }
.np-danger-soft:hover { background: var(--heat-soft) !important; border-color: #F2B8AE !important; }

/* ---------- chart ---------- */
#np-chartbox { padding: 18px 20px 14px !important; border-radius: var(--r-lg) !important; }
.np-chart { position: relative; }
.np-chart-empty { height: 160px; display: grid; place-items: center; background: var(--sunk); border-radius: var(--r-md); }
.np-chart-empty p { color: var(--muted); margin: 0; font-size: 14px; }
.np-flags { position: relative; height: 30px; margin-right: 128px; }
.np-flag { position: absolute; top: 0; transform: translateX(-50%); min-width: 26px; height: 26px; padding: 0 7px; border-radius: 6px 6px 6px 0;
  background: var(--sheet); color: var(--heat); border: 1.5px solid var(--heat); font: 700 14px/22px "Instrument Sans", sans-serif; font-stretch: 80%; }
.np-flag:hover, .np-flag.on { background: var(--heat); color: #fff; }
.np-plotwrap { display: flex; }
.np-plot { position: relative; flex: 1; height: 190px; border-bottom: 1px solid var(--ink); }
.np-plot svg { position: absolute; inset: 0; width: 100%; height: 100%; overflow: visible; }
.np-plot .area { fill: #E3E6EB; }
.np-plot .trace { fill: none; stroke: var(--ink); stroke-width: 1.25; vector-effect: non-scaling-stroke; stroke-linejoin: round; }
.np-plot .band { fill: var(--heat); fill-opacity: .14; }
.np-plot .band.on { fill-opacity: .30; }
.np-plot line.avg { stroke: var(--muted); stroke-width: 1; stroke-dasharray: 2 3; vector-effect: non-scaling-stroke; }
.np-plot line.spk { stroke: var(--heat); stroke-width: 1.25; stroke-dasharray: 6 4; vector-effect: non-scaling-stroke; }
.np-ylab { position: relative; width: 128px; flex: none; }
.np-ylab span { position: absolute; left: 10px; transform: translateY(-50%); font-size: 12.5px; white-space: nowrap; }
.np-ylab .spk { color: var(--heat); font-weight: 600; }
.np-ylab .avg { color: var(--muted); }
.np-ticks { position: relative; height: 22px; margin-right: 128px; }
.np-ticks span { position: absolute; top: 6px; font-size: 12.5px; color: var(--muted); transform: translateX(-50%); }
.np-ticks span:first-child { transform: none; }
.np-plot .voice { fill: none; stroke: #3B6FD8; stroke-width: 1.25; vector-effect: non-scaling-stroke; stroke-linejoin: round; opacity: .85; }
.np-plot .voice.main { stroke: var(--ink); opacity: 1; }
.np-plot .voice-area { fill: #E3E6EB; }
.np-said { position: absolute; left: 0; right: 0; bottom: -7px; height: 6px; }
.np-said i { position: absolute; width: 2px; height: 6px; background: #6A3FB5; transform: translateX(-1px); }
.np-legend { display: flex; flex-wrap: wrap; gap: 6px 18px; margin: 10px 0 0; font-size: 13px; color: var(--muted); }
.np-legend i { display: inline-block; width: 14px; height: 3px; margin-right: 6px; vertical-align: 3px; border-radius: 2px; }
.np-legend .k-chat { background: var(--ink); }
.np-legend .k-voice { background: #3B6FD8; }
.np-legend .k-said { background: #6A3FB5; width: 3px; height: 10px; vertical-align: -1px; }
.np-legend .k-clip { background: var(--heat); opacity: .5; height: 10px; vertical-align: -1px; }
.np-legend .when { margin-left: auto; }

/* clip selector row */
#np-cliprow { align-items: flex-end; gap: 12px; background: transparent !important; border: 0 !important; max-width: 760px; }
#np-cliprow > .form, #np-cliprow .form { background: transparent !important; border: 0 !important; box-shadow: none !important; }
#np-clipsel { max-width: 560px; border: 1px solid var(--rule) !important; border-radius: var(--r-lg) !important; }
#np-delclip { align-self: center; }

/* basis picker */
#np-basis .wrap { display: flex; flex-direction: column; gap: 6px; }
#np-basis label { width: 100%; }
#np-basis .info, #np-basis [data-testid="block-info"] + span { font-size: 12.5px; }
.np-by { font-size: 18px; font-stretch: 100%; font-weight: 600; }
.by { display: inline-block; font-size: 12px; font-weight: 600; padding: 1px 7px; border-radius: 4px; margin-right: 4px; }
.by-chat { background: var(--sunk); color: var(--ink); border: 1px solid var(--rule); }
.by-audio { background: #E6EEFB; color: #2D5BB9; }
.by-speech { background: #EFE8FB; color: #6A3FB5; }
.chip.sc b { color: var(--heat); margin: 0 2px 0 0; }
.by-line { display: inline-flex; align-items: center; flex-wrap: wrap; gap: 4px; margin-right: 8px; }
.by-line .by { margin-right: 0; }
.by-label { font-size: 12px; color: var(--muted); margin-right: 2px; }
.by-plus { font-size: 12px; color: var(--muted); }
.np-open .facts .by-line { flex-basis: 100%; margin-bottom: 2px; }
.np-foundby { padding: 12px 0 0; }
.chip.said { background: #EFE8FB; border-color: #DCCDF5; }

/* ---------- tabs ---------- */
.tab-wrapper, [role="tablist"] { border-bottom: 1px solid var(--rule) !important; }
button[role="tab"] { font-size: 15px !important; font-weight: 600 !important; color: var(--muted) !important; padding: 10px 2px !important; margin-right: 22px !important; background: transparent !important; border: 0 !important; }
button[role="tab"].selected, button[role="tab"][aria-selected="true"] { color: var(--ink) !important; }
button[role="tab"].selected::after, button[role="tab"][aria-selected="true"]::after { background: var(--ink) !important; height: 2px !important; }
.tabitem { padding: 16px 0 0 !important; border: 0 !important; background: transparent !important; }

/* clip list */
.np-list { display: flex; flex-direction: column; background: var(--sheet); border: 1px solid var(--rule); border-radius: var(--r-lg); overflow: hidden; }
.np-list .np-bulk { padding-left: 6px; border-bottom: 1px solid var(--rule); background: var(--sunk); }
.np-list-empty { padding: 26px 20px; }
.np-list-empty p { margin: 0; font-size: 15px; color: var(--muted); }
.np-row { display: flex; align-items: flex-start; padding-left: 6px; border-left: 3px solid transparent; border-bottom: 1px solid var(--rule); }
.np-row:last-child { border-bottom: 0; }
.np-row:hover { background: var(--sunk); }
.np-row.on { background: var(--sunk); border-left-color: var(--heat); }
.np-row.picked { background: var(--heat-soft); }
.np-row label.np-tick { margin-top: 10px; }
.np-open { all: unset; box-sizing: border-box; flex: 1; min-width: 0; display: grid; grid-template-columns: 30px 54px minmax(0,1fr) auto; gap: 12px; align-items: start; padding: 16px 16px 16px 4px; }
.np-open:focus-visible { outline: 2px solid var(--ink); outline-offset: -2px; }
.np-open .rank { font-size: 26px; line-height: 1; font-weight: 700; font-stretch: 75%; color: var(--ink); padding-top: 2px; }
.np-open .score { display: flex; flex-direction: column; line-height: 1; }
.np-open .score b { font-size: 26px; font-weight: 700; font-stretch: 75%; color: var(--heat); }
.np-open .score small { font-size: 12px; color: var(--muted); margin-top: 4px; }
.np-open .facts { display: flex; flex-wrap: wrap; gap: 4px 14px; font-size: 14px; color: var(--muted); }
.np-open .facts b { color: var(--ink); font-weight: 600; }
.np-open .what { margin: 6px 0 0; font-size: 14.5px; line-height: 1.45; color: var(--ink); display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.np-open .what.none { color: var(--faint); }
.pill { font-size: 12.5px; font-weight: 600; padding: 3px 9px; border-radius: 99px; white-space: nowrap; }
.pill.rendered { background: var(--ok-soft); color: var(--ok); }
.pill.ready { background: var(--sunk); color: var(--ink); border: 1px solid var(--rule); }
.pill.wait { background: var(--wait-soft); color: var(--wait); }

/* clip card */
.np-card { background: var(--sheet); border: 1px solid var(--rule); border-radius: var(--r-lg); padding: 20px 22px 22px; }
.np-card-empty { padding: 26px 22px; }
.np-card-empty p { margin: 0; font-size: 15px; color: var(--muted); }
.np-card-top { display: flex; gap: 18px; align-items: center; padding-bottom: 16px; border-bottom: 1px solid var(--rule); }
.np-score { display: flex; align-items: baseline; }
.np-score b { font-size: 64px; line-height: .9; font-weight: 700; font-stretch: 75%; color: var(--heat); letter-spacing: -0.02em; }
.np-score span { font-size: 16px; color: var(--muted); margin-left: 3px; }
.np-card .mult { margin: 0; font-size: 18px; color: var(--ink); }
.np-card .mult b { font-weight: 700; }
.np-card .muted, .muted { color: var(--muted); margin: 2px 0 0; font-size: 14px; }
.np-facts { display: grid; grid-template-columns: repeat(3, minmax(0,1fr)); gap: 12px 16px; margin: 16px 0 4px; }
.np-facts dt { font-size: 12.5px; color: var(--muted); }
.np-facts dd { margin: 1px 0 0; font-size: 16px; font-weight: 600; color: var(--ink); }
.np-card h4 { font-size: 13px; font-weight: 600; color: var(--muted); margin: 18px 0 5px; }
.np-card p { margin: 0; font-size: 15px; line-height: 1.55; max-width: 70ch; }
.np-card em { color: var(--muted); font-style: normal; }
.chips { display: flex; flex-wrap: wrap; gap: 6px; }
.chip { font-size: 13px; padding: 3px 9px; border-radius: var(--r-sm); background: var(--sunk); border: 1px solid var(--rule); color: var(--ink); }
.chip b { font-weight: 700; margin-left: 2px; }
#np-delclip { align-self: flex-start; }

/* section heads */
.np-sec h3 { font-size: 16px; font-weight: 650; margin: 4px 0 2px; color: var(--ink); }
.np-sec p { font-size: 13.5px; color: var(--muted); margin: 0; line-height: 1.45; }
.np-sec { margin-top: 6px; }

/* colour row */
#np-colours { align-items: center; gap: 6px; flex-wrap: nowrap !important; }
#np-picker { flex: 0 0 auto !important; width: auto !important; padding: 0 !important; overflow: visible !important; background: transparent !important; }
#np-picker .dialog-button { display: block; width: 40px !important; height: 32px !important; border: 1px solid #B9BFC9 !important; border-radius: 6px !important; }
.np-sw { min-width: 0 !important; font-weight: 500 !important; padding-left: 8px !important; }
.np-sw::before { content: ""; width: 14px; height: 14px; border-radius: 50%; display: inline-block; margin-right: 6px; border: 1px solid rgba(0,0,0,.25); vertical-align: -2px; }
#sw-white::before { background: #FFFFFF; } #sw-yellow::before { background: #FFD400; } #sw-sky::before { background: #3FA0FF; }
#sw-green::before { background: #39FF6A; } #sw-pink::before { background: #FF4FD8; }

/* media */
.np-media img, .np-media video { border-radius: 6px; }
#np-pv img { background: #000; }

/* confirm dialog */
#np-modal { position: fixed !important; z-index: 1000; left: 50%; top: 28vh; transform: translateX(-50%); width: min(460px, calc(100vw - 32px));
  background: var(--sheet) !important; border: 1px solid var(--rule) !important; border-radius: var(--r-lg) !important; padding: 20px 20px 16px !important;
  box-shadow: 0 0 0 100vmax rgba(24,27,33,.45), 0 12px 40px rgba(24,27,33,.25) !important; }
#np-modal #np-modal { position: static !important; transform: none !important; width: auto !important; padding: 0 !important;
  box-shadow: none !important; border: 0 !important; }
#np-modal h3 { margin: 0 0 6px; font-size: 19px; font-weight: 700; color: var(--ink); }
#np-modal p { margin: 0 0 14px; font-size: 14.5px; line-height: 1.5; color: var(--muted); }
#np-modal .row, #np-modal > div { background: transparent !important; border: 0 !important; gap: 10px; }
"""


def section(title, note=""):
    return gr.HTML(f'<div class="np-sec"><h3>{esc(title)}</h3>' + (f"<p>{esc(note)}</p>" if note else "") + "</div>",
                   js_on_load=None)


# One script for the sidebar, the clip list and the chart: open on click, tick to select, delete the selection.
PICK_JS = """
const upd = () => {
  const n = element.querySelectorAll('.np-pick:checked').length;
  const c = element.querySelector('.np-count'), d = element.querySelector('.np-bulk-del');
  if (c) { if (!c.dataset.idle) c.dataset.idle = c.textContent; c.textContent = n ? n + ' selected' : c.dataset.idle; }
  if (d) d.disabled = !n;
  element.querySelectorAll('.np-row, .sb-row').forEach(r => { const x = r.querySelector('.np-pick'); r.classList.toggle('picked', !!(x && x.checked)); });
};
// a fresh list never keeps old ticks (rows shift after a delete)
const clear = () => { element.querySelectorAll('.np-pick, .np-all').forEach(x => { x.checked = false; }); upd(); };
if (typeof watch === 'function') watch('value', clear);
element.addEventListener('change', (e) => {
  if (e.target.classList.contains('np-all')) element.querySelectorAll('.np-pick').forEach(x => { x.checked = e.target.checked; });
  upd();
});
element.addEventListener('click', (e) => {
  if (e.target.closest('.np-tick')) return;
  if (e.target.closest('.np-bulk-del')) {
    const ids = [...element.querySelectorAll('.np-pick:checked')].map(x => x.dataset.id);
    if (ids.length) trigger('click', {action: 'delete', ids: ids});
    return;
  }
  const o = e.target.closest('[data-clip],[data-key]');
  if (o) trigger('click', {action: 'open', id: o.dataset.clip !== undefined ? o.dataset.clip : o.dataset.key});
});
"""


def build():
    jobs0 = job_choices()
    first = jobs0[0][1] if jobs0 else ""
    with gr.Blocks(title="Clipper") as demo:
        job = gr.Textbox(value=first, show_label=False, container=False, elem_classes="np-hide")
        pend_kind, pend_ids = gr.State(""), gr.State([])
        with gr.Group(visible=False, elem_id="np-modal") as modal:
            modal_msg = gr.HTML(js_on_load=None)
            with gr.Row():
                m_cancel = gr.Button("Cancel")
                m_yes = gr.Button("Delete", elem_classes="np-danger")

        with gr.Row(elem_id="np-shell", equal_height=False):
            # ---------- sidebar: find + library ----------
            with gr.Column(scale=0, min_width=330, elem_id="np-side"):
                gr.HTML('<div id="np-brand"><h1>Clipper</h1><p>Finds where chat went off in a stream, past or live, '
                        'and cuts those moments into vertical Shorts.</p></div>', js_on_load=None)
                url = gr.Textbox(label="Stream link", show_label=False, container=False, elem_id="np-url", lines=1,
                                 max_lines=1, placeholder="Paste a past VOD or a live channel")
                by_opts = [("Chat spikes", "chat"), ("Streamer gets loud (live streams)", "audio"),
                           ("What's said (subtitles)", "speech")]
                basis = gr.CheckboxGroup(by_opts, value=["chat"], label="Find clips by", elem_id="np-basis",
                                         info="Tick one or more; moments where several agree rank higher. Past "
                                              "streams use YouTube's own subtitles (Twitch and Kick VODs have none) "
                                              "and are never listened to. Live streams are listened to on your computer.")
                keywords = gr.Textbox(label="Words to listen for (optional)", visible=False, elem_id="np-words",
                                      placeholder="no way, let's go, clip that", lines=1, max_lines=2)
                min_score = gr.Slider(0, 100, value=85, step=1, label="Minimum score",
                                      info="Clips scoring lower are left out, so you only get strong ones.",
                                      elem_id="np-minscore")
                go = gr.Button("Find clips", variant="primary", elem_id="np-go")
                with gr.Accordion("More options", open=False, elem_id="np-opts"):
                    top_n = gr.Slider(1, 30, value=10, step=1, label="How many clips to keep (past streams)")
                    hours = gr.Slider(0.5, 12, value=6, step=0.5, label="Live streams: stop watching after (hours)")
                    cookies = gr.Dropdown([("None", ""), ("Chrome", "chrome"), ("Safari", "safari"),
                                           ("Firefox", "firefox"), ("Brave", "brave"), ("Edge", "edge")], value="",
                                          label="Sign in with browser cookies (members-only or age-gated)")
                    chat_file = gr.File(label="Chat file (exported Twitch or Kick chat, JSON or CSV)",
                                        file_types=[".json", ".csv"], height=100)
                    antigravity_review = gr.Checkbox(
                        label="Automatically review new clips with Antigravity",
                        value=bool(config().get("antigravity_review", True)),
                        info="Uses a 9-frame low-resolution contact sheet plus short transcript/chat context. "
                             "No full clip or stream is sent. Runs headlessly with --sandbox and "
                             "--dangerously-skip-permissions (all tool approvals are bypassed).",
                    )
                side = gr.HTML(sidebar_html(first), js_on_load=PICK_JS)

            # ---------- main ----------
            with gr.Column(scale=1, min_width=560, elem_id="np-main"):
                with gr.Row(elem_id="np-head", equal_height=False):
                    status = gr.HTML(status_html(None), js_on_load=None, scale=1)
                    with gr.Column(scale=0, min_width=170, elem_id="np-actions"):
                        stop_btn = gr.Button("Stop watching", visible=False, elem_classes="np-danger")
                        pause_btn = gr.Button("Pause", visible=False, size="sm")
                        resume_btn = gr.Button("Resume", visible=False, size="sm")
                        del_stream = gr.Button("Delete stream", visible=False, size="sm", elem_classes="np-danger-soft")
                with gr.Accordion("Runtime health", open=False, elem_id="np-health"):
                    runtime_panel = gr.HTML(runtime.html_panel(), js_on_load=None)
                with gr.Column(elem_id="np-chartbox", variant="panel"):
                    chart = gr.HTML(chart_html(None), js_on_load=PICK_JS)
                with gr.Row(elem_id="np-cliprow", equal_height=True):
                    clip = gr.Dropdown(choices=[], label="Open clip", interactive=True, elem_id="np-clipsel", scale=1)
                    del_clip = gr.Button("Delete this clip", size="sm", visible=False, scale=0, min_width=150,
                                         elem_classes="np-danger-soft", elem_id="np-delclip")

                with gr.Tabs():
                    with gr.Tab("Clips"):
                        with gr.Row(equal_height=False):
                            with gr.Column(scale=6, min_width=420):
                                clip_list = gr.HTML(list_html(None), js_on_load=PICK_JS)
                            with gr.Column(scale=5, min_width=360):
                                card = gr.HTML(card_html(None, None), js_on_load=None)
                                review_btn = gr.Button("Review with Antigravity", variant="primary", visible=False)
                                review_msg = gr.HTML(js_on_load=None)
                                with gr.Row():
                                    feedback_good = gr.Button("Good clip", size="sm", visible=False)
                                    feedback_bad = gr.Button("Poor clip", size="sm", visible=False)
                                    feedback_unsure = gr.Button("Not sure", size="sm", visible=False)
                                feedback_msg = gr.HTML(js_on_load=None)
                                src_vid = gr.Video(label="Clip as downloaded", show_label=False, interactive=False,
                                                   elem_classes="np-media", visible=False)

                    with gr.Tab("Make the Short"):
                        with gr.Row(equal_height=False):
                            with gr.Column(scale=3, min_width=380):
                                section("Crops", "Pick a moment, then frame the face cam (blue) and the gameplay (orange).")
                                t = gr.Slider(0, 60, value=1, step=0.1, label="Frame to set the crops on (seconds)")
                                crop_view = gr.Image(show_label=False, interactive=False, elem_classes="np-media", buttons=[])
                                with gr.Row():
                                    with gr.Column(min_width=180):
                                        section("Face cam")
                                        with gr.Group():
                                            fx = gr.Slider(0, 1, step=0.005, label="Left edge")
                                            fy = gr.Slider(0, 1, step=0.005, label="Top edge")
                                            fw = gr.Slider(0.03, 1, step=0.005, label="Width (height follows the band)")
                                    with gr.Column(min_width=180):
                                        section("Gameplay")
                                        with gr.Group():
                                            gx = gr.Slider(0, 1, step=0.005, label="Left edge")
                                            gy = gr.Slider(0, 1, step=0.005, label="Top edge")
                                            gw = gr.Slider(0.05, 1, step=0.005, label="Width (height follows the band)")
                            with gr.Column(scale=2, min_width=340):
                                section("Preview", "The Short at this frame, 9:16.")
                                pv = gr.Image(show_label=False, interactive=False, height=PV_H, elem_id="np-pv",
                                              elem_classes="np-media", buttons=[])
                                section("Layout")
                                layout = gr.Radio([("Face on top", "faceTop"), ("Face at bottom", "faceBottom"),
                                                   ("No face", "noFace")], show_label=False, value="faceTop")
                                split = gr.Slider(0.25, 0.55, step=0.01, value=0.4, label="Face band height (share of the frame)")
                                section("Opening")
                                with gr.Group():
                                    hook_on = gr.Checkbox(label="Open with the hot moment, then play the full clip", value=True)
                                    with gr.Row():
                                        hs = gr.Number(label="Hook starts (s)", step=0.1)
                                        he = gr.Number(label="Hook ends (s)", step=0.1)
                                    with gr.Row():
                                        ts = gr.Number(label="Clip starts (s)", step=0.1)
                                        te = gr.Number(label="Clip ends (s)", step=0.1)
                                section("Subtitles", "One word at a time, no box, black shadow.")
                                with gr.Group():
                                    with gr.Row():
                                        subs_on = gr.Checkbox(label="Show subtitles", value=True)
                                        upper = gr.Checkbox(label="Uppercase", value=True)
                                swatches = []
                                with gr.Row(elem_id="np-colours", equal_height=True):
                                    color = gr.ColorPicker(label="Colour", show_label=False, container=False,
                                                           value="#FFFFFF", scale=0, min_width=48, elem_id="np-picker")
                                    for name, hx in (("White", "#FFFFFF"), ("Yellow", "#FFD400"), ("Sky", "#3FA0FF"),
                                                     ("Green", "#39FF6A"), ("Pink", "#FF4FD8")):
                                        swatches.append((gr.Button(name, size="sm", min_width=40, elem_id=f"sw-{name.lower()}",
                                                                   elem_classes="np-sw"), hx))
                                with gr.Group():
                                    size = gr.Slider(50, 200, step=2, value=110, label="Size (px)")
                                    sy = gr.Slider(0.08, 0.92, step=0.01, value=0.4, label="Height on screen (0 is the top)")
                                seam = gr.Button("Put words on the seam between face and gameplay", size="sm")
                                section("Export")
                                with gr.Row():
                                    render_btn = gr.Button("Render Short", variant="primary", scale=2)
                                    studio_btn = gr.Button("Open in Remotion Studio", scale=2)
                                render_msg = gr.HTML(js_on_load=None)
                                final_vid = gr.Video(label="Finished Short", show_label=False, interactive=False,
                                                     elem_classes="np-media", visible=False)
                                files = gr.File(label="Downloads (MP4 and SRT)", file_count="multiple", interactive=False,
                                                visible=False)


        timer = gr.Timer(2.0)
        runtime_timer = gr.Timer(5.0)
        edit_inputs = [job, clip, t, layout, split, fx, fy, fw, gx, gy, gw, hook_on, hs, he, ts, te, subs_on, color, size, sy, upper]
        editor_fields = [t, layout, split, fx, fy, fw, gx, gy, gw, hook_on, hs, he, ts, te, subs_on, color, size, sy, upper]
        quiet = dict(show_progress="hidden")
        head_out = [status, chart, clip_list, clip, stop_btn, pause_btn, resume_btn, del_stream, side]
        ask_out = [pend_kind, pend_ids, modal, modal_msg]

        # streams
        basis.change(lambda b: gr.update(visible="speech" in (b or [])), basis, keywords, **quiet)
        antigravity_review.change(save_antigravity_review, antigravity_review, None, **quiet)
        go.click(start_analysis, [url, top_n, cookies, chat_file, hours, basis, keywords, min_score, antigravity_review], job).then(
            lambda k: refresh_job(k), job, head_out, **quiet)
        job.change(switch_job, job, head_out, **quiet)
        demo.load(first_load, job, head_out, **quiet)
        timer.tick(tick, [job, clip], head_out, **quiet)
        runtime_timer.tick(runtime.html_panel, None, runtime_panel, **quiet)
        stop_btn.click(stop_live, job, [stop_btn, pause_btn, resume_btn, status], **quiet)
        pause_btn.click(pause_live, job, [status, pause_btn, resume_btn, side], **quiet)
        resume_btn.click(resume_live, job, [status, pause_btn, resume_btn, side], **quiet)
        side.click(side_click, None, [job] + ask_out, **quiet)

        # deleting (one confirm dialog for everything)
        del_stream.click(ask_stream, job, ask_out, **quiet)
        del_clip.click(ask_clip, [job, clip], ask_out, **quiet)
        m_cancel.click(lambda: gr.update(visible=False), None, modal, **quiet)
        m_yes.click(do_delete, [pend_kind, pend_ids, job, clip],
                    [modal, job, clip, status, chart, clip_list, side, del_stream], **quiet)

        # clips
        clip_list.click(list_click, job, [clip] + ask_out, **quiet)
        chart.click(list_click, job, [clip] + ask_out, **quiet)
        clip.change(highlight, [job, clip], [chart, clip_list], **quiet)
        clip.change(lambda n: gr.update(visible=n is not None), clip, del_clip, **quiet)
        clip.change(review_button_state, [job, clip], review_btn, **quiet)
        clip.change(feedback_button_state, [job, clip], [feedback_good, feedback_bad, feedback_unsure], **quiet)
        feedback_good.click(lambda k, n: save_clip_feedback(k, n, "keep"), [job, clip], feedback_msg, **quiet)
        feedback_bad.click(lambda k, n: save_clip_feedback(k, n, "reject"), [job, clip], feedback_msg, **quiet)
        feedback_unsure.click(lambda k, n: save_clip_feedback(k, n, "unsure"), [job, clip], feedback_msg, **quiet)
        review_btn.click(review_start_state, None, [review_btn, review_msg, clip], queue=False).then(
            review_with_antigravity, [job, clip], [review_msg, card, clip_list, clip], **quiet
        ).then(review_button_state, [job, clip], review_btn, **quiet)
        clip.change(load_clip, [job, clip], [card, src_vid] + editor_fields + [final_vid, files], **quiet).then(
            apply_edit, edit_inputs, [crop_view, pv], **quiet)
        for comp in editor_fields:      # user edits only (not the values load_clip fills in)
            ev = comp.release if isinstance(comp, gr.Slider) else comp.input
            ev(apply_edit, edit_inputs, [crop_view, pv], **quiet)
        for btn, hx in swatches:
            btn.click(lambda h=hx: h, None, color, **quiet).then(apply_edit, edit_inputs, [crop_view, pv], **quiet)
        seam.click(snap_seam, [layout, split], sy, **quiet).then(apply_edit, edit_inputs, [crop_view, pv], **quiet)
        render_btn.click(apply_edit, edit_inputs, [crop_view, pv], **quiet).then(
            do_render, [job, clip], [render_msg, final_vid, files], **quiet)
        studio_btn.click(apply_edit, edit_inputs, [crop_view, pv], **quiet).then(open_studio, [job, clip], render_msg, **quiet)

    return demo


def serve(port=8765):
    build().launch(server_name="127.0.0.1", server_port=port, allowed_paths=[str(JOBS)], inbrowser=True,
                   css=CSS, theme=THEME, head=HEAD, js=FORCE_LIGHT)
