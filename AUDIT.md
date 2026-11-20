# Initial repository audit

## Source inventory

The repository keeps the existing Python/Gradio Clipper architecture:

- Dashboard and CLI: `clipper/app.py`, `clipper/cli.py`, `clipper/common.py`, `clipper/update.py`.
- Ingestion and adapters: `clipper/live.py`, `clipper/platforms.py`, `clipper/download.py`, `clipper/chat/{twitch,youtube,kick,importer}.py`.
- Detection and candidates: `clipper/spikes.py`, `clipper/signals.py`, `clipper/pipeline.py`, `clipper/captions.py`, `clipper/hotspot.py`, `clipper/ai_review.py`.
- Media processing: `clipper/face.py`, `clipper/stt.py`, `clipper/subs.py`, `clipper/render.py`, `clipper/memes.py`, `remotion/`.
- Packaging and launch: `pyproject.toml`, `install.sh`, `install.ps1`, `clipper.sh`, `clipper.cmd`.
- Licensing: top-level MIT `LICENSE`; Instrument Sans asset licensing in `clipper/assets/OFL.txt`.

This is a source and packaging inventory with an entry-point review, not a full platform/network acceptance test or exhaustive audit of every failure path. No GitHub Actions media jobs or new Actions workflows were added.

## Runtime dependencies

Python is declared as 3.11+. Required Python packages are Gradio 6.32.0, NumPy 2+, OpenCV headless 4.x, Pillow, websockets and yt-dlp. Speech recognition is optional through the `stt` extra (`sherpa-onnx`). FFmpeg is an external runtime prerequisite. The optional Remotion renderer requires Node.js 18+ and the checked-in npm lockfile.

The OpenCV dependency is constrained to `>=4.10,<5`. A previous local diagnostic reported OpenCV 5.1.0 without `cv2.CascadeClassifier`, while `clipper/face.py` uses the Haar-cascade API. Face detection has a gameplay-only fallback, but dependency resolution should not select that incompatible major line.

## Local baseline

From the repository root:

```sh
python -m pip install -e .
python -m unittest discover -s tests -v
python -m clipper serve
```

For speech-to-text, install `python -m pip install -e ".[stt]"` and run `python -m clipper stt-download`. For vertical rendering, install Node.js 18+ and run `npm ci` inside `remotion/`. FFmpeg must be on PATH. The interactive shell/PowerShell installers prepare these tools; audit them before use on a new machine.

## Current implementation status and known gaps

- The app remains manually launched; no login item or OS daemon is installed.
- Past-stream analysis runs in a background thread and live monitoring runs in a detached child process. State remains file-backed; there is not yet a unified persisted service registry or restart-safe task queue.
- The existing dashboard exposes worker/resource health and live Start/Stop/Pause/Resume controls. Pause/resume transitions use recoverable state markers under a shared cross-process guard; the watcher is frozen before descendants are enumerated. Stop records its marker under the same guard, verifies PID creation time and command identity, uses a verified process handle for signals, and falls back to best-effort verified process-tree termination if recovery from pause fails.
- Detached live-worker launch reserves the one-source slot before releasing the launch guard; the child adopts that reservation. A readiness handshake installs the signal handler and persists the worker's own PID before readers/recording start, so a Stop during startup is not lost. A separate heartbeat is updated independently of long clip/review operations, allowing the dashboard to identify an alive-but-stale new live worker while preserving compatibility with older jobs.
- Twitch, YouTube and Kick chat readers are wrapped so unexpected adapter exits are visible. Shutdown joins reader threads; Kick HTTP retry waits and websocket polling are stop-aware. YouTube recorder retry/recovery continues while live status is current or inconclusive and only ends after repeated explicit post-live results.
- A stdlib unittest suite covers review validation, rejected-clip disposal, slot ownership, mocked recorder cleanup/reconnect and selected lifecycle transitions. The current branch adds regression tests for launch reservation, PID reuse safety, stop markers, heartbeat and reader-exit handling. These are mocked/source-level checks only; no claim is made that the latest complete suite has passed on the user's Mac.
- The implementation still needs local suite execution, real YouTube/Twitch/Kick stream testing, process-tree/Windows validation, M1 resource measurements and broader crash-recovery testing. These remain acceptance gates, not completed work.

M1 still needs a persisted runtime service registry, restart-safe queue state and stronger process-tree recovery. Preserve the existing Gradio dashboard and one-source/one-render defaults.
