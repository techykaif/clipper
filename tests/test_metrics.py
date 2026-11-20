import json
import tempfile
import unittest
from pathlib import Path

from clipper import metrics


class ProductionMetricsTests(unittest.TestCase):
    def test_metrics_aggregate_existing_job_artifacts(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            job = root / "twitch-live-sample"
            clips = job / "clips"
            one, two = clips / "1", clips / "2"
            one.mkdir(parents=True)
            two.mkdir(parents=True)
            (job / "job.json").write_text('{"live": true}')
            (job / "clips.json").write_text(json.dumps({"clips": [{"id": 1}, {"id": 2}]}))
            (job / "candidates.json").write_text(json.dumps({"items": [{"id": 1}, {"id": 2}, {"id": 3}]}))
            (one / "source.mp4").write_bytes(b"source")
            (one / "final.mp4").write_bytes(b"export")
            (one / "review.json").write_text('{"status":"reviewed","verdict":"KEEP"}')
            (one / "render.json").write_text('{"state":"completed"}')
            (two / "review.json").write_text('{"status":"needs_review","verdict":"REVIEW"}')
            (two / "render.json").write_text('{"state":"failed"}')
            result = metrics.snapshot(jobs_dir=root)
        self.assertEqual(result["streams"], 1)
        self.assertEqual(result["candidates"], 3)
        self.assertEqual(result["clips"], 2)
        self.assertEqual(result["rendered"], 1)
        self.assertEqual(result["render_failed"], 1)
        self.assertEqual(result["review_keep"], 1)
        self.assertEqual(result["review_pending"], 1)
        self.assertEqual(result["media_bytes"], 12)

    def test_empty_library_returns_zero_metrics(self):
        with tempfile.TemporaryDirectory() as folder:
            result = metrics.snapshot(jobs_dir=folder)
        self.assertEqual(result["streams"], 0)
        self.assertEqual(result["rendered"], 0)


if __name__ == "__main__":
    unittest.main()
