"""M4 knowledge tools call the real registered corpus and M2 retrieval path."""

import asyncio
import os

from fastmcp import Client
from fastmcp.exceptions import ToolError
import pytest

from packages.knowledge.documents import registered_sources
from services.mcp_knowledge.server import build_real_server, create_server

KNOWLEDGE_TOOLS = {"search_knowledge", "get_chunk", "get_document",
                   "get_document_metadata"}


def test_knowledge_tool_registration_and_no_collection_parameter():
    server = create_server(object(), object(), object())

    async def check():
        async with Client(server) as client:
            tools = {tool.name: tool for tool in await client.list_tools()}
            assert set(tools) == KNOWLEDGE_TOOLS
            assert "collection" not in tools["search_knowledge"].input_schema["properties"]
            assert "doc_id" in tools["get_document"].input_schema["required"]
            assert {"evidence", "count"} <= set(
                tools["search_knowledge"].output_schema["required"])
            assert {"source_sha256", "status"} <= set(
                tools["get_document_metadata"].output_schema["required"])

    asyncio.run(check())


def test_real_knowledge_tool_contracts():
    uri = os.environ.get("M4_TEST_MILVUS_URI")
    if not uri:
        pytest.skip("set M4_TEST_MILVUS_URI for real M4 Milvus/BGE tool contracts")
    server = build_real_server(uri)

    async def check():
        async with Client(server) as client:
            found = (await client.call_tool("search_knowledge", {
                "query": "等待期是多少天？", "product_code": "product_006", "limit": 3
            })).structured_content
            assert found["count"] == 3
            assert all(item["product_code"] == "product_006"
                       and item["source_type"] == "synthetic_product"
                       for item in found["evidence"])
            assert all(item["source_url"] == "" for item in found["evidence"])

            chunk_id = found["evidence"][0]["chunk_id"]
            chunk = (await client.call_tool("get_chunk", {"chunk_id": chunk_id})).structured_content
            assert chunk["chunk_id"] == chunk_id
            assert chunk["content_hash"] == found["evidence"][0]["content_hash"]
            assert chunk["evidence_id"] == found["evidence"][0]["evidence_id"]

            document = (await client.call_tool("get_document", {
                "doc_id": "product_006", "limit": 2})).structured_content
            assert document["doc_id"] == "product_006"
            assert len(document["chunks"]) == 2
            assert document["total_chunks"] >= 2
            assert document["next_offset"] == 2
            metadata = (await client.call_tool("get_document_metadata", {
                "doc_id": "product_006"})).structured_content
            assert metadata["status"] == "synthetic"
            assert metadata["product_code"] == "product_006"
            assert len(metadata["source_sha256"]) == 64

            public_id = next(item.doc_id for item in registered_sources()
                             if item.source_type == "public_consultation_draft")
            public = (await client.call_tool("get_document_metadata", {
                "doc_id": public_id})).structured_content
            assert public["status"] == "draft" and public["product_code"] is None

            for name, args in (
                ("search_knowledge", {"query": " ", "limit": 3}),
                ("search_knowledge", {"query": "等待期", "product_code": "product_006\" or true"}),
                ("search_knowledge", {"query": "等待期", "limit": 21}),
                ("get_chunk", {"chunk_id": "not-a-chunk-id"}),
                ("get_document", {"doc_id": "insur_qa_corpus"}),
                ("get_document", {"doc_id": "product_006", "limit": 21}),
                ("get_document_metadata", {"doc_id": "insur_qa_corpus"}),
            ):
                with pytest.raises(ToolError):
                    await client.call_tool(name, args)

    asyncio.run(check())
