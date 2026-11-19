import unittest

from clipper.runtime import classify_job_state


class RuntimeStateClassificationTests(unittest.TestCase):
    def test_live_process_is_running(self):
        self.assertEqual(classify_job_state("live", True, process_alive=True), "RUNNING")

    def test_missing_live_process_is_stale(self):
        self.assertEqual(classify_job_state("live", True, process_alive=False), "STALE")

    def test_ended_live_job_is_stopped(self):
        self.assertEqual(classify_job_state("live", True, process_alive=False, ended=True), "STOPPED")

    def test_recent_past_job_is_running(self):
        self.assertEqual(classify_job_state("download", False, age_s=20), "RUNNING")

    def test_old_past_job_is_stale(self):
        self.assertEqual(classify_job_state("download", False, age_s=600), "STALE")

    def test_error_takes_precedence(self):
        self.assertEqual(classify_job_state("error", True, process_alive=True), "ERROR")


if __name__ == "__main__":
    unittest.main()
