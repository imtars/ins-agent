"""Malformed JSON is retried once and every actual completion is tracked."""

import asyncio

import httpx
import pytest

from packages.llm.client import JsonChatClient, select_chat_client


def test_m5_provider_prefers_local_luna_without_reading_fallback(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text("port: 8317\napi-keys:\n  - test-key\n", encoding="utf-8")
    fallback = tmp_path / "fallback"

    def get(url, **kwargs):
        assert kwargs["trust_env"] is False
        return httpx.Response(200, json={"data": [{"id": "gpt-6-luna"}]},
                              request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", get)
    model = select_chat_client(proxy_config=config, fallback_key_file=fallback)
    assert (model.provider, model.model) == ("CLIProxyAPI", "gpt-6-luna")
    assert not fallback.exists()


def test_json_client_retries_once_without_accepting_extra_objects(monkeypatch):
    calls = []

    def respond(request):
        calls.append(request)
        content = '{"ok":true}{"extra":true}' if len(calls) == 1 else '{"ok":true}'
        return httpx.Response(200, json={"model": "gpt-6-luna",
                                          "choices": [{"finish_reason": "stop",
                                                       "message": {"content": content}}]})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_client(
        transport=httpx.MockTransport(respond), **kwargs))
    model = JsonChatClient(provider="test", model="gpt-6-luna",
                           base_url="http://127.0.0.1:8317/v1", api_key="test-key")
    result = asyncio.run(model.complete_json("synthesis", "system", "user"))
    assert result == {"ok": True}
    assert len(calls) == 2
    assert model.response_models == ["gpt-6-luna", "gpt-6-luna"]
    assert model.roles_called == ["synthesis", "synthesis"]
    assert model.parse_failures == ["synthesis:JSONDecodeError"]


def test_json_client_fails_after_two_malformed_responses(monkeypatch):
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_client(
        transport=httpx.MockTransport(lambda request: httpx.Response(
            200, json={"model": "gpt-6-luna", "choices": [{"finish_reason": "stop",
                "message": {"content": "{}{}"}}]})), **kwargs))
    model = JsonChatClient(provider="test", model="gpt-6-luna",
                           base_url="http://127.0.0.1:8317/v1", api_key="test-key")
    with pytest.raises(ValueError, match="single JSON object"):
        asyncio.run(model.complete_json("planner", "system", "user"))
    assert len(model.response_models) == 2
