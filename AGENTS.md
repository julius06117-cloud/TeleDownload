# AGENTS.md — tgdl

Guide for coding agents (Cursor, Copilot, Claude, Codex, …). Read this before changing code. [`README.md`](README.md) has the architecture diagram and the hard invariants. The automation contract is [`docs/AGENT_API.md`](docs/AGENT_API.md).

Human setup is Simplified Chinese in [`README.zh-CN.md`](README.zh-CN.md). CLI output and JSON field names stay English so one contract serves both audiences.

## What this is

tgdl downloads documents (and, optionally, photos) from Telegram **group chats** using a **user account** (Telethon / MTProto). It is not a bot-API helper. Humans get a loopback web UI; agents keep using the CLI and JSON API. Both call the same jobs.

- Python 3.11+. Package `tgdl`. Version in `tgdl/__init__.py`.
- Commands: `login`, `list-chats`, `scan`, `download`, `status`, `serve`, `ui`, `version`, `help`.
- Loopback API: `http://127.0.0.1:18765/api/v1/*` with a bearer token.
- Web UI: `http://127.0.0.1:18765/` (`tgdl ui` or `tgdl serve --open`). Static files in `tgdl/web/`. Login stays `tgdl login`.
- Concurrency: asyncio tasks on **one** `TelegramClient`, capped by `--workers` (default 4).
- Resume: `*.part` + `*.tgdl.json`, then rename.

Do not invent `api_id` / `api_hash` / phone numbers. Tests must not open Telegram or bind anything except `127.0.0.1`.

## Repository map

| Path | Responsibility |
|------|----------------|
| `tgdl/cli.py` | Parser, exit codes, `--json`, proxy to `serve`, SIGINT |
| `tgdl/api.py` | `/api/v1/*`, Host + token middleware, loopback UI + bootstrap, `start_api` (127.0.0.1 only) |
| `tgdl/web/` | Chinese single-page UI. No Node toolchain. Served by `api.py` |
| `tgdl/api_client.py` | Discover a live server, poll jobs, write `api-pid` / `api-port` / `api-token` |
| `tgdl/service.py` | Shared job runner: scan, download, list-chats, cancel, idempotency |
| `tgdl/telegram_io.py` | Telethon only. Login, resolve, history, `iter_download`, FloodWait |
| `tgdl/transfer.py` | Pure chunk loop (cancel, fsync). Unit-tested without Telethon |
| `tgdl/resume.py` | Alignment, sidecar, `decide_slot` |
| `tgdl/naming.py` | Filenames, collision suffix, month folders, `plan_items` |
| `tgdl/jobs.py` | On-disk jobs and `canonical_key` |
| `tgdl/params.py` | Normalize CLI and JSON bodies. Must stay idempotent |
| `tgdl/filters.py` | Match rules |
| `tgdl/targets.py` | Chat reference parser |
| `tgdl/config.py` | Precedence: CLI > env > YAML > defaults |
| `tgdl/contract.py` | `SCHEMA`, exit codes 0–5, status strings |
| `tgdl/helptext.py` | Catalog returned by `help --json` and `GET /api/v1/help` |
| `tgdl/models.py` | `MediaItem`, `Planned`, file JSON object |
| `tests/` | Pure tests plus one loopback API test with a fake service |
| `docs/AGENT_API.md` | External contract. Update it in the same change as any JSON or exit-code edit |
| `README.zh-CN.md` | End-user manual. Update it when setup or flags change |

Runtime (never commit): `.env`, `config.yaml`, `*.session`, `.tgdl/` (`api-token`, jobs, session), `downloads/`, `*.part`, `*.tgdl.json`.

## Invariants

1. **Resume** uses `client.iter_download` into a `.part` file. `download_media(path)` overwrites. Offset is a multiple of 4096 (`ALIGNMENT`). Request size is 512 KiB (`REQUEST_SIZE` in `contract.py`). Checkpoint with `fsync` before sleeping on FloodWait and before returning on Ctrl+C.
2. **Sidecar** `.tgdl.json` is the identity (`fileUniqueId` + `fileSize`). Same-size skip applies only when no sidecar is present. Collision suffix is `name (1).ext`.
3. **`plan_items(write=False)` is scan.** It must not create sidecars, parts, or finals.
4. **Workers** are asyncio tasks. Do not share the client across OS threads. Max 32, default 4.
5. **`flood_sleep_threshold=0`** on the client so FloodWait is handled in `telegram_io` after a checkpoint. Over `--flood-cap` → exit 4, part kept.
6. **Exit codes** 0 ok, 1 failure, 2 usage, 3 auth, 4 partial/timeout, 5 rejected. Do not renumber. JSON fields are add-only (`SCHEMA` stays 1).
7. **String ids** for `chatId`, `messageId`, `senderId`, `fileUniqueId`.
8. **API** binds `127.0.0.1` only. Token via `Authorization: Bearer` or `X-Token` (`hmac.compare_digest`). `Host` must be `127.0.0.1:<port>` or `localhost:<port>`. No CORS. The exceptions are `GET /`, `GET /ui/*`, and `GET /api/v1/ui/bootstrap`: still loopback-only, still no CORS. Bootstrap additionally requires `X-Tgdl-Ui: 1` and refuses a cross-site `Sec-Fetch-Site` or a foreign `Origin`. It returns the API token for the local page (`sessionStorage`). Do not log that body. Do not put the token in the HTML or the query string.
9. **One download at a time** inside a process. Same idempotency key returns the running or resumable job unless `--force`. `--force` still rejects a second download while one task is active.
10. **Session lock.** If `discover_api` succeeds, `list-chats` / `scan` / `download` go through HTTP. `login` errors out instead of opening the sqlite session.
11. **Secrets.** No credential flags. Do not log the API hash, login code, or API token. Do not commit them.
12. **Ctrl+C** sets a cancel event. The in-flight chunk is written, then the part and sidecar are saved. Exit 4. Second Ctrl+C cancels the asyncio tasks (the writer still fsyncs on the way out).

`fileUniqueId` is the MTProto `document.id` / `photo.id` as a decimal string. Do not switch it to `message.file.id`.

Invite links call `ImportChatInviteRequest` and therefore **join** the chat. That is user-visible; keep it documented in `README.zh-CN.md`.

## Job model

A job file is `data_dir/jobs/job_<16 hex>.json`.

- `status`: `queued | running | complete | error | partial | cancelled | rejected | paused | removed` (`queued` is reserved). The UI reads `job.task.status`: `waiting | active | paused | complete | error | removed`. `speedBps` is 0 unless that status is `active`. Do not rename the tgdl `status` strings.
- `kind`: `list-chats | scan | download`.
- File `state`: `listed | pending | active | complete | skipped | failed | partial`.
- `listed` means a scan row, not an unfinished download. `finalize_job` only treats `pending` / `active` / `partial` as unfinished work.
- Failed file beats cancel: exit **1** if any file is `failed`, even if others are partial. Exit **4** when the run stopped with resume state and nothing failed.
- Idempotency key (`canonical_key`) includes chats, output, filters, `groupByMonth`, `resume`, `messageIds`. It excludes `workers`, `timeoutSec`, `floodCapSec`, `force`.
- A completed download is reused only when every dest file is still present and the same size. A completed job that matched **zero** files is not reused.
- `recover_stale_jobs` rewrites `running` → `partial` after a crash. Do not call it while `tgdl serve` is alive (it would clobber a live job). `status` calls it only when ping fails.

## CLI / API editing rules

- Parser and `help_payload()` must list the same commands. `tests/test_cli_api.py::test_help_json_matches_parser` checks this.
- `normalize_params` is the only place that interprets filters. Keep `normalize(normalize(x)) == normalize(x)`.
- HTTP `ok: true` means the call was accepted, not that the download succeeded. `job.exitCode` is the process exit code.
- POST `wait` defaults to false (202 + job id). The CLI polls. Login stays CLI-only.
- Unknown JSON fields are ignored so newer clients keep working.

## Definition of done

1. `pytest` passes. New resume or filename behavior has a unit test that does not mock Telethon.
2. CLI or JSON changes update `docs/AGENT_API.md` and `tgdl/helptext.py` in the same change.
3. User-visible setup or flag changes update `README.zh-CN.md`. Invariant changes update `README.md` and this file.
4. `python -m tgdl version --json` and `python -m tgdl help --json` succeed with no credentials.
5. No new secret, session, or download artifact is tracked by git.

```bash
pytest
python -m tgdl help --json
python -m tgdl version --json
```
