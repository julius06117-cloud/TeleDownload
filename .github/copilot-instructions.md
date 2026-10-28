# Copilot instructions — tgdl

See [`AGENTS.md`](../AGENTS.md) for the file map, resume invariants, and definition of done.

- Agent-first Telegram group downloader. Telethon user account, asyncio worker pool, resumable `.part` files.
- CLI (`tgdl`, `--json`, exit codes 0–5) and loopback `GET/POST /api/v1/*` share `service.py`. Contract: [`docs/AGENT_API.md`](../docs/AGENT_API.md).
- Do not overwrite partial downloads. `iter_download` + 4096-byte alignment + sidecar `fileUniqueId`.
- Bind the API to `127.0.0.1` only. Never commit secrets or `*.session`.
- Local web UI: `tgdl/web/` served by `tgdl serve` at `http://127.0.0.1:18765/`. Do not put the API token in the page or the URL.
- Human setup lives in `README.zh-CN.md`. Keep JSON field names stable (add-only).
- Before finishing: `pytest`, and update `helptext.py` if commands or exit codes change.
