"""M9 bounded dependency retries and opt-in deterministic fault points."""

import asyncio
import os
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Literal, TypeVar

from fastmcp.exceptions import ToolError
import httpx
from psycopg import OperationalError


FaultCase = Literal["llm_timeout", "llm_invalid_json", "postgres_timeout",
                    "milvus_timeout", "reranker_failure", "mcp_failure",
                    "runner_crash", "verifier_failure"]
CASES = frozenset(FaultCase.__args__)
T = TypeVar("T")


class InjectedFault(RuntimeError):
    """A deliberate test fault; never enabled by an incoming API request."""


@dataclass
class FaultInjector:
    case: FaultCase | None = None
    stage: str | None = None
    occurrences: int = 1
    enabled: bool = False
    fired: int = 0

    @classmethod
    def from_env(cls) -> "FaultInjector":
        enabled = os.getenv("FAULT_INJECTION_ENABLED", "0") == "1"
        case = os.getenv("FAULT_CASE") if enabled else None
        if enabled and case not in CASES:
            raise ValueError("enabled fault injection requires a known FAULT_CASE")
        occurrences = int(os.getenv("FAULT_OCCURRENCES", "1"))
        if occurrences < 1:
            raise ValueError("FAULT_OCCURRENCES must be positive")
        return cls(case=case, stage=os.getenv("FAULT_STAGE") or None,
                   occurrences=occurrences, enabled=enabled)

    def fire(self, case: str, stage: str) -> None:
        if (not self.enabled or self.case != case or self.fired >= self.occurrences
                or self.stage not in (None, stage)):
            return
        self.fired += 1
        if case in {"llm_timeout", "milvus_timeout", "postgres_timeout"}:
            raise TimeoutError(f"injected {case} at {stage}")
        if case == "mcp_failure":
            raise ToolError("injected temporary MCP unavailable")
        if case == "llm_invalid_json":
            raise ValueError("injected invalid JSON completion")
        if case == "verifier_failure":
            raise InjectedFault("injected verifier unavailable")
        if case == "reranker_failure":
            raise InjectedFault("injected reranker unavailable")
        if case == "runner_crash":
            raise InjectedFault("runner_crash uses the worker process failpoint")


def error_code(exc: Exception) -> str:
    if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
        return "dependency_timeout"
    if isinstance(exc, httpx.HTTPStatusError):
        return f"http_{exc.response.status_code}"
    if isinstance(exc, OperationalError):
        return "postgres_connection_error" if getattr(exc, "sqlstate", None) != "57014" \
            else "postgres_statement_timeout"
    if isinstance(exc, (httpx.NetworkError, ConnectionError, OSError)):
        return "connection_error"
    if isinstance(exc, ToolError):
        return "mcp_error"
    if isinstance(exc, ValueError):
        return "invalid_output"
    if isinstance(exc, InjectedFault):
        return "dependency_unavailable"
    return type(exc).__name__


def is_transient(exc: Exception) -> bool:
    if isinstance(exc, OperationalError):
        state = getattr(exc, "sqlstate", None)
        return state is None or state.startswith(("08", "53")) or state == "57P03"
    if isinstance(exc, (TimeoutError, httpx.TimeoutException,
                        httpx.NetworkError, ConnectionError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in {408, 429, 500, 502, 503, 504}
    if isinstance(exc, ToolError):
        message = str(exc).lower()
        return any(marker in message for marker in
                   ("temporar", "unavailable", "timeout", "timed out", "429", "503"))
    return False


@dataclass
class RetryEvent:
    boundary: str
    error_code: str
    attempt: int


@dataclass
class RetryPolicy:
    max_attempts: int = 3
    base_delay_seconds: float = 0.2
    events: list[RetryEvent] = field(default_factory=list)

    async def run(self, boundary: str, operation: Callable[[], Awaitable[T]]) -> T:
        if self.max_attempts < 1:
            raise ValueError("retry attempts must be positive")
        for attempt in range(1, self.max_attempts + 1):
            try:
                return await operation()
            except Exception as exc:
                if not is_transient(exc) or attempt == self.max_attempts:
                    raise
                self.events.append(RetryEvent(boundary, error_code(exc), attempt))
                await asyncio.sleep(self.base_delay_seconds * (2 ** (attempt - 1)))
        raise AssertionError("unreachable")


class FaultTolerantModel:
    def __init__(self, inner, policy: RetryPolicy, injector: FaultInjector):
        self.inner, self.policy, self.injector = inner, policy, injector

    @property
    def response_models(self):
        return getattr(self.inner, "response_models", [])

    async def complete_json(self, role: str, system: str, user: str,
                            *, max_tokens: int = 1200) -> dict:
        async def attempt():
            if role == "verifier":
                self.injector.fire("verifier_failure", role)
            self.injector.fire("llm_timeout", role)
            self.injector.fire("llm_invalid_json", role)
            return await self.inner.complete_json(role, system, user,
                                                  max_tokens=max_tokens)
        return await self.policy.run(f"llm:{role}", attempt)


class FaultTolerantTools:
    def __init__(self, inner, policy: RetryPolicy, injector: FaultInjector):
        self.inner, self.policy, self.injector = inner, policy, injector

    async def list_tools(self):
        return await self.policy.run("mcp:list_tools", self.inner.list_tools)

    async def call_tool(self, name: str, arguments: dict):
        async def attempt():
            self.injector.fire("mcp_failure", name)
            if name.startswith("data_"):
                self.injector.fire("postgres_timeout", name)
            if name.startswith("knowledge_"):
                self.injector.fire("milvus_timeout", name)
            return await self.inner.call_tool(name, arguments)
        return await self.policy.run(f"mcp:{name}", attempt)
