# GTA Clip Factory — Agent Rules

## Mission
Evolve this fork of Clipper into a locally launched, dashboard-controlled autonomous livestream clipping application. Preserve the upstream engine where practical.

## Source and branch safety
- Upstream: https://github.com/guleriaakshit/clipper
- This repository is a fork: https://github.com/techykaif/clipper
- Work on focused feature branches and pull requests. Never push feature work directly to main.
- Preserve the upstream MIT LICENSE and attribution. Do not remove upstream copyright notices.
- Inspect upstream changes before updating or merging them.

## Product constraints
- Local-first runtime: the user explicitly launches the app from VS Code or a standalone app. Do not install launch-at-login services or background daemons by default.
- One-click start/stop from the dashboard; clean shutdown of child processes.
- Target Apple Silicon Mac with 8 GB RAM and Windows x64 machine with 16 GB RAM.
- Keep defaults conservative: one live stream and one render at a time; disk-based rolling buffer; bounded caches; cleanup old temporary segments.
- No local generative LLM or Ollama, and no dependency on Antigravity/Claude for the core runtime.
- Optional Antigravity candidate review is separately user-configurable. It runs only after extraction and must fail to REVIEW (never reject) when unavailable, uncertain or errored.
- A completed REJECT is automatically removed from the active clip library and sent to the OS Trash / Recycle Bin where available. Keep failed/uncertain reviews; do not delete their media.
- The Antigravity subprocess must use --sandbox --dangerously-skip-permissions and run from a disposable temporary workspace containing only a sampled low-resolution contact sheet and bounded context text. Never give it the project tree, source clip, full stream, credentials or secrets.
- Do not silently upload full streams or source clips. Disclose the exact candidate-only payload: sampled frames and bounded transcript/chat metadata.
- No campaign auto-publishing without user-configured accounts, campaign rules and explicit authorization for the source content/platform. Respect platform terms, creator rights and campaign requirements.

## Engineering
- First map the code paths; avoid a parallel replacement dashboard. Extend existing Gradio dashboard unless a documented reason supports changing frameworks.
- Prefer small typed modules and explicit interfaces for orchestration, candidate evaluation, rendering, publishing and storage.
- Persist job state and make tasks restart-safe; avoid duplicate clip creation and duplicate publication.
- Use safe subprocess management (timeouts, process groups, termination and cleanup).
- Never commit API keys, OAuth tokens, cookies, credentials, local job data, rendered media, models, or caches.
- Add tests for new logic. Report tests actually run; never claim local FFmpeg/stream tests when only GitHub source inspection was possible.
- Do not call remote shell installers (curl|bash / irm|iex). Audit scripts before use.
- Do not enable GitHub Actions workflows that consume large minutes or run media processing.
