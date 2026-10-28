"""In-memory media records and the JSON file object written on jobs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class Planned:
    action: str  # download | resume | finish | skip
    final_path: Path
    part_path: Path
    sidecar_path: Path
    offset: int
    duplicate: bool = False


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


@dataclass(frozen=True)
class MediaItem:
    chat_id: int
    chat_title: str
    message_id: int
    date: datetime
    sender_id: int | None
    sender_username: str | None
    sender_name: str | None
    file_name: str
    file_size: int | None
    mime_type: str | None
    file_unique_id: str
    kind: str  # document | photo
    extension: str

    def to_json(self, planned: Planned | None = None, *, state: str = "listed") -> dict:
        dest = part = sidecar = None
        offset = 0
        action = None
        duplicate = False
        written = 0
        if planned is not None:
            dest = str(planned.final_path)
            part = str(planned.part_path)
            sidecar = str(planned.sidecar_path)
            offset = planned.offset
            action = planned.action
            duplicate = planned.duplicate
            if planned.action in {"skip", "finish"}:
                written = planned.offset
            elif planned.action == "resume":
                written = planned.offset
        return {
            "chatId": str(self.chat_id),
            "chatTitle": self.chat_title,
            "messageId": str(self.message_id),
            "date": _iso(self.date),
            "senderId": None if self.sender_id is None else str(self.sender_id),
            "senderUsername": self.sender_username,
            "senderName": self.sender_name,
            "fileName": self.file_name,
            "fileSize": self.file_size,
            "mimeType": self.mime_type,
            "fileUniqueId": str(self.file_unique_id),
            "kind": self.kind,
            "extension": self.extension,
            "dest": dest,
            "part": part,
            "sidecar": sidecar,
            "bytesWritten": written,
            "resumeOffset": offset,
            "state": state,
            "plannedAction": action,
            "duplicate": duplicate,
            "error": None,
        }


def state_for(planned: Planned) -> str:
    if planned.action in {"skip", "finish"}:
        return "skipped"
    return "pending"
