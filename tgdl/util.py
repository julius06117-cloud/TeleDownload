"""Small parsing and filesystem helpers. No Telethon imports."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from tgdl.errors import UsageError

_SIZE = re.compile(r"(?i)^\s*(\d+(?:\.\d+)?)\s*([kmgt]?i?b?|b)?\s*$")
_SIZE_UNIT = {
    "": 1,
    "b": 1,
    "k": 1024,
    "kb": 1024,
    "kib": 1024,
    "m": 1024**2,
    "mb": 1024**2,
    "mib": 1024**2,
    "g": 1024**3,
    "gb": 1024**3,
    "gib": 1024**3,
    "t": 1024**4,
    "tb": 1024**4,
    "tib": 1024**4,
}
_DURATION = re.compile(
    r"(?i)^\s*(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?\s*(?:(\d+)\s*s)?\s*$"
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def utcnow_iso() -> str:
    return utcnow().replace(microsecond=0).isoformat()


def parse_size(value: str | int | float) -> int:
    """Parse a byte count. Units are powers of 1024 (`1.5MB` = 1572864)."""
    if isinstance(value, bool):
        raise UsageError("size must be a number")
    if isinstance(value, (int, float)):
        if value < 0:
            raise UsageError("size must be >= 0")
        return int(value)
    match = _SIZE.match(str(value))
    if not match:
        raise UsageError(f"invalid size: {value}")
    number = float(match.group(1))
    unit = (match.group(2) or "").lower()
    if unit not in _SIZE_UNIT:
        raise UsageError(f"invalid size unit: {value}")
    return int(number * _SIZE_UNIT[unit])


def parse_duration(value: str | int | float) -> int:
    """Parse seconds. Accepts `90`, `90s`, `30m`, `2h`, `1h30m`."""
    if isinstance(value, bool):
        raise UsageError("duration must be a number of seconds")
    if isinstance(value, (int, float)):
        seconds = int(value)
    else:
        text = str(value).strip()
        if text.isdigit():
            seconds = int(text)
        else:
            match = _DURATION.match(text)
            if not match or not any(match.groups()):
                raise UsageError(f"invalid duration: {value}")
            hours = int(match.group(1) or 0)
            minutes = int(match.group(2) or 0)
            secs = int(match.group(3) or 0)
            seconds = hours * 3600 + minutes * 60 + secs
    if seconds <= 0:
        raise UsageError("duration must be > 0")
    return seconds


def flood_sleep_seconds(server_seconds: int, *, padding: int = 1) -> int:
    """Honor Telegram's FloodWait and add a small pad so the next call is legal."""
    if server_seconds < 0:
        raise ValueError("server_seconds must be >= 0")
    return int(server_seconds) + padding


def atomic_write_text(path: Path, text: str, *, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def atomic_write_json(path: Path, obj: object, *, mode: int = 0o644) -> None:
    atomic_write_text(
        path,
        json.dumps(obj, ensure_ascii=False, indent=2) + "\n",
        mode=mode,
    )


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def chmod_private(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
