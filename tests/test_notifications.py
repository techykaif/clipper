import os
import unittest
from unittest.mock import patch

from clipper import notifications


class DesktopNotificationTests(unittest.TestCase):
    def test_notifications_are_disabled_by_default(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(notifications.subprocess, "run") as run:
            self.assertFalse(notifications.notify("Clipper", "Ready"))
        run.assert_not_called()

    def test_macos_notification_uses_osascript_and_escapes_text(self):
        with patch.dict(os.environ, {"CLIPPER_DESKTOP_NOTIFICATIONS": "1"}), \
                patch.object(notifications.sys, "platform", "darwin"), \
                patch.object(notifications.subprocess, "run") as run:
            self.assertTrue(notifications.notify('A "title"', "Line one\nLine two"))
        args = run.call_args.args[0]
        self.assertEqual(args[0], "osascript")
        self.assertIn("\\", args[2])

    def test_linux_without_notify_send_fails_quietly(self):
        with patch.dict(os.environ, {"CLIPPER_DESKTOP_NOTIFICATIONS": "1"}), \
                patch.object(notifications.sys, "platform", "linux"), \
                patch.object(notifications, "shutil") as shutil_mock:
            shutil_mock.which.return_value = None
            self.assertFalse(notifications.notify("Clipper", "Ready"))


if __name__ == "__main__":
    unittest.main()
