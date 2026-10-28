"""Load configuration. Precedence: CLI > environment > config file > defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from tgdl.errors import UsageError
from tgdl.util import parse_duration


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(path, override=False)


def _yaml(path: Path) -> dict:
    try:
        import yaml
    except ImportError as exc:
        raise UsageError("PyYAML is required to read config files") from exc
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise UsageError("config root must be a mapping")
    return data


def _env(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _first(*values: object) -> object:
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and value.strip() == "":
            continue
        return value
    return None


def _as_int(value: object, *, name: str) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise UsageError(f"{name} must be an integer") from exc


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


@dataclass(frozen=True)
class Config:
    api_id: int | None
    api_hash: str | None
    phone: str | None
    session: Path
    output: Path
    workers: int
    data_dir: Path
    group_by_month: bool
    include_photos: bool
    flood_cap_sec: int
    api_host: str
    api_port: int
    api_token: str | None
    config_path: Path | None

    @property
    def has_credentials(self) -> bool:
        return bool(self.api_id and self.api_hash)


def load_config(
    *,
    config_path: str | Path | None = None,
    data_dir: str | Path | None = None,
    session: str | Path | None = None,
    output: str | Path | None = None,
    cwd: Path | None = None,
) -> Config:
    root = (cwd or Path.cwd()).resolve()
    env_file = Path(_env("TGDL_ENV") or (root / ".env"))
    if not env_file.is_absolute():
        env_file = root / env_file
    _load_dotenv(env_file)

    explicit = config_path or _env("TGDL_CONFIG")
    if explicit:
        file_path = Path(str(explicit))
        if not file_path.is_absolute():
            file_path = root / file_path
        if not file_path.exists():
            raise UsageError(f"config not found: {file_path}")
    else:
        file_path = root / "config.yaml"
        if not file_path.exists():
            file_path = None
    file_cfg: dict = _yaml(file_path) if file_path else {}
    api_cfg = file_cfg.get("api") if isinstance(file_cfg.get("api"), dict) else {}

    api_id = _as_int(_first(_env("TGDL_API_ID"), file_cfg.get("api_id")), name="api_id")
    if api_id == 0:
        api_id = None
    api_hash = _first(_env("TGDL_API_HASH"), file_cfg.get("api_hash"))
    if isinstance(api_hash, str) and api_hash.strip() in {"", "replace-me"}:
        api_hash = None
    else:
        api_hash = str(api_hash) if api_hash is not None else None

    phone = _first(_env("TGDL_PHONE"), file_cfg.get("phone"))
    phone = str(phone) if phone else None

    data_value = _first(data_dir, _env("TGDL_DATA_DIR"), file_cfg.get("data_dir"), ".tgdl")
    data_path = Path(str(data_value))
    if not data_path.is_absolute():
        data_path = root / data_path
    data_path = data_path.resolve()

    session_value = _first(session, _env("TGDL_SESSION"), file_cfg.get("session"))
    if session_value:
        session_path = Path(str(session_value))
        if not session_path.is_absolute():
            session_path = root / session_path
    else:
        session_path = data_path / "tgdl.session"
    session_path = session_path.resolve()

    output_value = _first(output, _env("TGDL_OUTPUT"), file_cfg.get("output"), "downloads")
    output_path = Path(str(output_value))
    if not output_path.is_absolute():
        output_path = root / output_path
    output_path = output_path.resolve()

    workers = _as_int(_first(_env("TGDL_WORKERS"), file_cfg.get("workers"), 4), name="workers") or 4
    if workers < 1:
        raise UsageError("workers must be >= 1")

    flood_raw = _first(_env("TGDL_FLOOD_CAP"), file_cfg.get("flood_cap"), "15m")
    flood_cap = parse_duration(flood_raw if isinstance(flood_raw, (str, int, float)) else str(flood_raw))

    host = str(_first(_env("TGDL_API_HOST"), file_cfg.get("api_host"), api_cfg.get("host"), "127.0.0.1"))
    port = _as_int(
        _first(_env("TGDL_API_PORT"), file_cfg.get("api_port"), api_cfg.get("port"), 18765),
        name="api_port",
    )
    if port is None or not (1 <= port <= 65535):
        raise UsageError("api_port must be between 1 and 65535")
    token = _first(_env("TGDL_API_TOKEN"), file_cfg.get("api_token"), api_cfg.get("token"))
    token = str(token) if token else None

    return Config(
        api_id=api_id,
        api_hash=api_hash,
        phone=phone,
        session=session_path,
        output=output_path,
        workers=workers,
        data_dir=data_path,
        group_by_month=_as_bool(
            _first(_env("TGDL_GROUP_BY_MONTH"), file_cfg.get("group_by_month")),
            True,
        ),
        include_photos=_as_bool(
            _first(_env("TGDL_INCLUDE_PHOTOS"), file_cfg.get("include_photos")),
            False,
        ),
        flood_cap_sec=flood_cap,
        api_host=host,
        api_port=int(port),
        api_token=token,
        config_path=file_path,
    )
