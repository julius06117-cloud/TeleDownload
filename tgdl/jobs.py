"""On-disk job store. One JSON file per job plus a latest pointer."""

from __future__ import annotations

import json
import threading
from pathlib import Path

from tgdl.contract import SCHEMA
from tgdl.util import atomic_write_json, utcnow_iso

_COUNT_KEYS = (
    "matched",
    "listed",
    "pending",
    "active",
    "downloaded",
    "skipped",
    "failed",
    "partial",
)

_TERMINAL = {"complete", "error", "partial", "cancelled", "rejected"}


def empty_counts() -> dict:
    return {key: 0 for key in _COUNT_KEYS}


def recompute(job: dict) -> None:
    files = job.get("files") or []
    counts = empty_counts()
    counts["matched"] = len(files)
    written = 0
    total = 0
    known = True
    for item in files:
        state = item.get("state")
        if state == "complete":
            counts["downloaded"] += 1
        elif state in counts:
            counts[state] += 1
        written += int(item.get("bytesWritten") or 0)
        size = item.get("fileSize")
        if size is None:
            known = False
        else:
            total += int(size)
    job["counts"] = counts
    job["bytesWritten"] = written
    job["bytesTotal"] = total if known else None


def canonical_key(kind: str, params: dict) -> str:
    """Identity of a scan/download request. Workers and timeout are not part of it."""
    import hashlib

    body = {
        "kind": kind,
        "chats": list(params.get("chats") or []),
        "output": params.get("output"),
        "groupByMonth": params.get("groupByMonth"),
        "resume": params.get("resume", True),
        "photos": params.get("photos"),
        "ext": list(params.get("ext") or []),
        "minSize": params.get("minSize"),
        "maxSize": params.get("maxSize"),
        "since": params.get("since"),
        "until": params.get("until"),
        "sender": params.get("sender"),
        "name": params.get("name"),
        "nameRegex": params.get("nameRegex"),
        "limit": params.get("limit"),
        "messageIds": params.get("messageIds"),
    }
    raw = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def new_job(kind: str, params: dict, key: str | None) -> dict:
    now = utcnow_iso()
    import secrets

    job = {
        "schema": SCHEMA,
        "id": "job_" + secrets.token_hex(8),
        "key": key,
        "kind": kind,
        "status": "running",
        "ok": False,
        "done": False,
        "exitCode": None,
        "createdAt": now,
        "updatedAt": now,
        "error": None,
        "params": params,
        "counts": empty_counts(),
        "bytesWritten": 0,
        "bytesTotal": 0,
        "files": [],
        "resolvedChats": [],
    }
    recompute(job)
    return job


def finalize_job(job: dict, *, cancelled: bool, timeout: bool) -> None:
    """Set status / exitCode from file states.

    Priority: a failed file -> exit 1. Otherwise a cancel or timeout or any
    partial/pending file -> exit 4. Otherwise exit 0.
    """
    states = [item.get("state") for item in job.get("files") or []]
    failed = any(state == "failed" for state in states)
    # `listed` is the finished state of a scan. Only in-progress download
    # states keep the job unfinished.
    unfinished = any(state in {"partial", "pending", "active"} for state in states)
    if failed:
        job["status"] = "error"
        job["exitCode"] = 1
        job["error"] = job.get("error") or "one or more files failed"
    elif timeout or cancelled or unfinished:
        job["status"] = "partial" if timeout or unfinished else "cancelled"
        if cancelled and not timeout:
            job["status"] = "cancelled"
        if timeout:
            job["status"] = "partial"
        job["exitCode"] = 4
        if timeout:
            job["error"] = "timed out; partial files can be resumed"
        elif cancelled:
            job["error"] = "interrupted; partial files can be resumed"
        else:
            job["error"] = "stopped with unfinished files"
    else:
        job["status"] = "complete"
        job["exitCode"] = 0
        job["error"] = None
    job["ok"] = job["status"] == "complete"
    job["done"] = job["status"] in _TERMINAL


def fail_job(job: dict, *, exit_code: int, message: str) -> None:
    if exit_code == 5:
        job["status"] = "rejected"
    elif exit_code == 4:
        job["status"] = "partial"
    else:
        job["status"] = "error"
    job["exitCode"] = exit_code
    job["error"] = message
    job["ok"] = False
    job["done"] = True


def recover_stale_jobs(store: JobStore) -> None:
    """Mark jobs left `running` by a dead process as partial so they can resume.

    Do not call this while a live `tgdl serve` owns the jobs.
    """
    for job in store.list_jobs(limit=200):
        if job.get("status") != "running":
            continue
        for item in job.get("files") or []:
            if item.get("state") == "active":
                item["state"] = "partial"
        fail_job(
            job,
            exit_code=4,
            message="interrupted by process exit; rerun to resume",
        )
        job["status"] = "partial"
        store.save(job, make_latest=False)


class JobStore:
    def __init__(self, data_dir: Path) -> None:
        self.root = data_dir / "jobs"
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _path(self, job_id: str) -> Path:
        if not job_id.startswith("job_") or "/" in job_id or "\\" in job_id:
            raise ValueError("invalid job id")
        return self.root / f"{job_id}.json"

    def save(self, job: dict, *, make_latest: bool = True) -> None:
        recompute(job)
        job["updatedAt"] = utcnow_iso()
        with self._lock:
            atomic_write_json(self._path(job["id"]), job)
            if job.get("key"):
                index = self._read_index_unlocked()
                index[str(job["key"])] = job["id"]
                atomic_write_json(self.root / "by-key.json", index)
            if make_latest:
                atomic_write_json(self.root / "latest.json", {"id": job["id"]})

    def get(self, job_id: str) -> dict | None:
        path = self._path(job_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def latest(self) -> dict | None:
        pointer = self.root / "latest.json"
        if not pointer.exists():
            return None
        try:
            data = json.loads(pointer.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        job_id = data.get("id") if isinstance(data, dict) else None
        if not job_id:
            return None
        return self.get(str(job_id))

    def list_jobs(self, limit: int = 50) -> list[dict]:
        jobs: list[dict] = []
        for path in self.root.glob("job_*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(data, dict):
                jobs.append(data)
        jobs.sort(key=lambda item: item.get("createdAt") or "", reverse=True)
        return jobs[:limit]

    def find_by_key(self, key: str) -> dict | None:
        with self._lock:
            index = self._read_index_unlocked()
        job_id = index.get(key)
        if not job_id:
            return None
        return self.get(str(job_id))

    def _read_index_unlocked(self) -> dict:
        path = self.root / "by-key.json"
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}
