"""Jobs shared by the CLI and the loopback API.

One process owns the Telethon session. `tgdl serve` keeps a Service alive;
the CLI proxies scan/download/list-chats to it when the API is up.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path

from tgdl.config import Config
from tgdl.errors import AuthError, RejectedError, TgdlError, UsageError
from tgdl.jobs import (
    JobStore,
    canonical_key,
    fail_job,
    finalize_job,
    new_job,
    recover_stale_jobs,
)
from tgdl.models import state_for
from tgdl.naming import plan_items
from tgdl.params import query_from_params
from tgdl.progress import NullReporter, ProgressReporter
from tgdl.resume import build_sidecar, read_sidecar, write_sidecar
from tgdl.transfer import sleep_or_cancel

log = logging.getLogger("tgdl.service")


def outputs_present(job: dict) -> bool:
    """True when a completed download still has every destination file."""
    if job.get("status") != "complete":
        return False
    files = job.get("files") or []
    if not files:
        return False
    for item in files:
        if item.get("state") not in {"complete", "skipped"}:
            return False
        dest = item.get("dest")
        if not dest:
            return False
        path = Path(dest)
        if not path.is_file():
            return False
        size = item.get("fileSize")
        if size is not None and path.stat().st_size != int(size):
            return False
    return True


class Service:
    def __init__(self, cfg: Config, reporter: ProgressReporter | None = None) -> None:
        self.cfg = cfg
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        self.store = JobStore(cfg.data_dir)
        self.reporter = reporter or NullReporter()
        self._tasks: dict[str, asyncio.Task] = {}
        self._cancels: dict[str, asyncio.Event] = {}
        self._client = None
        self._client_lock = asyncio.Lock()
        self._save_at = 0.0
        self.recover_stale()

    def recover_stale(self) -> None:
        """A previous process that died mid-job leaves status=running. Mark it partial."""
        recover_stale_jobs(self.store)

    async def close(self) -> None:
        client = self._client
        self._client = None
        if client is not None:
            await client.disconnect()

    async def shutdown(self) -> None:
        for event in list(self._cancels.values()):
            event.set()
        tasks = [task for task in self._tasks.values() if not task.done()]
        if tasks:
            await asyncio.wait(tasks, timeout=20)
        await self.close()

    def get_job(self, job_id: str) -> dict | None:
        try:
            return self.store.get(job_id)
        except ValueError:
            return None

    def list_jobs(self, limit: int = 50) -> list[dict]:
        return self.store.list_jobs(limit)

    def latest_job(self) -> dict | None:
        return self.store.latest()

    async def cancel(self, job_id: str) -> dict:
        job = self.get_job(job_id)
        if job is None:
            raise TgdlError(f"job not found: {job_id}")
        event = self._cancels.get(job_id)
        if event is not None:
            event.set()
            task = self._tasks.get(job_id)
            if task is not None:
                with_timeout = asyncio.wait({task}, timeout=20)
                await with_timeout
            latest = self.get_job(job_id) or job
            return latest
        if not job.get("done"):
            fail_job(job, exit_code=4, message="cancelled")
            job["status"] = "cancelled"
            self.store.save(job)
        return self.get_job(job_id) or job

    async def list_chats(self, *, limit: int, groups_only: bool, wait: bool) -> dict:
        if limit < 1 or limit > 5000:
            raise UsageError("limit must be between 1 and 5000")
        key = f"list:{limit}:{int(bool(groups_only))}"
        running = self._running_by_key(key)
        if running is not None:
            return await self._wait_existing(running, wait)
        params = {"limit": limit, "groupsOnly": bool(groups_only)}
        job = new_job("list-chats", params, key)
        return await self._spawn(job, self._run_list, wait, existing=False)

    async def scan(self, params: dict, *, wait: bool) -> dict:
        key = canonical_key("scan", params)
        running = self._running_by_key(key)
        if running is not None:
            return await self._wait_existing(running, wait)
        job = new_job("scan", params, key)
        return await self._spawn(job, self._run_scan, wait, existing=False)

    async def download(self, params: dict, *, wait: bool) -> dict:
        key = canonical_key("download", params)
        if not params.get("force"):
            running = self._running_by_key(key)
            if running is not None:
                return await self._wait_existing(running, wait)
            existing = self.store.find_by_key(key)
            if existing and existing.get("status") in {"partial", "cancelled", "running"}:
                return await self._restart(existing, params, wait)
            if existing and outputs_present(existing):
                self.store.save(existing)
                return {"existing": True, "job": self.store.get(existing["id"]) or existing}
        active = self._active_download()
        if active is not None:
            raise RejectedError(
                f"download {active['id']} is already running",
                jobId=active["id"],
            )
        job = new_job("download", params, key)
        return await self._spawn(job, self._run_download, wait, existing=False)

    async def _restart(self, job: dict, params: dict, wait: bool) -> dict:
        active = self._active_download()
        if active is not None and active["id"] != job["id"]:
            raise RejectedError(
                f"download {active['id']} is already running",
                jobId=active["id"],
            )
        if job["id"] in self._tasks and not self._tasks[job["id"]].done():
            return await self._wait_existing(job, wait)
        for field in ("workers", "timeoutSec", "floodCapSec"):
            if field in params:
                job["params"][field] = params[field]
        job["status"] = "running"
        job["done"] = False
        job["ok"] = False
        job["exitCode"] = None
        job["error"] = None
        job["files"] = []
        job["resolvedChats"] = []
        return await self._spawn(job, self._run_download, wait, existing=True)

    async def _wait_existing(self, job: dict, wait: bool) -> dict:
        if wait:
            task = self._tasks.get(job["id"])
            if task is not None:
                await task
        latest = self.store.get(job["id"]) or job
        return {"existing": True, "job": latest}

    def _running_by_key(self, key: str) -> dict | None:
        for job_id, task in self._tasks.items():
            if task.done():
                continue
            job = self.store.get(job_id)
            if job and job.get("key") == key and not job.get("done"):
                return job
        return None

    def _active_download(self) -> dict | None:
        for job_id, task in self._tasks.items():
            if task.done():
                continue
            job = self.store.get(job_id)
            if job and job.get("kind") == "download" and not job.get("done"):
                return job
        return None

    async def _spawn(self, job: dict, runner, wait: bool, *, existing: bool) -> dict:
        self.store.save(job)
        task = asyncio.create_task(self._guard(job["id"], runner), name=job["id"])
        self._tasks[job["id"]] = task
        task.add_done_callback(lambda done, job_id=job["id"]: self._on_done(job_id, done))
        if wait:
            await task
        latest = self.store.get(job["id"]) or job
        return {"existing": existing, "job": latest}

    def _on_done(self, job_id: str, task: asyncio.Task) -> None:
        self._tasks.pop(job_id, None)
        if task.cancelled():
            return
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            return
        if exc:
            log.error("job %s failed: %s", job_id, exc)

    async def _guard(self, job_id: str, runner) -> None:
        cancel = asyncio.Event()
        self._cancels[job_id] = cancel
        try:
            await runner(job_id, cancel)
        except asyncio.CancelledError:
            job = self.store.get(job_id)
            if job and not job.get("done"):
                self._mark_open_partial(job)
                finalize_job(job, cancelled=True, timeout=False)
                self.store.save(job)
            raise
        except TgdlError as exc:
            job = self.store.get(job_id)
            if job and not job.get("done"):
                self._mark_open_partial(job)
                fail_job(job, exit_code=exc.exit_code, message=str(exc))
                if exc.exit_code == 4:
                    job["status"] = "partial"
                self.store.save(job)
        except Exception as exc:
            log.exception("job %s", job_id)
            job = self.store.get(job_id)
            if job and not job.get("done"):
                self._mark_open_partial(job)
                fail_job(job, exit_code=1, message=str(exc) or type(exc).__name__)
                self.store.save(job)
        finally:
            self._cancels.pop(job_id, None)
            self.reporter.update(self.store.get(job_id) or {})

    @staticmethod
    def _mark_open_partial(job: dict) -> None:
        for item in job.get("files") or []:
            if item.get("state") == "active":
                item["state"] = "partial"

    async def _client_conn(self):
        from tgdl.telegram_io import make_client

        async with self._client_lock:
            client = self._client
            if client is None or not client.is_connected():
                client = make_client(self.cfg)
                await client.connect()
                if not await client.is_user_authorized():
                    await client.disconnect()
                    raise AuthError("not logged in; run: tgdl login")
                self._client = client
            return client

    def _save(self, job: dict, *, force: bool = False) -> None:
        now = time.monotonic()
        if force or now - self._save_at >= 0.4:
            self.store.save(job)
            self._save_at = now
        try:
            self.reporter.update(job)
        except Exception:
            log.debug("progress update failed", exc_info=True)

    async def _run_list(self, job_id: str, cancel: asyncio.Event) -> None:
        from tgdl.telegram_io import list_dialogs

        job = self.store.get(job_id)
        assert job is not None
        params = job["params"]
        client = await self._client_conn()
        chats = await list_dialogs(
            client,
            limit=int(params["limit"]),
            groups_only=bool(params["groupsOnly"]),
            cancel=cancel,
            flood_cap=self.cfg.flood_cap_sec,
        )
        job = self.store.get(job_id) or job
        job["resolvedChats"] = chats
        if cancel.is_set():
            finalize_job(job, cancelled=True, timeout=False)
        else:
            finalize_job(job, cancelled=False, timeout=False)
        self._save(job, force=True)

    async def _run_scan(self, job_id: str, cancel: asyncio.Event) -> None:
        job = self.store.get(job_id)
        assert job is not None
        items, resolved, interrupted, reason, _entities = await self._collect(job["params"], cancel)
        job = self.store.get(job_id) or job
        job["resolvedChats"] = resolved
        output = job["params"].get("output")
        planned = []
        if output:
            planned = plan_items(
                items,
                output=Path(output),
                group_by_month=bool(job["params"].get("groupByMonth", True)),
                resume=bool(job["params"].get("resume", True)),
                force=bool(job["params"].get("force")),
                write=False,
            )
        files = []
        for index, item in enumerate(items):
            slot = planned[index] if planned else None
            files.append(item.to_json(slot, state="listed"))
        job["files"] = files
        if interrupted or cancel.is_set():
            job["error"] = reason or "scan interrupted"
            timed_out = bool(reason) and ("cap" in reason or "FloodWait" in reason)
            finalize_job(job, cancelled=not timed_out, timeout=timed_out)
        else:
            finalize_job(job, cancelled=False, timeout=False)
        self._save(job, force=True)

    async def _run_download(self, job_id: str, cancel: asyncio.Event) -> None:
        job = self.store.get(job_id)
        assert job is not None
        params = job["params"]
        timeout_flag = {"value": False}

        async def watch() -> None:
            seconds = params.get("timeoutSec")
            if not seconds:
                return
            if await sleep_or_cancel(float(seconds), cancel):
                timeout_flag["value"] = True
                cancel.set()

        watcher = asyncio.create_task(watch())
        try:
            items, resolved, interrupted, reason, entities = await self._collect(params, cancel)
            job = self.store.get(job_id) or job
            job["resolvedChats"] = resolved
            planned = plan_items(
                items,
                output=Path(params["output"]),
                group_by_month=bool(params.get("groupByMonth", True)),
                resume=bool(params.get("resume", True)),
                force=bool(params.get("force")),
                write=True,
            )
            files = []
            for item, slot in zip(items, planned):
                files.append(item.to_json(slot, state=state_for(slot)))
            job["files"] = files
            self._save(job, force=True)
            if interrupted or cancel.is_set():
                finalize_job(job, cancelled=not timeout_flag["value"], timeout=timeout_flag["value"])
                if reason and not job.get("error"):
                    job["error"] = reason
                self._save(job, force=True)
                return
            await self._download_files(job, cancel, entities=entities)
            job = self.store.get(job_id) or job
            finalize_job(
                job,
                cancelled=cancel.is_set() and not timeout_flag["value"],
                timeout=timeout_flag["value"],
            )
            self._save(job, force=True)
        finally:
            watcher.cancel()

    async def _collect(self, params: dict, cancel: asyncio.Event):
        from tgdl.telegram_io import collect_chat, entity_kind, entity_title, resolve_chat
        from telethon import utils

        client = await self._client_conn()
        query = query_from_params(params)
        flood = int(params.get("floodCapSec") or self.cfg.flood_cap_sec)
        items = []
        resolved = []
        entities: dict = {}
        interrupted = False
        reason = None
        for raw in params["chats"]:
            if cancel.is_set():
                interrupted = True
                reason = "interrupted"
                break
            entity = await resolve_chat(client, raw, cancel=cancel, flood_cap=flood)
            peer = str(utils.get_peer_id(entity))
            entities[peer] = entity
            resolved.append(
                {
                    "id": peer,
                    "title": entity_title(entity),
                    "input": raw,
                    "kind": entity_kind(entity),
                }
            )
            remaining = None
            if params.get("limit") is not None:
                remaining = int(params["limit"]) - len(items)
                if remaining <= 0:
                    break
            try:
                found = await collect_chat(
                    client,
                    entity,
                    query,
                    limit=remaining,
                    cancel=cancel,
                    flood_cap=flood,
                )
            except TgdlError as exc:
                if exc.exit_code == 4:
                    found = list(exc.extra.get("items") or [])
                    items.extend(found)
                    interrupted = True
                    reason = str(exc)
                    break
                raise
            items.extend(found)
            if params.get("limit") is not None and len(items) >= int(params["limit"]):
                items = items[: int(params["limit"])]
                break
        return items, resolved, interrupted, reason, entities

    async def _download_files(self, job: dict, cancel: asyncio.Event, entities: dict) -> None:
        from tgdl.telegram_io import transfer_message

        pending = [item for item in job["files"] if item.get("state") == "pending"]
        if not pending:
            return
        workers = max(1, min(int(job["params"].get("workers") or 1), len(pending)))
        queue: asyncio.Queue = asyncio.Queue()
        for item in pending:
            queue.put_nowait(item)
        client = await self._client_conn()
        flood = int(job["params"].get("floodCapSec") or self.cfg.flood_cap_sec)
        resume = bool(job["params"].get("resume", True))

        async def worker() -> None:
            while True:
                try:
                    record = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    if cancel.is_set():
                        continue
                    await self._download_one(
                        job,
                        record,
                        client=client,
                        entities=entities,
                        cancel=cancel,
                        flood_cap=flood,
                        resume=resume and record.get("plannedAction") != "download",
                        transfer=transfer_message,
                    )
                finally:
                    queue.task_done()

        tasks = [asyncio.create_task(worker()) for _ in range(workers)]
        await asyncio.gather(*tasks)

    async def _download_one(
        self,
        job: dict,
        record: dict,
        *,
        client,
        entities: dict,
        cancel: asyncio.Event,
        flood_cap: int,
        resume: bool,
        transfer,
    ) -> None:
        record["state"] = "active"
        self._save(job, force=True)
        entity = entities.get(record["chatId"])
        part = Path(record["part"])
        final = Path(record["dest"])
        expected = record.get("fileSize")
        if entity is None:
            record["state"] = "failed"
            record["error"] = "chat entity missing"
            self._save(job, force=True)
            return

        def on_progress(done: int) -> None:
            record["bytesWritten"] = done
            self._save(job, force=False)

        status, written, error = await transfer(
            client,
            entity,
            int(record["messageId"]),
            part,
            expected=expected,
            resume=resume,
            cancel=cancel,
            flood_cap=flood_cap,
            on_progress=on_progress,
        )
        record["bytesWritten"] = written
        if status == "complete":
            size = part.stat().st_size if part.exists() else 0
            if expected is not None and size != int(expected):
                record["state"] = "failed"
                record["error"] = f"size mismatch: got {size} expected {expected}"
                self._write_progress_sidecar(record, complete=False)
            else:
                final.parent.mkdir(parents=True, exist_ok=True)
                os.replace(part, final)
                record["bytesWritten"] = final.stat().st_size
                record["state"] = "complete"
                record["error"] = None
                self._write_progress_sidecar(record, complete=True)
        elif status == "partial":
            record["state"] = "partial"
            record["error"] = error or "partial"
            self._write_progress_sidecar(record, complete=False)
        else:
            record["state"] = "failed"
            record["error"] = error or "failed"
            self._write_progress_sidecar(record, complete=False)
        self._save(job, force=True)

    @staticmethod
    def _write_progress_sidecar(record: dict, *, complete: bool) -> None:
        path = Path(record["sidecar"])
        state, meta = read_sidecar(path)
        written = int(record.get("bytesWritten") or 0)
        if state != "ok" or meta is None:
            meta = build_sidecar(
                message_id=str(record["messageId"]),
                chat_id=str(record["chatId"]),
                file_size=record.get("fileSize"),
                file_unique_id=str(record["fileUniqueId"]),
                file_name=record.get("fileName") or "file",
                bytes_written=written,
                aligned_offset=written,
                complete=complete,
                part_path=Path(record["part"]),
                final_path=Path(record["dest"]),
            )
        else:
            meta["bytesWritten"] = written
            meta["complete"] = complete
            meta["alignedOffset"] = written
        write_sidecar(path, meta)
