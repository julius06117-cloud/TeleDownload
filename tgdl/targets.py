"""Parse a chat reference. Resolution against Telegram happens later."""

from __future__ import annotations

import re
from dataclasses import dataclass

_INVITE_PLUS = re.compile(r"(?:https?://)?(?:t|telegram)\.me/\+([A-Za-z0-9_-]+)", re.I)
_INVITE_JOIN = re.compile(r"(?:https?://)?(?:t|telegram)\.me/joinchat/([A-Za-z0-9_-]+)", re.I)
_TG_JOIN = re.compile(r"tg://join\?invite=([A-Za-z0-9_-]+)", re.I)
_PUBLIC = re.compile(
    r"(?:https?://)?(?:t|telegram)\.me/([A-Za-z0-9_]+)(?:/\d+)?",
    re.I,
)
_TG_RESOLVE = re.compile(r"tg://resolve\?domain=([A-Za-z0-9_]+)", re.I)
_PRIVATE = re.compile(r"(?:https?://)?(?:t|telegram)\.me/c/(\d+)(?:/\d+)?", re.I)


@dataclass(frozen=True)
class ParsedTarget:
    raw: str
    kind: str  # username | invite | id
    value: str


def parse_target(raw: str) -> ParsedTarget:
    text = raw.strip()
    bare = text.split("?")[0].split("#")[0].rstrip("/")
    # tg:// links keep their query, so match the original text for those.
    invite = _INVITE_PLUS.match(bare) or _INVITE_JOIN.match(bare) or _TG_JOIN.match(text)
    if invite:
        return ParsedTarget(raw=raw, kind="invite", value=invite.group(1))
    private = _PRIVATE.match(bare)
    if private:
        return ParsedTarget(raw=raw, kind="id", value=f"-100{private.group(1)}")
    resolve = _TG_RESOLVE.match(text)
    if resolve:
        return ParsedTarget(raw=raw, kind="username", value=resolve.group(1))
    public = _PUBLIC.match(bare)
    if public and public.group(1).lower() not in {"joinchat", "c"}:
        return ParsedTarget(raw=raw, kind="username", value=public.group(1))
    if re.fullmatch(r"-?\d+", text):
        return ParsedTarget(raw=raw, kind="id", value=text)
    return ParsedTarget(raw=raw, kind="username", value=text.lstrip("@"))
