"""BGE-M3 dense/sparse vectors in Milvus 2.6 with explicit collection schemas."""

from datetime import datetime, timezone
from importlib.metadata import version
import json
from pathlib import Path

from pymilvus import DataType, MilvusClient

from packages.domain.hf_download import file_sha256

MODEL_DIR = Path("data/models/bge-m3")
MODEL_MANIFEST = Path("data/manifests/bge-m3_download.json")
BENCHMARK_COLLECTION = "insur_qa_corpus"
KNOWLEDGE_COLLECTION = "insurance_knowledge"
MAX_INDEX_CHARS = 1800
MAX_MODEL_TOKENS = 512
VECTOR_DIM = 1024
SCHEMA_VERSION = 1
SPARSE_INDEX = {"index_type": "SPARSE_INVERTED_INDEX", "metric_type": "IP",
                "params": {"drop_ratio_build": 0.0}}


def index_config(benchmark: bool) -> dict:
    """Use exact dense search for evaluation; retain HNSW for runtime knowledge."""
    dense = ({"index_type": "FLAT", "metric_type": "COSINE", "params": {}}
             if benchmark else
             {"index_type": "HNSW", "metric_type": "COSINE",
              "params": {"M": 16, "efConstruction": 200}})
    return {"dense": dense, "sparse": SPARSE_INDEX}


def index_text(value: str) -> str:
    """Use identical model input in every ablation; preserve full text in JSONL."""
    return value[:MAX_INDEX_CHARS]


def embedding_model():
    from FlagEmbedding import BGEM3FlagModel

    if not MODEL_MANIFEST.is_file():
        raise FileNotFoundError("download and verify BGE-M3 before indexing")
    return BGEM3FlagModel(str(MODEL_DIR), use_fp16=True, devices=["cuda:0"])


def encode(model, texts: list[str], batch_size: int = 16) -> list[dict]:
    result = model.encode(texts, batch_size=batch_size, max_length=MAX_MODEL_TOKENS,
                          return_dense=True, return_sparse=True,
                          return_colbert_vecs=False)
    vectors = []
    for dense, sparse in zip(result["dense_vecs"], result["lexical_weights"], strict=True):
        lexical = {int(key): float(weight) for key, weight in sparse.items()
                   if float(weight) > 0}
        if len(dense) != VECTOR_DIM or not lexical:
            raise ValueError("invalid BGE-M3 dense/sparse output")
        vectors.append({"dense_vector": dense.tolist(), "sparse_vector": lexical})
    return vectors


def client(uri: str = "http://127.0.0.1:19530") -> MilvusClient:
    result = MilvusClient(uri=uri)
    version = result.get_server_version()
    if not version.startswith("2.6."):
        raise RuntimeError(f"M2 requires Milvus 2.6.x, got {version}")
    return result


def create_collection(store: MilvusClient, name: str, *, benchmark: bool) -> None:
    schema = store.create_schema(auto_id=False, enable_dynamic_field=False)
    schema.add_field(field_name="pk", datatype=DataType.VARCHAR, is_primary=True, max_length=64)
    for field, length in (("doc_id", 128), ("chunk_id", 64), ("title", 512),
                          ("section", 512), ("text", 8192), ("source_type", 64),
                          ("source_name", 512), ("source_url", 2048),
                          ("product_code", 64), ("content_hash", 64),
                          ("created_at", 40)):
        schema.add_field(field_name=field, datatype=DataType.VARCHAR, max_length=length)
    schema.add_field(field_name="page", datatype=DataType.INT64)
    schema.add_field(field_name="dense_vector", datatype=DataType.FLOAT_VECTOR, dim=VECTOR_DIM)
    schema.add_field(field_name="sparse_vector", datatype=DataType.SPARSE_FLOAT_VECTOR)
    indexes = store.prepare_index_params()
    config = index_config(benchmark)
    for field, settings in (("dense_vector", config["dense"]),
                            ("sparse_vector", config["sparse"])):
        indexes.add_index(field_name=field, **settings)
    store.create_collection(collection_name=name, schema=schema, index_params=indexes)


def _entity(record: dict, vector: dict, created_at: str, benchmark: bool) -> dict:
    if benchmark:
        pid = record["passage_id"]
        source = {"pk": pid, "doc_id": pid, "chunk_id": pid[:24],
                  "title": "Insur-QA passage", "section": "provided passage",
                  "page": -1, "source_type": "benchmark_passage",
                  "source_name": "FrankRin/Insur-QA", "source_url":
                  "https://huggingface.co/datasets/FrankRin/Insur-QA",
                  "product_code": "", "content_hash": pid}
    else:
        source = {"pk": record["chunk_id"], "doc_id": record["doc_id"],
                  "chunk_id": record["chunk_id"], "title": record["title"],
                  "section": record["section"], "page": record["page"] or -1,
                  "source_type": record["source_type"],
                  "source_name": record["source_name"],
                  "source_url": record["source_url"],
                  "product_code": record["product_code"] or "",
                  "content_hash": record["content_hash"]}
    return {**source, "text": index_text(record["text"]),
            "created_at": created_at, **vector}


def index_jsonl(store: MilvusClient, model, name: str, corpus: Path,
                *, benchmark: bool, rebuild: bool = False,
                batch_size: int = 128) -> dict:
    """Idempotent only when the corpus/model marker matches; no silent stale reuse."""
    marker_path = corpus.parent / f"{name}_index.json"
    model_manifest = json.loads(MODEL_MANIFEST.read_text(encoding="utf-8"))
    expected = {"collection": name, "corpus_sha256": file_sha256(corpus),
                "model_revision": model_manifest["revision"],
                "model_weights_sha256": next(item["sha256"] for item in model_manifest["files"]
                                             if item["filename"] == "pytorch_model.bin"),
                "max_index_chars": MAX_INDEX_CHARS,
                "max_model_tokens": MAX_MODEL_TOKENS,
                "schema_version": SCHEMA_VERSION,
                "vector_dim": VECTOR_DIM,
                "index_config": index_config(benchmark),
                "index_code_sha256": file_sha256(Path(__file__)),
                "model_files_sha256": {item["filename"]: item["sha256"]
                                        for item in model_manifest["files"]},
                "encoder_dependencies": {name: version(name) for name in
                                         ("FlagEmbedding", "transformers", "torch")}}
    exists = store.has_collection(name)
    if exists and not rebuild:
        if not marker_path.is_file():
            raise ValueError(f"collection {name} exists without matching index marker")
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        if any(marker.get(key) != value for key, value in expected.items()):
            raise ValueError(f"collection {name} does not match current corpus/model/index code and config")
        if int(store.get_collection_stats(name)["row_count"]) != marker["row_count"]:
            raise ValueError(f"collection {name} row count changed")
        store.load_collection(name)
        return marker
    if exists:
        store.drop_collection(name)
    create_collection(store, name, benchmark=benchmark)
    created_at = datetime.now(timezone.utc).isoformat()
    batch = []
    count = 0
    truncated = 0

    def flush_batch() -> None:
        nonlocal count
        if not batch:
            return
        vectors = encode(model, [index_text(row["text"]) for row in batch], 16)
        entities = [_entity(row, vector, created_at, benchmark)
                    for row, vector in zip(batch, vectors, strict=True)]
        store.insert(collection_name=name, data=entities)
        count += len(batch)
        batch.clear()
        if count % 1000 < batch_size:
            print(f"indexed {name}: {count}", flush=True)

    with corpus.open(encoding="utf-8") as stream:
        for line in stream:
            row = json.loads(line)
            truncated += len(row["text"]) > MAX_INDEX_CHARS
            batch.append(row)
            if len(batch) >= batch_size:
                flush_batch()
    flush_batch()
    store.flush(name)
    store.load_collection(name)
    actual = int(store.get_collection_stats(name)["row_count"])
    if actual != count:
        raise RuntimeError(f"Milvus row count {actual} != inserted {count}")
    marker = {**expected, "row_count": count, "truncated_passages": truncated,
              "created_at": created_at, "milvus_version": store.get_server_version()}
    marker_path.write_text(json.dumps(marker, ensure_ascii=False, sort_keys=True, indent=2)
                           + "\n", encoding="utf-8")
    return marker
