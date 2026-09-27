"""M8 durable, reference-only LangGraph with reviewer-directed stage replay."""

import operator
from typing import Annotated, Awaitable, Callable, TypedDict

from langgraph.config import get_config
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from pydantic import TypeAdapter

from packages.agent.agents.roles import (DataAnalystAgent, KnowledgeResearcherAgent,
    PlannerAgent, SynthesisAnalystAgent, ToolCaller, VerificationAgent)
from packages.agent.contracts import (ContractViolation, NODE_CONTRACTS,
    SynthesisInput, SynthesisOutput, VerifierInput, VerifierOutput)
from packages.agent.models import (AnalysisResult, RagArtifact, SqlArtifact,
    TaskPlan, VerificationResult)
from packages.llm.client import JsonModel
from packages.persistence.approvals import PublishGuardViolation
from packages.persistence.artifacts import ArtifactRef, ArtifactStore
from packages.persistence.reviews import ReviewDecision, ReviewStore


SQL_ADAPTER = TypeAdapter(list[SqlArtifact])
RAG_ADAPTER = TypeAdapter(list[RagArtifact])
ANALYSIS_ADAPTER = TypeAdapter(AnalysisResult)
VERIFICATION_ADAPTER = TypeAdapter(VerificationResult)


class M8State(TypedDict, total=False):
    run_id: str
    user_query: str
    plan: TaskPlan
    refs: Annotated[dict[str, ArtifactRef], operator.or_]
    cycle: int
    revision_targets: list[str]
    approval: ReviewDecision
    publication: dict
    status: str
    degraded_flags: list[str]
    trace: Annotated[list[str], operator.add]


def build_m8_workflow(model: JsonModel, tools: ToolCaller, artifacts: ArtifactStore,
                      reviews: ReviewStore, *, checkpointer,
                      assert_lease: Callable[[], Awaitable[None]]):
    planner = PlannerAgent(model)
    data = DataAnalystAgent(model, tools)
    researcher = KnowledgeResearcherAgent(model, tools)
    synthesis = SynthesisAnalystAgent(model)
    verifier = VerificationAgent(model)

    async def guard(state: M8State) -> None:
        if get_config().get("configurable", {}).get("thread_id") != state["run_id"]:
            raise ContractViolation("run_id must equal durable thread_id")
        await assert_lease()

    async def load_inputs(state: M8State) -> tuple[list[SqlArtifact], list[RagArtifact]]:
        refs = state.get("refs", {})
        sql = await artifacts.load(refs["sql"], SQL_ADAPTER) if "sql" in refs else []
        rag = await artifacts.load(refs["rag"], RAG_ADAPTER) if "rag" in refs else []
        SynthesisInput.model_validate({"plan": state["plan"],
            "sql_results": sql, "rag_results": rag})
        return sql, rag

    async def planner_node(state: M8State):
        await guard(state)
        NODE_CONTRACTS["planner"].validate_input(state)
        plan = await planner.plan(state["user_query"])
        NODE_CONTRACTS["planner"].validate_output({"plan": plan, "trace": ["planner"]})
        await guard(state)
        return {"plan": plan, "cycle": 1, "refs": {}, "trace": ["planner"]}

    async def sql_node(state: M8State):
        await guard(state)
        NODE_CONTRACTS["sql_only"].validate_input(state)
        ref, value = await artifacts.load_or_compute(
            state["run_id"], "sql", state["cycle"], SQL_ADAPTER,
            lambda: data.run(state["plan"]), assert_lease=assert_lease)
        NODE_CONTRACTS["sql_only"].validate_output(
            {"sql_results": value, "trace": ["data_analyst"]})
        return {"refs": {"sql": ref}, "trace": ["data_analyst"]}

    async def rag_node(state: M8State):
        await guard(state)
        NODE_CONTRACTS["rag_only"].validate_input(state)
        ref, value = await artifacts.load_or_compute(
            state["run_id"], "rag", state["cycle"], RAG_ADAPTER,
            lambda: researcher.run(state["plan"]), assert_lease=assert_lease)
        NODE_CONTRACTS["rag_only"].validate_output(
            {"rag_results": value, "trace": ["knowledge_researcher"]})
        return {"refs": {"rag": ref}, "trace": ["knowledge_researcher"]}

    async def fork_node(state: M8State):
        await guard(state)
        return {"trace": ["fork"]}

    async def synthesis_node(state: M8State):
        await guard(state)
        sql, rag = await load_inputs(state)
        ref, value = await artifacts.load_or_compute(
            state["run_id"], "analysis", state["cycle"], ANALYSIS_ADAPTER,
            lambda: synthesis.compose(state["plan"], sql, rag),
            assert_lease=assert_lease)
        SynthesisOutput.model_validate({"analysis": value, "trace": ["synthesis"]})
        return {"refs": {"analysis": ref}, "trace": ["synthesis"],
                "degraded_flags": sorted({flag for item in rag
                                          for flag in item.degraded_flags})}

    async def verifier_node(state: M8State):
        await guard(state)
        sql, rag = await load_inputs(state)
        analysis = await artifacts.load(state["refs"]["analysis"], ANALYSIS_ADAPTER)
        VerifierInput.model_validate({"plan": state["plan"],
            "sql_results": sql, "rag_results": rag, "analysis": analysis})
        ref, result = await artifacts.load_or_compute(
            state["run_id"], "verification", state["cycle"], VERIFICATION_ADAPTER,
            lambda: verifier.verify(state["plan"], sql, rag, analysis),
            assert_lease=assert_lease)
        VerifierOutput.model_validate({"verification": result,
            "status": result.status, "trace": ["verifier"]})
        return {"refs": {"verification": ref}, "status": result.status,
                "trace": ["verifier"]}

    async def review_node(state: M8State):
        await guard(state)
        analysis_ref = state["refs"]["analysis"]
        analysis = await artifacts.load(analysis_ref, ANALYSIS_ADAPTER)
        resume = interrupt({"run_id": state["run_id"], "cycle": state["cycle"],
            "status": "WAITING_APPROVAL", "summary": analysis.summary,
            "artifact": analysis_ref.model_dump()})
        await guard(state)
        if not isinstance(resume, dict) or set(resume) != {"approval_id"}:
            raise PublishGuardViolation("resume requires stored review ID")
        decision = await reviews.get(state["run_id"], state["cycle"])
        if (decision is None or decision.approval_id != resume["approval_id"]
                or decision.artifact != analysis_ref):
            raise PublishGuardViolation("resume does not match reviewed artifact")
        return {"approval": decision, "revision_targets": decision.revision_targets,
                "status": decision.status, "trace": ["human_review"]}

    async def revision_node(state: M8State):
        await guard(state)
        targets = state["revision_targets"]
        allowed = {"sql"} if state["plan"].route == "SQL" else (
            {"rag"} if state["plan"].route == "RAG" else {"sql", "rag"})
        if (not targets or not set(targets) <= allowed | {"synthesis"}
                or len(targets) != len(set(targets))):
            raise ContractViolation("revision targets do not match route")
        return {"cycle": state["cycle"] + 1, "trace": ["revision_router"]}

    async def publish_node(state: M8State):
        await guard(state)
        verdict = await artifacts.load(state["refs"]["verification"],
                                       VERIFICATION_ADAPTER)
        receipt = await reviews.publish(state["run_id"], state["approval"],
                                        state["refs"]["analysis"], verdict)
        return {"publication": receipt, "status": "PUBLISHED", "trace": ["publish"]}

    builder = StateGraph(M8State)
    builder.add_node("planner", planner_node)
    builder.add_node("sql", sql_node)
    builder.add_node("rag", rag_node)
    builder.add_node("sql_parallel", sql_node)
    builder.add_node("rag_parallel", rag_node)
    builder.add_node("fork", fork_node)
    builder.add_node("synthesis", synthesis_node)
    builder.add_node("verifier", verifier_node)
    builder.add_node("human_review", review_node)
    builder.add_node("revision_router", revision_node)
    builder.add_node("publish", publish_node)
    builder.add_edge(START, "planner")
    builder.add_conditional_edges("planner", lambda s: s["plan"].route,
        {"SQL": "sql", "RAG": "rag", "BOTH": "fork", "REPORT": "fork"})
    builder.add_edge("fork", "sql_parallel")
    builder.add_edge("fork", "rag_parallel")
    builder.add_edge("sql", "synthesis")
    builder.add_edge("rag", "synthesis")
    # LangGraph waits for both fork branches before synthesis on BOTH/REPORT.
    builder.add_edge(["sql_parallel", "rag_parallel"], "synthesis")
    builder.add_edge("synthesis", "verifier")
    builder.add_conditional_edges("verifier", lambda s: s["status"],
        {"PASS": "human_review", "REVISE": END, "BLOCK": END})
    builder.add_conditional_edges("human_review", lambda s:
        "publish" if s["approval"].status == "APPROVED" else
        "revise" if s["revision_targets"] else "done",
        {"publish": "publish", "revise": "revision_router", "done": END})
    builder.add_conditional_edges("revision_router", lambda s:
        "both" if {"sql", "rag"} <= set(s["revision_targets"]) else
        "sql" if "sql" in s["revision_targets"] else
        "rag" if "rag" in s["revision_targets"] else "synthesis",
        {"both": "fork", "sql": "sql", "rag": "rag",
         "synthesis": "synthesis"})
    builder.add_edge("publish", END)
    return builder.compile(checkpointer=checkpointer)
