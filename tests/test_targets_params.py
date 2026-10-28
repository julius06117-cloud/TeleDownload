from pathlib import Path

import pytest

from tgdl.config import Config, load_config
from tgdl.errors import UsageError
from tgdl.params import normalize_params
from tgdl.targets import parse_target
from tgdl.util import flood_sleep_seconds, parse_duration, parse_size


def _cfg(tmp_path: Path) -> Config:
    return Config(
        api_id=None,
        api_hash=None,
        phone=None,
        session=tmp_path / "s.session",
        output=tmp_path / "downloads",
        workers=4,
        data_dir=tmp_path / ".tgdl",
        group_by_month=True,
        include_photos=False,
        flood_cap_sec=900,
        api_host="127.0.0.1",
        api_port=18765,
        api_token=None,
        config_path=None,
    )


def test_parse_target_forms():
    assert parse_target("@News").value == "News"
    assert parse_target("https://t.me/News/10").kind == "username"
    assert parse_target("https://t.me/News/10").value == "News"
    assert parse_target("https://t.me/+AbCd_1").kind == "invite"
    assert parse_target("https://t.me/joinchat/AbCd_1").value == "AbCd_1"
    assert parse_target("tg://join?invite=ZZ").kind == "invite"
    assert parse_target("https://t.me/c/123456/7").value == "-100123456"
    assert parse_target("-100123").kind == "id"
    assert parse_target("tg://resolve?domain=News").value == "News"


def test_size_and_duration_and_flood_pad():
    assert parse_size("1.5KB") == 1536
    assert parse_size(10) == 10
    assert parse_duration("1h30m") == 5400
    assert parse_duration("90s") == 90
    assert flood_sleep_seconds(5) == 6
    with pytest.raises(UsageError):
        parse_duration(0)
    with pytest.raises(ValueError):
        flood_sleep_seconds(-1)


def test_normalize_is_idempotent(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = _cfg(tmp_path)
    raw = {
        "chats": ["@News", " @News ", "https://t.me/Other"],
        "ext": ["PDF", "zip"],
        "minSize": "1KB",
        "maxSize": "2MB",
        "since": "2024-01-01",
        "until": "2024-01-31",
        "nameRegex": r"report",
        "messageIds": ["10", "2", "10"],
        "workers": "4",
        "output": "downloads",
    }
    once = normalize_params(raw, kind="download", cfg=cfg)
    twice = normalize_params(once, kind="download", cfg=cfg)
    assert once == twice
    assert once["chats"] == ["@News", "https://t.me/Other"]
    assert once["ext"] == [".pdf", ".zip"]
    assert once["messageIds"] == ["2", "10"]
    assert once["minSize"] == 1024
    assert once["workers"] == 4
    assert once["output"].endswith("/downloads")
    assert once["resume"] is True
    assert once["groupByMonth"] is True


def test_normalize_rejects_bad_input(tmp_path: Path):
    cfg = _cfg(tmp_path)
    with pytest.raises(UsageError):
        normalize_params({}, kind="download", cfg=cfg)
    with pytest.raises(UsageError):
        normalize_params({"chats": ["a"], "workers": 0}, kind="scan", cfg=cfg)
    with pytest.raises(UsageError):
        normalize_params({"chats": ["a"], "nameRegex": "["}, kind="scan", cfg=cfg)
    with pytest.raises(UsageError):
        normalize_params({"chats": ["a"], "minSize": "5MB", "maxSize": "1KB"}, kind="scan", cfg=cfg)


def test_config_precedence(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text(
        "api_id: 42\napi_hash: from-file\nworkers: 2\noutput: from-file\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TGDL_API_HASH", "from-env")
    monkeypatch.setenv("TGDL_WORKERS", "6")
    cfg = load_config(output=tmp_path / "cli-out")
    assert cfg.api_id == 42
    assert cfg.api_hash == "from-env"
    assert cfg.workers == 6
    assert cfg.output == (tmp_path / "cli-out").resolve()
    assert cfg.api_host == "127.0.0.1"
    assert cfg.api_port == 18765
