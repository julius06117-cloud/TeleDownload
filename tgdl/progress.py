"""Progress on stderr. Rich bars on a TTY; plain lines otherwise.

stdout is reserved for the JSON document or the human result.
"""

from __future__ import annotations

import sys
import time


def format_progress(job: dict) -> str:
    counts = job.get("counts") or {}
    total = job.get("bytesTotal")
    total_text = str(total) if total is not None else "?"
    return (
        f"{job.get('kind')} {job.get('status')} "
        f"done={counts.get('downloaded', 0)} "
        f"skip={counts.get('skipped', 0)} "
        f"fail={counts.get('failed', 0)} "
        f"partial={counts.get('partial', 0)} "
        f"bytes={job.get('bytesWritten') or 0}/{total_text}"
    )


class ProgressReporter:
    def update(self, job: dict) -> None:
        return None

    def close(self) -> None:
        return None


class NullReporter(ProgressReporter):
    pass


class LineReporter(ProgressReporter):
    def __init__(self) -> None:
        self._last = ""
        self._at = 0.0

    def update(self, job: dict) -> None:
        now = time.monotonic()
        line = format_progress(job)
        if line == self._last and now - self._at < 0.4 and job.get("status") == "running":
            return
        self._last = line
        self._at = now
        sys.stderr.write(line + "\n")
        sys.stderr.flush()


class RichReporter(ProgressReporter):
    def __init__(self) -> None:
        from rich.console import Console
        from rich.progress import (
            BarColumn,
            MofNCompleteColumn,
            Progress,
            TextColumn,
            TimeElapsedColumn,
        )

        self._progress = Progress(
            TextColumn("{task.description}"),
            BarColumn(bar_width=24),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            console=Console(file=sys.stderr, highlight=False),
            transient=False,
            refresh_per_second=4,
        )
        self._progress.start()
        self._task = self._progress.add_task("tgdl", total=0)

    def update(self, job: dict) -> None:
        try:
            files = job.get("files") or []
            done = sum(
                1
                for item in files
                if item.get("state") in {"complete", "skipped", "failed", "partial"}
            )
            self._progress.update(
                self._task,
                total=max(len(files), done),
                completed=done,
                description=format_progress(job)[:160],
            )
        except Exception:
            return None

    def close(self) -> None:
        try:
            self._progress.stop()
        except Exception:
            return None


def make_reporter(enabled: bool) -> ProgressReporter:
    if not enabled:
        return NullReporter()
    if not sys.stderr.isatty():
        return LineReporter()
    try:
        return RichReporter()
    except Exception:
        return LineReporter()
