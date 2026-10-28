# tgdl — Agent / automation API

tgdl is an **agent-first Telegram downloader**: a Python CLI and a loopback HTTP API that list group chats, scan media, and download files with a bounded worker pool and resumable `.part` files. JSON field names and exit codes are a contract. **Add fields. Do not rename, remove, or renumber them.** `schema` is `1`.

Machine-readable catalog: `tgdl help --json` and `GET /api/v1/help` (same body).

Version covered here: **1.0.0**.

stdout is the result. stderr is logs and progress. Do not parse stderr.

---

## 1. Install

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env
# edit TGDL_API_ID and TGDL_API_HASH — never commit .env
tgdl help --json
```

Create `api_id` and `api_hash` at <https://my.telegram.org> → API development tools. The first `tgdl login` is interactive and writes the session file. Later commands are non-interactive.

There is no bundled binary and no GUI in v1.

---

## 2. CLI

```
tgdl login [--phone E164] [--json]
tgdl list-chats [--limit N] [--groups-only] [--json]
tgdl scan --chat TARGET [--chat TARGET ...] [filters] [--json]
tgdl download --chat TARGET ... [filters] [--workers N] [--json]
tgdl status [JOB_ID | --job JOB_ID] [--json]
tgdl serve [--host 127.0.0.1] [--port N] [--json]
tgdl version [--json]
tgdl help [--json]
```

Global (before or after the subcommand): `--json`, `--config PATH`, `--data-dir PATH`, `--session PATH`, `--quiet`, `--verbose`.

There is no flag for `api_hash`. Putting secrets on argv would leak them into the process list.

### Targets

`--chat` is repeatable. Each value is one of:

| Form | Example |
|------|---------|
| username | `@group` or `group` |
| public link | `https://t.me/group` (a trailing `/123` message id is ignored) |
| invite | `https://t.me/+HASH`, `https://t.me/joinchat/HASH`, `tg://join?invite=HASH` |
| private link | `https://t.me/c/123456789/10` → chat id `-100123456789` |
| numeric id | `-1001234567890` (must already be in the session cache; run `list-chats` once) |
| deep link | `tg://resolve?domain=group` |

An invite link **joins** the account to that chat.

### Filters

| Flag | JSON field | Meaning |
|------|------------|---------|
| `--ext pdf,zip` | `ext` | Case-insensitive suffix. `.tar.gz` is allowed. Empty = all |
| `--min-size 1MB` | `minSize` | Bytes. Units are powers of 1024 (`KB`, `MB`, `GB`) |
| `--max-size 2GB` | `maxSize` | Same |
| `--since 2024-01-01` | `since` | UTC, inclusive. `YYYY-MM-DD` or ISO-8601 |
| `--until 2024-01-31` | `until` | UTC, inclusive (end of that day for a date) |
| `--sender ada` | `sender` | User id, `@username`, or display-name substring |
| `--name report` | `name` | Filename substring, case-insensitive |
| `--name-regex` | `nameRegex` | Python regex |
| `--photos` / `--no-photos` | `photos` | Photos are off unless this is set or `include_photos: true` |
| `--limit N` | `limit` | Max matching files across all chats |
| `--message-id ID` | `messageIds` | Repeatable. Restricts to those messages; other filters still apply |
| `--output DIR` | `output` | Default `downloads/`. Stored absolute |
| `--flat` | `groupByMonth: false` | Files go directly in `output` |
| `--workers N` | `workers` | 1–32, default 4. Not part of the idempotency key |
| `--no-resume` | `resume: false` | Truncate a matching part and start at byte 0 |
| `--force` | `force` | Do not reuse a completed job; do not skip a finished file (next collision name) |
| `--timeout 30m` | `timeoutSec` | `90s`, `30m`, `2h`, `1h30m`. On expiry the part is saved and the process exits 4 |
| `--flood-cap 15m` | `floodCapSec` | Longest FloodWait to sleep. Longer → exit 4 |

Documents (including video, audio, and other files sent as documents) are always included. `--photos` adds photos.

### Version

`tgdl version` and `tgdl version --json` always exit 0. They do not touch Telegram.

```json
{
  "schema": 1,
  "ok": true,
  "exitCode": 0,
  "tgdl": "1.0.0",
  "build": {"python": "3.12.3", "telethon": "1.45.0", "os": "linux", "arch": "x86_64"}
}
```

`telethon` is `null` if the package is not installed.

### Exit codes

| Code | `code` | Meaning |
|------|--------|---------|
| 0 | `ok` | Command finished successfully |
| 1 | `failure` | Task or runtime failure, or unknown job id on `status` |
| 2 | `usage` | Bad arguments or invalid JSON |
| 3 | `auth` | Missing `api_id`/`api_hash`, or the session is not logged in. HTTP 401 is the API token, not this |
| 4 | `partial` | Interrupted, timed out, or FloodWait cap. Part files and the job are resumable |
| 5 | `rejected` | Chat not accessible, bad invite, public bind refused, or a download is already running |

`status` with no jobs exits 0 and returns `"job": null`. `status JOB_ID` for an unknown id exits 1. When the job is finished, `status` exits with that job's `exitCode`. A running job exits 0; poll until `job.done` is true.

### Idempotency

Download key = SHA-256 (first 32 hex chars) of the canonical filter set: chats (order preserved, duplicates removed), absolute output, extensions, sizes, dates, sender, name, regex, limit, message ids, `groupByMonth`, `photos`, `resume`.

Not in the key: `workers`, `timeoutSec`, `floodCapSec`, `force`.

| Situation | Behavior |
|-----------|----------|
| Same key, download **running** | Return that job. `"existing": true` |
| Same key, **partial** or **cancelled** | Run it again. Parts resume. `"existing": true` |
| Same key, **complete**, every dest file still present and the same size | Return that job. No network transfer |
| Same key, complete, but **zero files** or a dest is missing | New job |
| `--force` | New job, and finished files get the next collision name. A `.part` of the same file is still resumed. Rejected (exit 5) if another download task is active |
| Second download with a **different** key while one is active | Exit 5. Body includes `jobId` when known |
| Scan / list-chats | In-flight duplicate returns the running job. A finished scan is never cached; messages change |

### Resume

For each file the downloader plans a path, then:

1. Writes `filename.ext.tgdl.json` (sidecar) and downloads into `filename.ext.part`.
2. On restart, if the sidecar `fileUniqueId` and `fileSize` match, truncates the part to a multiple of **4096** (Telegram's minimum `upload.getFile` offset) and continues with `iter_download` at `request_size` **524288**.
3. If the part length already equals `fileSize`, it is renamed and not truncated.
4. When the byte count matches, `os.replace` moves the part onto the final name and sets `complete: true` on the sidecar.
5. Ctrl+C writes the chunk already received, fsyncs, updates the sidecar, and exits 4.

Skip rules:

- Sidecar matches this file and the final file has the expected size → skip.
- No sidecar, final file size equals the expected size → skip (size heuristic; a different file with the same name and size will be skipped).
- Sidecar has a different `fileUniqueId` → `name (1).ext`, even if the sizes match.

`fileUniqueId` is the decimal MTProto document or photo id, not a Bot API `file_id`.

Default layout: `output/<sanitized chat title>/<YYYY-MM>/<filename>`. `--flat` disables the extra directories. Duplicate messages that share a directory and `fileUniqueId`+size are one transfer; the extra rows have `"duplicate": true` and `"state": "skipped"`.

### Examples

```bash
tgdl login
tgdl list-chats --groups-only --json
tgdl scan --chat @mygroup --ext pdf,zip --min-size 100KB --since 2024-01-01 --json
tgdl download --chat @mygroup --chat -1001234567890 --workers 4 --output ./downloads --json
tgdl download --chat 'https://t.me/+InviteHash' --timeout 2h --json
tgdl status --json
```

Human stdout for `download` (without `--json`) is one destination path per completed or skipped file. Progress is on stderr.

---

## 3. Job object

Returned as `job` from scan, download, list-chats, status, task, and cancel.

```json
{
  "schema": 1,
  "id": "job_0123456789abcdef",
  "key": "0123456789abcdef0123456789abcdef",
  "kind": "download",
  "status": "partial",
  "ok": false,
  "done": true,
  "exitCode": 4,
  "createdAt": "2026-01-01T00:00:00+00:00",
  "updatedAt": "2026-01-01T00:05:00+00:00",
  "error": "interrupted; partial files can be resumed",
  "params": {},
  "counts": {
    "matched": 2,
    "listed": 0,
    "pending": 0,
    "active": 0,
    "downloaded": 1,
    "skipped": 0,
    "failed": 0,
    "partial": 1
  },
  "bytesWritten": 1048576,
  "bytesTotal": 2097152,
  "files": [],
  "resolvedChats": [
    {"id": "-100123", "title": "Files", "input": "@files", "kind": "group"}
  ]
}
```

- `status`: `queued | running | complete | error | partial | cancelled | rejected`. `queued` is reserved and not produced in v1.
- `done` is true for `complete`, `error`, `partial`, `cancelled`, `rejected`.
- `ok` is true only for `complete`.
- `exitCode` is null while running, otherwise the process exit code.
- `bytesTotal` is null if any file has an unknown size.
- `kind: "list-chats"` puts dialogs in `resolvedChats` and leaves `files` empty.
- `error` is a short English string or null.

### File object

```json
{
  "chatId": "-100123",
  "chatTitle": "Files",
  "messageId": "42",
  "date": "2024-05-02T00:00:00+00:00",
  "senderId": "9",
  "senderUsername": "ada",
  "senderName": "Ada",
  "fileName": "report.pdf",
  "fileSize": 1000,
  "mimeType": "application/pdf",
  "fileUniqueId": "5123456789012345678",
  "kind": "document",
  "extension": ".pdf",
  "dest": "/abs/downloads/Files/2024-05/report.pdf",
  "part": "/abs/downloads/Files/2024-05/report.pdf.part",
  "sidecar": "/abs/downloads/Files/2024-05/report.pdf.tgdl.json",
  "bytesWritten": 4096,
  "resumeOffset": 4096,
  "state": "partial",
  "plannedAction": "resume",
  "duplicate": false,
  "error": null
}
```

- `state`: `listed | pending | active | complete | skipped | failed | partial`. Scan rows stay `listed` even when `plannedAction` is `skip`.
- `plannedAction`: `download | resume | finish | skip`, or null when scan had no output directory (v1 always has one, the configured download dir).
- `kind`: `document` or `photo`.
- `senderId` / `fileSize` / `mimeType` may be null.
- `counts.downloaded` is the number of files in state `complete` (transferred this run, or renamed from a finished part). `skipped` means no network transfer this run.

### Sidecar (`*.tgdl.json`)

```json
{
  "v": 1,
  "messageId": "42",
  "chatId": "-100123",
  "fileSize": 1000,
  "fileUniqueId": "5123456789012345678",
  "fileName": "report.pdf",
  "bytesWritten": 4096,
  "alignedOffset": 4096,
  "requestSize": 524288,
  "complete": false,
  "updatedAt": "2026-01-01T00:00:00+00:00",
  "partPath": "/abs/report.pdf.part",
  "finalPath": "/abs/report.pdf"
}
```

### Error object

Used for CLI failures that do not produce a job, and for HTTP 4xx:

```json
{"schema": 1, "ok": false, "exitCode": 3, "code": "auth", "error": "not logged in; run: tgdl login"}
```

Extra fields may appear (`jobId`, `url`, `port`). Ignore unknown fields.

---

## 4. HTTP API (`/api/v1`)

Base URL `http://127.0.0.1:<port>` (default port **18765**). The live port is `.tgdl/api-port`. The pid is `.tgdl/api-pid`.

**Auth.** `Authorization: Bearer <token>` or `X-Token: <token>`. The token is `.tgdl/api-token` (created on first `serve`, mode `0600`) or `TGDL_API_TOKEN` if that is set when serve starts. Comparison is constant-time. A missing or wrong token is **401** with `exitCode: 3` and `code: "auth"` — this is the API token, not the Telegram session.

**Host.** The `Host` header must be `127.0.0.1:<port>` or `localhost:<port>`. Anything else is **403** / exit 5 (DNS-rebinding guard). The socket is bound to `127.0.0.1` only. `--host 0.0.0.0` is rejected. No CORS. `OPTIONS` is 405.

**Body.** JSON object, at most 1 MiB. Unknown keys are ignored.

| Method & path | Body / query | Response |
|---------------|--------------|----------|
| `GET /api/v1/ping` | — | `{schema, ok, app:"tgdl", version, pid, python, telethon}` |
| `GET /api/v1/version` | — | version object, plus `ok` |
| `GET /api/v1/help` | — | help catalog, plus `ok` |
| `GET /api/v1/chats` | `limit` (default 100, max 5000), `groupsOnly` (`true`/`false`), `wait` (default `true`) | job envelope. `groupsOnly` skips private users and keeps groups and channels |
| `POST /api/v1/scan` | filter object; `wait` default **false** | job envelope |
| `POST /api/v1/download` | filter object; `wait` default **false** | job envelope |
| `GET /api/v1/task?id=` | — | `{schema, ok, exitCode, job}` or 404 |
| `GET /api/v1/tasks?limit=` | `limit` default 50, max 200 | `{schema, ok, tasks:[job...]}` |
| `GET /api/v1/status` | optional `id` | `{schema, ok, exitCode, job}`. `job` is null when nothing has run |
| `POST /api/v1/cancel` | `{id}` | `{schema, ok, exitCode, job}`. 404 if unknown |

### Job envelope

```json
{"schema": 1, "ok": true, "existing": false, "exitCode": null, "job": {}}
```

| HTTP | When |
|------|------|
| 200 | Ping, help, version, a finished job with exit 0, or a finished partial job (exit 4) so the resume JSON is a success body |
| 202 | `wait` was false and the job is still `running` |
| 400 | Usage (`exitCode` 2) |
| 401 | Bad or missing API token |
| 403 | Bad Host, or a finished job whose `exitCode` is 3 (Telegram auth) |
| 404 | Unknown job id |
| 409 | Finished job with exit 1 or 5, or a download rejected before a new job starts |
| 500 | Uncaught server error (`exitCode` 1) |

`ok: true` means the HTTP handler ran. It does **not** mean the download succeeded. Branch on `job.done`, then `job.exitCode` / `job.status`.

`wait: true` blocks the HTTP request until the job finishes (still on the same asyncio loop; do not stack these from inside tgdl). The CLI always posts `wait: false` and polls `GET /api/v1/task`.

### curl

```bash
token=$(tr -d ' \n' < .tgdl/api-token)
port=$(tr -d ' \n' < .tgdl/api-port)
curl -fsS -H "Authorization: Bearer $token" "http://127.0.0.1:$port/api/v1/ping"
curl -fsS -H "Authorization: Bearer $token" -H 'Content-Type: application/json' \
  -d '{"chats":["@example"],"ext":["pdf"],"workers":4,"wait":false}' \
  "http://127.0.0.1:$port/api/v1/download"
```

`serve --json` prints one line and then keeps running:

```json
{"schema":1,"ok":true,"url":"http://127.0.0.1:18765","pid":123,"tokenFile":"/abs/.tgdl/api-token","port":18765}
```

The token value is not printed. Read the file.

If serve is already up, a second `tgdl serve` exits 5. The CLI then proxies `list-chats`, `scan`, and `download` to it so two processes do not open the session. `status` reads the job files on disk (the server flushes them at least every 0.4s and on every state change).

---

## 5. Configuration

See the table in `README.md`. Precedence is CLI > environment > `config.yaml` / `.env` > defaults. `.env` does not override variables that are already set.

`api_id: 0` and `api_hash: "replace-me"` count as unset.

Relative paths are resolved against the process working directory. Start `tgdl serve` in the directory that should own `.tgdl/` and `downloads/`.

---

## 6. Concurrency and FloodWait

- `--workers` is the number of concurrent `iter_download` streams. They share one client and one connection pool. The default 4 is the recommended ceiling; higher values mostly buy FloodWait.
- The client is created with `flood_sleep_threshold=0` and `receive_updates=False`. Telegram's wait is slept in full, plus one second, after the part file is fsynced.
- Device model reported to Telegram is `tgdl` plus the package version. It is not a browser impersonation string.

---

## 7. What v1 does not do

- No WebView, no web UI, no bot token login.
- No HTTP endpoint for the interactive phone-code login.
- No multi-process downloads against one session file.
- No per-file multi-connection download. Workers download different files.
