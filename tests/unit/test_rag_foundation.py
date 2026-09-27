import json
from pathlib import Path

import pytest

from evaluation.rag.dataset import normalize_text, prepare, stable_id
from evaluation.rag.run import metrics, rrf
from packages.knowledge import documents, milvus_store
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
                                      "Recall@10": 1.0, "MRR@10": 0.5,
                                      "Hit@10": 1.0}
    assert metrics([row], {"q": ["x"]})["Hit@10"] == 0.0
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


def test_pdf_page_continuation_keeps_previous_section(tmp_path, monkeypatch):
    path = tmp_path / "policy.pdf"
    path.write_bytes(b"fixture")
    source = RegisteredSource("policy", path, "示范条款", "public_consultation_draft",
                              "source", "https://example.org/policy", None,
                              documents.file_sha256(path))
    monkeypatch.setattr(documents, "_pages", lambda _: iter([
        (1, ["第一条 保险责任", "AAA"]),
        (2, ["BBB", "第二条 责任免除", "CCC"]),
    ]))
    chunks = parse_and_chunk(source)
    assert [(chunk["page"], chunk["section"], chunk["text"]) for chunk in chunks] == [
        (1, "第一条 保险责任", "AAA"),
        (2, "第一条 保险责任", "BBB"),
        (2, "第二条 责任免除", "CCC"),
    ]


def test_existing_milvus_index_reuse_requires_config_and_code_hash(tmp_path, monkeypatch):
    corpus = tmp_path / "corpus.jsonl"
    corpus.write_text('{"text":"fixture"}\n', encoding="utf-8")
    manifest = tmp_path / "model.json"
    manifest.write_text(json.dumps({"revision": "test-revision", "files": [
        {"filename": "pytorch_model.bin", "sha256": "weights-sha"}]}), encoding="utf-8")
    monkeypatch.setattr(milvus_store, "MODEL_MANIFEST", manifest)

    class ExistingStore:
        def has_collection(self, name):
            return True

        def get_collection_stats(self, name):
            return {"row_count": 1}

        def load_collection(self, name):
            pass

    marker = {
        "collection": "test_index",
        "corpus_sha256": milvus_store.file_sha256(corpus),
        "model_revision": "test-revision", "model_weights_sha256": "weights-sha",
        "max_index_chars": milvus_store.MAX_INDEX_CHARS,
        "max_model_tokens": milvus_store.MAX_MODEL_TOKENS,
        "schema_version": milvus_store.SCHEMA_VERSION,
        "vector_dim": milvus_store.VECTOR_DIM,
        "index_config": milvus_store.INDEX_CONFIG,
        "index_code_sha256": milvus_store.file_sha256(Path(milvus_store.__file__)),
        "model_files_sha256": {"pytorch_model.bin": "weights-sha"},
        "encoder_dependencies": {name: milvus_store.version(name) for name in
                                 ("FlagEmbedding", "transformers", "torch")},
        "row_count": 1,
    }
    marker_path = tmp_path / "test_index_index.json"
    marker_path.write_text(json.dumps(marker), encoding="utf-8")
    assert milvus_store.index_jsonl(ExistingStore(), None, "test_index", corpus,
                                   benchmark=True) == marker

    marker["index_config"] = {"dense": {"index_type": "FLAT"}}
    marker_path.write_text(json.dumps(marker), encoding="utf-8")
    with pytest.raises(ValueError, match="index code and config"):
        milvus_store.index_jsonl(ExistingStore(), None, "test_index", corpus,
                                benchmark=True)

    marker["index_config"] = milvus_store.INDEX_CONFIG
    marker["index_code_sha256"] = "old-code-hash"
    marker_path.write_text(json.dumps(marker), encoding="utf-8")
    with pytest.raises(ValueError, match="index code and config"):
        milvus_store.index_jsonl(ExistingStore(), None, "test_index", corpus,
                                benchmark=True)
