"""CLI entry point. stdout is the result; stderr is logs and progress."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys

from tgdl.api_client import (
    clear_runtime,
    discover_api,
    ensure_token,
    run_remote_job,
    write_runtime,
)
from tgdl.config import load_config
from tgdl.contract import EXIT_NAME, SCHEMA, error_payload
from tgdl.errors import RejectedError, TgdlError
from tgdl.helptext import help_payload, help_text, version_payload
from tgdl.jobs import JobStore, recover_stale_jobs
from tgdl.progress import format_progress

log = logging.getLogger("tgdl")

_PROXY = {"list-chats", "scan", "download"}


def emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _common() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="JSON on stdout")
    parser.add_argument("--config", default=argparse.SUPPRESS, help="config.yaml path")
    parser.add_argument("--data-dir", default=argparse.SUPPRESS, help="jobs, token, and pid directory")
    parser.add_argument("--session", default=argparse.SUPPRESS, help="Telethon session file")
    parser.add_argument("--quiet", action="store_true", default=argparse.SUPPRESS, help="hide progress")
    parser.add_argument("--verbose", action="store_true", default=argparse.SUPPRESS, help="debug logs")
    return parser


def _add_filters(parser: argparse.ArgumentParser, *, download: bool) -> None:
    parser.add_argument("--chat", action="append", dest="chats", help="username, t.me link, invite, or numeric id")
    parser.add_argument("--ext", action="append", help="extension; repeat or comma-separate")
    parser.add_argument("--min-size", dest="min_size", help="e.g. 1MB")
    parser.add_argument("--max-size", dest="max_size", help="e.g. 2GB")
    parser.add_argument("--since", help="UTC start YYYY-MM-DD (inclusive)")
    parser.add_argument("--until", help="UTC end YYYY-MM-DD (inclusive)")
    parser.add_argument("--sender", help="user id, @username, or display-name substring")
    parser.add_argument("--name", help="filename substring")
    parser.add_argument("--name-regex", dest="name_regex", help="filename regular expression")
    parser.add_argument("--photos", action="store_true", help="also include photos")
    parser.add_argument("--no-photos", action="store_true", help="do not include photos")
    parser.add_argument("--limit", type=int, help="max matching files")
    parser.add_argument("--message-id", action="append", dest="message_ids", help="only these message ids")
    parser.add_argument("--output", help="download directory")
    parser.add_argument("--flat", action="store_true", help="do not group into chat/yyyy-mm folders")
    if download:
        parser.add_argument("--workers", type=int, help="concurrent downloads (default 4)")
        parser.add_argument("--no-resume", action="store_true", help="start matching parts over at byte 0")
        parser.add_argument("--force", action="store_true", help="do not reuse a completed job or skip finished files")
        parser.add_argument("--timeout", help="stop after this duration, e.g. 30m")
        parser.add_argument("--flood-cap", dest="flood_cap", help="max FloodWait to sleep, default 15m")


def build_parser() -> argparse.ArgumentParser:
    common = _common()
    parser = argparse.ArgumentParser(
        prog="tgdl",
        description="Agent-first Telegram group file downloader",
        parents=[common],
    )
    parser.add_argument("--version", action="store_true", help="print version and exit")
    sub = parser.add_subparsers(dest="command")

    login = sub.add_parser("login", help="interactive MTProto login", parents=[common])
    login.add_argument("--phone", help="E.164 phone; otherwise prompted or TGDL_PHONE")

    chats = sub.add_parser("list-chats", help="list dialogs", parents=[common])
    chats.add_argument("--limit", type=int, default=100)
    chats.add_argument("--groups-only", action="store_true")

    scan = sub.add_parser("scan", help="list matching files", parents=[common])
    _add_filters(scan, download=False)
    download = sub.add_parser("download", help="download matching files", parents=[common])
    _add_filters(download, download=True)

    status = sub.add_parser("status", help="show a job", parents=[common])
    status.add_argument("job_id", nargs="?")
    status.add_argument("--job", help="job id")

    sub.add_parser("version", help="version and runtime", parents=[common])
    sub.add_parser("help", help="command catalog", parents=[common])
    serve = sub.add_parser("serve", help="loopback HTTP API", parents=[common])
    serve.add_argument("--host", help="must be 127.0.0.1")
    serve.add_argument("--port", type=int)
    return parser


def setup_logging(verbose: bool, quiet: bool) -> None:
    level = logging.DEBUG if verbose else logging.WARNING if quiet else logging.INFO
    logging.basicConfig(level=level, format="%(levelname)s %(message)s", stream=sys.stderr, force=True)
    if not verbose:
        logging.getLogger("telethon").setLevel(logging.WARNING)
        logging.getLogger("aiohttp").setLevel(logging.WARNING)


def _cfg_from_args(args):
    return load_config(
        config_path=getattr(args, "config", None),
        data_dir=getattr(args, "data_dir", None),
        session=getattr(args, "session", None),
        output=getattr(args, "output", None),
    )


def _raw_filters(args) -> dict:
    raw: dict = {}
    if getattr(args, "chats", None):
        raw["chats"] = args.chats
    if getattr(args, "ext", None):
        raw["ext"] = args.ext
    if getattr(args, "min_size", None) is not None:
        raw["minSize"] = args.min_size
    if getattr(args, "max_size", None) is not None:
        raw["maxSize"] = args.max_size
    if getattr(args, "since", None):
        raw["since"] = args.since
    if getattr(args, "until", None):
        raw["until"] = args.until
    if getattr(args, "sender", None):
        raw["sender"] = args.sender
    if getattr(args, "name", None):
        raw["name"] = args.name
    if getattr(args, "name_regex", None):
        raw["nameRegex"] = args.name_regex
    if getattr(args, "photos", False):
        raw["photos"] = True
    if getattr(args, "no_photos", False):
        raw["noPhotos"] = True
    if getattr(args, "limit", None) is not None:
        raw["limit"] = args.limit
    if getattr(args, "message_ids", None):
        raw["messageIds"] = args.message_ids
    if getattr(args, "output", None):
        raw["output"] = args.output
    if getattr(args, "flat", False):
        raw["flat"] = True
    if getattr(args, "workers", None) is not None:
        raw["workers"] = args.workers
    if getattr(args, "no_resume", False):
        raw["noResume"] = True
    if getattr(args, "force", False):
        raw["force"] = True
    if getattr(args, "timeout", None):
        raw["timeout"] = args.timeout
    if getattr(args, "flood_cap", None):
        raw["floodCap"] = args.flood_cap
    return raw


def _print_job(job: dict | None, json_mode: bool, *, envelope: dict | None = None) -> None:
    if json_mode:
        if envelope is not None:
            emit(envelope)
        else:
            emit({"schema": SCHEMA, "ok": True, "exitCode": None if job is None else job.get("exitCode"), "job": job})
        return
    if job is None:
        print("no job")
        return
    kind = job.get("kind")
    if kind == "list-chats":
        for chat in job.get("resolvedChats") or []:
            username = chat.get("username") or ""
            print(f"{chat.get('id')}\t{chat.get('kind')}\t{username}\t{chat.get('title')}")
        return
    if kind == "scan":
        for item in job.get("files") or []:
            print(f"{item.get('date')}\t{item.get('fileSize')}\t{item.get('chatTitle')}\t{item.get('fileName')}")
        print(format_progress(job), file=sys.stderr)
        return
    for item in job.get("files") or []:
        if item.get("state") in {"complete", "skipped"} and item.get("dest"):
            print(item["dest"])
    print(format_progress(job), file=sys.stderr)
    if job.get("error") and job.get("status") != "complete":
        print(job["error"], file=sys.stderr)


async def _with_service(cfg, factory, quiet: bool):
    from tgdl.progress import make_reporter
    from tgdl.service import Service

    reporter = make_reporter(enabled=not quiet)
    service = Service(cfg, reporter=reporter)
    loop = asyncio.get_running_loop()
    hits = {"n": 0}

    def _stop() -> None:
        hits["n"] += 1
        sys.stderr.write("interrupt: finishing the current chunk\n")
        sys.stderr.flush()
        for event in list(service._cancels.values()):
            event.set()
        if hits["n"] >= 2:
            for task in list(service._tasks.values()):
                task.cancel()

    try:
        loop.add_signal_handler(signal.SIGINT, _stop)
        loop.add_signal_handler(signal.SIGTERM, _stop)
    except NotImplementedError:
        pass
    try:
        return await factory(service)
    finally:
        reporter.close()
        await service.close()


def _login(cfg, phone: str | None, json_mode: bool) -> int:
    if discover_api(cfg.data_dir) is not None:
        raise RejectedError("stop tgdl serve before login; it holds the session file")
    from tgdl.telegram_io import login, make_client

    async def _run():
        client = make_client(cfg)
        try:
            return await login(client, phone or cfg.phone, cfg.session)
        finally:
            await client.disconnect()

    info = asyncio.run(_run())
    payload = {"schema": SCHEMA, "ok": True, "exitCode": 0, **info}
    if json_mode:
        emit(payload)
    else:
        who = f"@{info['username']}" if info.get("username") else info.get("userId")
        state = "already authorized" if info.get("alreadyAuthorized") else "logged in"
        print(f"{state} as {who}")
        print(f"session {info['session']}")
    return 0


def _status(cfg, job_id: str | None, json_mode: bool) -> int:
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    store = JobStore(cfg.data_dir)
    if discover_api(cfg.data_dir) is None:
        recover_stale_jobs(store)
    if job_id:
        try:
            job = store.get(job_id)
        except ValueError as exc:
            raise TgdlError(f"job not found: {job_id}") from exc
        if job is None:
            raise TgdlError(f"job not found: {job_id}")
    else:
        job = store.latest()
    _print_job(job, json_mode)
    if job and job.get("done") and isinstance(job.get("exitCode"), int):
        return int(job["exitCode"])
    return 0


async def _serve(cfg, host: str | None, port: int | None, json_mode: bool) -> int:
    from tgdl.api import start_api
    from tgdl.service import Service

    chosen_host = host or cfg.api_host
    chosen_port = cfg.api_port if port is None else port
    existing = discover_api(cfg.data_dir)
    if existing is not None:
        raise RejectedError(
            f"API already running at {existing.base}",
            url=existing.base,
            port=existing.port,
        )
    token = ensure_token(cfg.data_dir, cfg.api_token)
    service = Service(cfg)
    runner, actual = await start_api(
        service,
        token=token,
        host=chosen_host,
        port=chosen_port,
        config=cfg,
    )
    write_runtime(cfg.data_dir, actual, token)
    payload = {
        "schema": SCHEMA,
        "ok": True,
        "url": f"http://127.0.0.1:{actual}",
        "pid": __import__("os").getpid(),
        "tokenFile": str((cfg.data_dir / "api-token").resolve()),
        "port": actual,
    }
    if json_mode:
        emit(payload)
    else:
        print(f"tgdl API {payload['url']}", file=sys.stderr)
        print(f"token file {payload['tokenFile']}", file=sys.stderr)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _stop() -> None:
        stop.set()

    try:
        loop.add_signal_handler(signal.SIGINT, _stop)
        loop.add_signal_handler(signal.SIGTERM, _stop)
    except NotImplementedError:
        pass
    try:
        await stop.wait()
    finally:
        await service.shutdown()
        await runner.cleanup()
        clear_runtime(cfg.data_dir)
    return 0


def _dispatch(args, json_mode: bool) -> int:
    quiet = bool(getattr(args, "quiet", False))
    verbose = bool(getattr(args, "verbose", False))
    setup_logging(verbose, quiet)
    command = args.command
    if command is None and getattr(args, "version", False):
        command = "version"
    if command is None:
        sys.stderr.write(help_text())
        if json_mode:
            emit(error_payload(2, "missing command"))
        return 2
    if command == "help":
        if json_mode:
            payload = help_payload()
            payload["ok"] = True
            emit(payload)
        else:
            sys.stdout.write(help_text())
        return 0
    if command == "version":
        payload = version_payload()
        payload["ok"] = True
        payload["exitCode"] = 0
        if json_mode:
            emit(payload)
        else:
            build = payload["build"]
            print(f"tgdl {payload['tgdl']}")
            print(f"python {build['python']}")
            print(f"telethon {build['telethon']}")
            print(f"os {build['os']}/{build['arch']}")
        return 0

    cfg = _cfg_from_args(args)
    if command == "login":
        return _login(cfg, getattr(args, "phone", None), json_mode)
    if command == "status":
        job_id = getattr(args, "job", None) or getattr(args, "job_id", None)
        return _status(cfg, job_id, json_mode)
    if command == "serve":
        return asyncio.run(_serve(cfg, getattr(args, "host", None), getattr(args, "port", None), json_mode))

    if command in {"scan", "download"}:
        from tgdl.params import normalize_params

        params = normalize_params(_raw_filters(args), kind=command, cfg=cfg)
    else:
        params = None

    remote = discover_api(cfg.data_dir) if command in _PROXY else None
    if remote is not None:
        if command == "list-chats":
            limit = getattr(args, "limit", 100)
            groups = "true" if getattr(args, "groups_only", False) else "false"
            path = f"/api/v1/chats?limit={limit}&groupsOnly={groups}&wait=false"
            code, payload = run_remote_job(remote, "GET", path, None, quiet=quiet, timeout_sec=cfg.flood_cap_sec)
        elif command == "scan":
            code, payload = run_remote_job(
                remote, "POST", "/api/v1/scan", params, quiet=quiet, timeout_sec=None
            )
        else:
            code, payload = run_remote_job(
                remote,
                "POST",
                "/api/v1/download",
                params,
                quiet=quiet,
                timeout_sec=params.get("timeoutSec") if params else None,
            )
        job = payload.get("job") if isinstance(payload, dict) else None
        if job is None and json_mode:
            emit(payload if isinstance(payload, dict) else error_payload(code, "api request failed"))
            return code
        if job is None:
            message = payload.get("error") if isinstance(payload, dict) else "api request failed"
            print(f"error: {message}", file=sys.stderr)
            return code
        _print_job(job, json_mode, envelope=payload if json_mode else None)
        return code

    async def factory(service):
        if command == "list-chats":
            return await service.list_chats(
                limit=getattr(args, "limit", 100),
                groups_only=bool(getattr(args, "groups_only", False)),
                wait=True,
            )
        if command == "scan":
            return await service.scan(params, wait=True)
        return await service.download(params, wait=True)

    result = asyncio.run(_with_service(cfg, factory, quiet))
    job = result["job"]
    code = job.get("exitCode")
    envelope = {
        "schema": SCHEMA,
        "ok": job.get("status") == "complete",
        "existing": result.get("existing", False),
        "exitCode": code,
        "code": EXIT_NAME.get(code, "failure") if isinstance(code, int) else None,
        "job": job,
    }
    if job.get("error"):
        envelope["error"] = job["error"]
    _print_job(job, json_mode, envelope=envelope if json_mode else None)
    code = job.get("exitCode")
    return int(code) if isinstance(code, int) else 0


def run(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    json_mode = "--json" in argv
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code
        if code not in (0, None):
            if json_mode:
                emit(error_payload(2, "invalid arguments"))
            return 2
        return 0
    json_mode = bool(getattr(args, "json", False)) or json_mode
    try:
        return _dispatch(args, json_mode)
    except TgdlError as exc:
        payload = error_payload(exc.exit_code, str(exc), **exc.extra)
        if json_mode:
            emit(payload)
        else:
            print(f"error: {exc}", file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        if json_mode:
            emit(error_payload(4, "interrupted"))
        else:
            print("interrupted", file=sys.stderr)
        return 4


def main(argv: list[str] | None = None) -> None:
    sys.exit(run(argv))


if __name__ == "__main__":
    main()
