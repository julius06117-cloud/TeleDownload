"""Stable automation constants. Add values; do not renumber or rename."""

from __future__ import annotations

SCHEMA = 1
APP_NAME = "tgdl"

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2
EXIT_AUTH = 3
EXIT_PARTIAL = 4
EXIT_REJECTED = 5

EXIT_NAME = {
    EXIT_OK: "ok",
    EXIT_FAILURE: "failure",
    EXIT_USAGE: "usage",
    EXIT_AUTH: "auth",
    EXIT_PARTIAL: "partial",
    EXIT_REJECTED: "rejected",
}

# Job.status. `queued` is reserved so a future queue does not break clients.
JOB_STATUSES = (
    "queued",
    "running",
    "complete",
    "error",
    "partial",
    "cancelled",
    "rejected",
)

# File record state.
FILE_STATES = (
    "listed",
    "pending",
    "active",
    "complete",
    "skipped",
    "failed",
    "partial",
)

# upload.getFile alignment. 512 KiB divides 1 MiB and is Telethon's max chunk.
ALIGNMENT = 4096
REQUEST_SIZE = 512 * 1024

assert REQUEST_SIZE % ALIGNMENT == 0
assert (1024 * 1024) % REQUEST_SIZE == 0


def error_payload(exit_code: int, message: str, **extra: object) -> dict:
    body = {
        "schema": SCHEMA,
        "ok": False,
        "exitCode": exit_code,
        "code": EXIT_NAME.get(exit_code, "failure"),
        "error": message,
    }
    body.update(extra)
    return body
