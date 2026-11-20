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
import shutil
import signal
import subprocess
import threading
import psutil
import time
from collections import deque
from contextlib import contextmanager
from pathlib import Path

from . import ai_review, candidates, library, signals, spikes
from .common import JOBS, fmt_t, read_json, set_status, video_info, write_json

BUFFER_S = 600          # seconds of video kept on disk
SEG_S = 4               # recorder segment length
CHECK_S = 10            # how often spikes are looked for
SETTLE_S = 12           # wait this long after a clip's end so the recorder has written it
WARMUP_S = float(os.environ.get("CLIPPER_LIVE_WARMUP", 120))   # no clips before this much chat history
SORT = "res,fps,vbr"
RECONNECT_DELAYS = (2, 4, 8)
RECORDER_READY_S = 10.0
RECORDER_STALL_S = 30.0
YT_CHAT_RECONNECT_S = 3
YT_CHAT_SEEN_LIMIT = 50000
YOUTUBE_RECONNECT_WAIT_S = 30
YOUTUBE_END_STATES = {"was_live", "post_live"}

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
    """Claim the slot, or adopt the exact reservation created for this watcher by start_process()."""
    JOBS.mkdir(parents=True, exist_ok=True)
    current_pid = os.getpid()
    current_created = psutil.Process(current_pid).create_time()
    with _slot_guard():
        if LIVE_LOCK.exists():
            owner = _lock_owner()
            if (owner and owner.get("pid") == current_pid and owner.get("job") == str(key)
                    and owner.get("create_time") is not None
                    and abs(float(owner["create_time"]) - current_created) <= 2.0):
                return owner
        _assert_live_slot_available()
        payload = {"pid": current_pid, "create_time": current_created,
                   "job": str(key), "started": time.time()}
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
    """Launch the detached watcher and reserve its slot before another launcher can race it."""
    import sys

    process = None
    owner = None
    detach = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW} if os.name == "nt"
              else {"start_new_session": True})
    try:
        with _slot_guard():
            _assert_live_slot_available()
            with (jd / "live.log").open("a", encoding="utf-8") as log:
                process = subprocess.Popen(
                    [sys.executable, "-m", "clipper", "live-run", jd.name],
                    cwd=Path(__file__).resolve().parents[1],
                    stdout=log, stderr=subprocess.STDOUT, **detach,
                )
            try:
                created = psutil.Process(process.pid).create_time()
            except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
                raise RuntimeError("Could not verify the newly launched live worker process.") from exc
            owner = {"pid": int(process.pid), "create_time": created, "job": jd.name, "started": time.time()}
            fd = os.open(str(LIVE_LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as out:
                    json.dump(owner, out)
                    out.flush()
                    os.fsync(out.fileno())
            except Exception:
                LIVE_LOCK.unlink(missing_ok=True)
                raise
            write_json(jd / "live.json", {**owner, "paused": False, "pause_state": "running",
                                          "ready": False})
        return int(process.pid)
    except Exception:
        if owner is not None:
            _release_live_slot(owner)
        if process is not None:
            Recorder._stop_process(process)
        raise


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
        if expected is None or abs(proc.create_time() - float(expected)) > 2.0:
            return False
        return True
    except (psutil.NoSuchProcess, psutil.AccessDenied, TypeError, ValueError):
        return False


def paused(jd: Path) -> bool:
    info = read_json(jd / "live.json", {}) or {}
    state = info.get("pause_state")
    is_paused = state == "paused" if state is not None else bool(info.get("paused"))
    return is_paused and running(jd)


def _matches_worker_command(command, jd: Path) -> bool:
    """Recognize both detached dashboard workers and the documented foreground CLI mode."""
    if "live-run" in command and jd.name in command:
        return True
    job = read_json(jd / "job.json", {}) or {}
    source_id = str(job.get("id") or "")
    return bool("live" in command and source_id
                and any(source_id in str(part) for part in command))


def _verified_worker(jd: Path):
    info = read_json(jd / "live.json", {}) or {}
    pid = info.get("pid")
    if not pid or not running(jd):
        raise RuntimeError("The live worker is not running.")
    proc = psutil.Process(int(pid))
    expected = info.get("create_time")
    if expected is None or abs(proc.create_time() - float(expected)) > 2.0:
        raise RuntimeError("The saved worker PID no longer matches this Clipper job.")
    command = proc.cmdline()
    if not _matches_worker_command(command, jd):
        raise RuntimeError("Refusing to control a process that does not match this Clipper job.")
    return proc, info


def _pause_state(info):
    return info.get("pause_state") or ("paused" if info.get("paused") else "running")


def _pause_locked(jd: Path):
    if (jd / "stop").exists():
        raise RuntimeError("This live job is already stopping; it cannot be paused.")
    root, info = _verified_worker(jd)
    if _pause_state(info) == "paused":
        return False
    write_json(jd / "live.json", {**info, "paused": False, "pause_state": "requested",
                                  "pause_requested_at": time.time()})
    suspended = []
    try:
        # Freeze the watcher before enumerating descendants so it cannot spawn a new
        # recorder/chat process after the snapshot has been taken.
        root.suspend()
        suspended.append(root)
        children = root.children(recursive=True)
        for child in reversed(children):
            try:
                child.suspend()
                suspended.append(child)
            except psutil.NoSuchProcess:
                continue
    except Exception as exc:
        rollback_errors = []
        for proc in reversed(suspended):
            try:
                proc.resume()
            except psutil.NoSuchProcess:
                pass
            except psutil.AccessDenied as rollback_exc:
                rollback_errors.append(str(rollback_exc))
        state = "requested" if rollback_errors else "running"
        write_json(jd / "live.json", {**info, "paused": False, "pause_state": state})
        raise RuntimeError(
            f"Could not safely pause the live worker tree: {exc}"
            + (f"; pause recovery is required: {rollback_errors[:2]}" if rollback_errors else "")
        ) from exc
    write_json(jd / "live.json", {**info, "paused": True, "pause_state": "paused",
                                  "paused_at": time.time()})
    return True


def pause(jd: Path):
    with _slot_guard():
        return _pause_locked(jd)


def _resume_locked(jd: Path):
    info = read_json(jd / "live.json", {}) or {}
    if _pause_state(info) == "running":
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
    except psutil.NoSuchProcess:
        pass
    except psutil.AccessDenied as exc:
        errors.append(str(exc))
    if errors:
        raise RuntimeError("Could not resume every process in the live worker tree: " + "; ".join(errors[:3]))
    write_json(jd / "live.json", {**info, "paused": False, "pause_state": "running",
                                  "resumed_at": time.time()})
    return True


def resume(jd: Path):
    with _slot_guard():
        return _resume_locked(jd)


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


def _terminate_worker_tree(root, timeout=5):
    """Best-effort tree termination for a verified worker when cooperative resume fails."""
    processes = []
    try:
        processes.extend(reversed(root.children(recursive=True)))
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass
    processes.append(root)
    for proc in processes:
        try:
            proc.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    _, alive = psutil.wait_procs(processes, timeout=timeout)
    for proc in alive:
        try:
            proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    if alive:
        psutil.wait_procs(alive, timeout=timeout)


def stop(jd: Path):
    resume_error = None
    with _slot_guard():
        (jd / "stop").touch()
        info = read_json(jd / "live.json", {}) or {}
        try:
            if _pause_state(info) in ("requested", "paused"):
                _resume_locked(jd)
        except Exception as exc:
            resume_error = exc
    try:
        current_info = read_json(jd / "live.json", {}) or {}
        if current_info.get("ready") is not True:
            raise RuntimeError("Live worker has not completed startup.")
        root, verified_info = _verified_worker(jd)
    except Exception:
        # A stale/reused/not-yet-ready PID is never a target for Stop. The marker remains
        # for the child startup handshake to observe before recording begins.
        root, verified_info = None, {}

    if root is not None and resume_error:
        # A partial resume may leave descendants suspended. Force a verified tree
        # shutdown rather than relying on the watcher to execute its stop handler.
        _terminate_worker_tree(root)
    elif root is not None and os.name != "nt":
        # Use the verified psutil.Process object so PID identity is rechecked by psutil.
        try:
            root.send_signal(signal.SIGCONT)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
        try:
            root.send_signal(signal.SIGTERM)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    if resume_error:
        action = ("the verified worker tree was terminated" if root is not None
                  else "the stop marker was persisted but no verified process signal could be sent")
        raise RuntimeError(
            f"Stop was requested; {action}, but the paused worker tree "
            f"could not be fully resumed: {resume_error}"
        ) from resume_error


# ---------------- chat readers ----------------
class Chat:
    def __init__(self, t0):
        self.t0, self.msgs, self.lock, self.error, self.alive = t0, [], threading.Lock(), "", True

    def add(self, wall, user, text):
        t = wall - self.t0
        if t < 0:
            return
        with self.lock:
            self.msgs.append({"t": round(t, 2), "user": user, "text": text})

    def snapshot(self):
        with self.lock:
            return sorted(self.msgs, key=lambda m: m["t"])


def _chat_reader_entry(target, args, chat: Chat, stop_evt):
    try:
        target(*args)
    except Exception as exc:
        with chat.lock:
            chat.error = f"Chat reader failed: {type(exc).__name__}: {exc}"
    finally:
        if not stop_evt.is_set():
            with chat.lock:
                chat.alive = False
                if not chat.error:
                    chat.error = "Chat reader stopped unexpectedly"


def _heartbeat_loop(jd: Path, stop_evt):
    heartbeat = jd / "heartbeat"
    try:
        heartbeat.touch()
    except OSError:
        pass
    while not stop_evt.wait(15):
        try:
            heartbeat.touch()
        except OSError:
            pass


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
    """Follow YouTube live chat and reconnect if yt-dlp's chat reader exits."""
    out = jd / "yt_live"
    seen_ids, seen_order = set(), deque()
    while not stop_evt.is_set():
        for stale in jd.glob("yt_live*"):
            stale.unlink(missing_ok=True)
        pos, buf, proc = 0, "", None
        try:
            with open(jd / "chat.log", "a", encoding="utf-8") as errlog:
                proc = subprocess.Popen(
                    ["yt-dlp", "--no-warnings", "--skip-download", "--write-subs", "--sub-langs", "live_chat",
                     "-o", str(out), url],
                    stdout=subprocess.DEVNULL, stderr=errlog, start_new_session=True,
                )
            chat.error = ""
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
                            data = json.loads(line)
                        except ValueError:
                            continue
                        for action in (data.get("replayChatItemAction") or {}).get("actions", []):
                            item = (action.get("addChatItemAction") or {}).get("item") or {}
                            message = item.get("liveChatTextMessageRenderer") or item.get("liveChatPaidMessageRenderer")
                            if not message or not message.get("timestampUsec"):
                                continue
                            message_id = message.get("id")
                            if message_id:
                                if message_id in seen_ids:
                                    continue
                                seen_ids.add(message_id)
                                seen_order.append(message_id)
                                if len(seen_order) > YT_CHAT_SEEN_LIMIT:
                                    seen_ids.discard(seen_order.popleft())
                            body = "".join(
                                run.get("text") or ((run.get("emoji") or {}).get("shortcuts") or [""])[0]
                                for run in (message.get("message") or {}).get("runs", [])
                            )
                            chat.add(
                                int(message["timestampUsec"]) / 1e6,
                                (message.get("authorName") or {}).get("simpleText", ""),
                                body,
                            )
                if proc.poll() is not None:
                    chat.error = "YouTube live chat disconnected; reconnecting"
                    break
                stop_evt.wait(2)
        except Exception as exc:
            chat.error = f"YouTube live chat error; reconnecting: {exc}"
        finally:
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass
        if stop_evt.wait(YT_CHAT_RECONNECT_S):
            break
    # Keep chat.alive true while video capture is running; the watcher owns the final stop decision.


# ---------------- recorder ----------------
class Recorder:
    """yt-dlp (best resolution, then highest frame rate) piped into ffmpeg, cut into SEG_S-second .ts files named
    by wall-clock start time."""

    def __init__(self, url, seg_dir: Path, log: Path):
        self.dir = seg_dir
        self.started = time.time()
        self.dl = None
        self.ff = None
        seg_dir.mkdir(parents=True, exist_ok=True)
        lf = open(log, "w")
        try:
            self.dl = subprocess.Popen(
                ["yt-dlp", "--no-warnings", "--no-part", "--socket-timeout", "30",
                 "--retries", "infinite", "--fragment-retries", "infinite",
                 "--retry-sleep", "http:exp=1:30", "--retry-sleep", "fragment:exp=1:30",
                 "-f", "b", "-S", SORT, "-o", "-", url],
                stdout=subprocess.PIPE, stderr=lf, start_new_session=True,
            )
            try:
                self.ff = subprocess.Popen(
                    ["ffmpeg", "-v", "error", "-i", "pipe:0", "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
                     "-f", "segment", "-segment_time", str(SEG_S), "-reset_timestamps", "1",
                     "-strftime", "1", str(seg_dir / "seg_%s.ts")],
                    stdin=self.dl.stdout, stdout=subprocess.DEVNULL, stderr=lf, start_new_session=True,
                )
            finally:
                if self.dl.stdout:
                    self.dl.stdout.close()
        except Exception:
            self._stop_process(self.ff)
            self._stop_process(self.dl)
            raise
        finally:
            lf.close()

    @staticmethod
    def _stop_process(proc):
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.terminate()
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        except OSError:
            pass

    def alive(self):
        return self.ff is not None and self.dl is not None and self.ff.poll() is None and self.dl.poll() is None

    def stalled(self, now=None):
        """Detect a live process tree that has stopped writing its rolling buffer."""
        now = time.time() if now is None else float(now)
        segs = self.segments()
        if not segs:
            return now - self.started > RECORDER_STALL_S
        try:
            last_write = segs[-1][1].stat().st_mtime
        except OSError:
            return True
        return now - last_write > RECORDER_STALL_S

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
        try:
            p = subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
                                "-ss", f"{off:.2f}", "-t", f"{wall_b - wall_a:.2f}",
                                "-c:v", "libx264", "-crf", "14", "-preset", "medium", "-c:a", "aac", "-b:a", "320k",
                                "-movflags", "+faststart", str(dest)], capture_output=True, text=True, timeout=180)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("ffmpeg timed out while cutting the clip (180-second limit).") from exc
        finally:
            lst.unlink(missing_ok=True)
        if p.returncode != 0 or not dest.exists():
            raise RuntimeError("ffmpeg couldn't cut the clip: " + p.stderr.strip()[-300:])
        return dest

    def close(self):
        # Stop FFmpeg first so the pipe closes before yt-dlp is asked to exit.
        self._stop_process(self.ff)
        self._stop_process(self.dl)


def _youtube_live_state(url):
    """Return live/ended/unknown; extraction errors are inconclusive, never proof that a broadcast ended."""
    try:
        info = _ytdlp_json(url, timeout=30)
    except Exception:
        return "unknown"
    status = str(info.get("live_status") or "").lower()
    if status == "is_live" or info.get("is_live") is True:
        return "live"
    if status in YOUTUBE_END_STATES:
        return "ended"
    return "unknown"


def _stop_requested(jd: Path, stop_evt) -> bool:
    """Observe both the in-process signal and the file-based Windows stop request."""
    return stop_evt.is_set() or (jd / "stop").exists()


def _prepare_live_worker(jd: Path, slot_owner, stop_evt) -> bool:
    """Atomically publish readiness or finalize a stop requested during process launch."""
    with _slot_guard():
        info = read_json(jd / "live.json", {}) or {}
        if _stop_requested(jd, stop_evt):
            try:
                write_json(jd / "live.json", {**info, "ended": time.time(), "ready": False,
                                              "paused": False, "pause_state": "stopped"})
                set_status(jd, "done", 100, "Stopped before live capture started")
            finally:
                _release_live_slot(slot_owner)
            return False
        write_json(jd / "live.json", {**info, "ready": True, "ready_at": time.time()})
        return True


def _finish_stopped_live_start(jd: Path, slot_owner, stop_evt, reader):
    """Finish an early stop after readiness without creating a recorder."""
    stop_evt.set()
    if reader.is_alive():
        reader.join(timeout=15)
    cleanup_error = "Chat reader did not stop within 15 seconds." if reader.is_alive() else ""
    try:
        info = read_json(jd / "live.json", {}) or {}
        write_json(jd / "live.json", {**info, "ended": time.time(), "ready": False,
                                      "paused": False, "pause_state": "stopped"})
        if cleanup_error:
            set_status(jd, "error", 0, "", cleanup_error)
        else:
            set_status(jd, "done", 100, "Stopped before video recording started")
    finally:
        _release_live_slot(slot_owner)


def _reconnect_recorder(job, jd: Path, stop_evt, deadline=None):
    """Reconnect only after a new segment is observed.

    YouTube is special: its live extractor can temporarily stop producing fragments while the broadcast
    remains active. Keep retrying until the user/time limit stops the job or YouTube reports a post-live
    state twice. A failed metadata request is inconclusive and must not be presented as 'live ended'.
    """
    errors = []
    segment_dir = jd / "buffer"
    cycle = 0
    ended_checks = 0
    youtube = job.get("platform") == "youtube"

    while not _stop_requested(jd, stop_evt):
        if deadline is not None and time.time() >= deadline:
            return None, f"Stopped after {job.get('hours')} hours"
        cycle += 1
        for attempt, delay in enumerate(RECONNECT_DELAYS, start=1):
            if deadline is not None and time.time() >= deadline:
                return None, f"Stopped after {job.get('hours')} hours"
            set_status(jd, "live", 0,
                       f"Video capture disconnected; retry cycle {cycle}, attempt {attempt}/{len(RECONNECT_DELAYS)} after {delay}s")
            if stop_evt.wait(delay) or _stop_requested(jd, stop_evt):
                return None, "Stopped"
            existing = {f.name for f in segment_dir.glob("seg_*.ts")}
            candidate = None
            try:
                candidate = Recorder(job["url"], segment_dir, jd / "record.log")
                ready_until = time.monotonic() + RECORDER_READY_S
                while candidate.alive() and time.monotonic() < ready_until:
                    for _, segment in candidate.segments():
                        try:
                            if segment.name not in existing and segment.stat().st_size > 0:
                                return candidate, f"Video recorder reconnected on cycle {cycle}, attempt {attempt}."
                        except OSError:
                            continue
                    if stop_evt.wait(0.25) or _stop_requested(jd, stop_evt):
                        candidate.close()
                        return None, "Stopped"
                    if deadline is not None and time.time() >= deadline:
                        candidate.close()
                        return None, f"Stopped after {job.get('hours')} hours"
                candidate.close()
                errors.append("no new non-empty video segment appeared after restart")
            except Exception as exc:
                if candidate is not None:
                    candidate.close()
                errors.append(str(exc)[:300])

        detail = errors[-1] if errors else "no retry was available"
        if not youtube:
            return None, f"Video capture stopped and could not reconnect after {cycle * len(RECONNECT_DELAYS)} attempts: {detail}"

        state = _youtube_live_state(job["url"])
        if state == "ended":
            ended_checks += 1
            if ended_checks >= 2:
                return None, "YouTube reports that this live broadcast has ended (confirmed twice)."
            message = "YouTube reported a post-live state once; checking again before ending the watcher"
        else:
            ended_checks = 0
            if state == "live":
                message = "YouTube still reports this broadcast as live; continuing video recovery"
            else:
                message = "YouTube could not confirm that the broadcast ended; keeping the watcher alive"

        set_status(jd, "live", 0, f"{message}. Retrying video capture in {YOUTUBE_RECONNECT_WAIT_S}s. Last error: {detail}")
        wait_s = YOUTUBE_RECONNECT_WAIT_S
        if deadline is not None:
            wait_s = max(0.0, min(wait_s, deadline - time.time()))
        if stop_evt.wait(wait_s) or _stop_requested(jd, stop_evt):
            return None, "Stopped"

    return None, "Stopped"


# ---------------- the watcher ----------------
def run(key):
    from .pipeline import prepare_clip
    jd = JOBS / key
    job = read_json(jd / "job.json")
    meta = read_json(jd / "meta.json", {}) or {}
    previous = read_json(jd / "clips.json", {}) or {}
    clips = list(previous.get("clips", []) or [])
    first_id = max((int(c.get("id") or 0) for c in clips), default=0) + 1
    t0 = time.time()
    stop_evt = threading.Event()
    signal.signal(signal.SIGTERM, lambda *a: stop_evt.set())
    chat = Chat(t0)
    if job["platform"] == "twitch":
        reader_target, reader_args = twitch_chat, (job["id"], chat, stop_evt)
    elif job["platform"] == "kick":
        from .chat import kick
        reader_target, reader_args = kick.live, (job["id"], chat.add, stop_evt,
                                                  lambda e: setattr(chat, "error", e))
    else:
        reader_target, reader_args = youtube_chat, (job["url"], jd, chat, stop_evt)
    reader = threading.Thread(target=_chat_reader_entry, args=(reader_target, reader_args, chat, stop_evt),
                              daemon=True)
    try:
        slot_owner = _claim_live_slot(key)
    except RuntimeError as exc:
        set_status(jd, "error", 0, "", str(exc))
        raise

    # Mutate persistent candidate state only after this process owns the one-live slot.
    try:
        candidate_queue = candidates.CandidateQueue(jd, first_id=first_id)
        candidate_queue.seed(clips)
    except Exception as exc:
        _release_live_slot(slot_owner)
        set_status(jd, "error", 0, "", f"Could not initialize candidate queue: {exc}")
        raise

    try:
        info = read_json(jd / "live.json", {}) or {}
        write_json(jd / "live.json", {**info, "pid": os.getpid(),
                                      "create_time": psutil.Process(os.getpid()).create_time(),
                                      "t0": t0, "ready": False, "paused": False,
                                      "pause_state": "running"})
    except Exception as exc:
        _release_live_slot(slot_owner)
        set_status(jd, "error", 0, "", f"Could not persist live worker identity: {exc}")
        raise

    # Do not start chat or video capture until an early Stop request has been checked.
    if not _prepare_live_worker(jd, slot_owner, stop_evt):
        return

    rec = None
    try:
        reader.start()
        if _stop_requested(jd, stop_evt):
            _finish_stopped_live_start(jd, slot_owner, stop_evt, reader)
            return
        rec = Recorder(job["url"], jd / "buffer", jd / "record.log")
    except Exception as exc:
        stop_evt.set()
        if reader.is_alive():
            reader.join(timeout=15)
        if rec is not None:
            rec.close()
        _release_live_slot(slot_owner)
        set_status(jd, "error", 0, "", f"Could not start live capture: {exc}")
        raise

    deadline = t0 + float(job.get("hours") or 6) * 3600
    why_stop = "Stopped"
    basis = job.get("basis") or ["chat"]
    listen = "audio" in basis or "speech" in basis
    scan, heard = None, set()
    try:
        (jd / "heartbeat").touch()
        write_json(jd / "live.json", {**(read_json(jd / "live.json", {}) or {}),
                                      "heartbeat_enabled": True})
    except Exception as exc:
        stop_evt.set()
        rec.close()
        if reader.is_alive():
            reader.join(timeout=15)
        if reader.is_alive():
            chat.error = "Chat reader did not stop after worker-state persistence failed."
        _release_live_slot(slot_owner)
        set_status(jd, "error", 0, "", f"Could not persist live worker state: {exc}")
        raise
    heartbeat_stop = threading.Event()
    heartbeat_thread = threading.Thread(target=_heartbeat_loop, args=(jd, heartbeat_stop), daemon=True)
    heartbeat_thread.start()
    try:
        while True:
            if (jd / "stop").exists() or stop_evt.wait(CHECK_S):
                why_stop = "Stopped"
                break
            if time.time() > deadline:
                why_stop = f"Stopped after {job.get('hours')} hours"
                break
            if not rec.alive() or rec.stalled():
                rec.close()
                rec, reconnect_status = _reconnect_recorder(job, jd, stop_evt, deadline=deadline)
                if rec is None:
                    why_stop = reconnect_status
                    break
                set_status(jd, "live", 0, reconnect_status)
                continue
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
            _publish(jd, meta, job, msgs, elapsed, clips, chat.error, scan, candidate_queue)
            if elapsed < WARMUP_S or ("chat" in basis and len(msgs) < 10) or (listen and not scan):
                set_status(jd, "live", 0, f"Watching for {fmt_t(elapsed)}. Learning this chat's normal pace before "
                                          f"clipping ({len(msgs)} messages so far)")
                continue
            res = signals.find(basis, elapsed, msgs=msgs, db=scan.db if scan else None,
                               words=scan.words if scan else None, keywords=job.get("keywords", ""), top_n=200,
                               min_score=job.get("min_score", 0))
            eligible = [
                c for c in res.get("clips", [])
                if c["end"] + SETTLE_S <= elapsed and c["start"] >= elapsed - BUFFER_S + 30
            ]
            candidate_queue.discover(
                eligible, dedupe_window=spikes.DEDUPE, minimum_score=job.get("min_score", 0)
            )
            # Only this worker's newly queued items are cut; interrupted work is preserved for review.
            for c in candidate_queue.pending():
                c["vod_time"] = fmt_t(c["start"])
                c["what"], c["why"] = "", ""
                candidate_queue.mark(c["id"], "processing")
                cd = jd / "clips" / str(c["id"])
                cd.mkdir(parents=True, exist_ok=True)
                set_status(jd, "live", 0, f"Cutting candidate {c['id']} (signal score {c['score']} at {c['vod_time']})")
                try:
                    src = rec.cut(t0 + c["start"], t0 + c["end"], cd / "source.mp4")
                    info = video_info(src)
                    visual = candidates.inspect_video(src)
                    c.update(candidates.apply_visual_quality(c, visual))
                    candidate_queue.mark(
                        c["id"], "processing", score=c["score"], signal_score=c.get("signal_score"),
                        quality_adjustment=c.get("quality_adjustment", 0),
                        quality_flags=c.get("quality_flags", []), visual_quality=visual,
                        auto_reason=c.get("auto_reason", ""),
                    )
                    if c["score"] < int(job.get("min_score", 0)):
                        candidate_queue.mark(
                            c["id"], "filtered", score=c["score"], signal_score=c.get("signal_score"),
                            quality_adjustment=c.get("quality_adjustment", 0),
                            quality_flags=c.get("quality_flags", []), visual_quality=visual,
                            filtered_reason=f"Visual quality adjustment lowered the score below {job.get('min_score')}.",
                        )
                        shutil.rmtree(cd, ignore_errors=True)
                        continue
                    c["end"] = round(c["start"] + info["duration"], 2)
                    prepare_clip(cd, c, src, info, visual_quality=visual)
                    if job.get("antigravity_review"):
                        set_status(jd, "review", 0, f"Antigravity reviewing candidate {c['id']}")
                        c["review"] = ai_review.review_clip(jd, c)
                        if c["review"].get("status") == "reviewed" and c["review"].get("verdict") == "REJECT":
                            library.discard_rejected_clip(jd.name, c["id"])
                            candidate_queue.mark(c["id"], "rejected", review_verdict="REJECT",
                                                 score=c["score"], quality_flags=c.get("quality_flags", []))
                            clips.append({**c, "rejected": True})  # retain for deduplication; never republish
                        else:
                            state = ("needs_review" if c["review"].get("verdict") == "REVIEW"
                                     or c["review"].get("status") == "needs_review" else "ready")
                            candidate_queue.mark(c["id"], state, review_verdict=c["review"].get("verdict"),
                                                 score=c["score"], quality_flags=c.get("quality_flags", []))
                            clips.append(c)
                    else:
                        candidate_queue.mark(c["id"], "ready", score=c["score"],
                                             quality_flags=c.get("quality_flags", []))
                        clips.append(c)
                except Exception as e:
                    error = str(e)[:1000]
                    (cd / "error.log").write_text(error, encoding="utf-8")
                    candidate_queue.mark(c["id"], "failed", error=error)
                    clips.append({**c, "failed": error})
                _publish(jd, meta, job, msgs, elapsed, clips, chat.error, scan, candidate_queue)
            rec.prune(t0 + elapsed - BUFFER_S)
            n = len([c for c in clips if not c.get("failed") and not c.get("rejected")])
            set_status(jd, "live", 0, f"Watching for {fmt_t(elapsed)}. {n} clip{'' if n == 1 else 's'} so far"
                                      + (f". {chat.error}" if chat.error else ""))
    finally:
        try:
            stop_evt.set()
            heartbeat_stop.set()
            heartbeat_thread.join(timeout=2)
            if rec is not None:
                rec.close()
            if reader.is_alive():
                reader.join(timeout=15)
            if reader.is_alive():
                chat.error = (chat.error + "; " if chat.error else "") + "Chat reader did not stop within 15 seconds."
            chat.alive = False
            msgs = chat.snapshot()
            _publish(jd, meta, job, msgs, time.time() - t0, clips, chat.error, scan, candidate_queue)
            for f in (jd / "buffer").glob("*"):
                f.unlink(missing_ok=True)
            try:
                (jd / "buffer").rmdir()
            except OSError:
                pass
            for f in jd.glob("yt_live*"):
                f.unlink(missing_ok=True)
            info = read_json(jd / "live.json", {}) or {}
            write_json(jd / "live.json", {**info, "ended": time.time(), "ready": False,
                                          "paused": False, "pause_state": "stopped"})
            n = len([c for c in clips if not c.get("failed") and not c.get("rejected")])
            set_status(jd, "done", 100, f"{why_stop}. {n} clip{'' if n == 1 else 's'} from {fmt_t(time.time() - t0)} of watching")
        except Exception as cleanup_exc:
            # Cleanup errors must not strand the single-live-source lock.
            try:
                set_status(jd, "error", 0, "", f"Live worker cleanup failed: {cleanup_exc}")
            except Exception:
                pass
            try:
                info = read_json(jd / "live.json", {}) or {}
                write_json(jd / "live.json", {**info, "ended": time.time(), "ready": False,
                                              "paused": False, "pause_state": "stopped",
                                              "cleanup_error": str(cleanup_exc)[:1000]})
            except Exception:
                pass
        finally:
            _release_live_slot(slot_owner)


def _publish(jd, meta, job, msgs, elapsed, clips, note, scan=None, candidate_queue=None):
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
                                   "min_score": job.get("min_score", 0), "clips": good,
                                   "candidate_queue": candidate_queue.counts() if candidate_queue is not None else {}})
