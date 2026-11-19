# Tasks

## M0 — Baseline audit (current)
- [x] Confirm fork, upstream parent, default branch and repository write permission.
- [x] Create isolated feature branch.
- [x] Record product and hardware constraints.
- [ ] Inspect all runtime modules, dependency manifests, installer scripts and licenses.
- [ ] Establish reproducible local setup and baseline test commands.
- [ ] Record limitations and M1 plan.

## M1 — Dashboard-controlled local runtime
- [ ] Add explicit runtime service state and lifecycle management.
- [ ] Surface live worker health, current source, queue, errors and resource usage.
- [ ] Add Start / Stop / Pause controls with graceful child-process shutdown.
- [ ] Preserve manual launch; no login auto-start.
- [ ] Add bounded worker concurrency and restart-safe state.

## M2 — One-source live reliability
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
