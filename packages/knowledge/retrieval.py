"""Milvus dense/sparse search, reciprocal-rank fusion, and cited evidence."""

import re

from packages.knowledge.milvus_store import BENCHMARK_COLLECTION, encode

FIELDS = ("text", "doc_id", "chunk_id", "title", "section", "page",
          "source_type", "source_name", "source_url", "product_code", "content_hash")


def search_vector(store, collection: str, vector: dict, field: str,
                  *, limit: int = 20, product_code: str | None = None) -> list[dict]:
    if field == "dense_vector":
        params = {"metric_type": "COSINE", "params": ({}
                  if collection == BENCHMARK_COLLECTION else {"ef": 128})}
    elif field == "sparse_vector":
        params = {"metric_type": "IP", "params": {"drop_ratio_search": 0.0}}
    else:
        raise ValueError(f"unexpected vector field: {field}")
    kwargs = {}
    if product_code is not None:
        if not re.fullmatch(r"product_[0-9]{3}", product_code):
            raise ValueError("invalid synthetic product_code")
        kwargs["filter"] = f'product_code == "{product_code}"'
    hits = store.search(collection_name=collection, data=[vector[field]],
                        anns_field=field, search_params=params, limit=limit,
                        output_fields=list(FIELDS), **kwargs)[0]
    return [{"id": str(dict(hit)["pk"]), "entity": dict(hit)["entity"],
             "distance": dict(hit)["distance"]} for hit in hits]


def rrf(dense: list[dict], sparse: list[dict], k: int = 60,
        limit: int = 20) -> list[dict]:
    scores: dict[str, float] = {}
    entities: dict[str, dict] = {}
    for ranking in (dense, sparse):
        for rank, hit in enumerate(ranking, start=1):
            key = str(hit["id"])
            scores[key] = scores.get(key, 0.0) + 1 / (k + rank)
            entities[key] = hit
    return [entities[key] for key in sorted(scores, key=lambda key: (-scores[key], key))[:limit]]


def retrieve_evidence(store, embedder, reranker, query: str, *, collection: str,
                      product_code: str | None = None, limit: int = 5) -> list[dict]:
    evidence, _ = retrieve_evidence_with_status(
        store, embedder, reranker, query, collection=collection,
        product_code=product_code, limit=limit)
    return evidence


def retrieve_evidence_with_status(store, embedder, reranker, query: str, *,
                                  collection: str, product_code: str | None = None,
                                  limit: int = 5, injector=None) -> tuple[list[dict], list[str]]:
    if not query.strip() or limit < 1 or limit > 20:
        raise ValueError("query must be nonempty and limit must be 1–20")
    vector = encode(embedder, [query], batch_size=1)[0]
    dense = search_vector(store, collection, vector, "dense_vector",
                          product_code=product_code)
    sparse = search_vector(store, collection, vector, "sparse_vector",
                           product_code=product_code)
    candidates = rrf(dense, sparse)
    if not candidates:
        return [], []
    try:
        if injector is not None:
            injector.fire("reranker_failure", "reranker")
        scores = reranker.compute_score([[query, hit["entity"]["text"]]
                                         for hit in candidates], batch_size=16, max_length=512)
        if isinstance(scores, (float, int)):
            scores = [scores]
        if len(scores) != len(candidates):
            raise RuntimeError("reranker returned wrong number of scores")
        order = sorted(range(len(candidates)), key=lambda i: (-float(scores[i]), i,
                                                               candidates[i]["id"]))
        degraded_flags = []
    except Exception:
        # Candidates already passed dense+sparse search and RRF. Keep that order;
        # the flag distinguishes these fallback scores from reranker scores.
        fused_scores = {}
        for ranking in (dense, sparse):
            for rank, hit in enumerate(ranking, 1):
                key = str(hit["id"])
                fused_scores[key] = fused_scores.get(key, 0.0) + 1 / (60 + rank)
        scores = [fused_scores[hit["id"]] for hit in candidates]
        order = list(range(len(candidates)))
        degraded_flags = ["reranker_unavailable"]
    evidence = []
    for index in order[:limit]:
        item = candidates[index]["entity"]
        evidence.append({"evidence_id": f"{item['doc_id']}:{item['section']}:{item['chunk_id']}",
                         "doc_id": item["doc_id"], "chunk_id": item["chunk_id"],
                         "title": item["title"], "section": item["section"],
                         "page": None if item["page"] == -1 else item["page"],
                         "source_type": item["source_type"],
                         "source_name": item["source_name"],
                         "source_url": item["source_url"],
                         "product_code": item["product_code"] or None,
                         "content_hash": item["content_hash"],
                         "text": item["text"], "rerank_score": float(scores[index])})
    return evidence, degraded_flags
