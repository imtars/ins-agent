"""Read registered knowledge documents and chunks without exposing benchmark data."""

import hashlib
import re

from packages.knowledge.documents import RegisteredSource, parse_and_chunk, registered_sources
from packages.knowledge.milvus_store import KNOWLEDGE_COLLECTION
from packages.knowledge.retrieval import FIELDS


def source_for(doc_id: str) -> RegisteredSource:
    source = next((item for item in registered_sources() if item.doc_id == doc_id), None)
    if source is None:
        raise ValueError("document is not registered in the knowledge corpus")
    return source


def document_metadata(doc_id: str) -> dict:
    source = source_for(doc_id)
    return {"doc_id": source.doc_id, "title": source.title,
            "source_type": source.source_type, "source_name": source.source_name,
            "source_url": source.source_url, "product_code": source.product_code,
            "source_sha256": source.sha256,
            "status": "draft" if source.source_type == "public_consultation_draft"
            else "synthetic"}


def document_chunks(doc_id: str, *, offset: int = 0, limit: int = 20) -> dict:
    if offset < 0 or limit < 1 or limit > 20:
        raise ValueError("offset must be nonnegative and limit must be 1–20")
    source = source_for(doc_id)
    chunks = parse_and_chunk(source)  # verifies source bytes against its registered SHA-256
    page = chunks[offset:offset + limit]
    next_offset = offset + len(page) if offset + len(page) < len(chunks) else None
    return {"doc_id": doc_id, "title": source.title,
            "source_type": source.source_type, "source_url": source.source_url,
            "chunks": page, "total_chunks": len(chunks),
            "offset": offset, "next_offset": next_offset}


def indexed_chunk(store, chunk_id: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{24}", chunk_id):
        raise ValueError("chunk_id must be a 24-character lowercase hex identifier")
    rows = store.query(collection_name=KNOWLEDGE_COLLECTION,
                       filter=f'pk == "{chunk_id}"', output_fields=list(FIELDS), limit=1)
    if not rows:
        raise ValueError("knowledge chunk not found")
    item = rows[0]
    source = source_for(item["doc_id"])
    if (item["chunk_id"] != chunk_id
            or item["source_type"] != source.source_type
            or item["source_url"] != source.source_url
            or (item["product_code"] or None) != source.product_code
            or item["content_hash"] != hashlib.sha256(item["text"].encode()).hexdigest()):
        raise ValueError("indexed chunk provenance mismatch")
    return {"doc_id": item["doc_id"], "chunk_id": item["chunk_id"],
            "evidence_id": f"{item['doc_id']}:{item['section']}:{item['chunk_id']}",
            "title": item["title"], "section": item["section"],
            "page": None if item["page"] == -1 else item["page"],
            "text": item["text"], "source_type": item["source_type"],
            "source_name": item["source_name"], "source_url": item["source_url"],
            "product_code": item["product_code"] or None,
            "content_hash": item["content_hash"]}
