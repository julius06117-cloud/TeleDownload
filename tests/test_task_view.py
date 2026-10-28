import asyncio
from pathlib import Path

import pytest

from tgdl.errors import RejectedError
from tgdl.jobs import new_job
from tgdl.taskview import SpeedMeter, apply_task_view


def _base(**extra) -> dict:
    job = {
        "id": "job_abc",
        "kind": "download",
        "status": "running",
        "ok": False,
        "done": False,
        "error": None,
        "params": {"output": "/tmp/dl", "chats": ["@files"]},
        "bytesWritten": 50,
        "bytesTotal": 100,
        "files": [
            {
                "fileName": "report.pdf",
                "dest": "/tmp/dl/report.pdf",
                "state": "active",
                "plannedAction": "resume",
                "resumeOffset": 4096,
                "bytesWritten": 50,
                "fileSize": 100,
            }
        ],
        "resolvedChats": [{"id": "-100", "title": "Files", "username": "files"}],
    }
    job.update(extra)
    return job


def test_hornet_status_mapping_and_zero_speed_when_inactive():
    active = apply_task_view(_base(), speed_bps=10)
    assert active["task"]["status"] == "active"
    assert active["taskStatus"] == "active"
    assert active["task"]["done"] is False
    assert active["task"]["ok"] is False
    assert active["name"] == "report.pdf"
    assert active["dir"] == "/tmp/dl"
    assert active["source"] == "Files (@files)"
    assert active["addedBy"] == "agent"
    assert active["paths"] == ["/tmp/dl/report.pdf"]
    assert active["task"]["files"] == ["/tmp/dl/report.pdf"]
    assert active["doneBytes"] == 50
    assert active["totalBytes"] == 100
    assert active["progress"] == 0.5
    assert active["speedBps"] == 10
    assert active["task"]["speedBps"] == 10
    assert active["resuming"] is True
    assert active["etaSec"] == 5

    paused = apply_task_view(_base(status="partial", done=True, error="interrupted"), speed_bps=9999)
    assert paused["task"]["status"] == "paused"
    assert paused["task"]["done"] is False
    assert paused["speedBps"] == 0
    assert paused["etaSec"] is None

    assert apply_task_view(_base(status="cancelled", done=True))["task"]["status"] == "paused"
    assert apply_task_view(_base(status="paused", done=True))["task"]["status"] == "paused"
    assert apply_task_view(_base(status="queued"))["task"]["status"] == "waiting"
    done = apply_task_view(_base(status="complete", ok=True, done=True, bytesWritten=100))
    assert done["task"]["status"] == "complete"
    assert done["task"]["done"] is True
    assert done["task"]["ok"] is True
    assert done["progress"] == 1.0
    assert done["speedBps"] == 0
    assert apply_task_view(_base(status="rejected", done=True))["task"]["status"] == "error"
    removed = apply_task_view(_base(status="removed", done=True))
    assert removed["task"]["status"] == "removed"
    assert removed["task"]["done"] is True
    assert removed["speedBps"] == 0


def test_speed_meter_window_and_reset():
    meter = SpeedMeter()
    assert meter.observe("job_a", 0, active=True, now=10) == 0
    assert meter.observe("job_a", 4000, active=True, now=12) == 2000
    assert meter.observe("job_a", 4000, active=False, now=13) == 0
    assert meter.observe("job_a", 4000, active=True, now=14) == 0


def test_remove_and_pause_guards(tmp_path: Path, monkeypatch):
    for name in ("TGDL_API_ID", "TGDL_API_HASH", "TGDL_CONFIG", "TGDL_ENV"):
        monkeypatch.delenv(name, raising=False)
    from tgdl.config import load_config
    from tgdl.service import Service

    cfg = load_config(data_dir=tmp_path / ".tgdl", cwd=tmp_path)
    service = Service(cfg)
    dest = tmp_path / "report.pdf"
    part = tmp_path / "report.pdf.part"
    side = tmp_path / "report.pdf.tgdl.json"
    for path, text in ((dest, "full"), (part, "part"), (side, "{}")):
        path.write_text(text, encoding="utf-8")
    job = new_job("download", {"chats": ["@files"], "output": str(tmp_path)}, "key")
    job["status"] = "complete"
    job["done"] = True
    job["ok"] = True
    job["exitCode"] = 0
    job["bytesWritten"] = 4
    job["bytesTotal"] = 4
    job["files"] = [
        {
            "fileName": "report.pdf",
            "dest": str(dest),
            "part": str(part),
            "sidecar": str(side),
            "state": "complete",
            "bytesWritten": 4,
            "fileSize": 4,
        }
    ]
    service.store.save(job)

    with pytest.raises(RejectedError):
        asyncio.run(service.pause(job["id"]))

    removed = asyncio.run(service.remove(job["id"], delete_files=True))
    assert removed["status"] == "removed"
    assert removed["task"]["status"] == "removed"
    assert removed["task"]["speedBps"] == 0
    assert removed["addedBy"] == "agent"
    assert not dest.exists()
    assert not part.exists()
    assert not side.exists()

    with pytest.raises(RejectedError):
        asyncio.run(service.resume(job["id"]))


def test_pause_resume_remove_routes(tmp_path: Path):
    import json
    import urllib.error
    import urllib.request

    from tgdl.api import start_api
    from tgdl.config import load_config
    from tests.test_cli_api import FakeService

    async def _run():
        cfg = load_config(data_dir=tmp_path / ".tgdl", cwd=tmp_path)
        service = FakeService()
        runner, port = await start_api(service, token="secret", host="127.0.0.1", port=0, config=cfg)
        try:
            def call(path: str, body: dict):
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}{path}",
                    data=json.dumps(body).encode(),
                    headers={
                        "Host": f"127.0.0.1:{port}",
                        "Authorization": "Bearer secret",
                        "Content-Type": "application/json",
                    },
                    method="POST",
                )
                try:
                    with urllib.request.urlopen(req, timeout=5) as response:
                        return response.status, json.loads(response.read().decode())
                except urllib.error.HTTPError as exc:
                    return exc.code, json.loads(exc.read().decode())

            status, payload = await asyncio.to_thread(call, "/api/v1/pause", {"id": "job_abc"})
            assert status == 200
            assert payload["ok"] is True
            assert payload["id"] == "job_abc"
            assert payload["job"]["status"] == "paused"
            status, payload = await asyncio.to_thread(call, "/api/v1/resume", {"id": "job_abc"})
            assert status == 202
            assert payload["job"]["status"] == "running"
            status, payload = await asyncio.to_thread(
                call, "/api/v1/remove", {"id": "job_abc", "deleteFiles": True}
            )
            assert status == 200
            assert payload["deleteFiles"] is True
            assert payload["job"]["status"] == "removed"
            assert ("pause", "job_abc") in service.calls
            assert ("resume", "job_abc") in service.calls
            assert ("remove", "job_abc", True) in service.calls
            status, payload = await asyncio.to_thread(call, "/api/v1/pause", {"id": "missing"})
            assert status == 404
        finally:
            await runner.cleanup()

    asyncio.run(_run())
