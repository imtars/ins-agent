"""Provider selection never needs the real local proxy or a credential in CI."""

import asyncio
import json

import httpx
import pytest

from packages.sql.deepseek import OpenAICompatibleSQLGenerator, select_sql_generator


def test_generator_records_observed_response_model_without_credentials(monkeypatch):
    def respond(request):
        assert request.headers["Authorization"] == "Bearer test-key"
        assert json.loads(request.read())["max_tokens"] == 8192
        return httpx.Response(200, json={"model": "proxy-reported-model",
                                          "choices": [{"finish_reason": "stop", "message": {
                                              "content": '{"decision":"refuse","sql":null}'}}]})

    transport = httpx.MockTransport(respond)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_client(
        transport=transport, **kwargs))
    generator = OpenAICompatibleSQLGenerator(provider="test", model="requested-model",
                                              base_url="http://127.0.0.1:8317/v1",
                                              api_key="test-key", max_tokens=8192)
    result = asyncio.run(generator.generate("question", "schema", None, 0))
    assert result["decision"] == "refuse"
    assert generator.response_models == ["proxy-reported-model"]


def test_generator_rejects_invalid_output_cap():
    with pytest.raises(ValueError, match="max_tokens"):
        OpenAICompatibleSQLGenerator(provider="test", model="test",
                                     base_url="http://localhost", api_key="key",
                                     max_tokens=0)


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
