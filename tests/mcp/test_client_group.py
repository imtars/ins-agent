"""The two M4 servers expose collision-free namespaced tools to M5 clients."""

import asyncio
import os

from fastmcp import Client, ClientGroup
import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from services.mcp_data.server import create_server as data_server
from services.mcp_knowledge.server import create_server as knowledge_server


def test_client_group_namespaces_and_routes_existing_tools():
    url = os.environ.get("M3_TEST_READER_DATABASE_URL")
    if not url:
        pytest.skip("set M3_TEST_READER_DATABASE_URL for ClientGroup contract")

    async def check():
        engine = create_async_engine(url)
        try:
            group = ClientGroup({"data": Client(data_server(engine)),
                                 "knowledge": Client(knowledge_server(
                                     object(), object(), object()))})
            async with group:
                listed = {tool.name: tool for tool in await group.list_tools()}
                names = set(listed)
                assert "data_execute_readonly_query" in names
                assert "knowledge_search_knowledge" in names
                assert len(names) == 11
                query_schema = listed["data_execute_readonly_query"].input_schema
                assert query_schema["required"] == ["sql"]
                assert query_schema["additionalProperties"] is False
                loss_schema = listed["data_compute_loss_ratio"].input_schema
                assert "metric" not in loss_schema["properties"]
                assert loss_schema["properties"]["kind"]["default"] == "incurred"
                result = await group.call_tool("data_execute_readonly_query", {
                    "sql": "SELECT count(*) AS n FROM policies"})
                assert result.structured_content["rows"] == [{"n": 30000}]
                metadata = await group.call_tool("knowledge_get_document_metadata", {
                    "doc_id": "product_006"})
                assert metadata.structured_content["status"] == "synthetic"
        finally:
            await engine.dispose()

    asyncio.run(check())
