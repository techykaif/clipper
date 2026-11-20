import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from clipper import storage


class StoragePolicyTests(unittest.TestCase):
    def test_default_reserve_is_two_gib(self):
        with patch.dict(os.environ, {storage.MIN_FREE_ENV: ""}):
            self.assertEqual(storage.minimum_free_bytes(), 2 * storage.GIB)

    def test_reserve_can_be_configured(self):
        with patch.dict(os.environ, {storage.MIN_FREE_ENV: "0.5"}):
            self.assertEqual(storage.minimum_free_bytes(), storage.GIB // 2)

    def test_invalid_reserve_fails_closed(self):
        for value in ("not-a-number", "-1", "nan", "inf", "1025"):
            with self.subTest(value=value), patch.dict(os.environ, {storage.MIN_FREE_ENV: value}):
                with self.assertRaises(storage.StorageSafetyError):
                    storage.minimum_free_bytes()

    def test_snapshot_flags_space_below_reserve(self):
        usage = SimpleNamespace(total=10 * storage.GIB, used=9 * storage.GIB, free=storage.GIB)
        with patch.dict(os.environ, {storage.MIN_FREE_ENV: "2"}), \
                patch("clipper.storage.shutil.disk_usage", return_value=usage):
            state = storage.snapshot("/tmp")
        self.assertTrue(state["below_reserve"])
        self.assertEqual(state["free_bytes"], storage.GIB)
        self.assertEqual(state["used_percent"], 90.0)

    def test_require_free_space_blocks_when_below_reserve(self):
        usage = SimpleNamespace(total=10 * storage.GIB, used=9 * storage.GIB, free=storage.GIB)
        with patch.dict(os.environ, {storage.MIN_FREE_ENV: "2"}), \
                patch("clipper.storage.shutil.disk_usage", return_value=usage):
            with self.assertRaises(storage.InsufficientDiskSpace) as ctx:
                storage.require_free_space("/tmp", "rendering a Short")
        self.assertIn("rendering a Short", str(ctx.exception))
        self.assertIn(storage.MIN_FREE_ENV, str(ctx.exception))

    def test_require_free_space_allows_work_above_reserve(self):
        usage = SimpleNamespace(total=10 * storage.GIB, used=7 * storage.GIB, free=3 * storage.GIB)
        with patch.dict(os.environ, {storage.MIN_FREE_ENV: "2"}), \
                patch("clipper.storage.shutil.disk_usage", return_value=usage):
            state = storage.require_free_space("/tmp", "rendering a Short")
        self.assertFalse(state["below_reserve"])

    def test_disk_usage_failure_fails_closed(self):
        with patch("clipper.storage.shutil.disk_usage", side_effect=OSError("volume unavailable")):
            with self.assertRaises(storage.StorageSafetyError):
                storage.require_free_space("/missing", "starting live capture")


if __name__ == "__main__":
    unittest.main()
