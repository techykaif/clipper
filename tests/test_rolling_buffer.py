import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from clipper import rolling_buffer


class RollingBufferCoverageTests(unittest.TestCase):
    def test_contiguous_segments_cover_candidate(self):
        spans = [
            {"start": 100.0, "end": 104.0, "path": "a"},
            {"start": 104.0, "end": 108.0, "path": "b"},
            {"start": 108.0, "end": 112.0, "path": "c"},
        ]
        result = rolling_buffer.coverage_from_spans(spans, 101.0, 111.0)
        self.assertTrue(result["covered"])
        self.assertEqual(len(result["segments"]), 3)

    def test_recovery_refuses_to_bridge_capture_gap(self):
        spans = [
            {"start": 100.0, "end": 104.0, "path": "a"},
            {"start": 110.0, "end": 114.0, "path": "b"},
        ]
        result = rolling_buffer.coverage_from_spans(spans, 102.0, 112.0)
        self.assertFalse(result["covered"])
        self.assertGreater(result["gap_seconds"], 0)

    def test_small_timestamp_quantisation_gap_is_tolerated(self):
        spans = [
            {"start": 100.0, "end": 104.0, "path": "a"},
            {"start": 104.5, "end": 108.5, "path": "b"},
        ]
        self.assertTrue(rolling_buffer.coverage_from_spans(spans, 101.0, 108.0)["covered"])

    def test_invalid_or_reversed_ranges_are_rejected(self):
        for a, b in ((1, 1), (2, 1), (float("nan"), 2), (0, float("inf"))):
            with self.subTest(a=a, b=b):
                with self.assertRaises(ValueError):
                    rolling_buffer.coverage_from_spans([], a, b)

    def test_segment_inventory_only_probes_requested_time_range(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for stamp in (100, 104, 108, 200, 204):
                (root / f"seg_{stamp}.ts").write_bytes(b"segment")
            seen = []
            spans = rolling_buffer.segment_spans(
                root, duration_reader=lambda path: seen.append(path.name) or 4.0,
                wall_start=105, wall_end=110,
            )
        self.assertEqual(seen, ["seg_104.ts", "seg_108.ts"])
        self.assertEqual(len(spans), 2)

    def test_segment_inventory_ignores_unprobeable_truncated_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "seg_100.ts").write_bytes(b"one")
            (root / "seg_104.ts").write_bytes(b"two")
            (root / "not-a-segment.ts").write_bytes(b"three")
            def probe(path):
                if path.name == "seg_104.ts":
                    raise rolling_buffer.BufferCoverageError("truncated")
                return 4.0
            spans = rolling_buffer.segment_spans(root, duration_reader=probe)
        self.assertEqual([x["start"] for x in spans], [100.0])

    def test_cut_refuses_gap_before_ffmpeg(self):
        spans = [
            {"start": 100.0, "end": 104.0, "duration": 4.0, "path": Path("seg_100.ts")},
            {"start": 110.0, "end": 114.0, "duration": 4.0, "path": Path("seg_110.ts")},
        ]
        with patch.object(rolling_buffer, "segment_spans", return_value=spans), \
                patch.object(rolling_buffer.subprocess, "run") as run:
            with self.assertRaises(rolling_buffer.BufferCoverageError):
                rolling_buffer.cut("/tmp", 102, 112, "/tmp/out.mp4")
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
