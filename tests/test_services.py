import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import psutil

from clipper import live, render, runtime, services


class ProcessAlive:
    def __init__(self, created=100.0):
        self.created = created

    def is_running(self):
        return True

    def status(self):
        return psutil.STATUS_RUNNING

    def create_time(self):
        return self.created


class RuntimeServiceRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.pid = 12345
        self.created = 100.0

    def tearDown(self):
        self.temp.cleanup()

    def register(self, state="starting", now=10.0):
        return services.register(
            "live:job-a", "live-worker", "job-a", self.pid, self.created,
            state=state, jobs_dir=self.root, now=now,
        )

    def test_register_persists_identity_and_preserves_original_start_time(self):
        first = self.register(now=10.0)
        second = services.register(
            "live:job-a", "live-worker", "job-a", self.pid, self.created,
            state="starting", metadata={"platform": "twitch"},
            jobs_dir=self.root, now=20.0,
        )
        payload = json.loads((self.root / services.REGISTRY_FILE).read_text(encoding="utf-8"))
        record = payload["services"]["live:job-a"]
        self.assertEqual(first["started_at"], 10.0)
        self.assertEqual(second["started_at"], 10.0)
        self.assertEqual(record["metadata"]["platform"], "twitch")
        self.assertEqual(record["pid"], self.pid)

    def test_update_refuses_to_modify_a_reused_pid(self):
        self.register()
        with self.assertRaisesRegex(services.RuntimeRegistryError, "identity changed"):
            services.update("live:job-a", self.pid, self.created - 50,
                            state="stopping", jobs_dir=self.root)

    def test_stale_running_service_recovers_after_a_fresh_heartbeat(self):
        self.register(state="running", now=10.0)
        with patch.object(services.psutil, "Process", return_value=ProcessAlive(self.created)):
            stale = services.snapshot(jobs_dir=self.root, now=250.0)
            self.assertEqual(stale["services"]["live:job-a"]["state"], "stale")
            resumed = services.heartbeat(
                "live:job-a", self.pid, self.created, jobs_dir=self.root, now=251.0
            )
            self.assertEqual(resumed["state"], "running")
            fresh = services.snapshot(jobs_dir=self.root, now=252.0)
        self.assertEqual(fresh["services"]["live:job-a"]["state"], "running")

    def test_paused_service_is_not_misclassified_by_suspended_heartbeat(self):
        self.register(state="paused", now=10.0)
        with patch.object(services.psutil, "Process", return_value=ProcessAlive(self.created)):
            data = services.snapshot(jobs_dir=self.root, now=10_000.0)
        self.assertEqual(data["services"]["live:job-a"]["state"], "paused")

    def test_dead_process_is_classified_as_crashed(self):
        self.register(state="running", now=10.0)
        with patch.object(services.psutil, "Process", side_effect=psutil.NoSuchProcess(self.pid)):
            data = services.snapshot(jobs_dir=self.root, now=20.0)
        self.assertEqual(data["services"]["live:job-a"]["state"], "crashed")
        self.assertIn("exited", data["services"]["live:job-a"]["message"])

    def test_pid_reuse_is_classified_as_crashed(self):
        self.register(state="running", now=10.0)
        with patch.object(services.psutil, "Process", return_value=ProcessAlive(self.created + 100)):
            data = services.snapshot(jobs_dir=self.root, now=20.0)
        self.assertEqual(data["services"]["live:job-a"]["state"], "crashed")

    def test_access_denied_is_unverified_not_a_false_crash(self):
        self.register(state="running", now=10.0)
        with patch.object(services.psutil, "Process", side_effect=psutil.AccessDenied(self.pid)):
            data = services.snapshot(jobs_dir=self.root, now=500.0)
        self.assertEqual(data["services"]["live:job-a"]["state"], "unverified")
        self.assertEqual(data["counts"]["crashed"], 0)

    def test_process_exiting_after_stop_request_is_stopped_not_crashed(self):
        self.register(state="stopping", now=10.0)
        with patch.object(services.psutil, "Process", side_effect=psutil.NoSuchProcess(self.pid)):
            data = services.snapshot(jobs_dir=self.root, now=20.0)
        self.assertEqual(data["services"]["live:job-a"]["state"], "stopped")

    def test_terminal_service_is_not_reclassified(self):
        self.register(state="running", now=10.0)
        services.finish("live:job-a", self.pid, self.created, state="completed",
                        message="Finished", jobs_dir=self.root, now=11.0)
        with patch.object(services.psutil, "Process", side_effect=psutil.NoSuchProcess(self.pid)):
            data = services.snapshot(jobs_dir=self.root, now=500.0)
        self.assertEqual(data["services"]["live:job-a"]["state"], "completed")

    def test_corrupt_registry_is_preserved_instead_of_overwritten(self):
        path = self.root / services.REGISTRY_FILE
        path.write_text("{broken json", encoding="utf-8")
        with self.assertRaises(services.RuntimeRegistryError):
            services.snapshot(jobs_dir=self.root)
        self.assertEqual(path.read_text(encoding="utf-8"), "{broken json")

    def test_concurrent_registrations_do_not_lose_records(self):
        failures = []

        def create(index):
            try:
                services.register(
                    f"analysis:{index}", "analysis", f"job-{index}",
                    self.pid + index + 1, self.created + index,
                    jobs_dir=self.root, now=10.0 + index,
                )
            except Exception as exc:
                failures.append(exc)

        threads = [threading.Thread(target=create, args=(i,)) for i in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(failures, [])
        payload = json.loads((self.root / services.REGISTRY_FILE).read_text(encoding="utf-8"))
        self.assertEqual(len(payload["services"]), 12)


class CrashedLiveWorkerRecoveryTests(unittest.TestCase):
    def test_runtime_recovery_preserves_clips_and_moves_unfinished_candidates_to_review(self):
        with tempfile.TemporaryDirectory() as folder:
            jobs = Path(folder)
            jd = jobs / "twitch-live-test"
            jd.mkdir()
            (jd / "job.json").write_text(json.dumps({"live": True}), encoding="utf-8")
            (jd / "live.json").write_text(json.dumps({
                "pid": 987654, "create_time": 123.0, "ended": False, "ready": True,
                "paused": False, "pause_state": "running",
            }), encoding="utf-8")
            (jd / "status.json").write_text(json.dumps({"stage": "live", "msg": "Watching"}),
                                            encoding="utf-8")
            (jd / "clips.json").write_text(json.dumps({"clips": [{"id": 7}]}), encoding="utf-8")
            (jd / "candidates.json").write_text(json.dumps({
                "version": 1, "next_id": 3,
                "items": [
                    {"id": 1, "start": 12, "queue_state": "queued"},
                    {"id": 2, "start": 30, "queue_state": "processing"},
                ],
            }), encoding="utf-8")
            lock = jobs / ".live-worker.lock"
            lock.write_text(json.dumps({
                "pid": 987654, "create_time": 123.0, "job": jd.name,
            }), encoding="utf-8")
            services.register(
                f"live:{jd.name}", "live-worker", jd.name, 987654, 123.0,
                state="running", jobs_dir=jobs, now=10.0,
            )

            with patch.object(runtime, "JOBS", jobs), patch.object(live, "JOBS", jobs), \
                 patch.object(live, "LIVE_LOCK", lock), \
                 patch.object(live.psutil, "Process", side_effect=psutil.NoSuchProcess(987654)):
                data = runtime.snapshot()
                first_state = json.loads((jd / "live.json").read_text(encoding="utf-8"))
                status = json.loads((jd / "status.json").read_text(encoding="utf-8"))
                queue = json.loads((jd / "candidates.json").read_text(encoding="utf-8"))
                second = runtime.snapshot()

            self.assertTrue(first_state["ended"])
            self.assertEqual(first_state["recovery_state"], "interrupted")
            self.assertEqual(status["stage"], "error")
            self.assertIn("cannot be resumed safely", status["error"])
            self.assertEqual([item["queue_state"] for item in queue["items"]],
                             ["needs_review", "needs_review"])
            self.assertEqual((jd / "clips.json").is_file(), True)
            self.assertFalse(lock.exists())
            self.assertEqual(data["service_counts"]["crashed"], 1)
            self.assertEqual(second["service_counts"]["crashed"], 1)


class CrashedRenderRecoveryTests(unittest.TestCase):
    def test_interrupted_render_preserves_previous_export_and_removes_temporary_files(self):
        with tempfile.TemporaryDirectory() as folder:
            jobs = Path(folder)
            jd = jobs / "youtube-video"
            cd = jd / "clips" / "2"
            cd.mkdir(parents=True)
            final = cd / "final.mp4"
            final.write_bytes(b"previous-good-export")
            temp_mp4 = cd / ".final-777-123.rendering.mp4"
            temp_srt = cd / ".final-777-123.rendering.srt"
            temp_mp4.write_bytes(b"partial")
            temp_srt.write_text("partial", encoding="utf-8")
            (cd / "render.json").write_text(json.dumps({"state": "running", "attempts": 3}),
                                            encoding="utf-8")
            service_id = "render:youtube-video:2:777:123"
            services.register(
                service_id, "render", jd.name, 777, 70.0, state="running",
                jobs_dir=jobs, metadata={"clip_id": 2, "render_attempt": 3},
            )
            record = services.finish(
                service_id, 777, 70.0, state="crashed",
                message="Renderer process exited", jobs_dir=jobs,
            )
            recovered = render.recover_crashed_render(record, jobs_dir=jobs)
            manifest = json.loads((cd / "render.json").read_text(encoding="utf-8"))
            saved_service = json.loads((jobs / services.REGISTRY_FILE).read_text(encoding="utf-8"))

            self.assertTrue(recovered)
            self.assertEqual(manifest["state"], "failed")
            self.assertTrue(manifest["recovered_previous_run"])
            self.assertEqual(final.read_bytes(), b"previous-good-export")
            self.assertFalse(temp_mp4.exists())
            self.assertFalse(temp_srt.exists())
            self.assertIn("recovered_at", saved_service["services"][service_id])


class CrashedAnalysisRecoveryTests(unittest.TestCase):
    def test_interrupted_analysis_is_marked_for_manual_retry(self):
        with tempfile.TemporaryDirectory() as folder:
            jobs = Path(folder)
            jd = jobs / "youtube-video"
            jd.mkdir()
            service_id = "analysis:youtube-video:777:1001"
            (jd / "job.json").write_text(json.dumps({
                "live": False, "runtime_service_id": service_id,
            }), encoding="utf-8")
            (jd / "status.json").write_text(json.dumps({
                "stage": "download", "pct": 54, "msg": "Downloading clip 2",
            }), encoding="utf-8")
            (jd / "clips.json").write_text(json.dumps({"clips": [{"id": 1}]}), encoding="utf-8")
            services.register(
                service_id, "analysis", jd.name, 777, 70.0,
                state="running", jobs_dir=jobs,
            )
            record = services.finish(
                service_id, 777, 70.0, state="crashed",
                message="Application process exited", jobs_dir=jobs,
            )

            with patch.object(runtime, "JOBS", jobs):
                recovered = runtime._recover_crashed_analysis(record)
                status = json.loads((jd / "status.json").read_text(encoding="utf-8"))
                data = json.loads((jobs / services.REGISTRY_FILE).read_text(encoding="utf-8"))

            self.assertTrue(recovered)
            self.assertEqual(status["stage"], "error")
            self.assertEqual(status["pct"], 54)
            self.assertIn("Run Find clips again", status["error"])
            self.assertTrue((jd / "clips.json").is_file())
            self.assertIn("recovered_at", data["services"][service_id])


if __name__ == "__main__":
    unittest.main()
