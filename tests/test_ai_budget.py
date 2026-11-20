import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from clipper import ai_budget


class ReviewBudgetTests(unittest.TestCase):
    def test_daily_and_per_job_limits_are_enforced(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
            "CLIPPER_AI_REVIEW_DAILY_LIMIT": "2", "CLIPPER_AI_REVIEW_JOB_LIMIT": "1",
        }):
            first = ai_budget.reserve("stream-a", 1, jobs_dir=folder, now=1800000000)
            second = ai_budget.reserve("stream-a", 2, jobs_dir=folder, now=1800000000)
            other = ai_budget.reserve("stream-b", 1, jobs_dir=folder, now=1800000000)
        self.assertTrue(first["allowed"])
        self.assertFalse(second["allowed"])
        self.assertTrue(other["allowed"])

    def test_duplicate_candidate_reservation_is_idempotent(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
            "CLIPPER_AI_REVIEW_DAILY_LIMIT": "2", "CLIPPER_AI_REVIEW_JOB_LIMIT": "2",
        }):
            first = ai_budget.reserve("stream-a", 4, jobs_dir=folder, now=1800000000)
            second = ai_budget.reserve("stream-a", 4, jobs_dir=folder, now=1800000000)
            payload = json.loads((Path(folder) / ai_budget.BUDGET_FILE).read_text())
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(payload["days"]["2027-01-15"]["count"], 1)

    def test_budget_rolls_over_by_local_day(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
            "CLIPPER_AI_REVIEW_DAILY_LIMIT": "1", "CLIPPER_AI_REVIEW_JOB_LIMIT": "1",
        }):
            first = ai_budget.reserve("stream-a", 1, jobs_dir=folder, now=1800000000)
            second = ai_budget.reserve("stream-a", 2, jobs_dir=folder, now=1800086400)
        self.assertTrue(first["allowed"])
        self.assertTrue(second["allowed"])

    def test_invalid_budget_fails_closed(self):
        with patch.dict(os.environ, {"CLIPPER_AI_REVIEW_DAILY_LIMIT": "many"}):
            with self.assertRaises(ai_budget.ReviewBudgetError):
                ai_budget.limits()

    def test_invalid_job_key_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError):
                ai_budget.reserve("../outside", 1, jobs_dir=folder)


if __name__ == "__main__":
    unittest.main()
