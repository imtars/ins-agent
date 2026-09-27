"""Build a full deduplicated Insur-QA corpus and a frozen query holdout."""

import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import unicodedata

from packages.domain.hf_download import file_sha256

SOURCE = Path("data/raw/benchmarks/insur_qa/Insur-QA-Retriever.json")
SOURCE_MANIFEST = Path("data/manifests/insur_qa_download.json")
OUTPUT = Path("data/processed/rag/insur_qa")
HOLDOUT_SIZE = 256
SPLIT_RULE = "sha256(normalized_query) first 64 bits modulo 10 == 0; smallest 256 hashes"
NORMALIZATION = "Unicode NFKC followed by whitespace collapse and trim"


def normalize_text(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("query and passages must be strings")
    normalized = " ".join(unicodedata.normalize("NFKC", value).split())
    if not normalized:
        raise ValueError("empty query or passage")
    return normalized


def stable_id(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def write_jsonl(path: Path, rows) -> None:
    with path.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def prepare(source: Path = SOURCE, output: Path = OUTPUT,
            holdout_size: int = HOLDOUT_SIZE,
            expected_source_sha256: str | None = None) -> dict:
    source_sha = file_sha256(source)
    if expected_source_sha256 and source_sha != expected_source_sha256:
        raise ValueError("Insur-QA source SHA-256 differs from provenance")
    passages: dict[str, str] = {}
    positives: dict[str, set[str]] = defaultdict(set)
    queries: dict[str, str] = {}
    negative_ids: set[str] = set()
    positive_ids: set[str] = set()
    row_count = 0
    skipped_empty_queries = 0
    with source.open("r", encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                continue
            item = json.loads(line)
            row_count += 1
            if not isinstance(item["query"], str) or not item["query"].strip():
                skipped_empty_queries += 1
                continue
            query = normalize_text(item["query"])
            query_id = stable_id(query)
            queries[query_id] = query
            if not isinstance(item["pos"], list) or not item["pos"]:
                raise ValueError(f"missing positive passage on line {row_count + 1}")
            for field, destination in (("pos", positive_ids), ("neg", negative_ids)):
                if not isinstance(item[field], list):
                    raise ValueError(f"{field} must be a list")
                for raw in item[field]:
                    text = normalize_text(raw)
                    passage_id = stable_id(text)
                    passages[passage_id] = text
                    destination.add(passage_id)
                    if field == "pos":
                        positives[query_id].add(passage_id)
    if row_count == 0:
        raise ValueError("empty benchmark source")
    all_queries = [
        {"query_id": query_id, "query": query,
         "positive_ids": sorted(positives[query_id])}
        for query_id, query in sorted(queries.items())
    ]
    pool = [row for row in all_queries if int(row["query_id"][:16], 16) % 10 == 0]
    if len(pool) < holdout_size:
        raise ValueError(f"holdout pool has only {len(pool)} queries")
    holdout = pool[:holdout_size]
    holdout_ids = {row["query_id"] for row in holdout}
    development = [row for row in all_queries if row["query_id"] not in holdout_ids]
    if not all(set(row["positive_ids"]) <= passages.keys() for row in all_queries):
        raise AssertionError("gold positive absent from full corpus")
    output.mkdir(parents=True, exist_ok=True)
    corpus_path = output / "corpus.jsonl"
    holdout_path = output / "holdout.jsonl"
    development_path = output / "development.jsonl"
    write_jsonl(corpus_path, (dict(passage_id=pid, text=text)
                              for pid, text in sorted(passages.items())))
    write_jsonl(holdout_path, holdout)
    write_jsonl(development_path, development)
    manifest = {
        "source_sha256": source_sha,
        "normalization": NORMALIZATION,
        "split_rule": SPLIT_RULE,
        "holdout_size": holdout_size,
        "source_rows": row_count,
        "skipped_empty_queries": skipped_empty_queries,
        "unique_queries": len(all_queries),
        "unique_passages": len(passages),
        "unique_positive_passages": len(positive_ids),
        "unique_negative_passages": len(negative_ids),
        "passages_both_positive_and_negative_across_queries": len(positive_ids & negative_ids),
        "development_queries": len(development),
        "files": {path.name: {"sha256": file_sha256(path), "bytes": path.stat().st_size}
                  for path in (corpus_path, holdout_path, development_path)},
    }
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False,
                                                  sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    provenance = json.loads(SOURCE_MANIFEST.read_text(encoding="utf-8"))
    expected = next(item["sha256"] for item in provenance["files"]
                    if item["filename"] == args.source.name)
    print(json.dumps(prepare(args.source, args.output,
                             expected_source_sha256=expected), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
