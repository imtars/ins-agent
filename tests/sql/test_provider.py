"""Provider selection never needs the real local proxy or a credential in CI."""

import httpx
import pytest

from packages.sql.deepseek import select_sql_generator


def test_proxy_selection_uses_luna_and_does_not_read_fallback(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text("port: 8317\napi-keys:\n  - test-key\n", encoding="utf-8")
    fallback = tmp_path / "fallback"

    def get(url, **kwargs):
        assert url == "http://127.0.0.1:8317/v1/models"
        assert kwargs["trust_env"] is False
        assert kwargs["headers"]["Authorization"] == "Bearer test-key"
        return httpx.Response(200, json={"data": [{"id": "gpt-6-luna"}]},
                              request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", get)
    generator = select_sql_generator(proxy_config=config, fallback_key_file=fallback)
    assert (generator.provider, generator.model, generator.base_url) == (
        "CLIProxyAPI", "gpt-6-luna", "http://127.0.0.1:8317/v1")
    assert not fallback.exists()


def test_proxy_required_rejects_unavailable_proxy(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text("port: 8317\napi-keys:\n  - test-key\n", encoding="utf-8")

    def get(url, **kwargs):
        raise httpx.ConnectError("unavailable")

    monkeypatch.setattr(httpx, "get", get)
    with pytest.raises(RuntimeError, match="local CLIProxyAPI unavailable"):
        select_sql_generator("proxy", proxy_config=config)


def test_fallback_uses_first_line_only(tmp_path, monkeypatch):
    from packages.domain.config import Settings

    monkeypatch.setattr("packages.sql.deepseek.get_settings", lambda: Settings(
        _env_file=None, deepseek_api_key=None))
    fallback = tmp_path / "key"
    fallback.write_text("first-key\nsecond-key\n", encoding="utf-8")
    generator = select_sql_generator("deepseek", fallback_key_file=fallback)
    assert generator.provider == "DeepSeek"
    assert generator.model == "deepseek-flash"
    assert generator.api_key == "first-key"
