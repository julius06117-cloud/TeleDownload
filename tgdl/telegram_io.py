"""Telethon adapter: login, dialogs, history scan, and one-file resume.

`flood_sleep_threshold=0` so FloodWait is visible here. The part file is
checkpointed before this module sleeps, which is what makes resume reliable.
"""

from __future__ import annotations

import logging
import platform
import sys
from datetime import timedelta
from getpass import getpass
from pathlib import Path

from telethon import TelegramClient, utils
from telethon.errors import (
    FloodWaitError,
    FileReferenceExpiredError,
    RPCError,
    TimedOutError,
    UserAlreadyParticipantError,
)
from telethon.tl.functions.messages import CheckChatInviteRequest, ImportChatInviteRequest
from telethon.tl.types import (
    Channel,
    Chat,
    ChatInviteAlready,
    InputMessagesFilterDocument,
    InputMessagesFilterPhotos,
    User,
)

from tgdl import __version__
from tgdl.config import Config
from tgdl.contract import REQUEST_SIZE
from tgdl.errors import AuthError, PartialError, RejectedError, TgdlError
from tgdl.filters import FileQuery
from tgdl.models import MediaItem
from tgdl.naming import sanitize_filename
from tgdl.resume import prepare_part_file
from tgdl.targets import parse_target
from tgdl.transfer import consume_chunks, sleep_or_cancel
from tgdl.util import chmod_private, flood_sleep_seconds

log = logging.getLogger("tgdl.telegram")

_AUTH_ERRORS = {
    "ApiIdInvalidError",
    "ApiIdPublishedFloodError",
    "PhoneNumberBannedError",
    "PhoneNumberInvalidError",
    "PhoneCodeInvalidError",
    "PhoneCodeExpiredError",
    "SessionPasswordNeededError",
}
_REJECT_ERRORS = {
    "ChannelBannedError",
    "ChannelInvalidError",
    "ChannelPrivateError",
    "ChatAdminRequiredError",
    "InviteHashExpiredError",
    "InviteHashInvalidError",
    "InviteRequestSentError",
    "PeerIdInvalidError",
    "UserBannedInChannelError",
    "UsernameInvalidError",
    "UsernameNotOccupiedError",
    "UsersTooMuchError",
}


def map_rpc(exc: Exception) -> TgdlError | None:
    name = type(exc).__name__
    text = str(exc) or name
    if name in _AUTH_ERRORS:
        return AuthError(text)
    if name in _REJECT_ERRORS:
        return RejectedError(text)
    return None


def make_client(cfg: Config) -> TelegramClient:
    if not cfg.has_credentials or cfg.api_id is None or not cfg.api_hash:
        raise AuthError(
            "missing api_id/api_hash. Set TGDL_API_ID and TGDL_API_HASH, or copy "
            "config.example.yaml. Create them at https://my.telegram.org — never commit them."
        )
    cfg.session.parent.mkdir(parents=True, exist_ok=True)
    return TelegramClient(
        str(cfg.session),
        int(cfg.api_id),
        cfg.api_hash,
        flood_sleep_threshold=0,
        receive_updates=False,
        device_model="tgdl",
        app_version=__version__,
        system_version=platform.platform()[:64],
    )


async def _pause_for_flood(exc: FloodWaitError, cancel, flood_cap: int) -> None:
    wait = flood_sleep_seconds(exc.seconds)
    log.warning("FloodWait %ss; sleeping %ss", exc.seconds, wait)
    if wait > flood_cap:
        raise PartialError(
            f"FloodWait {exc.seconds}s exceeds cap of {flood_cap}s; rerun to resume"
        ) from exc
    if not await sleep_or_cancel(wait, cancel):
        raise PartialError("interrupted during FloodWait") from exc


async def _call(factory, *, cancel, flood_cap: int):
    while True:
        try:
            return await factory()
        except FloodWaitError as exc:
            await _pause_for_flood(exc, cancel, flood_cap)


def _prompt(label: str) -> str:
    sys.stderr.write(label)
    sys.stderr.flush()
    return input().strip()


def user_public(me: object | None) -> dict:
    """Fields safe to show in the CLI and the local UI. No phone number."""
    if me is None:
        return {"userId": None, "username": None, "displayName": None}
    display = " ".join(
        part for part in (getattr(me, "first_name", None), getattr(me, "last_name", None)) if part
    )
    user_id = getattr(me, "id", None)
    return {
        "userId": None if user_id is None else str(user_id),
        "username": getattr(me, "username", None),
        "displayName": display or None,
    }


async def login(client: TelegramClient, phone: str | None, session: Path) -> dict:
    await client.connect()
    try:
        already = await client.is_user_authorized()
        if not already:
            try:
                await client.start(
                    phone=lambda: phone or _prompt("Phone (international format, e.g. +15551212): "),
                    code_callback=lambda: _prompt("Login code: "),
                    password=lambda: getpass("Two-factor password: "),
                )
            except (EOFError, KeyboardInterrupt) as exc:
                raise AuthError("login aborted") from exc
            except RPCError as exc:
                mapped = map_rpc(exc)
                raise (mapped or AuthError(str(exc) or type(exc).__name__)) from exc
        me = await client.get_me()
    finally:
        chmod_private(session)
    return {
        "alreadyAuthorized": already,
        "session": str(session),
        **user_public(me),
    }


def entity_kind(entity: object) -> str:
    if isinstance(entity, User):
        return "user"
    if isinstance(entity, Chat):
        return "group"
    if isinstance(entity, Channel):
        if getattr(entity, "megagroup", False):
            return "group"
        return "channel"
    return "chat"


def entity_title(entity: object) -> str:
    title = getattr(entity, "title", None)
    if title:
        return str(title)
    parts = [getattr(entity, "first_name", None), getattr(entity, "last_name", None)]
    name = " ".join(part for part in parts if part)
    if name:
        return name
    username = getattr(entity, "username", None)
    if username:
        return str(username)
    return str(utils.get_peer_id(entity))


def _dialog_json(dialog) -> dict:
    entity = dialog.entity
    return {
        "id": str(utils.get_peer_id(entity)),
        "title": dialog.title or entity_title(entity),
        "username": getattr(entity, "username", None),
        "kind": entity_kind(entity),
        "unread": int(getattr(dialog, "unread_count", 0) or 0),
        "members": getattr(entity, "participants_count", None),
    }


async def list_dialogs(client, *, limit: int, groups_only: bool, cancel, flood_cap: int) -> list[dict]:
    if limit < 1:
        raise RejectedError("limit must be >= 1")
    while True:
        chats: list[dict] = []
        scanned = 0
        try:
            async for dialog in client.iter_dialogs():
                if cancel.is_set():
                    raise PartialError("list-chats interrupted")
                scanned += 1
                if scanned > 5000:
                    break
                row = _dialog_json(dialog)
                if groups_only and row["kind"] == "user":
                    continue
                chats.append(row)
                if len(chats) >= limit:
                    break
            return chats
        except FloodWaitError as exc:
            await _pause_for_flood(exc, cancel, flood_cap)
        except RPCError as exc:
            mapped = map_rpc(exc)
            if mapped:
                raise mapped from exc
            raise


async def resolve_chat(client, raw: str, *, cancel, flood_cap: int):
    parsed = parse_target(raw)
    try:
        if parsed.kind == "invite":
            entity = await _resolve_invite(client, parsed.value, cancel=cancel, flood_cap=flood_cap)
        elif parsed.kind == "id":
            entity = await _call(lambda: client.get_entity(int(parsed.value)), cancel=cancel, flood_cap=flood_cap)
        else:
            entity = await _call(lambda: client.get_entity(parsed.value), cancel=cancel, flood_cap=flood_cap)
    except ValueError as exc:
        raise RejectedError(
            f"cannot resolve {raw}: {exc}. Numeric ids must already be in the session; "
            "run list-chats or pass @username / an invite link."
        ) from exc
    except RPCError as exc:
        mapped = map_rpc(exc)
        if mapped:
            raise mapped from exc
        raise
    return entity


async def _resolve_invite(client, invite_hash: str, *, cancel, flood_cap: int):
    try:
        updates = await _call(
            lambda: client(ImportChatInviteRequest(invite_hash)),
            cancel=cancel,
            flood_cap=flood_cap,
        )
    except UserAlreadyParticipantError:
        checked = await _call(
            lambda: client(CheckChatInviteRequest(invite_hash)),
            cancel=cancel,
            flood_cap=flood_cap,
        )
        if isinstance(checked, ChatInviteAlready):
            return checked.chat
        raise RejectedError(
            "already a member, but the invite did not include the chat. Use the id from list-chats."
        )
    except RPCError as exc:
        mapped = map_rpc(exc)
        if mapped:
            raise mapped from exc
        raise
    chats = list(getattr(updates, "chats", None) or [])
    if not chats:
        raise RejectedError("invite imported but Telegram returned no chat")
    return chats[0]


def media_from_message(message, *, chat_id: int, chat_title: str) -> MediaItem | None:
    document = getattr(message, "document", None)
    photo = getattr(message, "photo", None)
    if document is None and photo is None:
        return None
    kind = "document" if document is not None else "photo"
    unique = str(document.id if document is not None else photo.id)
    file = getattr(message, "file", None)
    raw_name = getattr(file, "name", None) if file is not None else None
    mime = getattr(file, "mime_type", None) if file is not None else None
    size = getattr(file, "size", None) if file is not None else None
    hinted = getattr(file, "ext", None) if file is not None else None
    if not raw_name:
        raw_name = f"{kind}_{message.id}{hinted or ''}"
    name = sanitize_filename(str(raw_name))
    if hinted and Path(name).suffix == "":
        name = sanitize_filename(name + str(hinted))
    extension = Path(name).suffix.lower()
    sender = getattr(message, "sender", None)
    username = getattr(sender, "username", None) if sender is not None else None
    display = None
    if sender is not None:
        title = getattr(sender, "title", None)
        if title:
            display = str(title)
        else:
            display = " ".join(
                part
                for part in (getattr(sender, "first_name", None), getattr(sender, "last_name", None))
                if part
            ) or None
    if not display and getattr(message, "post_author", None):
        display = str(message.post_author)
    when = message.date
    if when.tzinfo is None:
        from datetime import timezone

        when = when.replace(tzinfo=timezone.utc)
    sender_id = getattr(message, "sender_id", None)
    return MediaItem(
        chat_id=chat_id,
        chat_title=chat_title,
        message_id=int(message.id),
        date=when,
        sender_id=int(sender_id) if sender_id is not None else None,
        sender_username=username,
        sender_name=display,
        file_name=name,
        file_size=int(size) if size is not None else None,
        mime_type=mime,
        file_unique_id=unique,
        kind=kind,
        extension=extension,
    )


def _from_user(query: FileQuery):
    import re

    if not query.sender:
        return None
    token = query.sender.lstrip("@")
    if token.isdigit():
        return int(token)
    if re.fullmatch(r"[A-Za-z]\w{3,31}", token):
        return token
    return None


async def collect_chat(
    client,
    entity,
    query: FileQuery,
    *,
    limit: int | None,
    cancel,
    flood_cap: int,
) -> list[MediaItem]:
    chat_id = int(utils.get_peer_id(entity))
    title = entity_title(entity)
    if query.message_ids is not None:
        items = await _collect_ids(
            client,
            entity,
            query,
            chat_id=chat_id,
            chat_title=title,
            cancel=cancel,
            flood_cap=flood_cap,
        )
        return items[:limit] if limit is not None else items
    filters = [InputMessagesFilterDocument()]
    if query.photos:
        filters.append(InputMessagesFilterPhotos())
    items: list[MediaItem] = []
    seen: set[int] = set()
    try:
        for flt in filters:
            await _walk_history(
                client,
                entity,
                query,
                flt,
                chat_id=chat_id,
                chat_title=title,
                items=items,
                seen=seen,
                limit=limit,
                cancel=cancel,
                flood_cap=flood_cap,
            )
            if limit is not None and len(items) >= limit:
                break
    except PartialError as exc:
        exc.extra.setdefault("items", list(items))
        raise
    return items


async def _walk_history(
    client,
    entity,
    query: FileQuery,
    flt,
    *,
    chat_id: int,
    chat_title: str,
    items: list[MediaItem],
    seen: set[int],
    limit: int | None,
    cancel,
    flood_cap: int,
) -> None:
    last_id = 0
    floods = 0
    offset_date = query.until + timedelta(seconds=1) if query.until is not None else None
    from_user = _from_user(query)
    while True:
        kwargs = {"filter": flt}
        if from_user is not None:
            kwargs["from_user"] = from_user
        if offset_date is not None:
            kwargs["offset_date"] = offset_date
        if last_id:
            kwargs["offset_id"] = last_id
        stop = False
        try:
            async for message in client.iter_messages(entity, **kwargs):
                if cancel.is_set():
                    raise PartialError("scan interrupted", items=list(items))
                last_id = int(message.id)
                floods = 0
                when = message.date
                if query.since is not None and when < query.since:
                    stop = True
                    break
                if query.until is not None and when > query.until:
                    continue
                if message.id in seen:
                    continue
                item = media_from_message(message, chat_id=chat_id, chat_title=chat_title)
                if item is None or not query.matches(item):
                    continue
                seen.add(message.id)
                items.append(item)
                if limit is not None and len(items) >= limit:
                    stop = True
                    break
            else:
                stop = True
        except FloodWaitError as exc:
            floods += 1
            if floods > 8:
                raise PartialError("repeated FloodWait while scanning", items=list(items)) from exc
            await _pause_for_flood(exc, cancel, flood_cap)
            continue
        except PartialError:
            raise
        except RPCError as exc:
            mapped = map_rpc(exc)
            if mapped:
                raise mapped from exc
            raise
        if stop:
            return


async def _collect_ids(
    client,
    entity,
    query: FileQuery,
    *,
    chat_id: int,
    chat_title: str,
    cancel,
    flood_cap: int,
) -> list[MediaItem]:
    assert query.message_ids is not None
    ids = [int(value) for value in sorted(query.message_ids, key=int)]
    items: list[MediaItem] = []
    for start in range(0, len(ids), 100):
        if cancel.is_set():
            raise PartialError("scan interrupted", items=items)
        batch = ids[start : start + 100]
        messages = await _call(
            lambda batch=batch: client.get_messages(entity, ids=batch),
            cancel=cancel,
            flood_cap=flood_cap,
        )
        if messages is None:
            continue
        if not isinstance(messages, list):
            messages = [messages]
        for message in messages:
            if message is None:
                continue
            item = media_from_message(message, chat_id=chat_id, chat_title=chat_title)
            if item is not None and query.matches(item):
                items.append(item)
    return items


async def _chunks(client, message, offset: int, expected: int | None):
    agen = client.iter_download(
        message,
        offset=offset,
        request_size=REQUEST_SIZE,
        chunk_size=REQUEST_SIZE,
        file_size=expected if expected else None,
    )
    try:
        async for chunk in agen:
            yield bytes(chunk)
    finally:
        close = getattr(agen, "close", None)
        if close is not None:
            result = close()
            if hasattr(result, "__await__"):
                await result


async def transfer_message(
    client,
    entity,
    message_id: int,
    part: Path,
    *,
    expected: int | None,
    resume: bool,
    cancel,
    flood_cap: int,
    on_progress,
) -> tuple[str, int, str | None]:
    """Download one message into `part`.

    Returns `(status, bytes_written, error)` where status is
    `complete`, `partial`, or `failed`. Does not rename the part.
    """
    if expected == 0:
        part.parent.mkdir(parents=True, exist_ok=True)
        part.write_bytes(b"")
        return "complete", 0, None

    if resume:
        offset = prepare_part_file(part, expected)
        if expected is not None and offset == expected:
            return "complete", offset, None
    else:
        from tgdl.resume import reset_part_file

        offset = reset_part_file(part)

    transient = 0
    try:
        message = await _load_message(client, entity, message_id, cancel=cancel, flood_cap=flood_cap)
    except PartialError as exc:
        return "partial", _size(part), str(exc)
    except TgdlError as exc:
        return "failed", _size(part), str(exc)
    while True:
        if cancel.is_set():
            return "partial", _size(part), "cancelled"
        try:
            result = await consume_chunks(
                part,
                offset=offset,
                expected=expected,
                chunk_iter=_chunks(client, message, offset, expected),
                cancel=cancel,
                on_progress=on_progress,
            )
        except FloodWaitError as exc:
            offset = prepare_part_file(part, expected)
            try:
                await _pause_for_flood(exc, cancel, flood_cap)
            except PartialError as paused:
                return "partial", _size(part), str(paused)
            continue
        except FileReferenceExpiredError:
            transient += 1
            if transient > 5:
                return "failed", _size(part), "file reference expired"
            try:
                message = await _load_message(
                    client, entity, message_id, cancel=cancel, flood_cap=flood_cap
                )
            except PartialError as exc:
                return "partial", _size(part), str(exc)
            except TgdlError as exc:
                return "failed", _size(part), str(exc)
            offset = prepare_part_file(part, expected)
            continue
        except (OSError, ConnectionError, TimeoutError, TimedOutError) as exc:
            transient += 1
            if transient > 5:
                return "failed", _size(part), str(exc) or type(exc).__name__
            delay = min(2**transient, 30)
            if not await sleep_or_cancel(delay, cancel):
                return "partial", _size(part), "cancelled"
            offset = prepare_part_file(part, expected)
            continue
        except RPCError as exc:
            mapped = map_rpc(exc)
            if mapped and mapped.exit_code == 5:
                return "failed", _size(part), str(mapped)
            transient += 1
            if transient > 5:
                return "failed", _size(part), str(exc) or type(exc).__name__
            if not await sleep_or_cancel(min(2**transient, 30), cancel):
                return "partial", _size(part), "cancelled"
            offset = prepare_part_file(part, expected)
            continue

        offset = result.bytes_written
        if result.status == "partial":
            return "partial", offset, "cancelled"
        if result.status == "complete":
            return "complete", offset, None
        return "failed", offset, f"short read: got {offset} expected {expected}"


async def _load_message(client, entity, message_id: int, *, cancel, flood_cap: int):
    message = await _call(
        lambda: client.get_messages(entity, ids=message_id),
        cancel=cancel,
        flood_cap=flood_cap,
    )
    if message is None:
        raise RejectedError(f"message {message_id} is gone")
    return message


def _size(path: Path) -> int:
    if not path.exists():
        return 0
    return path.stat().st_size
