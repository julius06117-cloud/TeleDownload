"""Resume math and `.tgdl.json` sidecars.

Telegram `upload.getFile` requires the offset to be a multiple of 4096 and the
request size to be a multiple of 4096 that also divides 1 MiB. Telethon's
`iter_download` accepts any byte offset (the generic iterator re-fetches the
aligned block and slices it), but a crash can tear the last partial write.

The reliable pattern is:

1. Download into `name.ext.part` and checkpoint with fsync.
2. On resume, truncate the part down to a 4096-byte boundary (at most 4095
   bytes are fetched again) and pass that offset to `iter_download` with
   `request_size=512*1024`.
3. When the byte count matches the expected size, `os.replace` the part onto
   the final name. The sidecar records `messageId`, `fileSize`, and
   `fileUniqueId` (the MTProto document or photo id).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from tgdl.contract import ALIGNMENT, REQUEST_SIZE
from tgdl.util import atomic_write_json, utcnow_iso


def aligned_resume_offset(partial_bytes: int, alignment: int = ALIGNMENT) -> int:
    """Largest legal resume offset that is <= `partial_bytes`."""
    if partial_bytes < 0:
        raise ValueError("partial_bytes must be >= 0")
    if alignment <= 0 or alignment % ALIGNMENT != 0:
        raise ValueError("alignment must be a positive multiple of 4096")
    return (partial_bytes // alignment) * alignment


def prepare_part_file(path: Path, expected_size: int | None, *, alignment: int = ALIGNMENT) -> int:
    """Truncate a part file to a resume offset and return that offset.

    If the part is already exactly `expected_size`, it is not truncated: the
    bytes are a finished download that only needs a rename.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.touch()
        return 0
    size = path.stat().st_size
    if expected_size is not None and size > expected_size:
        size = expected_size
    if expected_size is not None and path.stat().st_size == expected_size:
        return expected_size
    offset = aligned_resume_offset(size, alignment)
    with open(path, "r+b") as handle:
        handle.truncate(offset)
        handle.flush()
        os.fsync(handle.fileno())
    return offset


def reset_part_file(path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as handle:
        handle.flush()
        os.fsync(handle.fileno())
    return 0


def read_sidecar(path: Path) -> tuple[str, dict | None]:
    """Return `("missing"|"ok"|"corrupt", data)`."""
    if not path.exists():
        return "missing", None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "corrupt", None
    if not isinstance(data, dict) or "fileUniqueId" not in data:
        return "corrupt", None
    return "ok", data


def build_sidecar(
    *,
    message_id: str,
    chat_id: str,
    file_size: int | None,
    file_unique_id: str,
    file_name: str,
    bytes_written: int,
    aligned_offset: int,
    complete: bool,
    part_path: Path,
    final_path: Path,
) -> dict:
    return {
        "v": 1,
        "messageId": str(message_id),
        "chatId": str(chat_id),
        "fileSize": file_size,
        "fileUniqueId": str(file_unique_id),
        "fileName": file_name,
        "bytesWritten": bytes_written,
        "alignedOffset": aligned_offset,
        "requestSize": REQUEST_SIZE,
        "complete": complete,
        "updatedAt": utcnow_iso(),
        "partPath": str(part_path),
        "finalPath": str(final_path),
    }


def write_sidecar(path: Path, payload: dict) -> None:
    payload = dict(payload)
    payload["updatedAt"] = utcnow_iso()
    atomic_write_json(path, payload)


def same_file(meta: dict, unique_id: str, expected_size: int | None) -> bool:
    if str(meta.get("fileUniqueId")) != str(unique_id):
        return False
    meta_size = meta.get("fileSize")
    if meta_size is None or expected_size is None:
        return True
    return int(meta_size) == int(expected_size)


def decide_slot(
    *,
    unique_id: str,
    expected_size: int | None,
    final_exists: bool,
    final_size: int | None,
    part_exists: bool,
    part_size: int | None,
    meta_state: str,
    meta: dict | None,
) -> str:
    """Decide what to do with one filename slot.

    Returns `take`, `resume`, `finish`, `skip`, or `collide`.

    A sidecar with a different `fileUniqueId` or size always collides, even
    when the on-disk size happens to match. Without a sidecar, a final file
    whose size equals the expected size is skipped (size heuristic).
    """
    if meta_state not in {"missing", "ok", "corrupt"}:
        raise ValueError(f"bad meta_state {meta_state}")

    if meta_state == "corrupt":
        return "collide" if (final_exists or part_exists) else "take"

    ours = False
    if meta_state == "ok":
        if meta is None or not same_file(meta, unique_id, expected_size):
            return "collide"
        ours = True

    final_complete = False
    if final_exists:
        if expected_size is not None and final_size == expected_size:
            final_complete = True
        elif expected_size is None and ours and bool(meta and meta.get("complete")):
            final_complete = True

    if final_complete and (ours or meta_state == "missing"):
        return "skip"

    if final_exists and not final_complete:
        return "resume" if ours else "collide"

    part_complete = (
        part_exists
        and expected_size is not None
        and part_size == expected_size
        and (ours or meta_state == "missing")
    )
    if part_complete:
        return "finish"

    if ours:
        return "resume"

    if part_exists and meta_state == "missing":
        size = part_size or 0
        if expected_size is None or size <= expected_size:
            return "resume"
        return "collide"

    if part_exists:
        return "collide"
    return "take"
