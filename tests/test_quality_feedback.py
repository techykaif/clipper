import json
import tempfile
import unittest
from pathlib import Path

from clipper import quality_feedback


class QualityFeedbackTests(unittest.TestCase):
    def test_record_upserts_feedback_for_same_candidate(self):
        with tempfile.TemporaryDirectory() as folder:
            candidate = {"id": 3, "score": 82, "signal_score": 90}
            quality_feedback.record("job-a", candidate, "keep", jobs_dir=folder, now=10)
            quality_feedback.record("job-a", candidate, "reject", "not entertaining", jobs_dir=folder, now=20)
            payload = json.loads((Path(folder) / quality_feedback.FEEDBACK_FILE).read_text())
        self.assertEqual(len(payload["items"]), 1)
        self.assertEqual(payload["items"][0]["label"], "reject")
        self.assertEqual(payload["items"][0]["updated_at"], 20)

    def test_feedback_requires_valid_label_and_candidate(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError):
                quality_feedback.record("job", {"id": 1, "score": 20}, "maybe", jobs_dir=folder)
            with self.assertRaises(ValueError):
                quality_feedback.record("../escape", {"id": 1, "score": 20}, "keep", jobs_dir=folder)

    def test_calibration_waits_for_minimum_labeled_samples(self):
        with tempfile.TemporaryDirectory() as folder:
            for cid in range(1, 10):
                quality_feedback.record("job", {"id": cid, "score": 80}, "keep", jobs_dir=folder)
            result = quality_feedback.calibration(jobs_dir=folder)
        self.assertFalse(result["ready"])
        self.assertEqual(result["samples"], 9)

    def test_calibration_suggests_threshold_but_does_not_mutate_source(self):
        with tempfile.TemporaryDirectory() as folder:
            for cid in range(1, 11):
                quality_feedback.record("job", {"id": cid, "score": 40}, "reject", jobs_dir=folder)
            for cid in range(11, 21):
                quality_feedback.record("job", {"id": cid, "score": 80}, "keep", jobs_dir=folder)
            result = quality_feedback.calibration(jobs_dir=folder)
            payload = json.loads((Path(folder) / quality_feedback.FEEDBACK_FILE).read_text())
        self.assertTrue(result["ready"])
        self.assertGreaterEqual(result["suggested_min_score"], 41)
        self.assertEqual(len(payload["items"]), 20)

    def test_one_sided_feedback_never_suggests_a_threshold(self):
        with tempfile.TemporaryDirectory() as folder:
            for cid in range(1, 21):
                quality_feedback.record("job", {"id": cid, "score": 70 + cid}, "keep", jobs_dir=folder)
            result = quality_feedback.calibration(jobs_dir=folder)
        self.assertFalse(result["ready"])
        self.assertIsNone(result["suggested_min_score"])

    def test_unsure_feedback_does_not_enter_calibration(self):
        with tempfile.TemporaryDirectory() as folder:
            quality_feedback.record("job", {"id": 1, "score": 90}, "unsure", jobs_dir=folder)
            result = quality_feedback.calibration(jobs_dir=folder)
        self.assertEqual(result["samples"], 0)


if __name__ == "__main__":
    unittest.main()
