"""M6 contract self-tests and deliberately invalid handoff/tool outputs."""

import asyncio
import hashlib
from types import SimpleNamespace

import pytest

from packages.agent.agents.roles import VerificationAgent
from packages.agent.contracts import (ContractViolation, NODE_CONTRACTS,
                                     NodeContract, VerifierInput, VerifierOutput,
                                     checked_node, validate_registry)
from packages.agent.evidence import label_source_scope, validate_claim_evidence
from packages.agent.graph import build_workflow, run_query
from packages.agent.models import (AnalysisResult, Evidence, RagArtifact,
                                   SqlArtifact, TaskPlan)


def test_registry_declares_every_node_and_typed_producers(monkeypatch):
    main = {"planner", "fork", "sql_only", "rag_only", "sql_parallel",
            "rag_parallel", "synthesis", "verifier"}
    subgraphs = {"sql_subgraph.data_analyst", "rag_subgraph.knowledge_researcher"}
    validate_registry(main, subgraphs)
    assert NODE_CONTRACTS["verifier"].requires >= {"plan", "analysis"}
    assert NODE_CONTRACTS["synthesis"].produces == {"analysis", "trace"}
    assert "properties" in NODE_CONTRACTS["planner"].input_schema
    assert "properties" in NODE_CONTRACTS["planner"].output_schema
    with pytest.raises(ContractViolation, match="registry nodes differ"):
        validate_registry(main - {"verifier"}, subgraphs)

    class Unproduced(VerifierInput):
        missing_source: str

    monkeypatch.setitem(NODE_CONTRACTS, "verifier", NodeContract(
        "verifier", Unproduced, VerifierOutput, ("synthesis",)))
    with pytest.raises(ContractViolation, match="without a typed producer"):
        validate_registry(main, subgraphs)


def test_boundary_blocks_missing_input_and_invalid_output_before_handoff():
    called = []

    async def node(state):
        called.append("node")
        return {"plan": {"intent": "x", "route": "SQL", "sql_tasks": ["count"]},
                "trace": "wrong type"}

    guarded = checked_node("planner", node)
    with pytest.raises(ContractViolation, match="input contract"):
        asyncio.run(guarded({}))
    assert called == []
    with pytest.raises(ContractViolation, match="output contract"):
        asyncio.run(guarded({"user_query": "count"}))
    assert called == ["node"]
    with pytest.raises(ContractViolation, match="output contract"):
        NODE_CONTRACTS["planner"].validate_output({"trace": ["planner"]})

    sql_plan = TaskPlan(intent="count", route="SQL", sql_tasks=["count"])
    with pytest.raises(ContractViolation, match="route requires SQL artifacts"):
        NODE_CONTRACTS["synthesis"].validate_input({"plan": sql_plan})


class BrokenModel:
    def __init__(self, route):
        self.route = route
        self.roles = []

    async def complete_json(self, role, system, user, *, max_tokens=1200):
        self.roles.append(role)
        if role == "planner":
            return {"intent": "injection", "route": self.route,
                    "sql_tasks": ["count policies"] if self.route == "SQL" else [],
                    "rag_tasks": ["product_006 waiting period"] if self.route == "RAG" else [],
                    "required_outputs": []}
        if role == "data_analyst":
            return {"tool": "data_execute_readonly_query",
                    "arguments": {"sql": "SELECT count(*) FROM policies"}}
        if role == "knowledge_researcher":
            return {"query": "product_006 waiting period", "product_code": "product_006"}
        raise AssertionError(f"downstream role must not run: {role}")


class BrokenTools:
    async def list_tools(self):
        from packages.agent.agents.roles import DataAnalystAgent
        return [SimpleNamespace(name=name, description="test", input_schema={})
                for name in DataAnalystAgent.TOOL_NAMES]

    async def call_tool(self, name, arguments):
        if name == "data_describe_schema":
            return SimpleNamespace(structured_content={"schema": "policies(id int)"})
        if name == "data_execute_readonly_query":
            return SimpleNamespace(structured_content={"rows": [{"n": 2}],
                                                       "row_count": 3})
        if name == "knowledge_search_knowledge":
            return SimpleNamespace(structured_content={"query": arguments["query"],
                "product_code": "product_006", "count": 1,
                "evidence": [{"evidence_id": "product_006:等待期:chunk", "text": "15 天"}]})
        raise AssertionError(name)


@pytest.mark.parametrize("route", ["SQL", "RAG"])
def test_invalid_mcp_tool_output_blocks_before_synthesis(route):
    model = BrokenModel(route)
    with pytest.raises(ContractViolation, match="invalid MCP output"):
        asyncio.run(run_query(build_workflow(model, BrokenTools()), "injection"))
    assert "synthesis" not in model.roles
    assert "verifier" not in model.roles


def test_each_claim_needs_current_numeric_or_exact_quote_support():
    sql = SqlArtifact(artifact_id="sql:1", task="count", tool="data_execute_readonly_query",
                      arguments={"sql": "SELECT count(*) FROM policies"},
                      result={"rows": [{"n": 30000}], "row_count": 1},
                      attempts=[{"number": 0, "status": "success",
                                 "tool": "data_execute_readonly_query",
                                 "arguments": {"sql": "SELECT count(*) FROM policies"}}])
    text = "疾病责任等待期为 15 天。"
    evidence = Evidence(evidence_id="product_006:等待期:chunk", doc_id="product_006",
                        chunk_id="chunk", title="Synthetic health", section="等待期",
                        page=None, source_type="synthetic_product", source_name="demo",
                        source_url="", product_code="product_006", text=text,
                        content_hash=hashlib.sha256(text.encode()).hexdigest(),
                        rerank_score=0.8)
    rag = RagArtifact(artifact_id="rag:1", task="waiting period", query="waiting period",
                      evidence=[evidence])
    plan = TaskPlan(intent="combined", route="BOTH", sql_tasks=["count"],
                    rag_tasks=["waiting period"])

    def issues(sql_text="有 30000 张保单。", rag_text=text, quote=text,
               sql_id="sql:1", summary="合成数据简报"):
        analysis = AnalysisResult(summary=summary, claims=[
            {"text": sql_text, "source_ids": [sql_id]},
            {"text": rag_text, "source_ids": [evidence.evidence_id],
             "evidence_quote": quote}])
        return validate_claim_evidence(plan, [sql], [rag], analysis)

    assert issues() == []
    labeled = label_source_scope(AnalysisResult(summary="简报", claims=[
        {"text": "有 30000 张保单。", "source_ids": ["sql:1"]},
        {"text": text, "source_ids": [evidence.evidence_id],
         "evidence_quote": text}]), [sql], [rag])
    assert "运营结果为合成演示数据" in labeled.summary
    assert labeled.claims[0].text.startswith("合成运营数据：")
    assert labeled.claims[1].text.startswith("合成演示产品条款：")
    assert validate_claim_evidence(plan, [sql], [rag], labeled) == []
    assert any("number absent" in item for item in issues(sql_text="有 99999 张保单。"))
    assert any("exact quote" in item for item in issues(quote="等待期为 30 天"))
    assert any("number absent" in item for item in issues(rag_text=text + "另有 30 天。"))
    assert any("unknown source" in item for item in issues(sql_id="sql:other"))
    assert any("summary" in item for item in issues(summary="赔付率为 9.9999"))
    class NeverPassModel:
        async def complete_json(self, *args, **kwargs):
            raise AssertionError("unsupported claim must be blocked before LLM verifier")

    unsupported = AnalysisResult(summary="简报", claims=[
        {"text": "有 99999 张保单。", "source_ids": ["sql:1"]}])
    verdict = asyncio.run(VerificationAgent(NeverPassModel()).verify(
        plan, [sql], [rag], unsupported))
    assert verdict.status == "BLOCK"
    analysis = AnalysisResult(summary="合成数据简报", claims=[
        {"text": "有 30000 张保单。", "source_ids": ["sql:1", evidence.evidence_id]}])
    assert any("exactly one source" in item for item in validate_claim_evidence(
        plan, [sql], [rag], analysis))

    with pytest.raises(ValueError, match="content hash mismatch"):
        Evidence.model_validate({**evidence.model_dump(), "content_hash": "0" * 64})
    with pytest.raises(ValueError, match="cannot bind synthetic product"):
        Evidence.model_validate({**evidence.model_dump(),
                                 "source_type": "public_consultation_draft"})
