"""Optional native desktop notifications; disabled unless explicitly enabled."""
import os
import shutil
import subprocess
import sys


TRUE_VALUES = {"1", "true", "yes", "on"}


def enabled():
    return os.environ.get("CLIPPER_DESKTOP_NOTIFICATIONS", "").strip().lower() in TRUE_VALUES


def _quote_osascript(value):
    text = " ".join(str(value or "").split())[:240]
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def notify(title, message):
    """Send a best-effort native notification without blocking the media pipeline."""
    if not enabled():
        return False
    title, message = str(title or "Clipper")[:100], " ".join(str(message or "").split())[:240]
    try:
        if sys.platform == "darwin":
            script = f"display notification {_quote_osascript(message)} with title {_quote_osascript(title)}"
            subprocess.run(["osascript", "-e", script], capture_output=True, timeout=5, check=False)
            return True
        if os.name == "nt":
            exe = shutil.which("powershell") or shutil.which("pwsh")
            if not exe:
                return False
            script = ("if (Get-Command New-BurntToastNotification -ErrorAction SilentlyContinue) { "
                      "New-BurntToastNotification -Text @($env:CLIPPER_TOAST_TITLE, $env:CLIPPER_TOAST_MESSAGE) }")
            subprocess.run([exe, "-NoProfile", "-NonInteractive", "-Command", script],
                           capture_output=True, timeout=6, check=False,
                           env={**os.environ, "CLIPPER_TOAST_TITLE": title, "CLIPPER_TOAST_MESSAGE": message})
            return True
        exe = shutil.which("notify-send")
        if not exe:
            return False
        subprocess.run([exe, "--app-name=Clipper", "--expire-time=6000", title, message],
                       capture_output=True, timeout=5, check=False)
        return True
    except (OSError, subprocess.SubprocessError):
        return False
