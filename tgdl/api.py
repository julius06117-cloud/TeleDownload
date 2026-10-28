"""Loopback HTTP API. Bind address is always 127.0.0.1."""

from __future__ import annotations

import hmac
import json
import logging
import os
from pathlib import Path
from typing import Any

from aiohttp import web
from aiohttp.web import AppKey

from tgdl import __version__
from tgdl.contract import APP_NAME, SCHEMA, error_payload
from tgdl.errors import RejectedError, TgdlError, UsageError
from tgdl.helptext import help_payload, telethon_version, version_payload

log = logging.getLogger("tgdl.api")

SERVICE_KEY: AppKey = web.AppKey("service")
TOKEN_KEY: AppKey = web.AppKey("token")
PORT_KEY: AppKey = web.AppKey("port")
CONFIG_KEY: AppKey = web.AppKey("config")

_HTTP_FOR_EXIT = {1: 409, 2: 400, 3: 403, 4: 200, 5: 409}
_WEB_DIR = Path(__file__).resolve().parent / "web"
_UI_FILES = {
    "/": ("index.html", "text/html"),
    "/ui/app.css": ("app.css", "text/css"),
    "/ui/app.js": ("app.js", "text/javascript"),
}
_BOOTSTRAP_PATH = "/api/v1/ui/bootstrap"
_UI_HEADER = "X-Tgdl-Ui"
_FAVICON = (
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
    b'<rect width="32" height="32" rx="7" fill="#0f6b45"/>'
    b'<path d="M9 23V9h3.4l3.6 7.4L19.6 9H23v14h-3.1v-8.2L16.4 23h-2.8l-3.5-8.2V23H9z" fill="#f6f3ea"/>'
    b"</svg>"
)


def dumps(obj: object) -> str:
    return json.dumps(obj, ensure_ascii=False)


def json_response(status: int, payload: dict, headers: dict | None = None) -> web.Response:
    response = web.json_response(payload, status=status, dumps=dumps)
    if headers:
        response.headers.update(headers)
    return _harden(response)


def token_ok(presented: str, expected: str) -> bool:
    if not presented or not expected:
        return False
    left = presented.encode("utf-8")
    right = expected.encode("utf-8")
    if len(left) != len(right):
        return False
    return hmac.compare_digest(left, right)


def presented_token(request: web.Request) -> str:
    header = request.headers.get("X-Token", "").strip()
    if header:
        return header
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


def host_allowed(value: str, port: int) -> bool:
    host = (value or "").strip().lower()
    return host in {f"127.0.0.1:{port}", f"localhost:{port}"}


def peer_is_loopback(request: web.Request) -> bool:
    remote = request.remote or ""
    if remote.startswith("::ffff:"):
        remote = remote[7:]
    return remote in {"127.0.0.1", "::1"}


def _loopback_origins(port: int) -> set[str]:
    return {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}


def bootstrap_allowed(request: web.Request, port: int) -> bool:
    """Token handoff for the local page only.

    The HTML never contains the token. The browser on this machine asks for it
    with a custom header, which forces a CORS preflight that we do not answer.
    Cross-site fetches and any foreign Origin are refused. No CORS headers are
    set, and the TCP peer must be loopback.
    """
    if not peer_is_loopback(request):
        return False
    if request.headers.get(_UI_HEADER, "").strip() != "1":
        return False
    site = request.headers.get("Sec-Fetch-Site", "").strip().lower()
    if site == "cross-site":
        return False
    origin = request.headers.get("Origin", "").strip().rstrip("/")
    if origin and origin not in _loopback_origins(port):
        return False
    return True


def _harden(response: web.Response) -> web.Response:
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers.setdefault("Cache-Control", "no-store")
    return response


def _as_bool(value: object, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    raise UsageError(f"expected a boolean, got {value!r}")


def _public_get(request: web.Request) -> bool:
    return request.method == "GET" and (request.path in _UI_FILES or request.path == "/favicon.ico")


@web.middleware
async def guard(request: web.Request, handler):
    if request.method == "OPTIONS":
        return json_response(405, error_payload(2, "method not allowed"))
    port = int(request.app[PORT_KEY][0])
    if not host_allowed(request.headers.get("Host", ""), port) or not peer_is_loopback(request):
        return json_response(403, error_payload(5, "invalid host"))
    bootstrap = request.method == "GET" and request.path == _BOOTSTRAP_PATH
    if bootstrap:
        if not bootstrap_allowed(request, port):
            return json_response(403, error_payload(5, "ui bootstrap refused"))
    elif not _public_get(request):
        if not token_ok(presented_token(request), request.app[TOKEN_KEY]):
            return json_response(401, error_payload(3, "unauthorized"))
    try:
        response = await handler(request)
    except TgdlError as exc:
        status = 404 if exc.exit_code == 1 and "not found" in str(exc).lower() else _HTTP_FOR_EXIT.get(exc.exit_code, 400)
        return json_response(status, error_payload(exc.exit_code, str(exc), **exc.extra))
    except web.HTTPException:
        raise
    except Exception as exc:
        log.exception("api request failed")
        return json_response(500, error_payload(1, str(exc) or "internal error"))
    if isinstance(response, web.StreamResponse) and not isinstance(response, web.Response):
        return response
    if isinstance(response, web.Response):
        return _harden(response)
    return response


def _job_response(result: dict) -> web.Response:
    job = result["job"]
    done = bool(job.get("done"))
    code = job.get("exitCode")
    if not done:
        status = 202
    elif isinstance(code, int) and code not in (0, None):
        status = _HTTP_FOR_EXIT.get(code, 409)
    else:
        status = 200
    return json_response(
        status,
        {
            "schema": SCHEMA,
            "ok": True,
            "existing": bool(result.get("existing")),
            "exitCode": code,
            "job": job,
        },
    )


async def _body(request: web.Request) -> dict:
    if request.content_length and request.content_length > 1_048_576:
        raise UsageError("JSON body is larger than 1 MiB")
    if not request.can_read_body:
        return {}
    try:
        payload = await request.json(loads=json.loads)
    except Exception as exc:
        raise UsageError("body must be a JSON object") from exc
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise UsageError("JSON body must be an object")
    return payload


async def ping(_request: web.Request) -> web.Response:
    return json_response(
        200,
        {
            "schema": SCHEMA,
            "ok": True,
            "app": APP_NAME,
            "version": __version__,
            "pid": os.getpid(),
            "python": __import__("platform").python_version(),
            "telethon": telethon_version(),
        },
    )


async def version(_request: web.Request) -> web.Response:
    payload = version_payload()
    payload["ok"] = True
    return json_response(200, payload)


async def help_route(_request: web.Request) -> web.Response:
    payload = help_payload()
    payload["ok"] = True
    return json_response(200, payload)


async def chats(request: web.Request) -> web.Response:
    try:
        limit = int(request.query.get("limit", "100"))
    except ValueError as exc:
        raise UsageError("limit must be an integer") from exc
    groups = request.query.get("groupsOnly", "false")
    wait = _as_bool(request.query.get("wait", "true"), True)
    result = await request.app[SERVICE_KEY].list_chats(
        limit=limit,
        groups_only=_as_bool(groups, False),
        wait=wait,
        added_by=_added_by({"addedBy": request.query.get("addedBy")}),
    )
    return _job_response(result)


async def scan(request: web.Request) -> web.Response:
    from tgdl.params import normalize_params

    body = await _body(request)
    params = normalize_params(body, kind="scan", cfg=request.app[CONFIG_KEY])
    result = await request.app[SERVICE_KEY].scan(
        params,
        wait=_as_bool(body.get("wait"), False),
        added_by=_added_by(body),
    )
    return _job_response(result)


async def download(request: web.Request) -> web.Response:
    from tgdl.params import normalize_params

    body = await _body(request)
    params = normalize_params(body, kind="download", cfg=request.app[CONFIG_KEY])
    result = await request.app[SERVICE_KEY].download(
        params,
        wait=_as_bool(body.get("wait"), False),
        added_by=_added_by(body),
    )
    return _job_response(result)


async def task(request: web.Request) -> web.Response:
    job_id = request.query.get("id", "").strip()
    if not job_id:
        raise UsageError("query id is required")
    job = request.app[SERVICE_KEY].get_job(job_id)
    if job is None:
        raise TgdlError(f"job not found: {job_id}")
    return json_response(200, {"schema": SCHEMA, "ok": True, "exitCode": job.get("exitCode"), "job": job})


async def tasks(request: web.Request) -> web.Response:
    try:
        limit = int(request.query.get("limit", "50"))
    except ValueError as exc:
        raise UsageError("limit must be an integer") from exc
    if limit < 1 or limit > 200:
        raise UsageError("limit must be between 1 and 200")
    found = request.app[SERVICE_KEY].list_jobs(limit)
    return json_response(200, {"schema": SCHEMA, "ok": True, "tasks": found})


async def status(request: web.Request) -> web.Response:
    job_id = request.query.get("id", "").strip()
    if job_id:
        job = request.app[SERVICE_KEY].get_job(job_id)
        if job is None:
            raise TgdlError(f"job not found: {job_id}")
    else:
        job = request.app[SERVICE_KEY].latest_job()
    return json_response(
        200,
        {"schema": SCHEMA, "ok": True, "exitCode": None if job is None else job.get("exitCode"), "job": job},
    )


def _job_id(body: dict) -> str:
    job_id = str(body.get("id") or "").strip()
    if not job_id:
        raise UsageError("id is required")
    return job_id


def _added_by(payload: dict) -> str:
    value = payload.get("addedBy", "agent")
    if value is None or value == "":
        return "agent"
    if value not in {"agent", "user"}:
        raise UsageError("addedBy must be agent or user")
    return str(value)


async def cancel(request: web.Request) -> web.Response:
    body = await _body(request)
    job = await request.app[SERVICE_KEY].cancel(_job_id(body))
    return json_response(200, {"schema": SCHEMA, "ok": True, "id": job.get("id"), "exitCode": job.get("exitCode"), "job": job})


async def pause(request: web.Request) -> web.Response:
    body = await _body(request)
    job = await request.app[SERVICE_KEY].pause(_job_id(body))
    return json_response(200, {"schema": SCHEMA, "ok": True, "id": job.get("id"), "exitCode": job.get("exitCode"), "job": job})


async def resume(request: web.Request) -> web.Response:
    body = await _body(request)
    result = await request.app[SERVICE_KEY].resume(_job_id(body))
    payload = _job_response(result)
    return payload


async def remove(request: web.Request) -> web.Response:
    body = await _body(request)
    delete_files = _as_bool(body.get("deleteFiles"), False)
    job = await request.app[SERVICE_KEY].remove(_job_id(body), delete_files=delete_files)
    return json_response(200, {"schema": SCHEMA, "ok": True, "id": job.get("id"), "deleteFiles": delete_files, "job": job})


async def account(request: web.Request) -> web.Response:
    info = await request.app[SERVICE_KEY].account()
    if not isinstance(info, dict):
        raise TgdlError("account status unavailable")
    payload = {"schema": SCHEMA, "ok": True, "loggedIn": bool(info.get("loggedIn"))}
    for key in ("userId", "username", "displayName", "reason"):
        payload[key] = info.get(key)
    return json_response(200, payload)


async def ui_bootstrap(request: web.Request) -> web.Response:
    return json_response(
        200,
        {
            "schema": SCHEMA,
            "ok": True,
            "token": request.app[TOKEN_KEY],
            "tokenHeader": "Authorization",
            "tokenScheme": "Bearer",
        },
    )


def _document_policy() -> str:
    return (
        "default-src 'none'; "
        "style-src 'self'; "
        "script-src 'self'; "
        "connect-src 'self'; "
        "img-src 'self'; "
        "base-uri 'none'; "
        "form-action 'self'; "
        "frame-ancestors 'none'"
    )


async def ui_asset(request: web.Request) -> web.Response:
    spec = _UI_FILES.get(request.path)
    if spec is None:
        raise TgdlError("not found")
    name, content_type = spec
    root = _WEB_DIR.resolve()
    path = (root / name).resolve()
    if path.parent != root or not path.is_file():
        return json_response(500, error_payload(1, "web UI files are missing"))
    headers = {"Cache-Control": "no-cache"}
    if content_type == "text/html":
        headers["Content-Security-Policy"] = _document_policy()
    return web.Response(
        body=path.read_bytes(),
        content_type=content_type,
        charset="utf-8",
        headers=headers,
    )


async def favicon(_request: web.Request) -> web.Response:
    return web.Response(
        body=_FAVICON,
        content_type="image/svg+xml",
        headers={"Cache-Control": "no-cache"},
    )


def create_app(service: Any, *, token: str, port: int, config: Any) -> web.Application:
    app = web.Application(middlewares=[guard], client_max_size=1_048_576)
    # A one-element list so the bound port can be filled in after listen
    # without mutating the application mapping (aiohttp freezes that).
    app[SERVICE_KEY] = service
    app[TOKEN_KEY] = token
    app[PORT_KEY] = [port]
    app[CONFIG_KEY] = config
    app.router.add_get("/", ui_asset)
    app.router.add_get("/ui/app.css", ui_asset)
    app.router.add_get("/ui/app.js", ui_asset)
    app.router.add_get("/favicon.ico", favicon)
    app.router.add_get("/api/v1/ui/bootstrap", ui_bootstrap)
    app.router.add_get("/api/v1/ping", ping)
    app.router.add_get("/api/v1/version", version)
    app.router.add_get("/api/v1/help", help_route)
    app.router.add_get("/api/v1/account", account)
    app.router.add_get("/api/v1/chats", chats)
    app.router.add_post("/api/v1/scan", scan)
    app.router.add_post("/api/v1/download", download)
    app.router.add_get("/api/v1/task", task)
    app.router.add_get("/api/v1/tasks", tasks)
    app.router.add_get("/api/v1/status", status)
    app.router.add_post("/api/v1/cancel", cancel)
    app.router.add_post("/api/v1/pause", pause)
    app.router.add_post("/api/v1/resume", resume)
    app.router.add_post("/api/v1/remove", remove)
    return app


async def start_api(service: Any, *, token: str, host: str, port: int, config: Any):
    """Bind 127.0.0.1 and return `(runner, actual_port)`."""
    if host not in {"127.0.0.1", "localhost"}:
        raise RejectedError("API host must be 127.0.0.1")
    app = create_app(service, token=token, port=port, config=config)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    try:
        await site.start()
    except OSError:
        await runner.cleanup()
        raise
    sockets = list(site._server.sockets or [])
    if not sockets:
        await runner.cleanup()
        raise RejectedError("API socket did not bind")
    bound_host, bound_port = sockets[0].getsockname()[:2]
    if bound_host != "127.0.0.1":
        await runner.cleanup()
        raise RejectedError("refusing non-loopback bind")
    app[PORT_KEY][0] = int(bound_port)
    return runner, int(bound_port)
