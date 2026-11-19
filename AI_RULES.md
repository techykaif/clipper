# AI and Automation Rules

- Do not use a local generative model, Ollama, Claude Code or Antigravity as part of the running app.
- Optional cloud inference is disabled by default and only invoked after deterministic candidate detection.
- Never send whole livestreams to a model by default. Use a small, documented set of low-resolution frames and minimal relevant chat/transcript context.
- Clearly disclose provider, model, request payload categories, privacy implications, quota and estimated usage to the user.
- Store secrets only in local environment variables or OS secret storage. Never put secrets in source, config committed to Git, logs, screenshots or prompts.
- Enforce timeouts, bounded retries, exponential backoff, per-candidate deduplication, request budgets and deterministic fallback.
- Treat model output as untrusted structured suggestions; validate schema and never allow it to bypass campaign or publishing policy.
- Do not autonomously publish content from creators without explicit authorization and an approved destination/campaign configuration.
