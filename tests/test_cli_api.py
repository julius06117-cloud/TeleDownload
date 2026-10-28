import asyncio
import json
from pathlib import Path

import pytest

from tgdl.cli import build_parser, run
from tgdl.contract import SCHEMA
from tgdl.errors import RejectedError


class FakeService:
    def __init__(self) -> None:
        self.calls = []

    async def list_chats(self, *, limit: int, groups_only: bool, wait: bool) -> dict:
        self.calls.append(("list", limit, groups_only, wait))
        return {"existing": False, "job": _job("list-chats", "complete", 0)}

    async def scan(self, params: dict, *, wait: bool) -> dict:
        self.calls.append(("scan", params["chats"], wait))
        return {"existing": False, "job": _job("scan", "complete", 0)}

    async def download(self, params: dict, *, wait: bool) -> dict:
        self.calls.append(("download", params["chats"], params["workers"], wait))
        job = _job("download", "running", None)
        if wait:
            job["status"] = "complete"
            job["done"] = True
            job["ok"] = True
            job["exitCode"] = 0
        return {"existing": False, "job": job}

    def get_job(self, job_id: str):
        if job_id == "missing":
            return None
        return _job("download", "partial", 4, job_id=job_id)

    def list_jobs(self, limit: int):
        return [_job("download", "complete", 0)]

    def latest_job(self):
        return _job("scan", "complete", 0)

    async def cancel(self, job_id: str):
        if job_id == "missing":
            from tgdl.errors import TgdlError

            raise TgdlError(f"job not found: {job_id}")
        return _job("download", "cancelled", 4, job_id=job_id)


def _job(kind: str, status: str, code, job_id: str = "job_abc") -> dict:
    done = status != "running"
    return {
        "schema": SCHEMA,
        "id": job_id,
        "key": "k",
        "kind": kind,
        "status": status,
        "ok": status == "complete",
        "done": done,
        "exitCode": code,
        "createdAt": "2026-01-01T00:00:00+00:00",
        "updatedAt": "2026-01-01T00:00:00+00:00",
        "error": None,
        "params": {},
        "counts": {
            "matched": 0,
            "listed": 0,
            "pending": 0,
            "active": 0,
            "downloaded": 0,
            "skipped": 0,
            "failed": 0,
            "partial": 0,
        },
        "bytesWritten": 0,
        "bytesTotal": 0,
        "files": [],
        "resolvedChats": [],
    }


def test_help_and_version_json(capsys):
    assert run(["help", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == 1
    assert payload["ok"] is True
    codes = [item["code"] for item in payload["exitCodes"]]
    assert codes == [0, 1, 2, 3, 4, 5]
    names = {item["name"] for item in payload["commands"]}
    assert {"login", "list-chats", "scan", "download", "status", "serve", "version", "help"} <= names
    paths = {item["path"] for item in payload["endpoints"]}
    assert "/api/v1/ping" in paths
    assert "/api/v1/download" in paths

    assert run(["version", "--json"]) == 0
    version = json.loads(capsys.readouterr().out)
    assert version["tgdl"]
    assert "python" in version["build"]


def test_help_json_matches_parser():
    parser = build_parser()
    sub = next(action for action in parser._actions if action.dest == "command")
    choices = set(sub.choices)
    listed = {item["name"] for item in __import__("tgdl.helptext", fromlist=["help_payload"]).help_payload()["commands"]}
    assert choices == listed


def test_usage_errors_are_exit_2(capsys):
    assert run(["nope"]) == 2
    err = capsys.readouterr()
    assert "invalid" in err.err.lower() or "error" in err.err.lower()
    assert run(["--json", "download"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["exitCode"] == 2
    assert payload["code"] == "usage"


def test_download_without_credentials_is_auth(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    code = run(
        [
            "download",
            "--chat",
            "@example",
            "--json",
            "--data-dir",
            str(tmp_path / ".tgdl"),
            "--session",
            str(tmp_path / ".tgdl" / "missing.session"),
        ]
    )
    assert code == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["code"] == "auth"


def test_status_missing_job(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert run(["status", "--json", "--data-dir", str(tmp_path / "state")]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["job"] is None
    assert run(["status", "job_missing", "--data-dir", str(tmp_path / "state2")]) == 1


def test_api_rejects_public_host():
    from tgdl.api import start_api

    async def _run():
        with pytest.raises(RejectedError):
            await start_api(FakeService(), token="t", host="0.0.0.0", port=9, config=None)

    asyncio.run(_run())


def test_api_loopback_auth_and_download(tmp_path: Path):
    from tgdl.api import start_api
    from tgdl.config import load_config

    async def _run():
        import urllib.error
        import urllib.request

        cfg = load_config(data_dir=tmp_path / ".tgdl", cwd=tmp_path)
        service = FakeService()
        runner, port = await start_api(service, token="secret", host="127.0.0.1", port=0, config=cfg)
        try:
            assert port > 0
            sock = next(iter(runner.sites))._server.sockets[0]
            assert sock.getsockname()[0] == "127.0.0.1"

            def call(path: str, *, token: str | None, method: str = "GET", body: dict | None = None, host: str | None = None):
                data = None if body is None else json.dumps(body).encode()
                headers = {"Host": host or f"127.0.0.1:{port}"}
                if token is not None:
                    headers["Authorization"] = f"Bearer {token}"
                if body is not None:
                    headers["Content-Type"] = "application/json"
                req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, headers=headers, method=method)
                try:
                    with urllib.request.urlopen(req, timeout=5) as response:
                        return response.status, json.loads(response.read().decode())
                except urllib.error.HTTPError as exc:
                    return exc.code, json.loads(exc.read().decode())

            async def call_async(*args, **kwargs):
                return await asyncio.to_thread(call, *args, **kwargs)

            status, payload = await call_async("/api/v1/ping", token=None)
            assert status == 401
            assert payload["code"] == "auth"
            status, payload = await call_async("/api/v1/ping", token="secret", host="evil.example")
            assert status == 403
            status, payload = await call_async("/api/v1/ping", token="secret")
            assert status == 200
            assert payload["ok"] is True
            assert payload["app"] == "tgdl"
            status, payload = await call_async(
                "/api/v1/download",
                token="secret",
                method="POST",
                body={"chats": ["@example"], "workers": 2, "wait": False},
            )
            assert status == 202
            assert payload["job"]["kind"] == "download"
            assert payload["job"]["status"] == "running"
            assert service.calls[-1][0] == "download"
            assert service.calls[-1][2] == 2
            status, payload = await call_async("/api/v1/status", token="secret")
            assert status == 200
            assert payload["job"]["kind"] == "scan"
            status, payload = await call_async("/api/v1/task?id=job_abc", token="secret")
            assert payload["job"]["exitCode"] == 4
            status, payload = await call_async("/api/v1/task?id=missing", token="secret")
            assert status == 404
            status, payload = await call_async("/api/v1/download", token="secret", method="POST", body={})
            assert status == 400
            assert payload["exitCode"] == 2
        finally:
            await runner.cleanup()

    asyncio.run(_run())
