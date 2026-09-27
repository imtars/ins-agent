"""M5 routing/fan-in tests use controlled role output, never benchmark scores."""

import asyncio
import json
from types import SimpleNamespace

from fastmcp.exceptions import ToolError
import pytest

from packages.agent.agents.roles import DataAnalystAgent
from packages.agent.graph import build_workflow, run_query
from packages.agent.models import TaskPlan


class StubModel:
    def __init__(self, *, bad_citation: bool = False, verifier_fails: bool = False):
        self.calls = []
        self.bad_citation = bad_citation
        self.verifier_fails = verifier_fails

    async def complete_json(self, role, system, user, *, max_tokens=1200):
        self.calls.append(role)
        if role == "planner":
            route = user.removeprefix("route_").upper()
            return {"intent": "demo", "route": route,
                    "sql_tasks": ["count policies"] if route != "RAG" else [],
                    "rag_tasks": ["product_006 waiting period"] if route != "SQL" else [],
                    "required_outputs": ["answer"]}
        if role == "data_analyst":
            return {"tool": "data_execute_readonly_query",
                    "arguments": {"sql": "SELECT count(*) AS n FROM policies"}}
        if role == "knowledge_researcher":
            return {"query": "product_006 waiting period", "product_code": "product_006"}
        if role == "synthesis":
            data = json.loads(user)
            claims = []
            if data["sql_results"]:
                claims.append({"text": "There are 30000 synthetic policies.",
                               "source_ids": ["sql:1"]})
            if data["rag_results"]:
                claims.append({"text": "The waiting period is 15 days.",
                               "source_ids": ["invalid" if self.bad_citation else
                                              "product_006:等待期:chunkid"]})
            return {"summary": "Synthetic demo answer.", "claims": claims}
        if role == "verifier":
            if self.verifier_fails:
                raise RuntimeError("test verifier unavailable")
            return {"status": "PASS", "issues": []}
        raise AssertionError(f"unexpected role: {role}")


class StubTools:
    def __init__(self):
        self.calls = []

    async def call_tool(self, name, arguments):
        self.calls.append(name)
        if name == "data_describe_schema":
            content = {"schema": "policies(id integer)"}
        elif name == "data_execute_readonly_query":
            content = {"rows": [{"n": 30000}], "row_count": 1}
        elif name == "knowledge_search_knowledge":
            content = {"evidence": [{"evidence_id": "product_006:等待期:chunkid",
                                     "doc_id": "product_006", "text": "等待期 15 天"}],
                       "count": 1}
        else:
            raise AssertionError(f"unexpected MCP tool: {name}")
        return SimpleNamespace(structured_content=content)


class RepairModel(StubModel):
    def __init__(self, always_invalid=False):
        super().__init__()
        self.always_invalid = always_invalid
        self.proposals = 0

    async def complete_json(self, role, system, user, *, max_tokens=1200):
        if role == "data_analyst":
            self.calls.append(role)
            self.proposals += 1
            if self.always_invalid or self.proposals == 1:
                return {"tool": "data_execute_readonly_query",
                        "arguments": {"query": "SELECT count(*) FROM policies"}}
        return await super().complete_json(role, system, user, max_tokens=max_tokens)


class ValidatingTools(StubTools):
    async def call_tool(self, name, arguments):
        if name == "data_execute_readonly_query" and "sql" not in arguments:
            self.calls.append(name)
            raise ToolError("missing sql argument")
        return await super().call_tool(name, arguments)


@pytest.mark.parametrize("route,expected_roles,expected_tools", [
    ("SQL", {"planner", "data_analyst", "synthesis", "verifier"},
     {"data_describe_schema", "data_execute_readonly_query"}),
    ("RAG", {"planner", "knowledge_researcher", "synthesis", "verifier"},
     {"knowledge_search_knowledge"}),
    ("BOTH", {"planner", "data_analyst", "knowledge_researcher", "synthesis", "verifier"},
     {"data_describe_schema", "data_execute_readonly_query", "knowledge_search_knowledge"}),
    ("REPORT", {"planner", "data_analyst", "knowledge_researcher", "synthesis", "verifier"},
     {"data_describe_schema", "data_execute_readonly_query", "knowledge_search_knowledge"}),
])
def test_four_routes_join_once_and_use_only_mcp_tools(route, expected_roles, expected_tools):
    model, tools = StubModel(), StubTools()
    state = asyncio.run(run_query(build_workflow(model, tools), f"route_{route.lower()}"))
    assert state["status"] == "PASS"
    assert set(state["trace"]) >= expected_roles
    assert state["trace"].count("synthesis") == 1
    assert state["trace"].count("verifier") == 1
    assert state["trace"].index("synthesis") > max(
        state["trace"].index(role) for role in
        ({"data_analyst", "knowledge_researcher"} & expected_roles))
    assert set(tools.calls) == expected_tools
    assert set(model.calls) == expected_roles
    assert bool(state.get("sql_results")) == (route != "RAG")
    assert bool(state.get("rag_results")) == (route != "SQL")
    assert ("fork" in state["trace"]) == (route in {"BOTH", "REPORT"})


def test_unsupported_citation_and_verifier_failure_block():
    for model in (StubModel(bad_citation=True), StubModel(verifier_fails=True)):
        state = asyncio.run(run_query(build_workflow(model, StubTools()), "route_both"))
        assert state["status"] == "BLOCK"
        assert state["verification"].issues


def test_data_tool_repair_is_bounded_and_traced():
    model = RepairModel()
    state = asyncio.run(run_query(build_workflow(model, ValidatingTools()), "route_sql"))
    assert state["status"] == "PASS"
    assert [item["status"] for item in state["sql_results"][0].attempts] == [
        "failed", "success"]
    assert model.proposals == 2

    model = RepairModel(always_invalid=True)
    with pytest.raises(RuntimeError, match="two repairs"):
        asyncio.run(run_query(build_workflow(model, ValidatingTools()), "route_sql"))
    assert model.proposals == 3


def test_incurred_loss_ratio_cannot_be_replaced_by_claim_frequency():
    class MetricModel:
        calls = 0

        async def complete_json(self, role, system, user, *, max_tokens=1200):
            self.calls += 1
            return {"tool": ("data_compute_claim_rate" if self.calls == 1 else
                             "data_compute_loss_ratio"),
                    "arguments": {"start_date": "2026-04-01", "end_date": "2026-07-01",
                                  "product_code": "product_006"}}

    class MetricTools(StubTools):
        async def call_tool(self, name, arguments):
            if name == "data_compute_loss_ratio":
                self.calls.append(name)
                return SimpleNamespace(structured_content={"metric": "incurred_loss_ratio",
                                                           "value": 1.3449})
            return await super().call_tool(name, arguments)

    model, tools = MetricModel(), MetricTools()
    plan = TaskPlan(intent="metric", route="SQL",
                    sql_tasks=["查询 product_006 的已发生赔付率"])
    artifact = asyncio.run(DataAnalystAgent(model, tools).run(plan))[0]
    assert model.calls == 2
    assert [attempt["status"] for attempt in artifact.attempts] == ["failed", "success"]
    assert "data_compute_claim_rate" not in tools.calls
    assert artifact.result["metric"] == "incurred_loss_ratio"
