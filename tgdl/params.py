"""Normalize CLI flags and API bodies into one job `params` object.

Normalization is idempotent: running it twice yields the same dict. Paths are
absolute. Telegram ids inside `messageIds` are strings.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tgdl.config import Config
from tgdl.errors import UsageError
from tgdl.util import parse_duration, parse_size

_MAX_WORKERS = 32


def _blank(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _as_bool(value: object, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    raise UsageError(f"expected a boolean, got {value!r}")


def _parse_time(value: str, *, end: bool) -> datetime:
    text = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        moment = datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        if end:
            return moment + timedelta(days=1) - timedelta(microseconds=1)
        return moment
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise UsageError(f"invalid date: {value}") from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _dump_time(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


def _extensions(value: object) -> list[str]:
    if value is None:
        return []
    tokens: list[str] = []
    items = value if isinstance(value, list) else [value]
    for item in items:
        for part in str(item).split(","):
            part = part.strip().lower()
            if not part:
                continue
            if not part.startswith("."):
                part = "." + part
            tokens.append(part)
    return sorted(set(tokens))


def _message_ids(value: object) -> list[str] | None:
    if value is None:
        return None
    items = value if isinstance(value, list) else [value]
    out: list[str] = []
    for item in items:
        text = str(item).strip()
        if not text:
            continue
        if not re.fullmatch(r"\d+", text):
            raise UsageError(f"message id must be an integer, got {item!r}")
        out.append(str(int(text)))
    return sorted(set(out), key=int) or None


def _abs_dir(value: str | Path) -> str:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return str(path.resolve())


def normalize_params(raw: dict, *, kind: str, cfg: Config) -> dict:
    if kind not in {"scan", "download"}:
        raise UsageError(f"unknown kind {kind}")
    chats_raw = raw.get("chats")
    if chats_raw is None:
        chats_raw = raw.get("chat")
    if isinstance(chats_raw, str):
        chats_raw = [chats_raw]
    if not isinstance(chats_raw, list) or not chats_raw:
        raise UsageError("at least one chat is required")
    chats: list[str] = []
    for chat in chats_raw:
        text = str(chat).strip()
        if text and text not in chats:
            chats.append(text)
    if not chats:
        raise UsageError("at least one chat is required")

    ext = _extensions(raw.get("ext") or raw.get("extensions"))
    min_size = raw.get("minSize", raw.get("min_size"))
    max_size = raw.get("maxSize", raw.get("max_size"))
    min_parsed = None if _blank(min_size) is None and not isinstance(min_size, (int, float)) else (
        None if min_size is None or min_size == "" else parse_size(min_size)
    )
    max_parsed = None if max_size is None or max_size == "" else parse_size(max_size)
    if min_parsed is not None and max_parsed is not None and min_parsed > max_parsed:
        raise UsageError("min size is greater than max size")

    since_raw = _blank(raw.get("since"))
    until_raw = _blank(raw.get("until"))
    since = _dump_time(_parse_time(since_raw, end=False)) if since_raw else None
    until = _dump_time(_parse_time(until_raw, end=True)) if until_raw else None
    if since and until and since > until:
        raise UsageError("since is after until")

    name_regex = _blank(raw.get("nameRegex", raw.get("name_regex")))
    if name_regex is not None:
        try:
            re.compile(name_regex)
        except re.error as exc:
            raise UsageError(f"invalid name regex: {exc}") from exc

    limit = raw.get("limit")
    if limit is None or limit == "":
        limit_parsed = None
    else:
        try:
            limit_parsed = int(limit)
        except (TypeError, ValueError) as exc:
            raise UsageError("limit must be an integer") from exc
        if limit_parsed < 1:
            raise UsageError("limit must be >= 1")

    if "photos" in raw:
        photos = _as_bool(raw.get("photos"), cfg.include_photos)
    elif raw.get("noPhotos") or raw.get("no_photos"):
        photos = False
    else:
        photos = cfg.include_photos

    if "groupByMonth" in raw:
        group = _as_bool(raw.get("groupByMonth"), cfg.group_by_month)
    elif raw.get("flat"):
        group = False
    else:
        group = cfg.group_by_month

    output_raw = raw.get("output") or cfg.output
    workers_raw = raw.get("workers", cfg.workers)
    try:
        workers = int(workers_raw)
    except (TypeError, ValueError) as exc:
        raise UsageError("workers must be an integer") from exc
    if not 1 <= workers <= _MAX_WORKERS:
        raise UsageError(f"workers must be between 1 and {_MAX_WORKERS}")

    resume = _as_bool(raw.get("resume"), True)
    if raw.get("noResume") or raw.get("no_resume"):
        resume = False
    force = _as_bool(raw.get("force"), False)

    timeout_raw = raw.get("timeoutSec", raw.get("timeout"))
    timeout_sec = None if timeout_raw is None or timeout_raw == "" else parse_duration(timeout_raw)
    flood_raw = raw.get("floodCapSec", raw.get("floodCap", raw.get("flood_cap")))
    if flood_raw is None or flood_raw == "":
        flood_cap = cfg.flood_cap_sec
    else:
        flood_cap = parse_duration(flood_raw)

    return {
        "chats": chats,
        "output": _abs_dir(str(output_raw)),
        "workers": workers,
        "groupByMonth": group,
        "resume": resume,
        "photos": photos,
        "ext": ext,
        "minSize": min_parsed,
        "maxSize": max_parsed,
        "since": since,
        "until": until,
        "sender": _blank(raw.get("sender")),
        "name": _blank(raw.get("name")),
        "nameRegex": name_regex,
        "limit": limit_parsed,
        "messageIds": _message_ids(raw.get("messageIds", raw.get("message_ids"))),
        "timeoutSec": timeout_sec,
        "floodCapSec": flood_cap,
        "force": force,
        "kind": kind,
    }


def query_from_params(params: dict):
    """Build a FileQuery. Imported lazily by callers that already normalized."""
    from tgdl.filters import FileQuery

    since = datetime.fromisoformat(params["since"]) if params.get("since") else None
    until = datetime.fromisoformat(params["until"]) if params.get("until") else None
    regex = re.compile(params["nameRegex"]) if params.get("nameRegex") else None
    ids = params.get("messageIds")
    return FileQuery(
        exts=frozenset(params.get("ext") or []),
        min_size=params.get("minSize"),
        max_size=params.get("maxSize"),
        since=since,
        until=until,
        sender=params.get("sender"),
        name=params.get("name"),
        name_regex=regex,
        photos=bool(params.get("photos")),
        message_ids=None if ids is None else frozenset(ids),
    )
