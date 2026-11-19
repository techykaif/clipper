# AI and Automation Rules

- Do not use a local generative model or Ollama.
- The existing Claude Code handoff remains a manual editing workflow and is never required by the running app.
- Optional Antigravity CLI review is allowed only as a locally configurable candidate-quality gate after a clip has been extracted. It may return KEEP, REJECT or REVIEW; failures and uncertain results must never reject a clip.
- A completed, schema-validated REJECT automatically removes that clip from the active library and sends its folder to the OS Trash / Recycle Bin where available. REVIEW, KEEP, CLI errors and malformed responses must never delete media.
- Launch Antigravity headlessly with both --sandbox and --dangerously-skip-permissions, from a disposable temporary workspace containing only a low-resolution sampled-frame contact sheet and bounded transcript/chat metadata. Never include source.mp4, full recordings, project files, credentials or secrets in that workspace.
- The permission-bypass flag auto-approves all Antigravity tool calls; retain the isolated workspace and sandbox requirement. Do not let review tasks edit project code, modify source media, run publishing actions or access unrelated user files.
- Optional cloud inference is user-configurable, candidate-only, quota-aware and privacy-disclosed. No full livestream or source clip is uploaded by the review gate.
- Never send whole livestreams to a model by default. Use a small, documented set of low-resolution frames and minimal relevant chat/transcript context.
- Clearly disclose provider, model, request payload categories, privacy implications, quota and estimated usage to the user.
- Store secrets only in local environment variables or OS secret storage. Never put secrets in source, config committed to Git, logs, screenshots or prompts.
- Enforce timeouts, bounded retries, exponential backoff, per-candidate deduplication, request budgets and deterministic fallback.
- Treat model output as untrusted structured suggestions; validate schema and never allow it to bypass campaign or publishing policy.
- Do not autonomously publish content from creators without explicit authorization and an approved destination/campaign configuration.
