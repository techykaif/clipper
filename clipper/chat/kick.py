"""Kick chat, read through the same public endpoints kick.com uses for logged-out visitors.

Checked 2026-11-17: these answer plain requests (no bot-check page). If Kick ever puts them behind a challenge,
Clipper stops and asks for an exported chat file instead; it never tries to get around a bot check.

- VOD chat replay: GET kick.com/api/v2/channels/{channel_id}/messages?cursor={microseconds} returns the 25
  messages just before that moment, and its "cursor" points further back. A VOD is split into time slices that
  are fetched in parallel, each paging backwards until it reaches its start.
- Live chat: Kick's realtime service (Centrifugo over a websocket) with an anonymous connection token, see live().
"""
import json
import math
import re
import subprocess
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"
HEADERS = {"User-Agent": UA, "Accept": "application/json, text/plain, */*", "Referer": "https://kick.com/",
           "Origin": "https://kick.com"}
EMOTE = re.compile(r"\[emote:\d+:([^\]]+)\]")
SLICE_S = 600          # one worker pages back through 10 minutes of chat
WORKERS = 6


class KickBlocked(RuntimeError):
    pass


class KickStopped(RuntimeError):
    """The live chat worker was asked to stop during an HTTP request or retry delay."""


def _req(method, url, body=None, tries=5, timeout=30, stop_evt=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = dict(HEADERS, **({"Content-Type": "application/json"} if body is not None else {}))
    for i in range(tries):
        if stop_evt is not None and stop_evt.is_set():
            raise KickStopped("Kick chat stop requested")
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers, method=method),
                                        timeout=timeout) as r:
                raw = r.read()
            if raw.lstrip()[:1] not in (b"{", b"["):
                raise KickBlocked("Kick answered with a web page instead of data (a bot check).")
            return json.loads(raw)
        except urllib.error.HTTPError as e:
            if e.code in (403, 503) and b"challenge" in e.read()[:4000].lower():
                raise KickBlocked("Kick asked for a bot check, so Clipper stopped (it never gets around those). "
                                  "Add an exported chat file instead.")
            if e.code == 404:
                raise RuntimeError("Kick says this channel or video doesn't exist.")
            if i == tries - 1:
                raise
            delay = 1.5 * (i + 1) if e.code != 429 else 5 * (i + 1)
        except (urllib.error.URLError, TimeoutError):
            if i == tries - 1:
                raise
            delay = 1.5 * (i + 1)
        else:
            continue
        if stop_evt is not None:
            if stop_evt.wait(delay):
                raise KickStopped("Kick chat stop requested")
        else:
            time.sleep(delay)


def _text(content):
    return EMOTE.sub(lambda m: m.group(1), content or "")


def _epoch(s):
    s = s.replace(" ", "T").replace("Z", "+00:00")
    d = datetime.fromisoformat(s)
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()


def channel(slug, stop_evt=None):
    return _req("GET", f"https://kick.com/api/v2/channels/{slug}",
                timeout=10 if stop_evt is not None else 30, stop_evt=stop_evt)


def video(uuid):
    """start (epoch s), duration (s), channel_id, title, channel name of a VOD."""
    v = _req("GET", f"https://kick.com/api/v1/video/{uuid}")
    ls = v.get("livestream") or {}
    ch = ls.get("channel") or {}
    return {"start": _epoch(ls["start_time"]), "duration": (ls.get("duration") or 0) / 1000.0,
            "channel_id": ls.get("channel_id") or ch.get("id"), "title": ls.get("session_title") or v.get("title"),
            "channel": (ch.get("user") or {}).get("username") or ch.get("slug"), "source": v.get("source")}


def meta_of(url, cookies_browser=None):
    vid = re.search(r"([0-9a-f]{8}-[0-9a-f-]{27})", url)
    if vid:
        try:
            v = video(vid.group(1))
            return {"title": v["title"], "channel": v["channel"], "duration": v["duration"]}
        except Exception:
            pass
    cmd = ["yt-dlp", "-J", "--no-warnings", "--skip-download", url]
    if cookies_browser:
        cmd[1:1] = ["--cookies-from-browser", cookies_browser]
    j = json.loads(subprocess.run(cmd, check=True, capture_output=True, text=True).stdout)
    return {"title": j.get("title"), "channel": j.get("uploader") or j.get("channel"), "duration": j.get("duration")}


def _slice(channel_id, start, a, b):
    """Messages with a <= t < b (seconds into the VOD), paging backwards from b."""
    out, cursor, last = [], int((start + b) * 1e6), None
    while True:
        d = (_req("GET", f"https://kick.com/api/v2/channels/{channel_id}/messages?cursor={cursor}") or {}).get("data") or {}
        msgs = d.get("messages") or []
        stop = not msgs
        for m in msgs:
            t = _epoch(m["created_at"]) - start
            if t < a:
                stop = True
                continue
            if t < b:
                out.append({"id": m.get("id"), "t": round(t, 2), "user": (m.get("sender") or {}).get("username", ""),
                            "text": _text(m.get("content"))})
        nxt = d.get("cursor")
        if stop or not nxt or (last is not None and int(nxt) >= last):
            return out
        last = cursor = int(nxt)


def fetch(url, vid, workdir, progress, cookies_browser=None):
    progress(2, "Reading the Kick video")
    v = video(vid)
    dur = v["duration"] or meta_of(url, cookies_browser)["duration"] or 0
    if not dur:
        raise RuntimeError("Couldn't tell how long this Kick video is.")
    n = max(1, math.ceil(dur / SLICE_S))
    slices = [(i * SLICE_S, min(dur, (i + 1) * SLICE_S)) for i in range(n)]
    msgs, seen, done = [], set(), 0
    progress(5, f"Downloading Kick chat replay (0 of {n} ten-minute parts)")
    with ThreadPoolExecutor(WORKERS) as pool:
        futs = [pool.submit(_slice, v["channel_id"], v["start"], a, b) for a, b in slices]
        for f in as_completed(futs):
            for m in f.result():
                if m["id"] not in seen:
                    seen.add(m["id"])
                    msgs.append({"t": m["t"], "user": m["user"], "text": m["text"]})
            done += 1
            progress(5 + 25 * done / n, f"Kick chat: {len(msgs):,} messages, {done} of {n} ten-minute parts")
    msgs.sort(key=lambda m: m["t"])
    return msgs, {"title": v["title"], "channel": v["channel"], "duration": dur}


# ---------------- live ----------------
def live(slug, on_message, stop_evt, on_error=lambda e: None):
    """Read a live channel's chat until stop_evt is set; on_message(wall_time, user, text). Reconnects with a fresh
    anonymous token when the connection drops (tokens last about 45 minutes)."""
    from websockets.sync.client import connect
    if stop_evt.is_set():
        return
    try:
        ch = channel(slug, stop_evt=stop_evt)
    except KickStopped:
        return
    cid, room = ch["id"], ch["chatroom"]["id"]
    while not stop_evt.is_set():
        try:
            client = f"clipper-{time.time_ns()}"
            conn = _req("POST", f"https://web.kick.com/api/v1/realtime/channels/{cid}/chat/connection",
                        {"client": {"id": client, "type": "web"},
                         "capabilities": {"accepted_providers": [{"provider": "centrifugo"}]}},
                        timeout=10, stop_evt=stop_evt)
            url = next(c["credentials"]["url"] for c in conn["data"]["connections"] if c["provider"] == "centrifugo")
            tok = _req("POST", "https://web.kick.com/api/v1/realtime/auth/connection", {"client_id": client},
                       timeout=10, stop_evt=stop_evt)["data"]["token"]
            if stop_evt.is_set():
                break
            with connect(url, additional_headers={"Origin": "https://kick.com", "User-Agent": UA}, open_timeout=10) as ws:
                ws.send(json.dumps({"id": 1, "connect": {"token": tok, "name": "clipper"}}))
                ws.send(json.dumps({"id": 2, "subscribe": {"channel": f"chatrooms.{room}.v2"}}))
                last_activity = time.monotonic()
                while not stop_evt.is_set():
                    try:
                        raw = ws.recv(timeout=5)
                    except TimeoutError:
                        if stop_evt.is_set():
                            break
                        if time.monotonic() - last_activity >= 120:
                            raise RuntimeError("Kick chat connection timed out; reconnecting")
                        continue
                    last_activity = time.monotonic()
                    for line in raw.split("\n"):
                        if not line.strip():
                            continue
                        f = json.loads(line)
                        if f == {}:
                            ws.send("{}")                         # server ping
                            continue
                        if f.get("error"):
                            raise RuntimeError(f"Kick chat refused: {f['error']}")
                        push = f.get("push") or {}
                        if push.get("disconnect"):
                            raise RuntimeError("Kick closed the chat connection")
                        pub = (push.get("pub") or {}).get("data")
                        if pub and pub.get("event") == "App\\Events\\ChatMessageEvent":
                            d = pub["data"]
                            d = json.loads(d) if isinstance(d, str) else d
                            on_message(time.time(), (d.get("sender") or {}).get("username", ""), _text(d.get("content")))
        except KickStopped:
            return
        except KickBlocked as e:
            on_error(str(e))
            return
        except Exception as e:
            on_error(f"Kick chat: {e}")
            stop_evt.wait(3)
