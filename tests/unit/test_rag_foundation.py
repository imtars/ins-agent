import json
from pathlib import Path

import pytest

from evaluation.rag.dataset import normalize_text, prepare, stable_id
from evaluation.rag.run import metrics, rrf
from packages.knowledge.documents import RegisteredSource, parse_and_chunk, registered_sources


def test_insur_qa_deduplicates_passages_and_unions_repeated_query_positives(tmp_path):
    query = next(f"固定查询 {i}" for i in range(100)
                 if int(stable_id(f"固定查询 {i}")[:16], 16) % 10 == 0)
    source = tmp_path / "retriever.json"
    rows = [
        {"query": query, "pos": ["甲  条款"], "neg": ["乙条款"]},
        {"query": query, "pos": ["丙条款"], "neg": ["甲 条款"]},
        {"query": "其他问题", "pos": ["乙条款"], "neg": ["丙条款"]},
        {"query": "", "pos": ["忽略"], "neg": []},
    ]
    source.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
                      encoding="utf-8")
    first = prepare(source, tmp_path / "first", holdout_size=1)
    second = prepare(source, tmp_path / "second", holdout_size=1)
    holdout = json.loads((tmp_path / "first/holdout.jsonl").read_text(encoding="utf-8"))
    assert first["files"]["corpus.jsonl"]["sha256"] == second["files"]["corpus.jsonl"]["sha256"]
    assert first["skipped_empty_queries"] == 1
    assert first["unique_passages"] == 3
    assert first["unique_queries"] == 2
    assert len(holdout["positive_ids"]) == 2
    assert set(holdout["positive_ids"]) == {stable_id("甲 条款"), stable_id("丙条款")}
    assert normalize_text("甲  条款") == "甲 条款"


def test_multi_positive_recall_and_rrf_use_only_ranked_ids():
    row = {"query_id": "q", "positive_ids": ["a", "b"]}
    ranked = {"q": ["x", "a", "b"]}
    assert metrics([row], ranked) == {"Recall@1": 0.0, "Recall@5": 1.0,
                                      "Recall@10": 1.0, "MRR@10": 0.5}
    dense = [{"id": "a"}, {"id": "b"}]
    sparse = [{"id": "b"}, {"id": "c"}]
    assert [hit["id"] for hit in rrf(dense, sparse)] == ["b", "a", "c"]
    with pytest.raises(ValueError, match="duplicate"):
        metrics([row], {"q": ["a", "a"]})


def test_document_allowlist_excludes_generator_manifest_and_preserves_citation(tmp_path):
    sources = registered_sources()
    assert len(sources) == 17
    assert all(source.path.suffix in (".md", ".pdf", ".docx") for source in sources)
    assert all(source.path.name != "manifest.json" for source in sources)
    path = tmp_path / "product_001.md"
    path.write_text("Synthetic demo document.\n## 保险责任\n碰撞责任。\n## 责任免除\n酒后驾驶。\n",
                    encoding="utf-8")
    from packages.domain.hf_download import file_sha256
    source = RegisteredSource("product_001", path, "演示", "synthetic_product",
                              "Synthetic demo product", "", "product_001", file_sha256(path))
    chunks = parse_and_chunk(source)
    assert {chunk["section"] for chunk in chunks} >= {"保险责任", "责任免除"}
    assert all(chunk["product_code"] == "product_001" and chunk["chunk_id"]
               and chunk["content_hash"] for chunk in chunks)
    path.write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        parse_and_chunk(source)
