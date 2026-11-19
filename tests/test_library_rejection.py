import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from clipper import library


class RejectedClipDisposalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.jobs = root / "jobs"
        self.remotion = root / "remotion"
        self.trash = root / "Trash"
        self.key = "twitch-teststream"
        self.job = self.jobs / self.key
        self.clip = self.job / "clips" / "1"
        self.clip.mkdir(parents=True)
        self.trash.mkdir()
        self.remotion.mkdir()
        (self.clip / "source.mp4").write_bytes(b"test clip")
        (self.clip / "review.json").write_text(json.dumps({
            "status": "reviewed", "verdict": "REJECT", "score": 12
        }), encoding="utf-8")
        (self.job / "clips.json").write_text(json.dumps({
            "clips": [{"id": 1}, {"id": 2}]
        }), encoding="utf-8")
        for name, value in (("JOBS", self.jobs), ("REMOTION", self.remotion), ("TRASH", self.trash)):
            p = patch.object(library, name, value)
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.temp.cleanup()

    def test_rejected_clip_moves_to_trash_and_leaves_active_list(self):
        self.assertTrue(library.discard_rejected_clip(self.key, 1))
        self.assertFalse(self.clip.exists())
        moved = list(self.trash.iterdir())
        self.assertEqual(len(moved), 1)
        self.assertTrue((moved[0] / "source.mp4").is_file())
        summary = json.loads((self.job / "clips.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["clips"], [{"id": 2}])

    def test_non_rejected_clip_cannot_use_discard_path(self):
        (self.clip / "review.json").write_text(json.dumps({
            "status": "reviewed", "verdict": "KEEP", "score": 90
        }), encoding="utf-8")
        with self.assertRaises(ValueError):
            library.discard_rejected_clip(self.key, 1)
        self.assertTrue(self.clip.exists())


if __name__ == "__main__":
    unittest.main()
