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

## Known gaps / M1 plan

- The app is manually launched; no login item or daemon is installed.
- Past-stream analysis runs in a background thread and live monitoring runs in a detached child process. Status is file-backed, but there is no unified runtime coordinator or explicit pause/resume lifecycle.
- The dashboard has per-job stop behavior, but no app-level service state, worker registry, resource usage panel or restart-safe pause queue.
- Live capture uses a 600-second rolling disk buffer and prunes old segments. End-to-end reconnect, crash recovery and target-hardware resource measurements remain to be performed.
- A stdlib unittest baseline covers review-result validation and rejected-clip disposal. Tests do not emulate live platform APIs, FFmpeg, Antigravity authentication or real trash behavior on the user's machine.
- Twitch/YouTube/Kick live reliability and M1 hardware targets must not be considered validated until exercised on the user's machines.

M1 should add a small local runtime coordinator, safe worker status discovery, explicit pause/resume semantics, resource visibility and idempotent shutdown while keeping the current Gradio UI and one-source/one-render defaults.
