"""mcp-knowledge: expose only the registered runtime knowledge corpus."""

import os
from pathlib import Path
from typing import Annotated, TypedDict

from fastmcp import FastMCP
from pydantic import Field

from packages.knowledge.catalog import document_chunks, document_metadata, indexed_chunk
from packages.knowledge.documents import build_knowledge_corpus
from packages.knowledge.milvus_store import (KNOWLEDGE_COLLECTION, client,
                                              embedding_model, index_jsonl)
from packages.knowledge.retrieval import retrieve_evidence_with_status
from packages.agent.faults import FaultInjector
from scripts.download_m2_models import verify_local_model


class SearchResult(TypedDict):
    query: str
    product_code: str | None
    evidence: list[dict]
    count: int
    degraded_flags: list[str]


class ChunkResult(TypedDict):
    doc_id: str
    chunk_id: str
    evidence_id: str
    title: str
    section: str
    page: int | None
    text: str
    source_type: str
    source_name: str
    source_url: str
    product_code: str | None
    content_hash: str


class DocumentResult(TypedDict):
    doc_id: str
    title: str
    source_type: str
    source_url: str
    chunks: list[dict]
    total_chunks: int
    offset: int
    next_offset: int | None


class MetadataResult(TypedDict):
    doc_id: str
    title: str
    source_type: str
    source_name: str
    source_url: str
    product_code: str | None
    source_sha256: str
    status: str


def create_server(store, embedder, reranker, *, injector=None) -> FastMCP:
    mcp = FastMCP("mcp-knowledge", instructions=(
        "Retrieve cited evidence only from 12 synthetic product documents and five "
        "registered public consultation drafts. Public drafts are not synthetic products."))

    @mcp.tool
    def search_knowledge(query: Annotated[str, Field(min_length=1, max_length=1000)],
                         product_code: str | None = None,
                         limit: Annotated[int, Field(ge=1, le=10)] = 5) -> SearchResult:
        """Run accepted BGE-M3 dense/sparse, RRF, and reranker retrieval."""
        evidence, degraded_flags = retrieve_evidence_with_status(
            store, embedder, reranker, query, collection=KNOWLEDGE_COLLECTION,
            product_code=product_code, limit=limit, injector=injector)
        return {"query": query, "product_code": product_code,
                "evidence": evidence, "count": len(evidence),
                "degraded_flags": degraded_flags}

    @mcp.tool
    def get_chunk(chunk_id: str) -> ChunkResult:
        """Fetch one indexed chunk by exact ID with source and content provenance."""
        return indexed_chunk(store, chunk_id)

    @mcp.tool
    def get_document(doc_id: str, offset: Annotated[int, Field(ge=0)] = 0,
                     limit: Annotated[int, Field(ge=1, le=20)] = 20) -> DocumentResult:
        """Page through a registered source document in original chunk order."""
        return document_chunks(doc_id, offset=offset, limit=limit)

    @mcp.tool
    def get_document_metadata(doc_id: str) -> MetadataResult:
        """Return registered provenance and draft/synthetic status for a document."""
        return document_metadata(doc_id)

    return mcp


def build_real_server(uri: str, *, injector=None) -> FastMCP:
    """Validate pinned model bytes and existing M2 index before serving queries."""
    verify_local_model("bge-m3")
    verify_local_model("bge-reranker-v2-m3")
    corpus = build_knowledge_corpus()
    store = client(uri)
    embedder = embedding_model()
    index_jsonl(store, embedder, KNOWLEDGE_COLLECTION, Path(corpus["corpus_path"]),
                benchmark=False, rebuild=False)
    from FlagEmbedding import FlagReranker
    reranker = FlagReranker("data/models/bge-reranker-v2-m3", use_fp16=True,
                           devices=["cuda:0"])
    return create_server(store, embedder, reranker,
                         injector=injector if injector is not None else
                         FaultInjector.from_env())


def main() -> None:
    uri = os.environ.get("MILVUS_URI", "http://127.0.0.1:19530")
    build_real_server(uri).run(transport="stdio")


if __name__ == "__main__":
    main()
