# Architecture

## Current base
The application is Python + Gradio. The existing CLI starts the dashboard and the dashboard already supports VOD analysis, live monitoring, clip review and Remotion rendering. Keep this as the first UI rather than introducing a separate web stack.

## Intended runtime
- UI/control plane: existing Gradio dashboard, manually launched.
- Runtime coordinator: owns explicit application state and lifecycle for monitoring jobs.
- Ingestion workers: existing platform-specific chat adapters and FFmpeg/yt-dlp recorder.
- Rolling storage: short segmented media window; prune old segments; never retain complete livestream by default.
- Signal workers: existing chat velocity/reaction scoring and optional audio/STT signals.
- Candidate evaluator: deterministic baseline plus optional Antigravity CLI quality gate after extraction. It receives a 9-frame low-resolution contact sheet and bounded transcript/chat metadata from an isolated temporary workspace; no source media or project tree is exposed.
- Review verdicts: KEEP continues; REJECT is held out of rendering but the source remains recoverable; REVIEW preserves the candidate for human inspection. CLI failures always fail to REVIEW.
- Clip pipeline: extract candidate window, validate duration/audio/video, render vertical format.
- Campaign layer: validate creator authorization, length, aspect ratio, text/hashtag/mention/overlay/destination requirements.
- Publisher: platform-supported APIs/OAuth only; queue/retry/dedupe. Manual export pack fallback where no supported API exists.
- Persistence: local job metadata, task queue, idempotency keys and audit log.
- Observability: dashboard health, worker state, queue depth, CPU/RAM/disk, errors and recent actions.

## Lifecycle
The process starts only when the user launches the application. Dashboard Start creates workers; Stop requests graceful shutdown. App exit stops child workers and releases media handles. No OS login item/daemon is installed by default.

## Resource policy
Default to one source and one render. Use bounded buffers, small frame samples, lazy loading and cache limits. Keep large media on disk. Cloud APIs are optional and never receive full recordings by default.
