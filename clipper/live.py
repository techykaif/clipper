"""Clip a stream that is live right now.

One watcher process per stream (`clipper live <url>`, started by the dashboard):
- chat is read as it happens: Twitch through its public chat server (anonymous, read-only), YouTube through
  yt-dlp's live chat reader;
- the video is recorded with yt-dlp into short segments, keeping only the last BUFFER_S seconds on disk;
- every CHECK_S seconds the same spike scoring as for past streams (spikes.analyse) runs on the chat so far;
  each spike that has calmed down is cut from the buffer into clips/<n>/source.mp4 and prepared like any clip.
Times count from when watching started. Stop with the dashboard's Stop button, `clipper stop <job>`, or when
the stream ends. Kick live chat comes from Kick's realtime service with an anonymous token (chat/kick.py).
"""
import json
import os
import re
import signal
import subprocess
import threading
import time
from pathlib import Path

from . import ai_review, signals, spikes
from .common import JOBS, fmt_t, read_json, set_status, video_info, write_json

BUFFER_S = 600          # seconds of video kept on disk
SEG_S = 4               # recorder segment length
CHECK_S = 10            # how often spikes are looked for
SETTLE_S = 12           # wait this long after a clip's end so the recorder has written it
WARMUP_S = float(os.environ.get("CLIPPER_LIVE_WARMUP", 120))   # no clips before this much chat history
SORT = "res,fps,vbr"

TWITCH_CH = re.compile(r"(?:^|//)(?:www\.|m\.)?twitch\.tv/([A-Za-z0-9_]{3,25})/?(?:\?.*)?$")
KICK_CH = re.compile(r"(?:^|//)(?:www\.)?kick\.com/([A-Za-z0-9_-]+)/?(?:\?.*)?$")
YOUTUBE = re.compile(r"(youtube\.com|youtu\.be)/")


class NotLive(RuntimeError):
    pass


def _ytdlp_json(url, timeout=60):
    p = subprocess.run(["yt-dlp", "-J", "--no-warnings", "--skip-download", "--no-playlist", url],
                       capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise NotLive((p.stderr.strip().splitlines() or ["yt-dlp couldn't read that link"])[-1].replace("ERROR: ", ""))
    return json.loads(p.stdout)


def probe(url):
    """Live target for this link, or None if it isn't a live stream (then it's handled as a past stream)."""
    url = (url or "").strip()
    if "twitch.tv/videos/" in url or re.search(r"twitch\.tv/[^/]+/v(?:ideo)?/\d+", url):
        return None
    m = TWITCH_CH.search(url)
    if m and m.group(1).lower() not in ("videos", "directory", "settings", "downloads"):
        ch = m.group(1).lower()
        try:
            j = _ytdlp_json(f"https://www.twitch.tv/{ch}")
        except NotLive as e:
            if "not currently live" in str(e) or "does not exist" in str(e).lower():
                raise NotLive(f"{ch} isn't live on Twitch right now.")
            raise NotLive(f"Couldn't open {ch} on Twitch: {e}")
        return {"platform": "twitch", "id": ch, "url": f"https://www.twitch.tv/{ch}", "channel": j.get("uploader") or ch,
                "title": j.get("description") or j.get("title") or ch}
    m = KICK_CH.search(url)
    if m and "/videos/" not in url and "/video/" not in url:
        from .chat import kick
        slug = m.group(1).lower()
        try:
            ch = kick.channel(slug)
        except Exception as e:
            raise NotLive(f"Couldn't open {slug} on Kick: {e}")
        ls = ch.get("livestream")
        if not ls or not ls.get("is_live", True):
            raise NotLive(f"{slug} isn't live on Kick right now.")
        return {"platform": "kick", "id": slug, "url": f"https://kick.com/{slug}",
                "channel": (ch.get("user") or {}).get("username") or slug, "title": ls.get("session_title") or slug}
    if YOUTUBE.search(url):
        j = _ytdlp_json(url)
        status = j.get("live_status")
        if status == "is_upcoming":
            raise NotLive("That YouTube stream hasn't started yet. Try again once it's live.")
        if status != "is_live":
            return None
        return {"platform": "youtube", "id": j["id"], "url": f"https://www.youtube.com/watch?v={j['id']}",
                "channel": j.get("channel") or j.get("uploader"), "title": j.get("title")}
    return None


def new_job(target, hours=6.0, top_n=30, basis=("chat",), keywords="", min_score=85,
             antigravity_review=False):
    key = f"{target['platform']}-live-{re.sub(r'[^A-Za-z0-9_-]', '', target['id'])}-{time.strftime('%Y%m%d-%H%M%S')}"
    jd = JOBS / key
    jd.mkdir(parents=True)
    basis = [b for b in ("chat", "audio", "speech") if b in (basis or ())] or ["chat"]
    write_json(jd / "job.json", {"url": target["url"], "platform": target["platform"], "id": target["id"], "live": True,
                                 "hours": hours, "top_n": top_n, "basis": basis, "keywords": keywords,
                                 "min_score": min_score, "antigravity_review": bool(antigravity_review)})
    write_json(jd / "meta.json", {"title": target.get("title"), "channel": target.get("channel"), "duration": 0})
    set_status(jd, "live", 0, "Starting")
    return jd


def start_process(jd: Path):
    """Run the watcher detached, so it keeps going if the dashboard restarts."""
    import sys
    log = open(jd / "live.log", "a")
    # Windows: own process group and a hidden console (its yt-dlp and ffmpeg children share it, so no windows pop up)
    detach = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW} if os.name == "nt"
              else {"start_new_session": True})
    p = subprocess.Popen([sys.executable, "-m", "clipper", "live-run", jd.name], cwd=Path(__file__).resolve().parents[1],
                         stdout=log, stderr=subprocess.STDOUT, **detach)
    write_json(jd / "live.json", {"pid": p.pid, "started": time.time()})
    return p.pid


def running(jd: Path) -> bool:
    info = read_json(jd / "live.json", {}) or {}
    pid = info.get("pid")
    if not pid or info.get("ended"):
        return False
    return _alive(pid)


def _alive(pid: int) -> bool:
    if os.name == "nt":       # os.kill(pid, 0) would terminate the process on Windows
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, pid)            # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
        k32.CloseHandle(h)
        return bool(ok) and code.value == 259              # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def stop(jd: Path):
    (jd / "stop").touch()
    info = read_json(jd / "live.json", {}) or {}
    # Windows has no SIGTERM (os.kill would hard-kill and orphan yt-dlp/ffmpeg): the watcher sees the stop file
    # within CHECK_S seconds and shuts down cleanly.
    if info.get("pid") and os.name != "nt":
        try:
            os.kill(info["pid"], signal.SIGTERM)
        except OSError:
            pass


# ---------------- chat readers ----------------
class Chat:
    def __init__(self, t0):
        self.t0, self.msgs, self.lock, self.error, self.alive = t0, [], threading.Lock(), "", True

    def add(self, wall, user, text):
        t = wall - self.t0
        if t < 0:
            return                      # history from before we started watching
        with self.lock:
            self.msgs.append({"t": round(t, 2), "user": user, "text": text})

    def snapshot(self):
        with self.lock:
            return sorted(self.msgs, key=lambda m: m["t"])


def twitch_chat(channel, chat: Chat, stop_evt):
    """Twitch's chat server, read-only with an anonymous login (justinfan), the way twitch.tv shows chat to visitors."""
    from websockets.sync.client import connect
    tag_rx = re.compile(r"(?:^|;)display-name=([^;]*)")
    while not stop_evt.is_set():
        try:
            with connect("wss://irc-ws.chat.twitch.tv:443", open_timeout=20) as ws:
                ws.send("CAP REQ :twitch.tv/tags")
                ws.send("PASS SCHMOOPIIE")
                ws.send(f"NICK justinfan{int(time.time()) % 100000}")
                ws.send(f"JOIN #{channel}")
                while not stop_evt.is_set():
                    try:
                        data = ws.recv(timeout=5)
                    except TimeoutError:
                        continue
                    for line in data.split("\r\n"):
                        if line.startswith("PING"):
                            ws.send("PONG :tmi.twitch.tv")
                        elif " PRIVMSG #" in line:
                            tags, _, rest = line.partition(" :") if line.startswith("@") else ("", "", line[1:])
                            user = rest.split("!", 1)[0]
                            m = tag_rx.search(tags)
                            text = rest.split(" :", 1)[1] if " :" in rest else ""
                            chat.add(time.time(), (m.group(1) if m and m.group(1) else user), text)
        except Exception as e:          # dropped connection: reconnect
            chat.error = f"Twitch chat: {e}"
            stop_evt.wait(3)


def youtube_chat(url, jd: Path, chat: Chat, stop_evt):
    """yt-dlp's YouTube live chat reader writes one JSON line per chat action; follow the file as it grows."""
    out = jd / "yt_live"
    for f in jd.glob("yt_live*"):
        f.unlink()
    proc = subprocess.Popen(["yt-dlp", "--no-warnings", "--skip-download", "--write-subs", "--sub-langs", "live_chat",
                             "-o", str(out), url], stdout=subprocess.DEVNULL, stderr=open(jd / "chat.log", "w"),
                            start_new_session=True)
    pos, buf = 0, ""
    try:
        while not stop_evt.is_set():
            files = [f for f in jd.glob("yt_live.live_chat.json*") if "Frag" not in f.name]
            if files:
                with open(files[0], encoding="utf-8", errors="replace") as fh:
                    fh.seek(pos)
                    buf += fh.read()
                    pos = fh.tell()
                *lines, buf = buf.split("\n")
                for line in lines:
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    for a in (d.get("replayChatItemAction") or {}).get("actions", []):
                        item = (a.get("addChatItemAction") or {}).get("item") or {}
                        m = item.get("liveChatTextMessageRenderer") or item.get("liveChatPaidMessageRenderer")
                        if not m or not m.get("timestampUsec"):
                            continue
                        text = "".join(r.get("text") or ((r.get("emoji") or {}).get("shortcuts") or [""])[0]
                                       for r in (m.get("message") or {}).get("runs", []))
                        chat.add(int(m["timestampUsec"]) / 1e6, (m.get("authorName") or {}).get("simpleText", ""), text)
            if proc.poll() is not None:
                chat.alive = False
                chat.error = "YouTube live chat ended"
                break
            stop_evt.wait(2)
    finally:
        if proc.poll() is None:
            proc.terminate()


# ---------------- recorder ----------------
class Recorder:
    """yt-dlp (best resolution, then highest frame rate) piped into ffmpeg, cut into SEG_S-second .ts files named
    by wall-clock start time."""

    def __init__(self, url, seg_dir: Path, log: Path):
        self.dir = seg_dir
        seg_dir.mkdir(parents=True, exist_ok=True)
        lf = open(log, "w")
        self.dl = subprocess.Popen(["yt-dlp", "--no-warnings", "--no-part", "-f", "b", "-S", SORT, "-o", "-", url],
                                   stdout=subprocess.PIPE, stderr=lf, start_new_session=True)
        self.ff = subprocess.Popen(["ffmpeg", "-v", "error", "-i", "pipe:0", "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
                                    "-f", "segment", "-segment_time", str(SEG_S), "-reset_timestamps", "1",
                                    "-strftime", "1", str(seg_dir / "seg_%s.ts")],
                                   stdin=self.dl.stdout, stdout=subprocess.DEVNULL, stderr=lf, start_new_session=True)
        self.dl.stdout.close()

    def alive(self):
        return self.ff.poll() is None and self.dl.poll() is None

    def segments(self):
        """[(wall_start, path)] oldest first. The newest file may still be growing."""
        out = []
        for f in self.dir.glob("seg_*.ts"):
            try:
                out.append((int(f.stem[4:]), f))
            except ValueError:
                pass
        return sorted(out)

    def prune(self, keep_from_wall):
        segs = self.segments()
        for i, (w, f) in enumerate(segs[:-1]):
            nxt = segs[i + 1][0]
            if nxt < keep_from_wall:
                f.unlink(missing_ok=True)

    def cut(self, wall_a, wall_b, dest: Path):
        """Concatenate the segments covering [wall_a, wall_b] and trim to exactly that range (re-encoded, high quality)."""
        segs = self.segments()
        use = []
        for i, (w, f) in enumerate(segs):
            end = segs[i + 1][0] if i + 1 < len(segs) else time.time()
            if end > wall_a and w < wall_b:
                use.append((w, f))
        if not use:
            raise RuntimeError("That moment is no longer in the recording buffer.")
        lst = dest.parent / "concat.txt"
        lst.write_text("".join(f"file '{f.resolve()}'\n" for _, f in use))
        off = max(0.0, wall_a - use[0][0])
        p = subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
                            "-ss", f"{off:.2f}", "-t", f"{wall_b - wall_a:.2f}",
                            "-c:v", "libx264", "-crf", "14", "-preset", "medium", "-c:a", "aac", "-b:a", "320k",
                            "-movflags", "+faststart", str(dest)], capture_output=True, text=True)
        lst.unlink(missing_ok=True)
        if p.returncode != 0 or not dest.exists():
            raise RuntimeError("ffmpeg couldn't cut the clip: " + p.stderr.strip()[-300:])
        return dest

    def close(self):
        for p in (self.dl, self.ff):
            if p.poll() is None:
                p.terminate()
        for p in (self.dl, self.ff):
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                p.kill()


# ---------------- the watcher ----------------
def run(key):
    from .pipeline import prepare_clip
    jd = JOBS / key
    job = read_json(jd / "job.json")
    meta = read_json(jd / "meta.json", {}) or {}
    (jd / "stop").unlink(missing_ok=True)
    t0 = time.time()
    stop_evt = threading.Event()
    signal.signal(signal.SIGTERM, lambda *a: stop_evt.set())
    chat = Chat(t0)
    if job["platform"] == "twitch":
        reader = threading.Thread(target=twitch_chat, args=(job["id"], chat, stop_evt), daemon=True)
    elif job["platform"] == "kick":
        from .chat import kick
        reader = threading.Thread(target=kick.live, args=(job["id"], chat.add, stop_evt,
                                                          lambda e: setattr(chat, "error", e)), daemon=True)
    else:
        reader = threading.Thread(target=youtube_chat, args=(job["url"], jd, chat, stop_evt), daemon=True)
    reader.start()
    rec = Recorder(job["url"], jd / "buffer", jd / "record.log")
    deadline = t0 + float(job.get("hours") or 6) * 3600
    clips, why_stop = [], "Stopped"
    basis = job.get("basis") or ["chat"]
    listen = "audio" in basis or "speech" in basis
    scan, heard = None, set()
    write_json(jd / "live.json", {**(read_json(jd / "live.json", {}) or {}), "pid": os.getpid(), "t0": t0})
    try:
        while True:
            if stop_evt.wait(CHECK_S) or (jd / "stop").exists():
                why_stop = "Stopped"
                break
            if time.time() > deadline:
                why_stop = f"Stopped after {job.get('hours')} hours"
                break
            if not rec.alive():
                why_stop = "The stream ended"
                break
            if not chat.alive and "chat" in basis:
                why_stop = chat.error or "Chat ended"
                break
            elapsed = time.time() - t0
            msgs = chat.snapshot()
            if listen:                                     # loudness (+ words) of every finished segment, in order
                segs = rec.segments()
                for w, f in segs[:-1]:
                    if f.name in heard:
                        continue
                    if scan is None:
                        scan = signals.AudioScan(speech="speech" in basis, t0=max(0.0, w - t0))
                        scan.db = [-90.0] * int(max(0.0, w - t0) // signals.BIN)
                    scan.feed(signals.segment_audio(f))
                    heard.add(f.name)
            _publish(jd, meta, job, msgs, elapsed, clips, chat.error, scan)
            if elapsed < WARMUP_S or ("chat" in basis and len(msgs) < 10) or (listen and not scan):
                set_status(jd, "live", 0, f"Watching for {fmt_t(elapsed)}. Learning this chat's normal pace before "
                                          f"clipping ({len(msgs)} messages so far)")
                continue
            res = signals.find(basis, elapsed, msgs=msgs, db=scan.db if scan else None,
                               words=scan.words if scan else None, keywords=job.get("keywords", ""), top_n=200,
                               min_score=job.get("min_score", 0))
            for c in sorted(res["clips"], key=lambda c: c["chat_peak"]):
                at = c.get("peak", c["chat_peak"])
                if any(abs(at - k.get("peak", k["chat_peak"])) < spikes.DEDUPE for k in clips):
                    continue
                if c["end"] + SETTLE_S > elapsed:
                    continue                                   # chat hasn't calmed down yet, or not recorded yet
                if c["start"] < elapsed - BUFFER_S + 30:
                    continue                                   # already gone from the buffer
                c["id"] = len(clips) + 1
                c["vod_time"] = fmt_t(c["start"])
                c["what"], c["why"] = "", ""
                cd = jd / "clips" / str(c["id"])
                cd.mkdir(parents=True, exist_ok=True)
                set_status(jd, "live", 0, f"Cutting clip {c['id']} (chat hit {c['multiple']}× at {c['vod_time']})")
                try:
                    src = rec.cut(t0 + c["start"], t0 + c["end"], cd / "source.mp4")
                    info = video_info(src)
                    c["end"] = round(c["start"] + info["duration"], 2)
                    prepare_clip(cd, c, src, info)
                    if job.get("antigravity_review"):
                        set_status(jd, "review", 0, f"Antigravity reviewing clip {c['id']}")
                        c["review"] = ai_review.review_clip(jd, c)
                    clips.append(c)
                except Exception as e:
                    (cd / "error.log").write_text(str(e))
                    clips.append({**c, "failed": str(e)})
                _publish(jd, meta, job, msgs, elapsed, clips, chat.error, scan)
            rec.prune(t0 + elapsed - BUFFER_S)
            n = len([c for c in clips if not c.get("failed")])
            set_status(jd, "live", 0, f"Watching for {fmt_t(elapsed)}. {n} clip{'' if n == 1 else 's'} so far"
                                      + (f". {chat.error}" if chat.error else ""))
    finally:
        stop_evt.set()
        rec.close()
        msgs = chat.snapshot()
        _publish(jd, meta, job, msgs, time.time() - t0, clips, "", scan)
        for f in (jd / "buffer").glob("*"):
            f.unlink(missing_ok=True)
        try:
            (jd / "buffer").rmdir()
        except OSError:
            pass
        for f in jd.glob("yt_live*"):
            f.unlink(missing_ok=True)
        info = read_json(jd / "live.json", {}) or {}
        write_json(jd / "live.json", {**info, "ended": time.time()})
        n = len([c for c in clips if not c.get("failed")])
        set_status(jd, "done", 100, f"{why_stop}. {n} clip{'' if n == 1 else 's'} from {fmt_t(time.time() - t0)} of watching")


def _publish(jd, meta, job, msgs, elapsed, clips, note, scan=None):
    """Chart, ranked list and chat for the dashboard. Keeps notes /clip wrote."""
    write_json(jd / "chat.json", msgs)
    if scan is not None:
        write_json(jd / "signals.json", {"bin": signals.BIN, "db": scan.db})
        if scan.speech:
            write_json(jd / "transcript.json", scan.words)
    counts = [0] * max(1, int(elapsed // spikes.BIN) + 1)
    for m in msgs:
        counts[min(len(counts) - 1, int(m["t"] // spikes.BIN))] += 1
    avg = sum(counts) / len(counts)
    write_json(jd / "rates.json", {"bin": spikes.BIN, "avg": avg, "counts": counts})
    old = {c["id"]: c for c in (read_json(jd / "clips.json", {}) or {}).get("clips", [])}
    good = []
    for c in clips:
        if c.get("failed"):
            continue
        prev = old.get(c["id"]) or {}
        good.append({**c, "what": prev.get("what") or c.get("what", ""), "why": prev.get("why") or c.get("why", "")})
    good.sort(key=lambda c: -c["score"])
    write_json(jd / "clips.json", {"url": job["url"], "platform": job["platform"], "id": job["id"], "live": True,
                                   "title": meta.get("title"), "channel": meta.get("channel"), "duration": round(elapsed),
                                   "messages": len(msgs), "avg_per_min": round(avg * 60 / spikes.BIN, 2),
                                   "bin": spikes.BIN, "basis": job.get("basis") or ["chat"],
                                   "min_score": job.get("min_score", 0), "clips": good})
