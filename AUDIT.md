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
- Past-stream analysis runs in a background thread and live monitoring runs in a detached child process whether started from the dashboard or the foreground CLI. Both launch paths use the same reserved worker slot and managed Stop/cleanup path. State remains file-backed; there is not yet a unified persisted service registry, restart-safe render queue or restart-persistent rolling video buffer.
- The existing dashboard exposes worker/resource health and live Start/Stop/Pause/Resume controls. Pause/resume transitions use recoverable state markers under a shared cross-process guard; the watcher is suspended before descendants are enumerated. Stop records its marker under the same guard, verifies PID creation time and command identity, uses a verified process handle for POSIX signals, and falls back to best-effort verified process-tree termination if recovery from pause fails. The Windows path remains unvalidated.
- Detached live-worker launch reserves the one-source slot before releasing the launch guard; the child adopts that reservation. The worker installs its signal handler and persists its own PID/creation time before publishing readiness. Stop before readiness is handled without signalling an unready process; a Stop after chat-reader startup is checked before video recording begins. A separate heartbeat is updated independently of long clip/review operations; new workers require that heartbeat for healthy classification while legacy jobs retain status-file fallback.
- Twitch, YouTube and Kick chat readers are wrapped so unexpected adapter exits are visible. Shutdown joins reader threads; Kick HTTP request retries, setup timeouts and websocket polling are stop-aware. YouTube recorder retry/recovery continues while live status is current or inconclusive and only ends after repeated explicit post-live results. Real provider/FFmpeg testing remains outstanding.
- The user ran the lifecycle revision's complete unittest suite on a Mac: 47 tests passed and the Gradio dashboard launched. This result predates the candidate-quality changes below; the updated 62-test source suite has not yet been run on the target Mac.
- The live candidate workflow now has a file-backed queue (`candidates.json`) with stable IDs, 60-second deduplication, bounded history and explicit processing/review/terminal states. Existing clips seed the queue when upgrading a job. The runtime panel counts saved candidates rather than only published clips.
- Clip preparation samples up to nine small frames with OpenCV and measures brightness, Laplacian sharpness and adjacent-frame motion. Very dark, severely blurred and very low-motion footage receives bounded score penalties; an unavailable frame decode does not penalize or discard the clip. These are visual-risk heuristics only: they do not semantically identify GTA menus, maps, photo mode, mission context or exciting events. The optional Antigravity review remains the semantic quality gate when enabled.
- Interrupted queued/processing candidates are preserved as `needs_review`. They are deliberately not automatically re-cut after worker restart because the rolling video buffer and its elapsed-time origin are not yet restart-persistent; replaying old timestamps against a new recording could produce the wrong footage.
- The user confirmed the 62-test revision passed on the Mac. The latest source now contains 75 test methods across runtime, candidate, AI-review, library and render suites; the 13 new render tests have not yet been run on the target Mac. Live pause/resume/stop smoke testing, real YouTube/Twitch/Kick capture and recovery, process-tree/Windows behavior, M1 resource measurements and visual-threshold calibration remain acceptance gates.
- The renderer now uses a cross-process single-render lock, per-clip `render.json` lifecycle state, a 20-minute hard render ceiling, process-tree termination on timeout, temporary MP4/SRT outputs, and ffprobe validation before replacing the last known-good final export. Dashboard clip status reads queued/running/failed render state. Validation checks 1080x1920, H.264, yuv420p, frame rate and expected duration. Render resources are selected at start from current available memory and physical CPU cores, with a bounded off-thread video cache and optional concurrency override; renders remain serialized across processes.
- Broader gaps remain: there is no unified persisted runtime service registry, no restart-safe media buffer, no adaptive CPU/RAM-aware render scheduler, no retention policy/notifications, no campaign authorization/OAuth/publishing path, and no end-to-end cross-platform acceptance run.

Preserve the existing Gradio dashboard, manual launch behavior and one-source/one-render defaults.
