"""Hornet-shaped task view layered onto a tgdl job.

`job.status` stays the Telegram lifecycle (`running`, `partial`, …).
`job.task` is the HornetDownloader task object the UI renders.
`speedBps` is 0 unless that task status is `active`.
"""

from __future__ import annotations

import time
from collections import deque
from pathlib import Path

# Hornet task.status. tgdl values map onto these; they are not a replacement.
TASK_STATUSES = ("waiting", "active", "paused", "complete", "error", "removed")

_HORNET_DONE = {"complete", "error", "removed"}


def hornet_status(job: dict) -> str:
    status = job.get("status")
    if status == "queued":
        return "waiting"
    if status == "running":
        return "active"
    if status in {"paused", "partial", "cancelled"}:
        return "paused"
    if status == "complete":
        return "complete"
    if status == "removed":
        return "removed"
    if status in {"error", "rejected"}:
        return "error"
    if job.get("done"):
        return "error"
    return "waiting"


def task_source(job: dict) -> str:
    parts: list[str] = []
    for chat in job.get("resolvedChats") or []:
        if not isinstance(chat, dict):
            continue
        title = str(chat.get("title") or "").strip()
        username = str(chat.get("username") or "").strip()
        if username and title:
            parts.append(f"{title} (@{username})")
        elif username:
            parts.append(f"@{username}")
        elif title:
            parts.append(title)
        elif chat.get("id"):
            parts.append(str(chat["id"]))
    if parts:
        return ", ".join(parts)
    chats = (job.get("params") or {}).get("chats") or []
    return ", ".join(str(item) for item in chats if str(item).strip())


def task_name(job: dict) -> str:
    files = [item for item in (job.get("files") or []) if isinstance(item, dict)]
    names = [str(item.get("fileName")) for item in files if item.get("fileName")]
    source = task_source(job)
    if len(names) == 1:
        return names[0]
    if len(names) > 1:
        return f"{len(names)} 个文件" + (f" · {source}" if source else "")
    kind = job.get("kind")
    if kind == "list-chats":
        return "对话列表"
    if kind == "scan":
        return f"扫描 {source}".strip()
    if source:
        return source
    return str(kind or "任务")


def _paths(job: dict) -> list[str]:
    found: list[str] = []
    for item in job.get("files") or []:
        if not isinstance(item, dict):
            continue
        dest = item.get("dest")
        if dest and str(dest) not in found:
            found.append(str(dest))
    return found


def _resuming(job: dict, status: str) -> bool:
    if status != "active":
        return False
    for item in job.get("files") or []:
        if not isinstance(item, dict) or item.get("state") != "active":
            continue
        if item.get("plannedAction") == "resume":
            return True
        try:
            offset = int(item.get("resumeOffset") or 0)
        except (TypeError, ValueError):
            offset = 0
        if offset > 0:
            return True
    return False


def apply_task_view(job: dict, *, speed_bps: float = 0) -> dict:
    """Attach Hornet fields. Existing tgdl keys are left in place."""
    status = hornet_status(job)
    done_bytes = int(job.get("bytesWritten") or 0)
    raw_total = job.get("bytesTotal")
    total_bytes = int(raw_total) if isinstance(raw_total, (int, float)) and raw_total is not None else None
    if raw_total is None:
        total_bytes = None
    if status == "active" and speed_bps > 0:
        speed = int(round(speed_bps))
    else:
        speed = 0
    if isinstance(total_bytes, int) and total_bytes > 0:
        progress = max(0.0, min(1.0, done_bytes / total_bytes))
    elif status == "complete":
        progress = 1.0
    else:
        progress = 0.0
    if status == "active" and speed > 0 and isinstance(total_bytes, int) and total_bytes > done_bytes:
        eta: int | None = int((total_bytes - done_bytes) / speed)
    else:
        eta = None
    added = job.get("addedBy")
    if added not in {"agent", "user"}:
        added = "agent"
        job["addedBy"] = added
    directory = str((job.get("params") or {}).get("output") or "")
    source = task_source(job)
    name = task_name(job)
    paths = _paths(job)
    resuming = _resuming(job, status)
    error = job.get("error")
    task = {
        "id": job.get("id"),
        "status": status,
        "done": status in _HORNET_DONE,
        "ok": status == "complete",
        "name": name,
        "dir": directory,
        "files": paths,
        "totalBytes": total_bytes if total_bytes is not None else 0,
        "doneBytes": done_bytes,
        "progress": float(progress),
        "speedBps": speed,
        "error": error,
        "source": source,
        "addedBy": added,
        "resuming": resuming,
        "etaSec": eta,
    }
    if total_bytes is None:
        # Unknown Telegram sizes stay visible as 0 so the field is always a number,
        # matching Hornet. `bytesTotal` on the job is still null.
        task["totalBytes"] = 0
    job["task"] = task
    job["taskStatus"] = status
    job["name"] = name
    job["dir"] = directory
    job["totalBytes"] = task["totalBytes"]
    job["doneBytes"] = done_bytes
    job["progress"] = float(progress)
    job["speedBps"] = speed
    job["source"] = source
    job["addedBy"] = added
    job["paths"] = paths
    job["resuming"] = resuming
    job["etaSec"] = eta
    return job


class SpeedMeter:
    """Bytes/second over a short window. Inactive tasks report 0 and drop history."""

    def __init__(self, *, window: float = 4.0) -> None:
        self.window = window
        self._points: dict[str, deque[tuple[float, int]]] = {}

    def observe(self, job_id: str, done_bytes: int, *, active: bool, now: float | None = None) -> float:
        if not job_id or not active:
            self._points.pop(job_id, None)
            return 0.0
        moment = time.monotonic() if now is None else now
        points = self._points.setdefault(job_id, deque())
        if not points or points[-1][1] != done_bytes or moment - points[-1][0] >= 0.25:
            points.append((moment, int(done_bytes)))
        cutoff = moment - self.window
        while len(points) > 1 and points[0][0] < cutoff:
            points.popleft()
        if len(points) < 2:
            return 0.0
        dt = points[-1][0] - points[0][0]
        if dt < 0.2:
            return 0.0
        delta = points[-1][1] - points[0][1]
        if delta <= 0:
            return 0.0
        return delta / dt


def delete_recorded_files(job: dict) -> list[str]:
    """Unlink dest, part, and sidecar paths recorded on the job. Returns error notes."""
    notes: list[str] = []
    seen: set[str] = set()
    for item in job.get("files") or []:
        if not isinstance(item, dict):
            continue
        for key in ("part", "sidecar", "dest"):
            raw = item.get(key)
            if not raw:
                continue
            text = str(raw)
            if text in seen:
                continue
            seen.add(text)
            path = Path(text)
            try:
                if path.is_file():
                    path.unlink()
            except OSError as exc:
                notes.append(f"{path.name}: {exc}")
    return notes
