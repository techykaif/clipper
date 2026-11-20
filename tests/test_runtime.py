import json
import os
import tempfile
import time
import threading
import unittest
import psutil
from pathlib import Path
from unittest.mock import Mock, patch

from clipper import live
from clipper.runtime import classify_job_state


class RuntimeStateClassificationTests(unittest.TestCase):
    def test_live_process_is_running(self):
        self.assertEqual(classify_job_state("live", True, process_alive=True, age_s=20), "RUNNING")

    def test_live_process_with_stale_heartbeat_is_stale(self):
        self.assertEqual(classify_job_state("live", True, process_alive=True, age_s=600), "STALE")

    def test_missing_live_process_is_stale(self):
        self.assertEqual(classify_job_state("live", True, process_alive=False), "STALE")

    def test_ended_live_job_is_stopped(self):
        self.assertEqual(classify_job_state("live", True, process_alive=False, ended=True), "STOPPED")

    def test_paused_live_job_is_paused(self):
        self.assertEqual(classify_job_state("live", True, process_alive=True, paused=True), "PAUSED")

    def test_recent_past_job_is_running(self):
        self.assertEqual(classify_job_state("download", False, age_s=20), "RUNNING")

    def test_old_past_job_is_stale(self):
        self.assertEqual(classify_job_state("download", False, age_s=600), "STALE")

    def test_error_takes_precedence(self):
        self.assertEqual(classify_job_state("error", True, process_alive=True), "ERROR")


class LiveWorkerSlotTests(unittest.TestCase):
    def test_only_one_live_worker_can_claim_slot(self):
        with tempfile.TemporaryDirectory() as folder:
            jobs = Path(folder)
            first = jobs / "live-a"
            second = jobs / "live-b"
            first.mkdir()
            second.mkdir()
            with patch.object(live, "JOBS", jobs), patch.object(live, "LIVE_LOCK", jobs / ".live-worker.lock"):
                owner = live._claim_live_slot(first.name)
                self.assertEqual(live._lock_owner()["job"], first.name)
                with self.assertRaises(RuntimeError):
                    live._claim_live_slot(second.name)
                live._release_live_slot(owner)
                next_owner = live._claim_live_slot(second.name)
                self.assertEqual(live._lock_owner()["job"], second.name)
                live._release_live_slot(next_owner)
                self.assertFalse((jobs / ".live-worker.lock").exists())

    def test_stale_lock_is_recovered(self):
        with tempfile.TemporaryDirectory() as folder:
            jobs = Path(folder)
            jobs.mkdir(exist_ok=True)
            lock = jobs / ".live-worker.lock"
            lock.write_text('{"pid": 999999999, "create_time": 1, "job": "dead"}', encoding="utf-8")
            stat = lock.stat()
            os.utime(lock, (stat.st_atime - 30, stat.st_mtime - 30))
            with patch.object(live, "JOBS", jobs), patch.object(live, "LIVE_LOCK", lock):
                owner = live._claim_live_slot("new-job")
                self.assertEqual(live._lock_owner()["job"], "new-job")
                live._release_live_slot(owner)

    def test_recent_partial_lock_fails_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            jobs = Path(folder)
            jobs.mkdir(exist_ok=True)
            lock = jobs / ".live-worker.lock"
            lock.write_text("{", encoding="utf-8")
            with patch.object(live, "JOBS", jobs), patch.object(live, "LIVE_LOCK", lock):
                with self.assertRaises(RuntimeError):
                    live._claim_live_slot("new-job")
                self.assertTrue(lock.exists())


class LiveWorkerLifecycleTests(unittest.TestCase):
    def test_start_process_reserves_slot_before_child_adopts_it(self):
        with tempfile.TemporaryDirectory() as folder:
            jobs = Path(folder)
            first, second = jobs / "live-a", jobs / "live-b"
            first.mkdir()
            second.mkdir()
            for jd in (first, second):
                (jd / "job.json").write_text(json.dumps({"live": True}), encoding="utf-8")
            with patch.object(live, "JOBS", jobs), patch.object(live, "LIVE_LOCK", jobs / ".live-worker.lock"):
                child = Mock()
                child.pid = os.getpid()
                with patch.object(live.subprocess, "Popen", return_value=child):
                    self.assertEqual(live.start_process(first), os.getpid())
                    adopted = live._claim_live_slot(first.name)
                    self.assertEqual(adopted["job"], first.name)
                    with self.assertRaises(RuntimeError):
                        live.start_process(second)
                    live._release_live_slot(adopted)
                    self.assertFalse((jobs / ".live-worker.lock").exists())

    def test_stop_marker_is_written_even_when_resume_fails(self):
        with tempfile.TemporaryDirectory() as folder:
            jd = Path(folder)
            (jd / "live.json").write_text(json.dumps({
                "pid": os.getpid(), "create_time": psutil.Process(os.getpid()).create_time(),
                "paused": True, "pause_state": "paused"
            }), encoding="utf-8")
            with patch.object(live, "JOBS", jd.parent), patch.object(live, "LIVE_LOCK", jd.parent / ".lock"), \
                 patch.object(live, "_resume_locked", side_effect=RuntimeError("resume denied")), \
                 patch.object(live.os, "killpg") as killpg, patch.object(live.os, "kill") as kill:
                with self.assertRaisesRegex(RuntimeError, "Stop was requested"):
                    live.stop(jd)
                self.assertTrue((jd / "stop").exists())
                killpg.assert_called_once_with(os.getpid(), live.signal.SIGCONT)
                kill.assert_called_once_with(os.getpid(), live.signal.SIGTERM)

    def test_unexpected_chat_reader_exit_marks_reader_unhealthy(self):
        chat = live.Chat(time.time())
        stop_evt = threading.Event()
        def exits():
            return None
        live._chat_reader_entry(exits, (), chat, stop_evt)
        self.assertFalse(chat.alive)
        self.assertIn("stopped unexpectedly", chat.error)

    def test_chat_reader_exception_is_visible(self):
        chat = live.Chat(time.time())
        stop_evt = threading.Event()
        def fails():
            raise RuntimeError("provider refused connection")
        live._chat_reader_entry(fails, (), chat, stop_evt)
        self.assertFalse(chat.alive)
        self.assertIn("provider refused connection", chat.error)

    def test_file_stop_request_is_visible_during_recovery(self):
        class StopEvent:
            def is_set(self):
                return False
        with tempfile.TemporaryDirectory() as folder:
            jd = Path(folder)
            (jd / "stop").touch()
            self.assertTrue(live._stop_requested(jd, StopEvent()))

    def test_heartbeat_is_created_on_worker_start(self):
        with tempfile.TemporaryDirectory() as folder:
            jd = Path(folder)
            stop_evt = threading.Event()
            stop_evt.set()
            live._heartbeat_loop(jd, stop_evt)
            self.assertTrue((jd / "heartbeat").is_file())


class YouTubeLiveChatReconnectTests(unittest.TestCase):
    def test_chat_reader_restarts_after_an_unexpected_process_exit(self):
        class StopEvent:
            def __init__(self):
                self.wait_count = 0
            def is_set(self):
                return self.wait_count >= 2
            def wait(self, _seconds):
                self.wait_count += 1
                return self.is_set()

        class FakeProcess:
            def __init__(self, exits):
                self.exits = exits
                self.terminated = False
            def poll(self):
                if self.exits:
                    return 1
                return 0 if self.terminated else None
            def terminate(self):
                self.terminated = True
            def wait(self, timeout=None):
                return 0
            def kill(self):
                self.terminated = True

        with tempfile.TemporaryDirectory() as folder:
            processes = [FakeProcess(True), FakeProcess(False)]
            stop_evt = StopEvent()
            chat = live.Chat(time.time())
            with patch.object(live.subprocess, "Popen", side_effect=processes) as popen:
                live.youtube_chat("https://youtube.com/watch?v=test", Path(folder), chat, stop_evt)
            self.assertEqual(popen.call_count, 2)
            self.assertTrue(chat.alive)
            self.assertTrue(processes[1].terminated)


class RecorderLifecycleTests(unittest.TestCase):
    def test_recorder_stall_detection_uses_latest_segment_write(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            seg_dir = root / "buffer"
            seg_dir.mkdir()
            recorder = live.Recorder.__new__(live.Recorder)
            recorder.dir = seg_dir
            recorder.started = 100.0
            fresh = seg_dir / "seg_101.ts"
            fresh.write_bytes(b"frame")
            now = time.time()
            self.assertFalse(recorder.stalled(now=now))
            os.utime(fresh, (now - live.RECORDER_STALL_S - 1, now - live.RECORDER_STALL_S - 1))
            self.assertTrue(recorder.stalled(now=now))

    def test_recorder_reconnect_stops_after_bounded_attempts(self):
        class StopEvent:
            def __init__(self):
                self.delays = []
            def is_set(self):
                return False
            def wait(self, delay):
                self.delays.append(delay)
                return False

        with tempfile.TemporaryDirectory() as folder:
            stop_evt = StopEvent()
            job_dir = Path(folder)
            job = {"url": "https://example.invalid/live"}
            with patch.object(live, "Recorder", side_effect=RuntimeError("offline")) as recorder:
                result, message = live._reconnect_recorder(job, job_dir, stop_evt)
            self.assertIsNone(result)
            self.assertIn("could not reconnect", message)
            self.assertEqual(stop_evt.delays, list(live.RECONNECT_DELAYS))
            self.assertEqual(recorder.call_count, len(live.RECONNECT_DELAYS))

    def test_recorder_reconnect_requires_a_new_nonempty_segment(self):
        class StopEvent:
            def is_set(self):
                return False
            def wait(self, _delay):
                return False

        class FakeRecorder:
            def __init__(self, segment):
                self.segment = segment
            def alive(self):
                return True
            def segments(self):
                return [(100, self.segment)]
            def close(self):
                pass

        with tempfile.TemporaryDirectory() as folder:
            job_dir = Path(folder)
            segment_dir = job_dir / "buffer"
            segment_dir.mkdir()
            new_segment = segment_dir / "seg_101.ts"
            recorder = FakeRecorder(new_segment)

            def start_recorder(*_args, **_kwargs):
                new_segment.write_bytes(b"video bytes")
                return recorder

            with patch.object(live, "Recorder", side_effect=start_recorder):
                result, message = live._reconnect_recorder(
                    {"url": "https://example.invalid/live"}, job_dir, StopEvent()
                )
            self.assertIs(result, recorder)
            self.assertIn("reconnected", message)

    def test_ffmpeg_start_failure_stops_yt_dlp_and_closes_pipe(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            stdout_pipe = Mock()
            downloader = Mock()
            downloader.stdout = stdout_pipe
            downloader.poll.return_value = None
            downloader.wait.return_value = 0
            with patch.object(live.subprocess, "Popen", side_effect=[downloader, OSError("ffmpeg failed")]):
                with self.assertRaises(OSError):
                    live.Recorder("https://example.invalid/live", root / "buffer", root / "record.log")
            downloader.terminate.assert_called_once()
            downloader.wait.assert_called_once_with(timeout=5)
            stdout_pipe.close.assert_called_once()

    def test_cut_timeout_removes_concat_file(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            segments = root / "segments"
            output = root / "clips" / "source.mp4"
            segments.mkdir()
            output.parent.mkdir()
            (segments / "seg_0.ts").write_bytes(b"segment")
            recorder = live.Recorder.__new__(live.Recorder)
            recorder.dir = segments
            with patch.object(live.subprocess, "run", side_effect=live.subprocess.TimeoutExpired("ffmpeg", 180)):
                with self.assertRaisesRegex(RuntimeError, "timed out"):
                    recorder.cut(0, 1, output)
            self.assertFalse((output.parent / "concat.txt").exists())



class YouTubeRecorderRecoveryPolicyTests(unittest.TestCase):
    def test_recorder_uses_long_lived_fragment_retries(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            downloader, ffmpeg = Mock(), Mock()
            downloader.stdout = Mock()
            downloader.poll.return_value = None
            ffmpeg.poll.return_value = None
            with patch.object(live.subprocess, "Popen", side_effect=[downloader, ffmpeg]) as popen:
                recorder = live.Recorder("https://youtube.com/watch?v=test", root / "buffer", root / "record.log")
                args = popen.call_args_list[0].args[0]
                self.assertIn("--retries", args)
                self.assertEqual(args[args.index("--retries") + 1], "infinite")
                self.assertIn("--fragment-retries", args)
                self.assertEqual(args[args.index("--fragment-retries") + 1], "infinite")
                self.assertIn("fragment:exp=1:30", args)
                recorder.close()

    def test_youtube_stays_in_recovery_when_provider_still_reports_live(self):
        class StopEvent:
            def __init__(self):
                self.delays = []
            def is_set(self):
                return False
            def wait(self, delay):
                self.delays.append(delay)
                return delay == live.YOUTUBE_RECONNECT_WAIT_S

        with tempfile.TemporaryDirectory() as folder:
            stop_evt = StopEvent()
            job_dir = Path(folder)
            job = {"platform": "youtube", "url": "https://youtube.com/watch?v=test"}
            with patch.object(live, "Recorder", side_effect=RuntimeError("temporary extractor failure")) as recorder, \
                 patch.object(live, "_youtube_live_state", return_value="live") as state:
                result, message = live._reconnect_recorder(job, job_dir, stop_evt)
            self.assertIsNone(result)
            self.assertEqual(message, "Stopped")
            self.assertEqual(recorder.call_count, len(live.RECONNECT_DELAYS))
            state.assert_called_once_with(job["url"])
            self.assertEqual(stop_evt.delays, [*live.RECONNECT_DELAYS, live.YOUTUBE_RECONNECT_WAIT_S])

    def test_youtube_ends_only_after_two_post_live_checks(self):
        class StopEvent:
            def __init__(self):
                self.delays = []
            def is_set(self):
                return False
            def wait(self, delay):
                self.delays.append(delay)
                return False

        with tempfile.TemporaryDirectory() as folder:
            stop_evt = StopEvent()
            job_dir = Path(folder)
            job = {"platform": "youtube", "url": "https://youtube.com/watch?v=test"}
            with patch.object(live, "Recorder", side_effect=RuntimeError("capture unavailable")) as recorder, \
                 patch.object(live, "_youtube_live_state", side_effect=["ended", "ended"]):
                result, message = live._reconnect_recorder(job, job_dir, stop_evt)
            self.assertIsNone(result)
            self.assertIn("confirmed twice", message)
            self.assertEqual(recorder.call_count, 2 * len(live.RECONNECT_DELAYS))
            self.assertEqual(stop_evt.delays, [*live.RECONNECT_DELAYS, live.YOUTUBE_RECONNECT_WAIT_S,
                                               *live.RECONNECT_DELAYS])


if __name__ == "__main__":
    unittest.main()
