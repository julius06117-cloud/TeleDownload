"""Machine-readable command catalog. Keep this aligned with docs/AGENT_API.md."""

from __future__ import annotations

import importlib.metadata
import platform

from tgdl import __version__
from tgdl.contract import (
    APP_NAME,
    EXIT_NAME,
    FILE_STATES,
    JOB_STATUSES,
    SCHEMA,
)


def telethon_version() -> str | None:
    try:
        return importlib.metadata.version("telethon")
    except importlib.metadata.PackageNotFoundError:
        return None


def version_payload() -> dict:
    return {
        "schema": SCHEMA,
        "tgdl": __version__,
        "build": {
            "python": platform.python_version(),
            "telethon": telethon_version(),
            "os": platform.system().lower(),
            "arch": platform.machine(),
        },
    }


def help_payload() -> dict:
    return {
        "schema": SCHEMA,
        "app": APP_NAME,
        "version": __version__,
        "stdout": "JSON when --json is set; human text otherwise",
        "stderr": "logs and progress",
        "exitCodes": [
            {"code": code, "name": name, "meaning": meaning}
            for code, name, meaning in (
                (0, EXIT_NAME[0], "Command finished successfully"),
                (1, EXIT_NAME[1], "Task or runtime failure"),
                (2, EXIT_NAME[2], "Bad arguments"),
                (3, EXIT_NAME[3], "API credentials or Telegram session unavailable"),
                (4, EXIT_NAME[4], "Interrupted or timed out; partial files and the job can be resumed"),
                (5, EXIT_NAME[5], "Target or request rejected"),
            )
        ],
        "jobStatus": list(JOB_STATUSES),
        "fileState": list(FILE_STATES),
        "commands": [
            {
                "name": "login",
                "summary": "Interactive MTProto login. Writes the session file.",
                "flags": ["--phone", "--session", "--json"],
            },
            {
                "name": "list-chats",
                "summary": "List dialogs to pick targets.",
                "flags": ["--limit", "--groups-only", "--json"],
            },
            {
                "name": "scan",
                "summary": "List matching files. Does not download or write sidecars.",
                "flags": [
                    "--chat",
                    "--ext",
                    "--min-size",
                    "--max-size",
                    "--since",
                    "--until",
                    "--sender",
                    "--name",
                    "--name-regex",
                    "--photos",
                    "--no-photos",
                    "--limit",
                    "--message-id",
                    "--output",
                    "--flat",
                    "--json",
                ],
            },
            {
                "name": "download",
                "summary": "Download matching files with a bounded worker pool. Resume is on by default.",
                "flags": [
                    "--chat",
                    "--ext",
                    "--min-size",
                    "--max-size",
                    "--since",
                    "--until",
                    "--sender",
                    "--name",
                    "--name-regex",
                    "--photos",
                    "--no-photos",
                    "--limit",
                    "--message-id",
                    "--output",
                    "--flat",
                    "--workers",
                    "--no-resume",
                    "--force",
                    "--timeout",
                    "--flood-cap",
                    "--json",
                ],
            },
            {
                "name": "status",
                "summary": "Show the latest job, or one job id.",
                "flags": ["JOB_ID", "--job", "--json"],
            },
            {
                "name": "serve",
                "summary": "Loopback HTTP API on 127.0.0.1. Same jobs as the CLI.",
                "flags": ["--host", "--port", "--json"],
            },
            {"name": "version", "summary": "Version and runtime.", "flags": ["--json"]},
            {"name": "help", "summary": "This catalog.", "flags": ["--json"]},
        ],
        "endpoints": [
            {"method": "GET", "path": "/api/v1/ping"},
            {"method": "GET", "path": "/api/v1/version"},
            {"method": "GET", "path": "/api/v1/help"},
            {"method": "GET", "path": "/api/v1/chats"},
            {"method": "POST", "path": "/api/v1/scan"},
            {"method": "POST", "path": "/api/v1/download"},
            {"method": "GET", "path": "/api/v1/task"},
            {"method": "GET", "path": "/api/v1/tasks"},
            {"method": "GET", "path": "/api/v1/status"},
            {"method": "POST", "path": "/api/v1/cancel"},
        ],
        "notes": [
            "Ids that can exceed 2^53 (chatId, messageId, senderId, fileUniqueId) are JSON strings.",
            "Workers are asyncio tasks sharing one TelegramClient, bounded by --workers (default 4, max 32).",
            "Resume writes name.ext.part plus name.ext.tgdl.json and renames the part when the size matches.",
            "A second download with the same chats, filters, and output returns the existing job unless --force.",
            "Do not put api_hash on the command line. Use the environment or a gitignored config file.",
        ],
    }


def help_text() -> str:
    payload = help_payload()
    lines = [
        f"{payload['app']} {payload['version']}",
        "stdout: result (JSON with --json). stderr: logs and progress.",
        "",
        "commands:",
    ]
    for command in payload["commands"]:
        flags = " ".join(command["flags"])
        lines.append(f"  {command['name']:<12} {command['summary']}")
        lines.append(f"               {flags}")
    lines.append("")
    lines.append("exit codes:")
    for item in payload["exitCodes"]:
        lines.append(f"  {item['code']}  {item['name']:<10} {item['meaning']}")
    lines.append("")
    lines.append("HTTP: tgdl serve  then  GET/POST /api/v1/*  with Authorization: Bearer <token>")
    lines.append("token file: $TGDL_DATA_DIR/api-token   (default .tgdl/api-token)")
    return "\n".join(lines) + "\n"
