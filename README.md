# tgdl

**Agent-first** Telegram group-chat file downloader. One user account (MTProto via Telethon), one session file, a scriptable CLI, and a loopback JSON API. Humans and coding agents drive the same jobs.

**Who this file is for:** coding agents and maintainers. Collaboration rules are in [`AGENTS.md`](AGENTS.md). The stable CLI/HTTP contract is [`docs/AGENT_API.md`](docs/AGENT_API.md). End-user setup in Simplified Chinese is [`README.zh-CN.md`](README.zh-CN.md).

Current version: **1.0.0**. Python **3.11+**. Package `tgdl`. Entry points: `tgdl` and `python -m tgdl`.

---

## Agent quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env    # fill TGDL_API_ID and TGDL_API_HASH; do not commit .env

tgdl version --json
tgdl help --json
pytest

# after a human has completed `tgdl login` once:
tgdl list-chats --json
tgdl scan --chat @example --ext pdf --json
tgdl download --chat @example --ext pdf --workers 4 --json
tgdl status --json
tgdl serve --json    # loopback API; progress stays on stderr
```

`login` is interactive (phone code, optional 2FA) and is **not** exposed on HTTP. Every later command reuses the session file.

Do not invent API credentials. Create them at <https://my.telegram.org> and keep them in `.env` or `config.yaml` (both gitignored).

---

## Architecture

```
tgdl login | list-chats | scan | download | status | serve
        |                         |
        |  if .tgdl/api-pid is live and /api/v1/ping answers
        +-------- proxy --------> 127.0.0.1:18765/api/v1/*  + Bearer token
                                  api.py  (Host must be loopback)
                                       |
                                  service.py   one job store, one TelegramClient
                                       |
                    telegram_io.py  (Telethon)   naming.py + resume.py
                                       |
                                  downloads/<chat>/<yyyy-mm>/<file>
                                  <file>.part + <file>.tgdl.json while transferring
```

- **One process owns the session.** Telethon's session is SQLite. `tgdl serve` holds it; the CLI forwards `list-chats`, `scan`, and `download` to that process. `login` refuses to start while serve is up.
- **Workers are asyncio tasks**, not OS threads. Telethon's client is async. `--workers` (default 4, max 32) caps how many `iter_download` streams run at once on that single client.
- **stdout is the result. stderr is logs and progress.** `--json` prints one JSON document. A TTY gets a Rich bar; a pipe gets plain progress lines.
- **No GUI.** Humans use the CLI and `README.zh-CN.md`.

Runtime files live in `.tgdl/` (`jobs/`, `api-token`, `api-port`, `api-pid`, default session). Do not commit them.

---

## Source map

| Path | Responsibility |
|------|----------------|
| `tgdl/cli.py` | argparse, exit codes, JSON vs human output, proxy-or-local dispatch, signals |
| `tgdl/api.py` | `GET/POST /api/v1/*`, loopback bind, Host check, bearer token |
| `tgdl/api_client.py` | CLI → serve HTTP client, runtime pid/port/token files |
| `tgdl/service.py` | Job lifecycle shared by CLI and API. Idempotent download, worker pool |
| `tgdl/telegram_io.py` | Telethon login, dialogs, history scan, `iter_download` + FloodWait |
| `tgdl/transfer.py` | Chunk writer: fsync, cancel after the current chunk. No Telethon import |
| `tgdl/resume.py` | 4096-byte alignment, `.tgdl.json` sidecar, slot decision |
| `tgdl/naming.py` | Sanitize, `name (1).ext`, `chat/yyyy-mm` layout, read-only vs writing plans |
| `tgdl/jobs.py` | `jobs/job_*.json`, latest pointer, idempotency key, stale-running recovery |
| `tgdl/params.py` | One normalizer for CLI flags and API bodies. Idempotent |
| `tgdl/filters.py` | Extension, size, date, sender, substring, regex |
| `tgdl/targets.py` | `@user`, `t.me`, invite, `t.me/c/`, numeric id |
| `tgdl/config.py` | CLI > environment > `config.yaml` > defaults |
| `tgdl/contract.py` | Schema version, exit codes, status enums. Add-only |
| `tgdl/helptext.py` | `help --json` / `GET /api/v1/help` catalog |
| `docs/AGENT_API.md` | Automation contract |

---

## Hard invariants

Break these and agents, resume, or the session will misbehave.

1. **Resume through `iter_download`, not `download_media(path)`.** A string path truncates the file. Write `name.ext.part`, fsync, then `os.replace` onto the final name when the byte count matches.
2. **Resume offset is `aligned_resume_offset`.** Telegram `upload.getFile` wants a multiple of 4096. Chunk size is `REQUEST_SIZE` (512 KiB), which divides 1 MiB. Truncate the part down to that boundary (at most 4095 bytes are fetched again). A part whose size already equals `fileSize` is renamed, not truncated.
3. **Sidecar identity wins over the size heuristic.** `.tgdl.json` stores `messageId`, `chatId`, `fileSize`, `fileUniqueId` (MTProto document/photo id). A different id on the same filename collides (`name (1).ext`) even when the sizes match. With no sidecar, a final file of the expected size is skipped.
4. **Scan does not write.** `plan_items(..., write=False)` may compute paths. Only download creates sidecars and promotes finished parts.
5. **`--workers` bounds concurrent downloads.** Implement the pool with asyncio tasks on one `TelegramClient`. Do not put the client in worker threads.
6. **`flood_sleep_threshold=0`.** FloodWait must surface in `telegram_io` so the part is checkpointed before the sleep. If the wait exceeds `--flood-cap` (default 15m), stop with exit 4 and leave the part.
7. **Exit codes stay 0–5** (`contract.py`). JSON fields are add-only. Update `docs/AGENT_API.md` and `help_payload()` together.
8. **Ids that do not fit in a JS number are strings** (`chatId`, `messageId`, `senderId`, `fileUniqueId`).
9. **Secrets stay out of git and out of argv.** No `--api-hash` flag. `.env`, `config.yaml`, `*.session`, `.tgdl/api-token` are gitignored.
10. **The API binds `127.0.0.1` only.** Reject any other host. Require `Authorization: Bearer` or `X-Token`. Require `Host: 127.0.0.1:<port>` or `localhost:<port>`.
11. **One active download per process.** A second download is exit 5 / HTTP 409 until the first finishes or is cancelled. The same key returns the running job (`existing: true`).
12. **Ctrl+C finishes the current chunk**, fsyncs, writes the sidecar, and exits 4. A second Ctrl+C cancels the tasks.

Tests that lock the resume and name rules: `tests/test_resume.py`, `tests/test_naming.py`, `tests/test_jobs_transfer.py`.

---

## CLI

```
tgdl login [--phone]
tgdl list-chats [--limit N] [--groups-only] [--json]
tgdl scan --chat TARGET [--chat TARGET ...] [filters] [--json]
tgdl download --chat TARGET ... [--workers N] [--output DIR] [--json]
tgdl status [JOB_ID] [--json]
tgdl serve [--port 18765] [--json]
tgdl version --json
tgdl help --json
```

Global flags (before or after the command): `--json`, `--config`, `--data-dir`, `--session`, `--quiet`, `--verbose`.

Filters: `--ext`, `--min-size`, `--max-size`, `--since`, `--until`, `--sender`, `--name`, `--name-regex`, `--photos` / `--no-photos`, `--limit`, `--message-id`, `--output`, `--flat`. Download also has `--no-resume`, `--force`, `--timeout`, `--flood-cap`.

Exit codes: `0` ok · `1` failure · `2` usage · `3` auth/session · `4` partial/timeout (resume state is in the JSON) · `5` rejected.

`--json` example shape (fields are stable; see the contract for the full job):

```json
{"schema":1,"ok":true,"exitCode":0,"code":"ok","existing":false,"job":{"id":"job_…","kind":"download","status":"complete"}}
```

---

## HTTP API

`tgdl serve` listens on `127.0.0.1:18765` (override with `TGDL_API_PORT` / `--port`). Token file: `.tgdl/api-token` (mode `0600`). The CLI discovers the server via `.tgdl/api-pid` + `.tgdl/api-port` and `GET /api/v1/ping`.

| Method | Path | Role |
|--------|------|------|
| GET | `/api/v1/ping` | Liveness. Does not open Telegram |
| GET | `/api/v1/version` | Same payload as `version --json` |
| GET | `/api/v1/help` | Same payload as `help --json` |
| GET | `/api/v1/chats` | `list-chats` |
| POST | `/api/v1/scan` | Scan job |
| POST | `/api/v1/download` | Download job |
| GET | `/api/v1/task?id=` | One job |
| GET | `/api/v1/tasks` | Recent jobs |
| GET | `/api/v1/status` | Latest job, or `?id=` |
| POST | `/api/v1/cancel` | `{"id":"job_…"}` |

`ok: true` on these responses means the HTTP call was accepted. Read `job.status` and `job.exitCode` for the Telegram job. `wait: false` (the POST default) returns **202** while the job is running.

Details, schemas, and idempotency: [`docs/AGENT_API.md`](docs/AGENT_API.md).

---

## Resume (implementation note)

Telethon 1.36–1.45 `download_file` opens a path with `'wb'` and overwrites it. `iter_download(message, offset=..., request_size=512*1024)` yields bytes starting at `offset`. Unaligned offsets still work (generic iterator) but a crash can tear the tail, so `prepare_part_file` truncates to a 4096-byte boundary first.

`fileUniqueId` is `str(document.id)` or `str(photo.id)`, not `message.file.id` (that bot-style id is deprecated).

Layout, unless `--flat`:

```
downloads/<sanitized chat title>/<YYYY-MM>/<filename>
```

---

## How to change code

- New CLI flag: add it in `cli.py`, accept it in `params.normalize_params` (keep that function idempotent), and add the field to the job `params` object. If it changes which files match, include it in `canonical_key`. Workers, timeout, and flood cap are intentionally **not** part of the key.
- New JSON field: append it. Do not rename or remove. Bump nothing in `SCHEMA` unless you are shipping a breaking contract, which v1 does not do.
- New endpoint: register it in `api.py` and `helptext.help_payload()`, then document it in `docs/AGENT_API.md`.
- Download behavior: change `transfer.py` / `resume.py` and extend `tests/test_resume.py` or `tests/test_jobs_transfer.py`. Do not mock Telethon for those tests.
- User-visible setup steps: edit `README.zh-CN.md`. Do not paste that manual into this file.

```bash
pytest
python -m tgdl help --json
python -m tgdl version --json
```

---

## Configuration

Precedence: **CLI flags > environment > `config.yaml` > defaults**.

| Env | YAML | Meaning |
|-----|------|---------|
| `TGDL_API_ID` | `api_id` | From my.telegram.org. `0` / empty = unset |
| `TGDL_API_HASH` | `api_hash` | Same. `replace-me` = unset |
| `TGDL_PHONE` | `phone` | Optional. Login prompts if missing |
| `TGDL_SESSION` | `session` | Default `.tgdl/tgdl.session` |
| `TGDL_OUTPUT` | `output` | Default `downloads/` |
| `TGDL_DATA_DIR` | `data_dir` | Default `.tgdl/` |
| `TGDL_WORKERS` | `workers` | Default 4 |
| `TGDL_FLOOD_CAP` | `flood_cap` | Default `15m` |
| `TGDL_API_HOST` | `api_host` | Must be `127.0.0.1` |
| `TGDL_API_PORT` | `api_port` | Default `18765` |
| `TGDL_API_TOKEN` | `api_token` | Optional. Else `.tgdl/api-token` |

`.env` is loaded with `override=False` (a real environment variable wins). Copy [`.env.example`](.env.example) and [`config.example.yaml`](config.example.yaml).

---

## Doc split

| File | Audience |
|------|----------|
| **This README** | Agents / maintainers: architecture, invariants, where to edit |
| `AGENTS.md` | Coding-assistant contract. `.cursor/rules` and `.github/copilot-instructions.md` point here |
| `docs/AGENT_API.md` | CLI + `/api/v1` schemas, exit codes, idempotency |
| `README.zh-CN.md` | Human setup in Simplified Chinese |

---

## License

MIT. See [`LICENSE`](LICENSE). Runtime dependency: [Telethon](https://github.com/LonamiWebs/Telethon) (MIT). Also aiohttp, PyYAML, python-dotenv, Rich.
