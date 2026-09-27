"""A real Milvus/BGE retrieval survives a failed reranker with an explicit flag."""

import asyncio
import os

from fastmcp import Client, ClientGroup
import pytest

from packages.agent.agents.roles import KnowledgeResearcherAgent
from packages.agent.faults import FaultInjector
from packages.agent.models import TaskPlan
from services.mcp_knowledge.server import build_real_server
from tests.agent.test_m5_graph import StubModel


def test_real_milvus_reranker_failure_uses_rrf_and_marks_artifact():
    uri = os.getenv("M9_TEST_MILVUS_URI")
    if not uri:
        pytest.skip("M9_TEST_MILVUS_URI required for real BGE/Milvus fault path")
    server = build_real_server(uri, injector=FaultInjector(
        case="reranker_failure", stage="reranker", enabled=True))

    async def scenario():
        async with ClientGroup({"knowledge": Client(server)}) as tools:
            plan = TaskPlan(intent="synthetic clause", route="RAG", sql_tasks=[],
                rag_tasks=["product_006 waiting period"], required_outputs=["waiting period"])
            artifacts = await KnowledgeResearcherAgent(StubModel(), tools).run(plan)
            assert len(artifacts) == 1
            assert artifacts[0].degraded_flags == ["reranker_unavailable"]
            assert artifacts[0].evidence
            assert all(item.product_code == "product_006" for item in artifacts[0].evidence)
            assert all(item.score_method == "rrf_fallback"
                       for item in artifacts[0].evidence)
            assert any("等待期" in item.section for item in artifacts[0].evidence)
    asyncio.run(scenario())
