import asyncio
import json
from pathlib import Path

from tgdl.api import bootstrap_allowed
from tgdl.cli import build_parser
from tests.test_cli_api import FakeService


class _Headers(dict):
    def get(self, key, default=None):
        return super().get(key, default)


class _Request:
    def __init__(self, headers: dict, remote: str = "127.0.0.1") -> None:
        self.headers = _Headers(headers)
        self.remote = remote


def test_bootstrap_policy_rejects_cross_site_and_foreign_origin():
    assert bootstrap_allowed(_Request({"X-Tgdl-Ui": "1"}), 18765)
    assert bootstrap_allowed(
        _Request({"X-Tgdl-Ui": "1", "Origin": "http://127.0.0.1:18765", "Sec-Fetch-Site": "same-origin"}),
        18765,
    )
    assert bootstrap_allowed(
        _Request({"X-Tgdl-Ui": "1", "Origin": "http://localhost:9"}),
        9,
    )
    assert not bootstrap_allowed(_Request({}), 9)
    assert not bootstrap_allowed(_Request({"X-Tgdl-Ui": "1", "Sec-Fetch-Site": "cross-site"}), 9)
    assert not bootstrap_allowed(_Request({"X-Tgdl-Ui": "1", "Origin": "https://evil.example"}), 9)
    assert not bootstrap_allowed(_Request({"X-Tgdl-Ui": "1", "Origin": "http://127.0.0.1:10"}), 9)
    assert not bootstrap_allowed(_Request({"X-Tgdl-Ui": "1"}, remote="10.1.2.3"), 9)
    assert not bootstrap_allowed(_Request({"X-Tgdl-Ui": "1"}, remote=""), 9)


def test_serve_and_ui_parser_flags():
    parser = build_parser()
    serve = parser.parse_args(["serve", "--open", "--port", "9"])
    assert serve.command == "serve"
    assert serve.open is True
    assert serve.port == 9
    ui = parser.parse_args(["ui", "--host", "127.0.0.1"])
    assert ui.command == "ui"
    assert ui.open is False


def test_account_without_credentials(tmp_path: Path, monkeypatch):
    for name in ("TGDL_API_ID", "TGDL_API_HASH", "TGDL_CONFIG", "TGDL_ENV"):
        monkeypatch.delenv(name, raising=False)
    from tgdl.config import load_config
    from tgdl.service import Service

    cfg = load_config(data_dir=tmp_path / ".tgdl", cwd=tmp_path)
    service = Service(cfg)

    info = asyncio.run(service.account())
    assert info["loggedIn"] is False
    assert "tgdl login" in info["reason"]
    assert info["userId"] is None


def test_ui_routes_and_bootstrap(tmp_path: Path, monkeypatch, capsys):
    import urllib.error
    import urllib.request

    from tgdl.api import start_api
    from tgdl.api_client import clear_runtime, write_runtime
    from tgdl.cli import _serve
    from tgdl.config import load_config

    token = "loopback-ui-token"
    opened: list[str] = []
    monkeypatch.setattr("webbrowser.open", lambda url, *args, **kwargs: opened.append(url) or True)

    async def _run():
        cfg = load_config(data_dir=tmp_path / ".tgdl", cwd=tmp_path)
        service = FakeService()
        runner, port = await start_api(service, token=token, host="127.0.0.1", port=0, config=cfg)
        try:
            def call(path: str, *, method: str = "GET", auth: str | None = None, headers: dict | None = None, host: str | None = None):
                hdrs = {"Host": host or f"127.0.0.1:{port}"}
                if auth is not None:
                    hdrs["Authorization"] = f"Bearer {auth}"
                if headers:
                    hdrs.update(headers)
                req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=hdrs, method=method)
                try:
                    with urllib.request.urlopen(req, timeout=5) as response:
                        raw = response.read()
                        return response.status, raw, response.headers
                except urllib.error.HTTPError as exc:
                    return exc.code, exc.read(), exc.headers

            status, raw, headers = await asyncio.to_thread(call, "/api/v1/ping")
            assert status == 401
            assert token not in raw.decode()

            status, raw, headers = await asyncio.to_thread(call, "/", host="evil.example")
            assert status == 403
            assert token not in raw.decode()
            assert b"\xe7\xbe\xa4\xe7\xbb\x84" not in raw  # 群组

            status, raw, headers = await asyncio.to_thread(call, "/")
            assert status == 200
            page = raw.decode()
            assert "text/html" in headers.get("Content-Type", "")
            assert "群组" in page
            assert 'src="/ui/app.js"' in page
            assert token not in page
            assert "no-cache" in headers.get("Cache-Control", "")
            assert headers.get("Cross-Origin-Resource-Policy") == "same-origin"
            assert headers.get("Access-Control-Allow-Origin") is None
            assert "script-src 'self'" in headers.get("Content-Security-Policy", "")

            status, raw, headers = await asyncio.to_thread(call, "/ui/app.js")
            assert status == 200
            script = raw.decode()
            assert "text/javascript" in headers.get("Content-Type", "")
            assert "/api/v1/ui/bootstrap" in script
            assert "X-Tgdl-Ui" in script
            assert "sessionStorage" in script
            assert token not in script

            status, raw, _headers = await asyncio.to_thread(call, "/ui/app.css")
            assert status == 200
            assert b"--accent" in raw

            status, raw, headers = await asyncio.to_thread(call, "/api/v1/ui/bootstrap")
            assert status == 403
            body = json.loads(raw.decode())
            assert body["exitCode"] == 5
            assert token not in raw.decode()

            status, raw, headers = await asyncio.to_thread(
                call,
                "/api/v1/ui/bootstrap",
                headers={"X-Tgdl-Ui": "1", "Sec-Fetch-Site": "cross-site", "Origin": "https://evil.example"},
            )
            assert status == 403
            assert token not in raw.decode()

            status, raw, headers = await asyncio.to_thread(
                call,
                "/api/v1/ui/bootstrap",
                headers={"X-Tgdl-Ui": "1", "Origin": "http://127.0.0.1:1"},
            )
            assert status == 403
            assert token not in raw.decode()

            status, raw, headers = await asyncio.to_thread(
                call,
                "/api/v1/ui/bootstrap",
                headers={"X-Tgdl-Ui": "1", "Sec-Fetch-Site": "same-origin", "Origin": f"http://127.0.0.1:{port}"},
            )
            assert status == 200
            payload = json.loads(raw.decode())
            assert payload["ok"] is True
            assert payload["token"] == token
            assert payload["tokenScheme"] == "Bearer"
            assert "no-store" in headers.get("Cache-Control", "")
            assert headers.get("Access-Control-Allow-Origin") is None

            status, raw, _headers = await asyncio.to_thread(call, "/", method="POST")
            assert status == 401

            status, raw, _headers = await asyncio.to_thread(call, "/api/v1/account", auth=token)
            assert status == 200
            account = json.loads(raw.decode())
            assert account["ok"] is True
            assert account["loggedIn"] is False
            assert "tgdl login" in account["reason"]
            assert service.calls[-1] == ("account",)

            status, raw, _headers = await asyncio.to_thread(call, "/api/v1/account")
            assert status == 401

            write_runtime(cfg.data_dir, port, token)

            def _reuse():
                return asyncio.run(_serve(cfg, None, None, True, open_browser=True, reuse=True))

            code = await asyncio.to_thread(_reuse)
            assert code == 0
            listened = json.loads(capsys.readouterr().out)
            assert listened["existing"] is True
            assert listened["ui"] == f"http://127.0.0.1:{port}/"
            assert listened["url"] == f"http://127.0.0.1:{port}"
            assert "token" not in listened
            assert opened == [listened["ui"]]
        finally:
            clear_runtime(cfg.data_dir)
            await runner.cleanup()

    asyncio.run(_run())
