"""M5 routing/fan-in tests use controlled role output, never benchmark scores."""

import asyncio
import hashlib
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
                                              "product_006:等待期:chunkid"],
                               "evidence_quote": "waiting period is 15 days"})
            return {"summary": "Synthetic demo answer.", "claims": claims}
        if role == "verifier":
            if self.verifier_fails:
                raise RuntimeError("test verifier unavailable")
            return {"status": "PASS", "issues": []}
        raise AssertionError(f"unexpected role: {role}")


class StubTools:
    def __init__(self):
        self.calls = []
        self.list_tools_calls = 0

    async def list_tools(self):
        self.list_tools_calls += 1
        schemas = {
            "data_execute_readonly_query": {"type": "object", "additionalProperties": False,
                                            "properties": {"sql": {"type": "string"}},
                                            "required": ["sql"]},
            "data_compute_claim_rate": {"type": "object", "properties": {
                "start_date": {"type": "string"}, "end_date": {"type": "string"}}},
            "data_compute_loss_ratio": {"type": "object", "properties": {
                "start_date": {"type": "string"}, "end_date": {"type": "string"}}},
            "data_group_statistics": {"type": "object", "properties": {
                "group_by": {"enum": ["all", "product_code", "region"]}}},
            "data_compute_growth": {"type": "object", "properties": {
                "metric": {"enum": ["earned_premium"]}}},
        }
        return [SimpleNamespace(name=name, description=f"live {name} description",
                                input_schema=schema) for name, schema in schemas.items()]

    async def call_tool(self, name, arguments):
        self.calls.append(name)
        if name == "data_describe_schema":
            content = {"schema": "policies(id integer)"}
        elif name == "data_execute_readonly_query":
            content = {"rows": [{"n": 30000}], "row_count": 1}
        elif name == "knowledge_search_knowledge":
            evidence_text = "The waiting period is 15 days."
            content = {"query": arguments["query"],
                       "product_code": arguments["product_code"],
                       "evidence": [{"evidence_id": "product_006:等待期:chunkid",
                                     "doc_id": "product_006", "chunk_id": "chunkid",
                                     "title": "Synthetic health", "section": "等待期",
                                     "page": None, "text": evidence_text,
                                     "source_type": "synthetic_product",
                                     "source_name": "Synthetic demo product", "source_url": "",
                                     "product_code": "product_006",
                                     "content_hash": hashlib.sha256(
                                         evidence_text.encode()).hexdigest(),
                                     "rerank_score": 0.9}], "count": 1}
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
    assert tools.list_tools_calls == (route != "RAG")
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
    assert [item.status for item in state["sql_results"][0].attempts] == [
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
                return SimpleNamespace(structured_content={
                    "metric": "incurred_loss_ratio", "value": "1.3449", "unit": "ratio",
                    "start_date": "2026-04-01", "end_date": "2026-07-01",
                    "product_code": "product_006", "region": None,
                    "incurred_amount": "2937010.48", "paid_amount": "2172304.76",
                    "earned_premium": "2183750.57"})
            return await super().call_tool(name, arguments)

    model, tools = MetricModel(), MetricTools()
    plan = TaskPlan(intent="metric", route="SQL",
                    sql_tasks=["查询 product_006 的已发生赔付率"])
    artifact = asyncio.run(DataAnalystAgent(model, tools).run(plan))[0]
    assert model.calls == 2
    assert [attempt.status for attempt in artifact.attempts] == ["failed", "success"]
    assert "data_compute_claim_rate" not in tools.calls
    assert artifact.result["metric"] == "incurred_loss_ratio"


def test_data_analyst_reads_live_mcp_input_schemas_and_fails_if_missing():
    class InspectModel(StubModel):
        async def complete_json(self, role, system, user, *, max_tokens=1200):
            if role == "data_analyst":
                catalog = json.loads(system.split("Live MCP tool catalog:\n", 1)[1])
                assert len(catalog) == 5
                by_name = {item["name"]: item for item in catalog}
                query = by_name["data_execute_readonly_query"]
                assert query["description"] == "live data_execute_readonly_query description"
                assert query["input_schema"]["required"] == ["sql"]
                assert query["input_schema"]["additionalProperties"] is False
                assert "data_describe_schema" not in by_name
            return await super().complete_json(role, system, user, max_tokens=max_tokens)

    plan = TaskPlan(intent="count", route="SQL", sql_tasks=["count policies"])
    tools = StubTools()
    asyncio.run(DataAnalystAgent(InspectModel(), tools).run(plan))
    assert tools.list_tools_calls == 1

    class MissingTool(StubTools):
        async def list_tools(self):
            return (await super().list_tools())[:-1]

    with pytest.raises(ValueError, match="required MCP data tool schemas missing"):
        asyncio.run(DataAnalystAgent(InspectModel(), MissingTool()).run(plan))
