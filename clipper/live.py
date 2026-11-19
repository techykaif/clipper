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
import psutil
import time
from contextlib import contextmanager
from pathlib import Path

from . import ai_review, library, signals, spikes
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


LIVE_LOCK = JOBS / ".live-worker.lock"


def _lock_owner():
    try:
        owner = read_json(LIVE_LOCK, {}) or {}
        pid = int(owner.get("pid") or 0)
        if pid <= 0:
            return None
    except (OSError, ValueError, TypeError):
        return None
    try:
        proc = psutil.Process(pid)
        if not proc.is_running() or proc.status() == psutil.STATUS_ZOMBIE:
            return None
        created = owner.get("create_time")
        if created is not None and abs(proc.create_time() - float(created)) > 2.0:
            return None
        return owner
    except psutil.AccessDenied:
        return {"pid": pid, "job": owner.get("job"), "unknown": True}
    except (psutil.NoSuchProcess, ValueError, TypeError):
        return None


def _legacy_live_owner(exclude_job=None):
    """Detect a live worker started before the lock-file protocol was installed."""
    if not JOBS.is_dir():
        return None
    for jd in JOBS.iterdir():
        if not jd.is_dir() or jd.name == exclude_job:
            continue
        job = read_json(jd / "job.json", {}) or {}
        if not job.get("live"):
            continue
        info = read_json(jd / "live.json", {}) or {}
        pid = info.get("pid")
        if not pid or info.get("ended"):
            continue
        try:
            proc = psutil.Process(int(pid))
            if not proc.is_running() or proc.status() == psutil.STATUS_ZOMBIE:
                continue
            created = info.get("create_time")
            if created is not None and abs(proc.create_time() - float(created)) > 2.0:
                continue
            command = proc.cmdline()
            if "live-run" in command and jd.name in command:
                return {"job": jd.name}
        except psutil.AccessDenied:
            return {"job": jd.name, "unknown": True}
        except (psutil.NoSuchProcess, TypeError, ValueError):
            continue
    return None


def _assert_live_slot_available(exclude_job=None):
    """Fail closed when another live worker owns the single-source slot."""
    if LIVE_LOCK.exists():
        owner = _lock_owner()
        if owner:
            if owner.get("unknown"):
                raise RuntimeError("The live-worker lock exists but its owner cannot be verified; refusing to start another source.")
            if owner.get("job") != exclude_job:
                raise RuntimeError(
                    f"Clipper already has a live source running or paused: {owner.get('job') or 'unknown job'}. "
                    "Stop that source before starting another."
                )
        else:
            try:
                if time.time() - LIVE_LOCK.stat().st_mtime < 10:
                    raise RuntimeError("Another live source is starting; try again in a moment.")
                LIVE_LOCK.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise RuntimeError(f"Could not clear a stale live-worker lock: {exc}") from exc
    legacy = _legacy_live_owner(exclude_job)
    if legacy:
        if legacy.get("unknown"):
            raise RuntimeError("An existing live worker was found but its identity cannot be verified; refusing to start another source.")
        raise RuntimeError(
            f"An existing live source is still running: {legacy.get('job')}. "
            "Stop that source before starting another."
        )


@contextmanager
def _slot_guard():
    """Serialize lock recovery and acquisition across independent OS processes."""
    JOBS.mkdir(parents=True, exist_ok=True)
    guard_path = JOBS / ".live-worker.guard"
    with guard_path.open("a+b") as guard:
        guard.seek(0, os.SEEK_END)
        if guard.tell() == 0:
            guard.write(b"0")
            guard.flush()
        guard.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(guard.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                guard.seek(0)
                msvcrt.locking(guard.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(guard.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(guard.fileno(), fcntl.LOCK_UN)


def _claim_live_slot(key):
    """Atomically claim the one-live-source slot across dashboard and CLI processes."""
    JOBS.mkdir(parents=True, exist_ok=True)
    payload = {
        "pid": os.getpid(),
        "create_time": psutil.Process(os.getpid()).create_time(),
        "job": str(key),
        "started": time.time(),
    }
    with _slot_guard():
        _assert_live_slot_available(key)
        try:
            fd = os.open(str(LIVE_LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise RuntimeError("Another live source claimed the slot first.") from exc
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as out:
                json.dump(payload, out)
                out.flush()
                os.fsync(out.fileno())
        except Exception:
            LIVE_LOCK.unlink(missing_ok=True)
            raise
    return payload


def _release_live_slot(owner):
    """Release only a lock still owned by this process/job."""
    current = read_json(LIVE_LOCK, {}) or {}
    if (current.get("pid") == owner.get("pid")
            and current.get("job") == owner.get("job")
            and current.get("create_time") == owner.get("create_time")):
        LIVE_LOCK.unlink(missing_ok=True)


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
    with _slot_guard():
        _assert_live_slot_available(jd.name)
    import sys
    log = open(jd / "live.log", "a")
    # Windows: own process group and a hidden console (its yt-dlp and ffmpeg children share it, so no windows pop up)
    detach = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW} if os.name == "nt"
              else {"start_new_session": True})
    p = subprocess.Popen([sys.executable, "-m", "clipper", "live-run", jd.name], cwd=Path(__file__).resolve().parents[1],
                         stdout=log, stderr=subprocess.STDOUT, **detach)
    try:
        created = psutil.Process(p.pid).create_time()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        created = None
    write_json(jd / "live.json", {"pid": p.pid, "started": time.time(),
                                  "create_time": created, "paused": False})
    return p.pid


def running(jd: Path) -> bool:
    info = read_json(jd / "live.json", {}) or {}
    pid = info.get("pid")
    if not pid or info.get("ended"):
        return False
    try:
        proc = psutil.Process(int(pid))
        if not proc.is_running() or proc.status() == psutil.STATUS_ZOMBIE:
            return False
        expected = info.get("create_time")
        if expected is not None and abs(proc.create_time() - float(expected)) > 2.0:
            return False
        return True
    except (psutil.NoSuchProcess, psutil.AccessDenied, TypeError, ValueError):
        return _alive(int(pid))


def paused(jd: Path) -> bool:
    info = read_json(jd / "live.json", {}) or {}
    return bool(info.get("paused")) and running(jd)


def _verified_worker(jd: Path):
    info = read_json(jd / "live.json", {}) or {}
    pid = info.get("pid")
    if not pid or not running(jd):
        raise RuntimeError("The live worker is not running.")
    proc = psutil.Process(int(pid))
    expected = info.get("create_time")
    if expected is not None and abs(proc.create_time() - float(expected)) > 2.0:
        raise RuntimeError("The saved worker PID no longer matches this Clipper job.")
    command = proc.cmdline()
    if "live-run" not in command or jd.name not in command:
        raise RuntimeError("Refusing to control a process that does not match this Clipper job.")
    return proc, info


def pause(jd: Path):
    """Suspend the verified watcher and its media/chat process tree."""
    if paused(jd):
        return False
    root, info = _verified_worker(jd)
    # Persist intent before suspending any process: a dashboard crash mid-pause can still be resumed.
    write_json(jd / "live.json", {**info, "paused": True, "pause_requested_at": time.time()})
    children = root.children(recursive=True)
    suspended = []
    try:
        for child in reversed(children):
            try:
                child.suspend()
                suspended.append(child)
            except psutil.NoSuchProcess:
                continue
        root.suspend()
        suspended.append(root)
    except Exception as exc:
        rollback_errors = []
        for proc in reversed(suspended):
            try:
                proc.resume()
            except psutil.NoSuchProcess:
                pass
            except psutil.AccessDenied as rollback_exc:
                rollback_errors.append(str(rollback_exc))
        if not rollback_errors:
            write_json(jd / "live.json", {**info, "paused": False})
        raise RuntimeError(
            f"Could not safely pause the live worker tree: {exc}"
            + (f"; rollback was incomplete: {rollback_errors[:2]}" if rollback_errors else "")
        ) from exc
    write_json(jd / "live.json", {**info, "paused": True, "paused_at": time.time()})
    return True


def resume(jd: Path):
    """Resume media/chat children before the watcher."""
    if not paused(jd):
        return False
    root, info = _verified_worker(jd)
    errors = []
    for child in reversed(root.children(recursive=True)):
        try:
            child.resume()
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied as exc:
            errors.append(str(exc))
    try:
        root.resume()
    except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
        errors.append(str(exc))
    if errors:
        raise RuntimeError("Could not resume every process in the live worker tree: " + "; ".join(errors[:3]))
    write_json(jd / "live.json", {**info, "paused": False, "resumed_at": time.time()})
    return True


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
    # A suspended watcher cannot process its stop file or SIGTERM.
    if paused(jd):
        resume(jd)
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
    try:
        slot_owner = _claim_live_slot(key)
    except RuntimeError as exc:
        set_status(jd, "error", 0, "", str(exc))
        raise

    rec = None
    try:
        reader.start()
        rec = Recorder(job["url"], jd / "buffer", jd / "record.log")
    except Exception as exc:
        stop_evt.set()
        if reader.is_alive():
            reader.join(timeout=5)
        if rec is not None:
            rec.close()
        _release_live_slot(slot_owner)
        set_status(jd, "error", 0, "", f"Could not start live capture: {exc}")
        raise

    deadline = t0 + float(job.get("hours") or 6) * 3600
    clips, why_stop = [], "Stopped"
    basis = job.get("basis") or ["chat"]
    listen = "audio" in basis or "speech" in basis
    scan, heard = None, set()
    try:
        write_json(jd / "live.json", {**(read_json(jd / "live.json", {}) or {}), "pid": os.getpid(),
                                      "create_time": psutil.Process(os.getpid()).create_time(), "t0": t0})
    except Exception as exc:
        stop_evt.set()
        rec.close()
        _release_live_slot(slot_owner)
        set_status(jd, "error", 0, "", f"Could not persist live worker state: {exc}")
        raise
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
                        if c["review"].get("status") == "reviewed" and c["review"].get("verdict") == "REJECT":
                            library.discard_rejected_clip(jd.name, c["id"])
                            clips.append({**c, "rejected": True})  # retain for deduplication; never republish
                        else:
                            clips.append(c)
                    else:
                        clips.append(c)
                except Exception as e:
                    (cd / "error.log").write_text(str(e))
                    clips.append({**c, "failed": str(e)})
                _publish(jd, meta, job, msgs, elapsed, clips, chat.error, scan)
            rec.prune(t0 + elapsed - BUFFER_S)
            n = len([c for c in clips if not c.get("failed") and not c.get("rejected")])
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
        n = len([c for c in clips if not c.get("failed") and not c.get("rejected")])
        set_status(jd, "done", 100, f"{why_stop}. {n} clip{'' if n == 1 else 's'} from {fmt_t(time.time() - t0)} of watching")
        _release_live_slot(slot_owner)


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
        if c.get("failed") or c.get("rejected"):
            continue
        prev = old.get(c["id"]) or {}
        good.append({**c, "what": prev.get("what") or c.get("what", ""), "why": prev.get("why") or c.get("why", "")})
    good.sort(key=lambda c: -c["score"])
    write_json(jd / "clips.json", {"url": job["url"], "platform": job["platform"], "id": job["id"], "live": True,
                                   "title": meta.get("title"), "channel": meta.get("channel"), "duration": round(elapsed),
                                   "messages": len(msgs), "avg_per_min": round(avg * 60 / spikes.BIN, 2),
                                   "bin": spikes.BIN, "basis": job.get("basis") or ["chat"],
                                   "min_score": job.get("min_score", 0), "clips": good})
