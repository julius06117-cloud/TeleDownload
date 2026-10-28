"""Filesystem names: sanitizing, collision suffixes, destination layout."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from tgdl.errors import TgdlError
from tgdl.models import MediaItem, Planned
from tgdl.resume import (
    aligned_resume_offset,
    build_sidecar,
    decide_slot,
    read_sidecar,
    write_sidecar,
)

_INVALID = __import__("re").compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def sanitize_filename(name: str, *, fallback: str = "file") -> str:
    """Return a single path segment safe on Windows and POSIX."""
    text = (name or "").replace("\\", "/").split("/")[-1].strip()
    text = _INVALID.sub("_", text).strip(" .")
    if text in {"", ".", ".."}:
        return fallback
    stem, ext = os.path.splitext(text)
    if stem.upper() in _RESERVED:
        stem = stem + "_"
    # Keep extensions usable and stay under common path limits.
    stem = stem[:180] or fallback
    ext = ext[:32]
    return stem + ext


def with_collision_index(filename: str, index: int) -> str:
    """`report.pdf` + 1 -> `report (1).pdf`. Index 0 is the original name."""
    if index < 0:
        raise ValueError("index must be >= 0")
    if index == 0:
        return filename
    stem, ext = os.path.splitext(filename)
    return f"{stem} ({index}){ext}"


def destination_directory(
    output: Path,
    chat_title: str,
    when: datetime,
    group_by_month: bool,
) -> Path:
    if not group_by_month:
        return output
    # Chat titles are directory names, so slashes stay in the title as words
    # instead of being treated as path separators.
    folder = sanitize_filename(chat_title.replace("/", "_").replace("\\", "_"), fallback="chat")
    moment = when if when.tzinfo else when.replace(tzinfo=timezone.utc)
    month = moment.astimezone(timezone.utc).strftime("%Y-%m")
    return output / folder / month


def _paths_for(directory: Path, filename: str) -> tuple[Path, Path, Path]:
    final = directory / filename
    return final, Path(str(final) + ".part"), Path(str(final) + ".tgdl.json")


def _slot_view(final: Path, part: Path, sidecar: Path) -> dict:
    meta_state, meta = read_sidecar(sidecar)
    return {
        "final_exists": final.exists() and final.is_file(),
        "final_size": final.stat().st_size if final.exists() and final.is_file() else None,
        "part_exists": part.exists() and part.is_file(),
        "part_size": part.stat().st_size if part.exists() and part.is_file() else None,
        "meta_state": meta_state,
        "meta": meta,
    }


def allocate_slot(
    directory: Path,
    filename: str,
    *,
    unique_id: str,
    expected_size: int | None,
    resume: bool,
    force: bool,
) -> Planned:
    """Pick a collide-safe path. Read-only aside from nothing: no writes."""
    safe = sanitize_filename(filename)
    for index in range(0, 1000):
        candidate = with_collision_index(safe, index)
        final, part, sidecar = _paths_for(directory, candidate)
        view = _slot_view(final, part, sidecar)
        action = decide_slot(
            unique_id=unique_id,
            expected_size=expected_size,
            **view,
        )
        if action == "skip" and force:
            continue
        if action == "collide":
            continue
        if action == "take":
            action = "download"
        if action == "resume" and not resume:
            action = "download"
        offset = _offset_for(action, expected_size, view)
        return Planned(action, final, part, sidecar, offset, False)
    raise TgdlError(f"too many filename collisions for {safe}")


def _offset_for(action: str, expected_size: int | None, view: dict) -> int:
    if action == "download":
        return 0
    if action == "skip":
        if view["final_size"] is not None:
            return int(view["final_size"])
        return int(expected_size or 0)
    if action == "finish":
        return int(expected_size or view["part_size"] or 0)
    # resume: preview the aligned offset without truncating
    part_size = int(view["part_size"] or 0)
    if expected_size is not None and part_size > expected_size:
        part_size = expected_size
    if part_size == expected_size and expected_size is not None:
        return expected_size
    return aligned_resume_offset(part_size)


def _apply(item: MediaItem, planned: Planned) -> None:
    """Persist sidecar / promote a finished part. Used only by download, not scan."""
    planned.final_path.parent.mkdir(parents=True, exist_ok=True)
    if planned.action == "finish":
        os.replace(planned.part_path, planned.final_path)
        _write_meta(item, planned, complete=True, written=planned.offset)
        return
    if planned.action == "skip":
        state, meta = read_sidecar(planned.sidecar_path)
        if (
            state == "ok"
            and meta is not None
            and str(meta.get("fileUniqueId")) == item.file_unique_id
            and planned.part_path.exists()
        ):
            planned.part_path.unlink()
        if state == "ok" and meta is not None and str(meta.get("fileUniqueId")) == item.file_unique_id:
            if not meta.get("complete"):
                meta["complete"] = True
                meta["bytesWritten"] = planned.offset
                write_sidecar(planned.sidecar_path, meta)
        return
    if planned.action == "download":
        _write_meta(item, planned, complete=False, written=0)
        return
    if planned.action == "resume":
        _write_meta(item, planned, complete=False, written=planned.offset)


def _write_meta(item: MediaItem, planned: Planned, *, complete: bool, written: int) -> None:
    payload = build_sidecar(
        message_id=str(item.message_id),
        chat_id=str(item.chat_id),
        file_size=item.file_size,
        file_unique_id=item.file_unique_id,
        file_name=item.file_name,
        bytes_written=written,
        aligned_offset=planned.offset if not complete else written,
        complete=complete,
        part_path=planned.part_path,
        final_path=planned.final_path,
    )
    write_sidecar(planned.sidecar_path, payload)


def plan_items(
    items: list[MediaItem],
    *,
    output: Path,
    group_by_month: bool,
    resume: bool,
    force: bool,
    write: bool,
) -> list[Planned]:
    """Assign a destination to each item.

    Duplicate file identities in the same destination directory are skipped
    and point at the first item's path. `write=True` creates sidecars and
    promotes finished parts; scan must pass `write=False`.
    """
    planned: list[Planned] = []
    seen: dict[tuple[str, str, int | None], Planned] = {}
    for item in items:
        directory = destination_directory(output, item.chat_title, item.date, group_by_month)
        ident = (str(directory), item.file_unique_id, item.file_size)
        if ident in seen:
            first = seen[ident]
            planned.append(
                Planned(
                    "skip",
                    first.final_path,
                    first.part_path,
                    first.sidecar_path,
                    first.offset,
                    True,
                )
            )
            continue
        slot = allocate_slot(
            directory,
            item.file_name,
            unique_id=item.file_unique_id,
            expected_size=item.file_size,
            resume=resume,
            force=force,
        )
        if write:
            _apply(item, slot)
        seen[ident] = slot
        planned.append(slot)
    return planned
