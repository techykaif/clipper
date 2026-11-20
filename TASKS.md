# Tasks

## Delivery sequence and deadline

**Target: complete the agreed product scope by 2026-11-21 (India time).**

Work must follow these gates in order; do not start external integrations early:

1. **Core implementation first:** finish the local application, dashboard, ingestion, recorder/recovery, candidate pipeline, clip management, review safeguards, vertical production, persistence, resource controls and error handling. Add code-level regression tests alongside changes, but do not call the core ready yet.
2. **Core verification gate:** after the core code is implemented and reviewed, run the complete automated suite, then component/integration tests for each local function. Fix failures and repeat until the release criteria are met. Only then mark the core application ready.
3. **External integrations:** after core readiness, implement campaign/creator authorization, supported platform authentication/OAuth, publishing/submission, retries, audit trail and manual fallback.
4. **Final acceptance:** run the end-to-end workflow through integrations, validate permissions and failure recovery, and record any unsupported or unverified cases. Do not claim production-ready where a platform or device has not been tested.

If the 2026-11-21 deadline conflicts with reliable implementation or acceptance, report the exact unfinished scope instead of marking unchecked work complete or weakening the gates.

## Current batch — live-worker lifecycle + autonomous candidate quality (implementation complete; acceptance pending)

- [x] Reserve the single-live slot atomically during detached worker launch and let the child adopt its reservation.
- [x] Preserve immediate Stop requests through a readiness handshake; install the signal handler and persist the worker's own PID/creation time before capture begins. Both dashboard and foreground CLI launch use the same managed detached worker; a Stop after chat-reader startup is checked before video recording begins.
- [x] Serialize pause/resume transitions, freeze the watcher before enumerating descendants, and persist recoverable pause states.
- [x] Verify PID creation time and command identity before Stop; use verified psutil process objects and best-effort tree termination after partial resume failure.
- [x] Add worker heartbeats and classify new live workers with missing/stale heartbeats as stale while retaining legacy job compatibility.
- [x] Surface unexpected chat-reader exits, join reader threads during shutdown, and make Kick HTTP request retries, setup timeouts and websocket polling stop-aware.
- [x] Release the single-live slot even if publication, cleanup or status writes fail.
- [x] Add persistent live-candidate records with stable IDs, 60-second deduplication, bounded queue history and explicit queued/processing/ready/rejected/filtered/failed/needs-review states.
- [x] Recover old queue records conservatively: unfinished candidates are preserved for review, not automatically re-cut against a reset rolling-buffer timeline.
- [x] Add sampled-frame blur, brightness and low-motion risk scoring; unavailable frame decoding fails open. This is a quality signal, not a semantic GTA menu/photo-mode classifier.
- [x] Wire quality scoring into VOD/live clip preparation and show persistent candidate totals in the runtime dashboard.
- [x] Add 75 source-level regression test methods across runtime, AI-review, library, candidate and render suites. The user confirmed the prior 62-test revision passed on the Mac; the 13 new render tests still need a fresh local run.
- [ ] Run the complete updated automated suite on the Mac after pulling this batch.
- [ ] Validate live-worker pause/resume/stop and real authorized-stream recovery behavior.
- [ ] Validate visual scoring against representative GTA V/VI clips and tune thresholds from observed false positives/negatives.
- [ ] Exercise Windows-specific process/locking paths.

This batch is not marked ready until the local suite and functional checks pass.

## M0 — Baseline audit (current)
- [x] Confirm fork, upstream parent, default branch and repository write permission.
- [x] Create isolated feature branch.
- [x] Record product and hardware constraints.
- [x] Inventory runtime module responsibilities, dependency and installer entry points, and top-level licenses; detailed platform/edge-case validation remains in M1/M2.
- [x] Add a stdlib unittest baseline and document reproducible setup/test commands.
- [x] Record limitations and M1 plan in AUDIT.md and ARCHITECTURE.md.

## M1 — Dashboard-controlled local runtime
- [ ] Add explicit runtime service state and lifecycle management.
- [x] Surface live/stale worker health, candidate counts, errors, CPU, memory and disk usage in a read-only panel.
- [x] Add per-live-job Start / Stop / Pause / Resume controls; pause suspends the verified watcher + descendant process tree, and Stop resumes before shutdown.
- [x] Preserve manual launch; no login auto-start.
- [x] Enforce a cross-process single-live-source slot with stale lock recovery; the dashboard's render lock continues to serialize analysis/render work.
- [ ] Add a unified persisted runtime service registry and restart-safe render queue; the live candidate queue is now file-backed, but the rolling video buffer is not restart-persistent.

## M2 — One-source live reliability
- [x] Harden partial recorder startup cleanup and bound each FFmpeg clip cut to 180 seconds; mocked regression tests added (execution still requires running the suite).
- [x] Add yt-dlp HTTP/fragment retry backoff, fresh-segment recorder recovery and YouTube live-status confirmation before ending after capture loss; add YouTube live-chat reconnect/deduplication. Mocked regression tests added; real platform validation remains open.
- [x] Bound yt-dlp socket/fragment retries and surface reconnect status in the dashboard.
- [ ] Test Twitch live, rolling buffer, chat timestamps, reconnects and cleanup on a real authorized stream.
- [ ] Validate 720p resource profile on M1 8 GB.
- [ ] Validate i3 / 16 GB on Windows after the Mac path is stable.

## M3 — Autonomous candidate workflow
- [x] Persistent live candidate queue, stable IDs, deduplication, restart-safe state preservation and live clip extraction; visual-quality scores are applied before acceptance.
- [x] Optional Antigravity quality gate for extracted candidates (KEEP / REJECT / REVIEW); rejected media is removed from the active library and sent to OS Trash / Recycle Bin where available.
- [x] Isolated sampled-frame review workspace, structured JSON verdict validation, bounded timeout and fail-to-REVIEW behavior.
- [ ] Add configurable provider/model budgets and review-quality calibration from user feedback.

## M4 — Automated vertical production
- [x] Automatic crop/layout defaults, hook timeline, configurable subtitles and default vertical export composition.
- [x] Add a cross-process single-render admission gate with persisted per-clip render state, waiting progress, wall-clock timeout, process-tree cleanup, temporary output promotion and ffprobe validation for 1080x1920 H.264/yuv420p exports.
- [ ] Add adaptive resource-aware scheduling based on available CPU/RAM and device profile; the current scheduler safely serializes renders but does not dynamically tune concurrency or quality.

## M5 — Campaign compliance and publishing
- [ ] Campaign configuration and source authorization.
- [ ] Rules validation and audit trail.
- [ ] OAuth and supported publishing/submission integrations only.
- [ ] Manual submission package fallback.

## M6 — Reliability and analytics
- [ ] Persistent render/runtime queues, rolling-buffer recovery, storage retention policy and notifications. The live candidate queue is implemented, but it cannot reconstruct the previous worker's rolling video buffer after a crash.
- [ ] Metrics and campaign performance/earnings tracking.

## Rules
- Keep changes small, focused and reviewable.
- Never claim a local or live test passed unless actually run on the target runtime.
- Do not enable GitHub Actions media jobs; user has previously hit Actions-minute limits.
