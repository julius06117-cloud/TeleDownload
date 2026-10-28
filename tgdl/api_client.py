"""Talk to a running `tgdl serve` from the CLI. Stdlib HTTP only."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

import secrets

from tgdl.contract import error_payload
from tgdl.progress import ProgressReporter, make_reporter
from tgdl.util import atomic_write_text, chmod_private, pid_alive


class ApiClient:
    def __init__(self, port: int, token: str) -> None:
        self.port = port
        self.token = token
        self.base = f"http://127.0.0.1:{port}"

    def request(self, method: str, path: str, body: dict | None = None, *, timeout: float = 60) -> tuple[int, dict]:
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            self.base + path,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                raw = response.read().decode("utf-8")
                payload = json.loads(raw) if raw else {}
                return response.status, payload if isinstance(payload, dict) else {"ok": False}
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                payload = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                payload = error_payload(1, raw or exc.reason)
            if not isinstance(payload, dict):
                payload = error_payload(1, "non-object error response")
            return exc.code, payload
        except urllib.error.URLError as exc:
            raise ConnectionError(str(exc.reason)) from exc


def _tokens(data_dir: Path) -> list[str]:
    found: list[str] = []
    env = os.environ.get("TGDL_API_TOKEN")
    if env and env.strip():
        found.append(env.strip())
    path = data_dir / "api-token"
    if path.exists():
        file_token = path.read_text(encoding="utf-8").strip()
        if file_token and file_token not in found:
            found.append(file_token)
    return found


def discover_api(data_dir: Path, *, timeout: float = 0.6) -> ApiClient | None:
    """Return a client when this data dir has a live loopback API."""
    pid_path = data_dir / "api-pid"
    port_path = data_dir / "api-port"
    if not pid_path.exists() or not port_path.exists():
        return None
    try:
        pid = int(pid_path.read_text(encoding="utf-8").strip())
        port = int(port_path.read_text(encoding="utf-8").strip())
    except ValueError:
        return None
    if not pid_alive(pid):
        return None
    for token in _tokens(data_dir):
        client = ApiClient(port, token)
        try:
            status, body = client.request("GET", "/api/v1/ping", timeout=timeout)
        except Exception:
            continue
        if status == 200 and body.get("ok") and body.get("app") == "tgdl":
            return client
    return None


def ensure_token(data_dir: Path, configured: str | None) -> str:
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / "api-token"
    if configured and configured.strip():
        token = configured.strip()
        atomic_write_text(path, token + "\n", mode=0o600)
        return token
    if path.exists():
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            chmod_private(path)
            return existing
    token = secrets.token_urlsafe(32)
    atomic_write_text(path, token + "\n", mode=0o600)
    return token


def write_runtime(data_dir: Path, port: int, token: str) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(data_dir / "api-port", f"{port}\n", mode=0o644)
    atomic_write_text(data_dir / "api-pid", f"{os.getpid()}\n", mode=0o644)
    atomic_write_text(data_dir / "api-token", token + "\n", mode=0o600)


def clear_runtime(data_dir: Path) -> None:
    pid_path = data_dir / "api-pid"
    try:
        current = int(pid_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return
    if current != os.getpid():
        return
    for name in ("api-pid", "api-port"):
        try:
            (data_dir / name).unlink()
        except OSError:
            pass


def exit_from_http(status: int, payload: dict) -> int:
    code = payload.get("exitCode")
    if isinstance(code, int):
        return code
    if status in {401, 403}:
        return 3
    if status == 404:
        return 1
    if status == 400:
        return 2
    if status == 409:
        return 5
    if status >= 400:
        return 1
    return 0


def poll_job(
    client: ApiClient,
    job: dict,
    *,
    reporter: ProgressReporter,
    timeout_sec: int | None,
) -> dict:
    """Poll until the job is done. On Ctrl+C, cancel it and return the saved state."""
    deadline = None if not timeout_sec else time.monotonic() + timeout_sec + 15
    try:
        while not job.get("done"):
            if deadline is not None and time.monotonic() > deadline:
                client.request("POST", "/api/v1/cancel", {"id": job["id"]}, timeout=30)
                status, payload = client.request("GET", f"/api/v1/task?id={job['id']}")
                if status == 200 and isinstance(payload.get("job"), dict):
                    return payload["job"]
                return job
            time.sleep(0.4)
            status, payload = client.request("GET", f"/api/v1/task?id={job['id']}")
            if status == 200 and isinstance(payload.get("job"), dict):
                job = payload["job"]
                reporter.update(job)
    except KeyboardInterrupt:
        try:
            client.request("POST", "/api/v1/cancel", {"id": job["id"]}, timeout=30)
            status, payload = client.request("GET", f"/api/v1/task?id={job['id']}")
            if status == 200 and isinstance(payload.get("job"), dict):
                return payload["job"]
        except Exception:
            pass
        job = dict(job)
        job["done"] = True
        job["status"] = job.get("status") if job.get("done") else "cancelled"
        job["exitCode"] = job.get("exitCode") if isinstance(job.get("exitCode"), int) else 4
        return job
    return job


def run_remote_job(
    client: ApiClient,
    method: str,
    path: str,
    body: dict | None,
    *,
    quiet: bool,
    timeout_sec: int | None,
) -> tuple[int, dict]:
    status, payload = client.request(method, path, body, timeout=60)
    if not isinstance(payload.get("job"), dict):
        return exit_from_http(status, payload), payload
    job = payload["job"]
    reporter = make_reporter(enabled=not quiet)
    try:
        if not job.get("done"):
            job = poll_job(client, job, reporter=reporter, timeout_sec=timeout_sec)
    finally:
        reporter.close()
    code = job.get("exitCode")
    exit_code = int(code) if isinstance(code, int) else 0
    return exit_code, {"schema": payload.get("schema", 1), "ok": exit_code == 0, "existing": payload.get("existing", False), "exitCode": exit_code, "job": job}
