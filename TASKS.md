# Tasks

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
- [ ] Add broader persisted runtime service registry and restart-safe queue state.

## M2 — One-source live reliability
- [x] Harden partial recorder startup cleanup and bound each FFmpeg clip cut to 180 seconds; mocked regression tests added (execution still requires running the suite).
- [ ] Test Twitch live, rolling buffer, chat timestamps, reconnects and cleanup.
- [ ] Validate 720p resource profile on M1 8 GB.
- [ ] Validate i3 / 16 GB on Windows after the Mac path is stable.

## M3 — Autonomous candidate workflow
- [ ] Candidate queue, deterministic scoring, deduplication and clip extraction.
- [x] Optional Antigravity quality gate for extracted candidates (KEEP / REJECT / REVIEW); rejected media is removed from the active library and sent to OS Trash / Recycle Bin where available.
- [x] Isolated sampled-frame review workspace, structured JSON verdict validation, bounded timeout and fail-to-REVIEW behavior.
- [ ] Add configurable provider/model budgets and review-quality calibration from user feedback.

## M4 — Automated vertical production
- [ ] Automatic crop/layout defaults, subtitles where configured, hook and export validation.
- [ ] Render queue and resource-aware scheduling.

## M5 — Campaign compliance and publishing
- [ ] Campaign configuration and source authorization.
- [ ] Rules validation and audit trail.
- [ ] OAuth and supported publishing/submission integrations only.
- [ ] Manual submission package fallback.

## M6 — Reliability and analytics
- [ ] Persistent queues, crash recovery, storage retention policy and notifications.
- [ ] Metrics and campaign performance/earnings tracking.

## Rules
- Keep changes small, focused and reviewable.
- Never claim a local or live test passed unless actually run on the target runtime.
- Do not enable GitHub Actions media jobs; user has previously hit Actions-minute limits.
