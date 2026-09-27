"""Single-process LangGraph with four routes and an explicit parallel join."""

from uuid import uuid4

from langgraph.graph import END, START, StateGraph

from packages.agent.agents.roles import (DataAnalystAgent, KnowledgeResearcherAgent,
                                         PlannerAgent, SynthesisAnalystAgent,
                                         ToolCaller, VerificationAgent)
from packages.agent.contracts import checked_node, validate_registry
from packages.agent.models import RagSubState, RunState, SqlSubState
from packages.llm.client import JsonModel


def build_workflow(model: JsonModel, tools: ToolCaller):
    planner = PlannerAgent(model)
    data_analyst = DataAnalystAgent(model, tools)
    researcher = KnowledgeResearcherAgent(model, tools)
    synthesis = SynthesisAnalystAgent(model)
    verifier = VerificationAgent(model)

    async def data_node(state: SqlSubState):
        return {"sql_results": await data_analyst.run(state["plan"])}

    sql_builder = StateGraph(SqlSubState)
    sql_builder.add_node("data_analyst", checked_node("sql_subgraph.data_analyst",
                                                     data_node))
    sql_builder.add_edge(START, "data_analyst")
    sql_builder.add_edge("data_analyst", END)
    sql_subgraph = sql_builder.compile()

    async def knowledge_node(state: RagSubState):
        return {"rag_results": await researcher.run(state["plan"])}

    rag_builder = StateGraph(RagSubState)
    rag_builder.add_node("knowledge_researcher", checked_node(
        "rag_subgraph.knowledge_researcher", knowledge_node))
    rag_builder.add_edge(START, "knowledge_researcher")
    rag_builder.add_edge("knowledge_researcher", END)
    rag_subgraph = rag_builder.compile()

    async def planner_node(state: RunState):
        return {"plan": await planner.plan(state["user_query"]), "trace": ["planner"]}

    async def sql_branch(state: RunState):
        result = await sql_subgraph.ainvoke({"plan": state["plan"]})
        return {"sql_results": result["sql_results"], "trace": ["data_analyst"]}

    async def rag_branch(state: RunState):
        result = await rag_subgraph.ainvoke({"plan": state["plan"]})
        return {"rag_results": result["rag_results"], "trace": ["knowledge_researcher"]}

    async def synthesis_node(state: RunState):
        analysis = await synthesis.compose(state["plan"], state.get("sql_results", []),
                                           state.get("rag_results", []))
        return {"analysis": analysis, "trace": ["synthesis"]}

    async def verifier_node(state: RunState):
        result = await verifier.verify(state["plan"], state.get("sql_results", []),
                                       state.get("rag_results", []), state["analysis"])
        return {"verification": result, "status": result.status, "trace": ["verifier"]}

    builder = StateGraph(RunState)
    async def fork_node(state: RunState):
        return {"trace": ["fork"]}

    builder.add_node("planner", checked_node("planner", planner_node))
    builder.add_node("sql_only", checked_node("sql_only", sql_branch))
    builder.add_node("rag_only", checked_node("rag_only", rag_branch))
    builder.add_node("fork", checked_node("fork", fork_node))
    builder.add_node("sql_parallel", checked_node("sql_parallel", sql_branch))
    builder.add_node("rag_parallel", checked_node("rag_parallel", rag_branch))
    builder.add_node("synthesis", checked_node("synthesis", synthesis_node))
    builder.add_node("verifier", checked_node("verifier", verifier_node))
    builder.add_edge(START, "planner")
    builder.add_conditional_edges("planner", lambda state: state["plan"].route,
                                  {"SQL": "sql_only", "RAG": "rag_only",
                                   "BOTH": "fork", "REPORT": "fork"})
    builder.add_edge("sql_only", "synthesis")
    builder.add_edge("rag_only", "synthesis")
    builder.add_edge("fork", "sql_parallel")
    builder.add_edge("fork", "rag_parallel")
    builder.add_edge(["sql_parallel", "rag_parallel"], "synthesis")
    builder.add_edge("synthesis", "verifier")
    builder.add_edge("verifier", END)
    subgraph_nodes = {f"sql_subgraph.{name}" for name in sql_builder.nodes}
    subgraph_nodes.update(f"rag_subgraph.{name}" for name in rag_builder.nodes)
    validate_registry(set(builder.nodes), subgraph_nodes)
    return builder.compile()  # Still in-memory; persistence and HITL belong to M7.


async def run_query(workflow, query: str) -> RunState:
    if not query.strip():
        raise ValueError("query is empty")
    return await workflow.ainvoke({"run_id": str(uuid4()),
                                   "user_query": query, "trace": []})
