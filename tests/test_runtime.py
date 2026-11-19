import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from clipper import live
from clipper.runtime import classify_job_state


class RuntimeStateClassificationTests(unittest.TestCase):
    def test_live_process_is_running(self):
        self.assertEqual(classify_job_state("live", True, process_alive=True), "RUNNING")

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


if __name__ == "__main__":
    unittest.main()
