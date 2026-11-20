import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from clipper import candidates


class CandidateVisualQualityTests(unittest.TestCase):
    def test_visual_flags_reduce_score_with_bounded_penalty(self):
        source = {"score": 92, "auto_reason": "Two signals agreed."}
        assessed = candidates.apply_visual_quality(source, {
            "status": "checked",
            "flags": ["very_dark", "severely_blurred", "low_motion_or_static_scene"],
        })
        self.assertEqual(assessed["signal_score"], 92)
        self.assertEqual(assessed["quality_adjustment"], -35)
        self.assertEqual(assessed["score"], 57)
        self.assertEqual(len(assessed["quality_flags"]), 3)
        self.assertIn("Visual quality caution", assessed["auto_reason"])
        self.assertEqual(source["score"], 92)  # scoring does not mutate caller-owned input

    def test_unavailable_visual_check_does_not_penalize_candidate(self):
        assessed = candidates.apply_visual_quality({"score": 77}, {
            "status": "unavailable", "reason": "decode unavailable",
        })
        self.assertEqual(assessed["score"], 77)
        self.assertEqual(assessed["quality_adjustment"], 0)
        self.assertEqual(assessed["quality_flags"], [])

    def test_inspect_video_flags_dark_blurred_static_footage(self):
        frame = np.full((180, 320, 3), 8, dtype=np.uint8)

        class FakeCapture:
            def __init__(self, _path):
                self.index = 0
                self.released = False

            def isOpened(self):
                return True

            def get(self, _prop):
                return 9

            def set(self, _prop, value):
                self.index = int(value)
                return True

            def read(self):
                return True, frame.copy()

            def release(self):
                self.released = True

        with patch.object(candidates.cv2, "VideoCapture", FakeCapture):
            result = candidates.inspect_video("sample.mp4")
        self.assertEqual(result["status"], "checked")
        self.assertIn("very_dark", result["flags"])
        self.assertIn("severely_blurred", result["flags"])
        self.assertIn("low_motion_or_static_scene", result["flags"])

    def test_inspect_video_fails_open_when_decode_is_unavailable(self):
        class FakeCapture:
            def __init__(self, _path):
                pass
            def isOpened(self):
                return False
            def release(self):
                pass

        with patch.object(candidates.cv2, "VideoCapture", FakeCapture):
            result = candidates.inspect_video("broken.mp4")
        self.assertEqual(result["status"], "unavailable")
        assessed = candidates.apply_visual_quality({"score": 80}, result)
        self.assertEqual(assessed["score"], 80)


class CandidateQueueTests(unittest.TestCase):
    def candidate(self, peak, score=90):
        return {
            "start": max(0, peak - 20), "end": peak + 20, "peak": peak,
            "score": score, "basis": ["chat", "audio"],
            "sample_chat": ["viewer: no way"],
        }

    def test_queue_assigns_stable_ids_and_deduplicates_within_window(self):
        with tempfile.TemporaryDirectory() as folder:
            queue = candidates.CandidateQueue(folder)
            first = queue.discover([self.candidate(100), self.candidate(220)])
            self.assertEqual([item["id"] for item in first], [1, 2])
            duplicate = queue.discover([self.candidate(125, score=95)])
            self.assertEqual(duplicate, [])
            self.assertEqual(queue.next_id, 3)
            self.assertEqual(queue.counts()["queued"], 2)
            self.assertEqual(queue.items[0]["observations"], 2)

    def test_queue_survives_reload_and_remembers_terminal_candidates(self):
        with tempfile.TemporaryDirectory() as folder:
            queue = candidates.CandidateQueue(folder)
            item = queue.discover([self.candidate(300)])[0]
            queue.mark(item["id"], "ready", score=88, quality_flags=["low_motion_or_static_scene"])
            reopened = candidates.CandidateQueue(folder)
            self.assertEqual(reopened.items[0]["queue_state"], "ready")
            self.assertEqual(reopened.items[0]["quality_flags"], ["low_motion_or_static_scene"])
            self.assertEqual(reopened.discover([self.candidate(330)]), [])
            self.assertEqual(reopened.next_id, 2)

    def test_seed_imports_clips_from_jobs_without_a_queue(self):
        with tempfile.TemporaryDirectory() as folder:
            queue = candidates.CandidateQueue(folder, first_id=10)
            queue.seed([{"id": 4, "start": 70, "end": 110, "peak": 90, "score": 81}])
            self.assertEqual(queue.items[0]["id"], 4)
            self.assertEqual(queue.items[0]["queue_state"], "ready")
            self.assertEqual(queue.next_id, 10)
            self.assertEqual(queue.discover([self.candidate(100)]), [])

    def test_queue_rejects_unknown_state(self):
        with tempfile.TemporaryDirectory() as folder:
            queue = candidates.CandidateQueue(folder)
            item = queue.discover([self.candidate(60)])[0]
            with self.assertRaises(ValueError):
                queue.mark(item["id"], "mystery")


if __name__ == "__main__":
    unittest.main()
