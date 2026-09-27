import json
import os
from pathlib import Path

import pytest

from packages.knowledge.milvus_store import (
    BENCHMARK_COLLECTION, KNOWLEDGE_COLLECTION, client
)


def test_real_milvus_indexes_keep_sources_separate():
    uri = os.environ.get("M2_TEST_MILVUS_URI")
    if not uri:
        pytest.skip("set M2_TEST_MILVUS_URI after indexing both M2 collections")
    store = client(uri)
    dataset = json.loads(Path("data/processed/rag/insur_qa/manifest.json")
                         .read_text(encoding="utf-8"))
    assert int(store.get_collection_stats(BENCHMARK_COLLECTION)["row_count"]) == \
           dataset["unique_passages"]
    assert int(store.get_collection_stats(KNOWLEDGE_COLLECTION)["row_count"]) == 365
    for i in range(1, 13):
        code = f"product_{i:03d}"
        rows = store.query(collection_name=KNOWLEDGE_COLLECTION,
                           filter=f'product_code == "{code}"',
                           output_fields=["doc_id", "source_type", "product_code"], limit=20)
        assert len(rows) == 10
        assert all(row["doc_id"] == code and row["source_type"] == "synthetic_product"
                   for row in rows)
    public = store.query(collection_name=KNOWLEDGE_COLLECTION,
                         filter='source_type == "public_consultation_draft"',
                         output_fields=["doc_id", "product_code", "source_url"], limit=1)
    assert public and public[0]["product_code"] == ""
    assert "iachina.cn" in public[0]["source_url"]
    benchmark = store.query(collection_name=BENCHMARK_COLLECTION, filter='source_type == "benchmark_passage"',
                            output_fields=["doc_id", "product_code"], limit=1)
    assert benchmark and benchmark[0]["product_code"] == ""
