"""Download pinned BGE models file by file and record verified local provenance."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil

from packages.domain.hf_download import file_sha256, portable_path, verify_manifest

MODELS = {
    "bge-m3": {
        "repo_id": "BAAI/bge-m3",
        "revision": "5617a9f61b028005a4858fdac845db406aefb181",
        "files": ("config.json", "pytorch_model.bin", "colbert_linear.pt", "sparse_linear.pt",
                  "tokenizer.json", "tokenizer_config.json", "sentencepiece.bpe.model",
                  "special_tokens_map.json"),
    },
    "bge-reranker-v2-m3": {
        "repo_id": "BAAI/bge-reranker-v2-m3",
        "revision": "953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e",
        "files": ("config.json", "model.safetensors", "tokenizer.json",
                  "tokenizer_config.json", "sentencepiece.bpe.model",
                  "special_tokens_map.json"),
    },
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default="https://huggingface.co")
    parser.add_argument("--transport", choices=("direct", "system"), default="direct")
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    if args.verify:
        for name in MODELS:
            verify_manifest(Path(f"data/manifests/{name}_download.json"))
        print("model provenance verified")
        return
    if args.transport == "direct":
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            os.environ.pop(key, None)
    os.environ["HF_HUB_DISABLE_XET"] = "1"
    import httpx
    from huggingface_hub import HfApi, hf_hub_download, set_client_factory

    set_client_factory(lambda: httpx.Client(trust_env=args.transport == "system",
                                            follow_redirects=True, timeout=120))
    for name, spec in MODELS.items():
        info = HfApi(endpoint=args.endpoint).model_info(spec["repo_id"],
                                                      revision=spec["revision"],
                                                      files_metadata=True)
        if info.sha != spec["revision"]:
            raise ValueError(f"revision mismatch for {name}")
        siblings = {item.rfilename: item for item in info.siblings}
        if not set(spec["files"]) <= siblings.keys():
            raise ValueError(f"missing required model files for {name}")
        directory = Path("data/models") / name
        directory.mkdir(parents=True, exist_ok=True)
        files = []
        for filename in spec["files"]:
            sibling = siblings[filename]
            cached = Path(hf_hub_download(repo_id=spec["repo_id"], filename=filename,
                                          revision=spec["revision"], endpoint=args.endpoint))
            target = directory / filename
            temporary = directory / f".{filename}.partial"
            try:
                shutil.copyfile(cached, temporary)
                actual_sha = file_sha256(temporary)
                expected_sha = sibling.lfs.sha256 if sibling.lfs else None
                if sibling.size is not None and temporary.stat().st_size != sibling.size:
                    raise ValueError(f"size mismatch for {name}/{filename}")
                if expected_sha and actual_sha != expected_sha:
                    raise ValueError(f"upstream hash mismatch for {name}/{filename}")
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
            files.append({"filename": filename, "local_path": portable_path(target),
                          "bytes": target.stat().st_size, "sha256": actual_sha,
                          "upstream_sha256": expected_sha})
            print(f"verified {name}/{filename}: {target.stat().st_size} bytes", flush=True)
        manifest = {"model": name, "repo_id": spec["repo_id"], "revision": info.sha,
                    "endpoint": args.endpoint, "transport": args.transport,
                    "retrieved_at": datetime.now(timezone.utc).isoformat(), "files": files}
        path = Path("data/manifests") / f"{name}_download.json"
        path.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                        encoding="utf-8")
        verify_manifest(path)


if __name__ == "__main__":
    main()
