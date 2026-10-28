import asyncio
from pathlib import Path

from tgdl.jobs import JobStore, canonical_key, finalize_job, new_job, recover_stale_jobs
from tgdl.transfer import consume_chunks


def test_canonical_key_ignores_workers_and_timeout():
    base = {
        "chats": ["@a"],
        "output": "/tmp/out",
        "groupByMonth": True,
        "resume": True,
        "photos": False,
        "ext": [".pdf"],
        "minSize": None,
        "maxSize": None,
        "since": None,
        "until": None,
        "sender": None,
        "name": None,
        "nameRegex": None,
        "limit": None,
        "messageIds": None,
        "workers": 4,
        "timeoutSec": 10,
    }
    other = dict(base, workers=8, timeoutSec=99)
    changed = dict(base, chats=["@b"])
    assert canonical_key("download", base) == canonical_key("download", other)
    assert canonical_key("download", base) != canonical_key("download", changed)


def test_job_store_roundtrip_and_stale_recovery(tmp_path: Path):
    store = JobStore(tmp_path)
    job = new_job("download", {"chats": ["@a"]}, "k")
    job["files"] = [
        {"state": "active", "bytesWritten": 5, "fileSize": 10},
    ]
    store.save(job)
    assert store.latest()["id"] == job["id"]
    assert store.find_by_key("k")["id"] == job["id"]
    recover_stale_jobs(store)
    loaded = store.get(job["id"])
    assert loaded["status"] == "partial"
    assert loaded["exitCode"] == 4
    assert loaded["files"][0]["state"] == "partial"
    assert store.latest()["id"] == job["id"]


def test_finalize_scan_listed_is_complete():
    job = new_job("scan", {}, None)
    job["files"] = [{"state": "listed", "bytesWritten": 0, "fileSize": 1}]
    finalize_job(job, cancelled=False, timeout=False)
    assert job["status"] == "complete"
    assert job["exitCode"] == 0


def test_finalize_download_priority():
    job = new_job("download", {}, None)
    job["files"] = [
        {"state": "failed", "bytesWritten": 1, "fileSize": 2},
        {"state": "partial", "bytesWritten": 1, "fileSize": 2},
    ]
    finalize_job(job, cancelled=True, timeout=False)
    assert job["exitCode"] == 1
    job["files"] = [{"state": "pending", "bytesWritten": 0, "fileSize": 2}]
    finalize_job(job, cancelled=True, timeout=False)
    assert job["status"] == "cancelled"
    assert job["exitCode"] == 4


def test_consume_writes_and_trims(tmp_path: Path):
    part = tmp_path / "a.part"

    async def chunks():
        yield b"hello"
        yield b"!!extra"

    result = asyncio.run(
        consume_chunks(
            part,
            offset=0,
            expected=7,
            chunk_iter=chunks(),
            cancel=asyncio.Event(),
        )
    )
    assert result.status == "complete"
    assert result.bytes_written == 7
    assert part.read_bytes() == b"hello!!"


def test_consume_resume_offset(tmp_path: Path):
    part = tmp_path / "a.part"
    part.write_bytes(b"helloXXXX")

    async def chunks():
        yield b"!!"

    result = asyncio.run(
        consume_chunks(part, offset=5, expected=7, chunk_iter=chunks(), cancel=asyncio.Event())
    )
    assert result.status == "complete"
    assert part.read_bytes() == b"hello!!"


def test_consume_cancel_before_chunk(tmp_path: Path):
    part = tmp_path / "a.part"
    part.write_bytes(b"keep")
    cancel = asyncio.Event()

    class Slow:
        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.Event().wait()

    async def run():
        task = asyncio.create_task(
            consume_chunks(part, offset=4, expected=10, chunk_iter=Slow(), cancel=cancel)
        )
        await asyncio.sleep(0.05)
        cancel.set()
        return await task

    result = asyncio.run(run())
    assert result.status == "partial"
    assert result.bytes_written == 4
    assert part.read_bytes() == b"keep"


def test_consume_writes_chunk_that_arrives_as_cancel_is_set(tmp_path: Path):
    part = tmp_path / "a.part"
    cancel = asyncio.Event()

    class One:
        def __aiter__(self):
            return self

        async def __anext__(self):
            cancel.set()
            return b"xyz"

    result = asyncio.run(
        consume_chunks(part, offset=0, expected=10, chunk_iter=One(), cancel=cancel)
    )
    assert result.status == "partial"
    assert part.read_bytes() == b"xyz"


def test_format_progress_shape():
    from tgdl.progress import format_progress

    text = format_progress(
        {
            "kind": "download",
            "status": "running",
            "counts": {"downloaded": 1, "skipped": 2, "failed": 0, "partial": 1},
            "bytesWritten": 10,
            "bytesTotal": 40,
        }
    )
    assert "done=1" in text
    assert "bytes=10/40" in text
