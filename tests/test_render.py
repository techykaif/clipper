import io
import json
import tempfile
import threading
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import Mock, patch

from clipper import render


class RenderOutputValidationTests(unittest.TestCase):
    def setUp(self):
        self.probe = {
            "streams": [{
                "codec_type": "video", "codec_name": "h264", "width": 1080, "height": 1920,
                "pix_fmt": "yuv420p", "avg_frame_rate": "30/1", "duration": "12.0",
            }],
            "format": {"duration": "12.0"},
        }

    def test_accepts_valid_vertical_h264_export(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "final.mp4"
            path.write_bytes(b"x" * 2048)
            result = render.validate_render_output(path, 12.0, 30.0, probe=self.probe)
        self.assertEqual(result["width"], 1080)
        self.assertEqual(result["height"], 1920)
        self.assertEqual(result["codec"], "h264")
        self.assertEqual(result["duration"], 12.0)

    def test_rejects_landscape_output(self):
        probe = json.loads(json.dumps(self.probe))
        probe["streams"][0]["width"] = 1920
        probe["streams"][0]["height"] = 1080
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "final.mp4"
            path.write_bytes(b"x" * 2048)
            with self.assertRaisesRegex(RuntimeError, "expected 1080x1920"):
                render.validate_render_output(path, 12.0, 30.0, probe=probe)

    def test_rejects_non_h264_codec(self):
        probe = json.loads(json.dumps(self.probe))
        probe["streams"][0]["codec_name"] = "hevc"
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "final.mp4"
            path.write_bytes(b"x" * 2048)
            with self.assertRaisesRegex(RuntimeError, "expected H.264"):
                render.validate_render_output(path, 12.0, 30.0, probe=probe)

    def test_rejects_wrong_pixel_format(self):
        probe = json.loads(json.dumps(self.probe))
        probe["streams"][0]["pix_fmt"] = "yuv444p"
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "final.mp4"
            path.write_bytes(b"x" * 2048)
            with self.assertRaisesRegex(RuntimeError, "expected yuv420p"):
                render.validate_render_output(path, 12.0, 30.0, probe=probe)

    def test_rejects_incorrect_render_duration(self):
        probe = json.loads(json.dumps(self.probe))
        probe["format"]["duration"] = "8.0"
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "final.mp4"
            path.write_bytes(b"x" * 2048)
            with self.assertRaisesRegex(RuntimeError, "expected about 12.00s"):
                render.validate_render_output(path, 12.0, 30.0, probe=probe)

    def test_rejects_missing_or_tiny_output(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "final.mp4"
            path.write_bytes(b"tiny")
            with self.assertRaisesRegex(RuntimeError, "missing or unexpectedly small"):
                render.validate_render_output(path, 12.0, 30.0, probe=self.probe)


class EffectValueValidationTests(unittest.TestCase):
    def test_rejects_infinite_numeric_effect_values(self):
        with self.assertRaisesRegex(render.FxError, "must be finite"):
            render._num({"strength": float("inf")}, "strength", 1, 0, 80, "shakes[0]")

    def test_rejects_nan_numeric_effect_values(self):
        with self.assertRaisesRegex(render.FxError, "must be finite"):
            render._num({"strength": float("nan")}, "strength", 1, 0, 80, "shakes[0]")


class RenderQueueTests(unittest.TestCase):
    def test_second_process_waits_for_the_global_render_lock(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(render, "REMOTION", Path(folder)):
                with render._render_guard(timeout=1):
                    with self.assertRaises(TimeoutError):
                        with render._render_guard(timeout=0.05):
                            self.fail("A second render unexpectedly acquired the global lock.")

    def test_success_promotes_validated_temp_output_atomically(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            jd = root / "job"
            cd = jd / "clips" / "1"
            cd.mkdir(parents=True)
            old_final = cd / "final.mp4"
            old_final.write_bytes(b"old-valid-export")
            props = {
                "fps": 30.0, "clipLength": 5.0, "trimStart": 0.0, "trimEnd": 5.0,
                "hookStart": 0.0, "hookEnd": 0.0, "hookEnabled": False,
                "subsEnabled": False, "words": [],
            }

            class FakeProcess:
                def __init__(self, cmd, **_kwargs):
                    Path(cmd[5]).write_bytes(b"x" * 2048)
                    self.pid = 0
                    self.stdout = io.BytesIO(b"Rendered 10/10\rEncoded\n")
                def wait(self):
                    return 0

            with patch.object(render, "props_for", return_value=props), \\
                 patch.object(render, "REMOTION", root / "remotion"), \\
                 patch.object(render, "_render_guard", return_value=nullcontext()), \\
                 patch.object(render.subprocess, "Popen", side_effect=FakeProcess), \\
                 patch.object(render, "tool", return_value="npx"), \\
                 patch.object(render, "chrome_env", return_value={}), \\
                 patch.object(render, "validate_render_output", return_value={
                     "width": 1080, "height": 1920, "codec": "h264", "bytes": 2048,
                 }) as validate:
                result = render.render(jd, 1)

            self.assertEqual(result, old_final)
            self.assertGreater(old_final.stat().st_size, 1024)
            self.assertEqual(json.loads((cd / "render.json").read_text())["state"], "completed")
            validate.assert_called_once()

    def test_failed_render_preserves_previous_good_export_and_marks_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            jd = root / "job"
            cd = jd / "clips" / "1"
            cd.mkdir(parents=True)
            final = cd / "final.mp4"
            final.write_bytes(b"previous-good-file")
            props = {
                "fps": 30.0, "clipLength": 5.0, "trimStart": 0.0, "trimEnd": 5.0,
                "hookStart": 0.0, "hookEnd": 0.0, "hookEnabled": False,
                "subsEnabled": False, "words": [],
            }

            class FailedProcess:
                def __init__(self, _cmd, **_kwargs):
                    self.pid = 0
                    self.stdout = io.BytesIO(b"render crashed\n")
                def wait(self):
                    return 1

            with patch.object(render, "props_for", return_value=props), \\
                 patch.object(render, "REMOTION", root / "remotion"), \\
                 patch.object(render, "_render_guard", return_value=nullcontext()), \\
                 patch.object(render.subprocess, "Popen", side_effect=FailedProcess), \\
                 patch.object(render, "tool", return_value="npx"), \\
                 patch.object(render, "chrome_env", return_value={}):
                with self.assertRaisesRegex(RuntimeError, "Remotion render failed"):
                    render.render(jd, 1)

            self.assertEqual(final.read_bytes(), b"previous-good-file")
            state = json.loads((cd / "render.json").read_text())
            self.assertEqual(state["state"], "failed")
            self.assertIn("Remotion render failed", state["error"])
            self.assertEqual(list(cd.glob("*.rendering.mp4")), [])

    def test_render_watchdog_terminates_a_hung_renderer(self):
        released = threading.Event()

        class HungProcess:
            pid = 123456789
            stdout = object()
            def wait(self):
                return -15

        def blocked_lines(_stream):
            released.wait(1)
            yield "renderer stopped"

        with patch.object(render.subprocess, "Popen", return_value=HungProcess()), \\
             patch.object(render, "_lines", side_effect=blocked_lines), \\
             patch.object(render, "_stop_render_process", side_effect=lambda _proc: released.set()) as stop, \\
             patch.object(render, "RENDER_WALL_TIMEOUT_S", 0.01):
            with self.assertRaisesRegex(TimeoutError, "exceeded the 0-second render limit"):
                render._run_remotion(["npx", "remotion"], lambda *_: None, [])
        stop.assert_called_once()

    def test_interrupted_running_manifest_is_recorded_as_recovery_attempt(self):
        with tempfile.TemporaryDirectory() as folder:
            cd = Path(folder)
            (cd / "render.json").write_text(json.dumps({"state": "running", "attempts": 2}))
            state = render._render_state(cd, "queued", attempts=3,
                                         recovered_previous_run=True)
            self.assertEqual(state["state"], "queued")
            self.assertTrue(state["recovered_previous_run"])
            self.assertEqual(state["attempts"], 3)


if __name__ == "__main__":
    unittest.main()
