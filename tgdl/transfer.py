"""Write download chunks to a part file and stop cleanly on cancel.

This module does not import Telethon. The caller supplies an async iterator of
byte chunks (normally `client.iter_download`) and handles FloodWait outside.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ConsumeResult:
    bytes_written: int
    status: str  # complete | partial | short


def _checkpoint(handle) -> None:
    handle.flush()
    try:
        os.fsync(handle.fileno())
    except OSError:
        pass


async def _next_chunk(iterator: AsyncIterator[bytes]) -> bytes:
    return await iterator.__anext__()


async def _aclose(obj: object) -> None:
    aclose = getattr(obj, "aclose", None)
    if aclose is not None:
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await aclose()
        return
    close = getattr(obj, "close", None)
    if close is None:
        return
    with contextlib.suppress(Exception, asyncio.CancelledError):
        result = close()
        if asyncio.iscoroutine(result):
            await result


def _finish_status(downloaded: int, expected: int | None, *, cancelled: bool, eof: bool) -> str:
    if expected is not None and downloaded >= expected:
        return "complete"
    if cancelled:
        return "partial"
    if eof and expected is None:
        return "complete"
    if eof:
        return "short"
    return "partial"


async def consume_chunks(
    part: Path,
    *,
    offset: int,
    expected: int | None,
    chunk_iter: AsyncIterator[bytes],
    cancel: asyncio.Event,
    on_progress: Callable[[int], None] | None = None,
) -> ConsumeResult:
    """Append chunks starting at `offset`. Truncates `part` to `offset` first.

    If cancel fires while a chunk is already in memory, that chunk is written
    before returning `partial`. The file is flushed and fsynced first.
    """
    part.parent.mkdir(parents=True, exist_ok=True)
    if not part.exists():
        part.touch()
    handle = open(part, "r+b")
    downloaded = offset
    status = "partial"
    try:
        try:
            handle.truncate(offset)
            handle.seek(offset)
            iterator = chunk_iter.__aiter__()
            while True:
                if expected is not None and downloaded >= expected:
                    status = "complete"
                    break
                if cancel.is_set():
                    status = "partial"
                    break
                nxt = asyncio.create_task(_next_chunk(iterator))
                stopper = asyncio.create_task(cancel.wait())
                try:
                    done, _pending = await asyncio.wait(
                        {nxt, stopper},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                except BaseException:
                    nxt.cancel()
                    stopper.cancel()
                    raise
                if nxt in done:
                    if not stopper.done():
                        stopper.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await stopper
                    try:
                        chunk = nxt.result()
                    except StopAsyncIteration:
                        status = _finish_status(
                            downloaded,
                            expected,
                            cancelled=cancel.is_set(),
                            eof=True,
                        )
                        break
                    data = bytes(chunk)
                    if expected is not None and downloaded + len(data) > expected:
                        data = data[: expected - downloaded]
                    if data:
                        handle.write(data)
                        downloaded += len(data)
                        if on_progress is not None:
                            on_progress(downloaded)
                    if expected is not None and downloaded >= expected:
                        status = "complete"
                        break
                    if cancel.is_set():
                        status = "partial"
                        break
                else:
                    nxt.cancel()
                    with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration):
                        await nxt
                    status = "partial"
                    break
        except BaseException:
            _checkpoint(handle)
            raise
        else:
            _checkpoint(handle)
    finally:
        handle.close()
        await _aclose(chunk_iter)
    return ConsumeResult(bytes_written=downloaded, status=status)


async def sleep_or_cancel(seconds: float, cancel: asyncio.Event) -> bool:
    """Sleep up to `seconds`. Return False if `cancel` is set first."""
    if seconds < 0:
        raise ValueError("seconds must be >= 0")
    if cancel.is_set():
        return False
    try:
        await asyncio.wait_for(cancel.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        return True
    return False
