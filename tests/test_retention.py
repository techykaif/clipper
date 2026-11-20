import os
import tempfile
import time
import unittest
from pathlib import Path

from clipper import retention


class RetentionPolicyTests(unittest.TestCase):
    def test_plan_is_dry_run_and_only_includes_old_safe_temp_files(self):
        with tempfile.TemporaryDirectory() as folder:
            tmp = Path(folder) / "tmp"
            tmp.mkdir()
            old = tmp / "old.cache"
            fresh = tmp / "fresh.cache"
            unsafe = tmp / "old.mp4"
            for path in (old, fresh, unsafe):
                path.write_bytes(b"x")
            now = time.time()
            os.utime(old, (now - 20 * 86400, now - 20 * 86400))
            os.utime(unsafe, (now - 20 * 86400, now - 20 * 86400))
            plan = retention.plan_cleanup(tmp_days=14, logs_days=30, tmp_dir=tmp, jobs_dir=tmp / "jobs", now=now)
            self.assertEqual(plan["count"], 1)
            self.assertTrue(old.exists())
            self.assertEqual(plan["items"][0]["category"], "temporary cache")

    def test_old_logs_are_selected_but_clip_media_is_never_selected(self):
        with tempfile.TemporaryDirectory() as folder:
            jobs = Path(folder) / "jobs"
            job = jobs / "twitch-live-a"
            job.mkdir(parents=True)
            (job / "job.json").write_text('{"live": true}')
            (job / "live.json").write_text('{"ended": 100}')
            log = job / "live.log"
            media = job / "final.mp4"
            log.write_text("diagnostics")
            media.write_bytes(b"finished")
            old = time.time() - 90 * 86400
            os.utime(log, (old, old))
            os.utime(media, (old, old))
            plan = retention.plan_cleanup(tmp_dir=Path(folder) / "tmp", jobs_dir=jobs, now=time.time())
        self.assertEqual([Path(x["path"]).name for x in plan["items"]], ["live.log"])

    def test_active_live_job_logs_are_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            jobs = Path(folder) / "jobs"
            job = jobs / "live-a"
            job.mkdir(parents=True)
            (job / "job.json").write_text('{"live": true}')
            (job / "live.json").write_text('{"ended": null}')
            log = job / "live.log"
            log.write_text("active")
            old = time.time() - 90 * 86400
            os.utime(log, (old, old))
            plan = retention.plan_cleanup(tmp_dir=Path(folder) / "tmp", jobs_dir=jobs)
        self.assertEqual(plan["count"], 0)

    def test_apply_removes_only_paths_in_preview(self):
        with tempfile.TemporaryDirectory() as folder:
            tmp = Path(folder) / "tmp"
            tmp.mkdir()
            old = tmp / "cache.cache"
            old.write_bytes(b"old cache")
            plan = retention.plan_cleanup(tmp_days=0, logs_days=30, tmp_dir=tmp, jobs_dir=tmp / "jobs")
            result = retention.apply_cleanup(plan, tmp_dir=tmp, jobs_dir=tmp / "jobs")
        self.assertFalse(old.exists())
        self.assertEqual(len(result["deleted"]), 1)

    def test_invalid_ages_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError):
                retention.plan_cleanup(tmp_days=-1, tmp_dir=folder, jobs_dir=folder)


if __name__ == "__main__":
    unittest.main()
