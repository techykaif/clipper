# Project Context

## Goal
GTA Clip Factory is a local-first, user-launched application built on the MIT-licensed Clipper project. The user starts it manually from VS Code or a standalone application; it must not auto-start at OS login. After launch, a dashboard should control monitoring and autonomous processing.

## Hardware targets
- Primary: Apple MacBook Air M1, 8 GB unified memory.
- Secondary: Windows desktop, Intel Core i3-2120, 16 GB RAM, 256 GB SSD + 500 GB HDD.
- Start with one 720p stream, one render at a time, and conservative memory use. Benchmark before scaling.
- Do not assume hardware acceleration or throughput; measure on target machines.

## Existing upstream foundation
- Gradio local dashboard: clipper/app.py
- CLI: clipper/cli.py
- Live stream ingestion and 10-minute rolling segment buffer: clipper/live.py
- Chat readers for Twitch / YouTube / Kick, signal scoring and clip preparation.
- FFmpeg is used for ingest/cutting; Remotion renderer is in remotion/.
- Optional local Parakeet speech-to-text exists; not a local generative LLM.
- Existing Claude Code handoff is manual and must not become a runtime dependency.

## Product rules
- No login auto-start. Explicit Start/Stop in dashboard.
- No local generative LLM / Ollama.
- Antigravity CLI quality review is a locally configurable candidate-only stage after clip extraction. It evaluates a sampled low-resolution contact sheet and bounded transcript/chat context; it never receives source.mp4 or a full stream.
- The review CLI runs headlessly with --sandbox --dangerously-skip-permissions inside a disposable review workspace. Failed or uncertain reviews stay in REVIEW and never cause deletion.
- Completed REJECT results are removed from the active clip list and moved to OS Trash / Recycle Bin where available; human-readable review status is shown for KEEP and REVIEW.
- Use only authorized stream sources; publishing must respect creator permissions, platform policies and campaign terms.
- The rolling stream buffer temporarily stores segments on disk. Do not claim zero stream data is downloaded; the target is bounded rolling storage rather than full-VOD retention.
- No automatic campaign publishing until account authorization and campaign constraints are implemented and tested.

## Current scope
Build a controlled dashboard experience and reliable local runtime first. Initial acceptance:
1. App launches from a documented local command.
2. Dashboard exposes service health, start/stop and clear status.
3. One live source can be monitored.
4. Candidate moments appear reliably.
5. Selected candidate yields a valid clip.
6. Bounded memory/disk usage and clean stop/restart.
7. Smoke tests and target-machine measurements are documented.
