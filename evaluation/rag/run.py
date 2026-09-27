"""Run four real Milvus/BGE-M3 retrieval ablations on the fixed Insur-QA holdout."""

import argparse
from datetime import datetime, timezone
from importlib.metadata import version
import json
from pathlib import Path
import subprocess
import time

from packages.domain.hf_download import file_sha256
from packages.knowledge.documents import build_knowledge_corpus
from packages.knowledge.retrieval import rrf, search_vector
from packages.knowledge.milvus_store import (
    BENCHMARK_COLLECTION, KNOWLEDGE_COLLECTION,
    MAX_MODEL_TOKENS, client, embedding_model, encode, index_jsonl
)
from scripts.download_m2_models import verify_local_model
from evaluation.rag.dataset import OUTPUT, SOURCE, SOURCE_MANIFEST, prepare

REPORT_DIR = Path("evaluation/reports")
TOP_K = 20
RRF_K = 60
MODES = ("dense", "sparse", "hybrid", "hybrid_rerank")


def metrics(rows: list[dict], rankings: dict[str, list[str]]) -> dict:
    if set(rankings) != {row["query_id"] for row in rows}:
        raise ValueError("rankings and holdout query IDs differ")
    totals = {1: 0.0, 5: 0.0, 10: 0.0}
    reciprocal = 0.0
    hit_at_10 = 0
    for row in rows:
        gold = set(row["positive_ids"])
        if not gold:
            raise ValueError("holdout query without a positive passage")
        ranked = rankings[row["query_id"]]
        if len(ranked) != len(set(ranked)):
            raise ValueError("duplicate passage ID in ranked results")
        for k in totals:
            totals[k] += len(gold.intersection(ranked[:k])) / len(gold)
        reciprocal += next((1 / rank for rank, passage_id in enumerate(ranked[:10], 1)
                            if passage_id in gold), 0.0)
        hit_at_10 += bool(gold.intersection(ranked[:10]))
    n = len(rows)
    if n == 0:
        raise ValueError("empty holdout")
    return {"Recall@1": totals[1] / n, "Recall@5": totals[5] / n,
            "Recall@10": totals[10] / n, "MRR@10": reciprocal / n,
            "Hit@10": hit_at_10 / n}


def _search(store, vector: dict, field: str) -> list[dict]:
    return search_vector(store, BENCHMARK_COLLECTION, vector, field, limit=TOP_K)


def _rankings(store, model, holdout: list[dict]) -> dict[str, dict[str, list[str]]]:
    from FlagEmbedding import FlagReranker

    vectors = encode(model, [row["query"] for row in holdout], batch_size=16)
    rankings = {mode: {} for mode in MODES}
    hybrid_hits = {}
    rerank_pairs = []
    offsets = {}
    for index, (row, vector) in enumerate(zip(holdout, vectors, strict=True), start=1):
        dense = _search(store, vector, "dense_vector")
        sparse = _search(store, vector, "sparse_vector")
        hybrid = rrf(dense, sparse, k=RRF_K, limit=TOP_K)
        query_id = row["query_id"]
        rankings["dense"][query_id] = [str(hit["id"]) for hit in dense]
        rankings["sparse"][query_id] = [str(hit["id"]) for hit in sparse]
        rankings["hybrid"][query_id] = [str(hit["id"]) for hit in hybrid]
        hybrid_hits[query_id] = hybrid
        offsets[query_id] = (len(rerank_pairs), len(hybrid))
        rerank_pairs.extend([[row["query"], hit["entity"]["text"]] for hit in hybrid])
        if index % 32 == 0:
            print(f"searched {index}/{len(holdout)} holdout queries", flush=True)
    if not rerank_pairs:
        raise RuntimeError("Milvus returned no candidates")
    if not Path("data/manifests/bge-reranker-v2-m3_download.json").is_file():
        raise FileNotFoundError("download the BGE reranker before evaluation")
    reranker = FlagReranker(str(Path("data/models/bge-reranker-v2-m3")),
                           use_fp16=True, devices=["cuda:0"])
    scores = reranker.compute_score(rerank_pairs, batch_size=16,
                                    max_length=MAX_MODEL_TOKENS)
    if len(scores) != len(rerank_pairs):
        raise RuntimeError("reranker returned the wrong number of scores")
    for row in holdout:
        query_id = row["query_id"]
        start, count = offsets[query_id]
        candidates = hybrid_hits[query_id]
        ranking = sorted(range(count), key=lambda i: (-float(scores[start + i]), i,
                                                    str(candidates[i]["id"])))
        rankings["hybrid_rerank"][query_id] = [str(candidates[i]["id"])
                                                 for i in ranking]
    return rankings


def run(uri: str, rebuild: bool = False) -> dict:
    model_info = verify_local_model("bge-m3")
    reranker_info = verify_local_model("bge-reranker-v2-m3")
    provenance = json.loads(SOURCE_MANIFEST.read_text(encoding="utf-8"))
    source_info = next(item for item in provenance["files"]
                       if item["filename"] == SOURCE.name)
    dataset = prepare(expected_source_sha256=source_info["sha256"])
    knowledge = build_knowledge_corpus()
    store = client(uri)
    model = embedding_model()
    benchmark_index = index_jsonl(store, model, BENCHMARK_COLLECTION,
                                  OUTPUT / "corpus.jsonl", benchmark=True,
                                  rebuild=rebuild)
    knowledge_index = index_jsonl(store, model, KNOWLEDGE_COLLECTION,
                                  Path(knowledge["corpus_path"]), benchmark=False,
                                  rebuild=rebuild)
    holdout = [json.loads(line) for line in (OUTPUT / "holdout.jsonl")
               .read_text(encoding="utf-8").splitlines()]
    if len(holdout) != dataset["holdout_size"]:
        raise RuntimeError("prepared holdout count differs from manifest")
    started = time.monotonic()
    rankings = _rankings(store, model, holdout)
    elapsed = time.monotonic() - started
    result = {mode: metrics(holdout, rankings[mode]) for mode in MODES}
    try:
        base_git_commit = subprocess.check_output(["git", "rev-parse", "HEAD"],
                                                  text=True).strip()
        git_dirty = bool(subprocess.check_output(["git", "status", "--porcelain"],
                                                 text=True).strip())
    except (OSError, subprocess.CalledProcessError):
        base_git_commit = None
        git_dirty = None
    code_files = ("evaluation/rag/dataset.py", "evaluation/rag/run.py",
                  "packages/knowledge/documents.py", "packages/knowledge/milvus_store.py",
                  "packages/knowledge/retrieval.py", "scripts/download_m2_models.py",
                  "packages/domain/hf_download.py")
    report = {
        "name": "Insur-QA local query holdout, full deduplicated passage corpus",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "base_git_commit": base_git_commit,
        "git_worktree_dirty": git_dirty,
        "code_sha256": {name: file_sha256(Path(name)) for name in code_files},
        "dataset_revision": provenance["revision"],
        "dataset_manifest": dataset,
        "model_revision": model_info["revision"],
        "reranker_revision": reranker_info["revision"],
        "embedder_files_sha256": {item["filename"]: item["sha256"]
                                  for item in model_info["files"]},
        "reranker_files_sha256": {item["filename"]: item["sha256"]
                                  for item in reranker_info["files"]},
        "software_versions": {name: version(name) for name in
                              ("FlagEmbedding", "pymilvus", "torch", "transformers")},
        "benchmark_index": benchmark_index,
        "knowledge_index": knowledge_index,
        "knowledge_sources": knowledge,
        "query_count": len(holdout),
        "candidate_limit_each_search": TOP_K,
        "rrf_k": RRF_K,
        "rerank_input": "hybrid RRF top 20",
        "rerank_output": "top 10",
        "metric_definition": "Recall@k is the macro mean of the per-query fraction of all positive IDs found at k. MRR@10 uses the first positive ID and zero beyond rank 10. Hit@10 is the fraction of queries with at least one positive ID in the top 10.",
        "caveats": ["Source is labeled retriever training data, not an official test split.",
                    "The 256-query holdout is selected by a fixed hash rule; remaining queries are development data.",
                    "Corpus includes all unique positive and negative passages from all source rows; a passage can be positive for one query and negative for another.",
                    "Passages above 1800 Unicode characters are truncated for indexing and reranking; full source text remains in the local processed corpus.",
                    "Public benchmark and model pretraining overlap cannot be ruled out."],
        "evaluation_seconds": elapsed,
        "results": result,
    }
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / "rag_ablation.json"
    path.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                    encoding="utf-8")
    rank_path = REPORT_DIR / "rag_rankings.jsonl"
    with rank_path.open("w", encoding="utf-8") as stream:
        for row in holdout:
            stream.write(json.dumps({"query_id": row["query_id"],
                                     "positive_ids": row["positive_ids"],
                                     "rankings": {mode: rankings[mode][row["query_id"]][:10]
                                                  for mode in MODES}}, sort_keys=True) + "\n")
    report["rankings_sha256"] = file_sha256(rank_path)
    path.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                    encoding="utf-8")
    lines = ["# Insur-QA 本地 holdout 检索消融", "",
             f"查询数：{len(holdout)}；完整去重语料：{dataset['unique_passages']} passages。", "",
             "| Pipeline | Recall@1 | Recall@5 | Recall@10 | MRR@10 | Hit@10 |",
             "| --- | ---: | ---: | ---: | ---: | ---: |"]
    for mode in MODES:
        values = result[mode]
        lines.append(f"| {mode} | {values['Recall@1']:.4f} | {values['Recall@5']:.4f} | "
                     f"{values['Recall@10']:.4f} | {values['MRR@10']:.4f} | "
                     f"{values['Hit@10']:.4f} |")
    lines.extend(["", "数字由 `python -m evaluation.rag.run` 实际生成；限制与参数见同名 JSON。", ""])
    (REPORT_DIR / "rag_ablation.md").write_text("\n".join(lines), encoding="utf-8")
    readme_path = Path("README.md")
    readme = readme_path.read_text(encoding="utf-8")
    start_marker = "<!-- RAG_ABLATION_START -->"
    end_marker = "<!-- RAG_ABLATION_END -->"
    if readme.count(start_marker) != 1 or readme.count(end_marker) != 1:
        raise ValueError("README RAG ablation markers are missing or duplicated")
    start = readme.index(start_marker) + len(start_marker)
    end = readme.index(end_marker)
    generated = "\n\n".join((f"本地 holdout：{len(holdout)} 题；完整语料：{dataset['unique_passages']} passages。",
                            "\n".join(lines[4:10])))
    readme_path.write_text(readme[:start] + "\n" + generated + "\n" + readme[end:],
                           encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--milvus-uri", default="http://127.0.0.1:19530")
    parser.add_argument("--rebuild", action="store_true")
    args = parser.parse_args()
    report = run(args.milvus_uri, args.rebuild)
    print(json.dumps({"query_count": report["query_count"],
                      "results": report["results"]}, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
