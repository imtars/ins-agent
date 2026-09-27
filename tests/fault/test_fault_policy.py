"""Classify transient failures and verify bounded retries at real boundaries."""

import asyncio
import json

from fastmcp.exceptions import ToolError
import httpx
import pytest
from psycopg import errors

from packages.agent.faults import (FaultInjector, FaultTolerantModel,
    FaultTolerantTools, RetryPolicy, is_transient)
from packages.llm.client import JsonChatClient
from tests.agent.test_m5_graph import StubModel, StubTools


def http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://example.invalid/chat/completions")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError(str(status), request=request, response=response)


@pytest.mark.parametrize("error,transient", [
    (TimeoutError("timeout"), True), (http_error(429), True),
    (http_error(503), True), (http_error(400), False),
    (ToolError("temporary MCP unavailable"), True),
    (ToolError("invalid sql argument"), False),
    (ValueError("invalid JSON"), False),
    (errors.QueryCanceled("statement timeout"), False),
])
def test_only_transient_errors_are_retried(error, transient):
    assert is_transient(error) is transient


def test_timeout_once_retries_then_succeeds_and_records_attempt():
    async def scenario():
        inner = StubModel()
        policy = RetryPolicy(base_delay_seconds=0)
        injector = FaultInjector(case="llm_timeout", stage="planner", enabled=True)
        model = FaultTolerantModel(inner, policy, injector)
        raw = await model.complete_json("planner", "system", "route_sql")
        assert raw["route"] == "SQL"
        assert injector.fired == 1
        assert [item.boundary for item in policy.events] == ["llm:planner"]
        assert inner.calls == ["planner"]
    asyncio.run(scenario())


@pytest.mark.parametrize("case,tool", [
    ("postgres_timeout", "data_describe_schema"),
    ("milvus_timeout", "knowledge_search_knowledge"),
    ("mcp_failure", "data_describe_schema"),
])
def test_tool_retry_replays_only_the_failed_read(case, tool):
    async def scenario():
        inner = StubTools()
        policy = RetryPolicy(base_delay_seconds=0)
        wrapped = FaultTolerantTools(inner, policy,
            FaultInjector(case=case, stage=tool, enabled=True))
        arguments = ({"query": "waiting period", "product_code": "product_006"}
                     if tool.startswith("knowledge_") else {})
        result = await wrapped.call_tool(tool, arguments)
        assert result.structured_content
        assert inner.calls == [tool]  # Injected failure was before the actual MCP call.
        assert len(policy.events) == 1
        assert policy.events[0].boundary == f"mcp:{tool}"
    asyncio.run(scenario())


def test_permanent_failure_exhausts_bounded_retries():
    async def scenario():
        policy = RetryPolicy(max_attempts=3, base_delay_seconds=0)
        calls = 0

        async def unavailable():
            nonlocal calls
            calls += 1
            raise http_error(429)

        with pytest.raises(httpx.HTTPStatusError):
            await policy.run("llm:planner", unavailable)
        assert calls == 3 and len(policy.events) == 2
    asyncio.run(scenario())


def test_invalid_json_is_not_retried_as_transport_failure():
    async def scenario():
        inner = StubModel()
        policy = RetryPolicy(base_delay_seconds=0)
        model = FaultTolerantModel(inner, policy,
            FaultInjector(case="llm_invalid_json", stage="planner", enabled=True))
        with pytest.raises(ValueError, match="invalid JSON"):
            await model.complete_json("planner", "system", "route_sql")
        assert not policy.events and not inner.calls
    asyncio.run(scenario())


def test_fault_env_requires_explicit_enable(monkeypatch):
    monkeypatch.setenv("FAULT_CASE", "verifier_failure")
    monkeypatch.delenv("FAULT_INJECTION_ENABLED", raising=False)
    assert FaultInjector.from_env().enabled is False
    monkeypatch.setenv("FAULT_INJECTION_ENABLED", "1")
    assert FaultInjector.from_env().case == "verifier_failure"
    monkeypatch.setenv("FAULT_CASE", "unknown")
    with pytest.raises(ValueError, match="known FAULT_CASE"):
        FaultInjector.from_env()


def test_http_429_from_json_client_is_retried_without_forging_completion(monkeypatch):
    requests = []

    def respond(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(429, request=request)
        return httpx.Response(200, request=request, json={
            "model": "test-model", "choices": [{"finish_reason": "stop",
                "message": {"content": json.dumps({"route": "SQL"})}}]})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_client(
        transport=httpx.MockTransport(respond), **kwargs))
    inner = JsonChatClient(provider="test", model="test-model",
        base_url="https://example.invalid", api_key="test-key")
    policy = RetryPolicy(base_delay_seconds=0)
    wrapped = FaultTolerantModel(inner, policy, FaultInjector())
    result = asyncio.run(wrapped.complete_json("planner", "system", "user"))
    assert result == {"route": "SQL"}
    assert len(requests) == 2 and len(policy.events) == 1
    assert policy.events[0].error_code == "http_429"
    assert inner.response_models == ["test-model"]
