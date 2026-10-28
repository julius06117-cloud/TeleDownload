"""Pure file filters applied after a message is turned into a MediaItem."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from tgdl.models import MediaItem


def size_matches(size: int | None, min_size: int | None, max_size: int | None) -> bool:
    if min_size is None and max_size is None:
        return True
    if size is None:
        return False
    if min_size is not None and size < min_size:
        return False
    if max_size is not None and size > max_size:
        return False
    return True


def extension_matches(filename: str, exts: frozenset[str]) -> bool:
    if not exts:
        return True
    lower = filename.lower()
    return any(lower.endswith(ext) for ext in exts)


def sender_matches(
    sender_id: int | None,
    sender_username: str | None,
    sender_name: str | None,
    query: str | None,
) -> bool:
    if not query:
        return True
    needle = query.lstrip("@").casefold()
    if sender_id is not None and needle == str(sender_id):
        return True
    if sender_username and sender_username.casefold() == needle:
        return True
    if sender_name and needle in sender_name.casefold():
        return True
    return False


@dataclass(frozen=True)
class FileQuery:
    exts: frozenset[str]
    min_size: int | None
    max_size: int | None
    since: datetime | None
    until: datetime | None
    sender: str | None
    name: str | None
    name_regex: re.Pattern[str] | None
    photos: bool
    message_ids: frozenset[str] | None

    def matches(self, item: MediaItem) -> bool:
        if item.kind == "photo" and not self.photos:
            return False
        if self.message_ids is not None and str(item.message_id) not in self.message_ids:
            return False
        if not extension_matches(item.file_name, self.exts):
            return False
        if not size_matches(item.file_size, self.min_size, self.max_size):
            return False
        if self.since is not None and item.date < self.since:
            return False
        if self.until is not None and item.date > self.until:
            return False
        if not sender_matches(item.sender_id, item.sender_username, item.sender_name, self.sender):
            return False
        if self.name and self.name.casefold() not in item.file_name.casefold():
            return False
        if self.name_regex is not None and self.name_regex.search(item.file_name) is None:
            return False
        return True
