"""Search the allowlisted M2 knowledge collection and return traceable evidence."""

import argparse
import json

from packages.knowledge.milvus_store import KNOWLEDGE_COLLECTION, client, embedding_model
from packages.knowledge.retrieval import retrieve_evidence
from scripts.download_m2_models import verify_local_model


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("query")
    parser.add_argument("--product-code")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--milvus-uri", default="http://127.0.0.1:19530")
    args = parser.parse_args()
    from FlagEmbedding import FlagReranker

    verify_local_model("bge-m3")
    verify_local_model("bge-reranker-v2-m3")
    store = client(args.milvus_uri)
    if not store.has_collection(KNOWLEDGE_COLLECTION):
        raise RuntimeError("knowledge collection is absent; run M2 index/evaluation first")
    embedder = embedding_model()
    reranker = FlagReranker("data/models/bge-reranker-v2-m3",
                            use_fp16=True, devices=["cuda:0"])
    evidence = retrieve_evidence(store, embedder, reranker, args.query,
                                 collection=KNOWLEDGE_COLLECTION,
                                 product_code=args.product_code, limit=args.limit)
    print(json.dumps(evidence, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
