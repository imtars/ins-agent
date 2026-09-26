"""Download named Hugging Face dataset files with independently verified provenance."""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil


DATASETS = {
    "insqabench": {
        "repo_id": "JaneDing2025/InsQABench",
        "files": ("clause_train.json", "clause_objective.json", "clause_subjective.json",
                  "db_train.json", "db_test.json"),
    },
    "insur_qa": {
        "repo_id": "FrankRin/Insur-QA",
        "files": ("Insur-QA-Retriever.json", "Insur-QA-LLM.json"),
    },
}
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def portable_path(path: Path) -> str:
    absolute = path.resolve()
    try:
        return str(absolute.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(absolute)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_manifest(manifest_path: Path) -> None:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for item in manifest["files"]:
        path = Path(item["local_path"])
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        if not path.is_file():
            raise FileNotFoundError(path)
        if path.stat().st_size != item["bytes"] or file_sha256(path) != item["sha256"]:
            raise ValueError(f"provenance verification failed: {path}")
        if item.get("upstream_sha256") and item["sha256"] != item["upstream_sha256"]:
            raise ValueError(f"upstream SHA-256 mismatch: {path}")


def download_dataset(*, key: str, output_dir: Path, manifest_path: Path,
                     endpoint: str = "https://huggingface.co",
                     revision: str | None = None,
                     transport: str = "direct") -> dict:
    """Resolve one immutable repo revision, download each named file, verify SHA-256."""
    from huggingface_hub import HfApi, hf_hub_download

    specification = DATASETS[key]
    repo_id = specification["repo_id"]
    info = HfApi(endpoint=endpoint).dataset_info(repo_id, revision=revision,
                                                 files_metadata=True)
    resolved_revision = info.sha
    siblings = {item.rfilename: item for item in info.siblings}
    missing = set(specification["files"]) - siblings.keys()
    if missing:
        raise ValueError(f"dataset files absent from {resolved_revision}: {sorted(missing)}")
    output_dir.mkdir(parents=True, exist_ok=True)
    files = []
    for filename in specification["files"]:
        item = siblings[filename]
        cached = Path(hf_hub_download(repo_id=repo_id, repo_type="dataset",
                                      filename=filename, revision=resolved_revision,
                                      endpoint=endpoint))
        destination = output_dir / filename
        temporary = output_dir / f".{filename}.partial"
        try:
            shutil.copyfile(cached, temporary)
            actual_size = temporary.stat().st_size
            actual_sha = file_sha256(temporary)
            expected_sha = item.lfs.sha256 if item.lfs else None
            if item.size is not None and actual_size != item.size:
                raise ValueError(f"size mismatch for {filename}: {actual_size} != {item.size}")
            if expected_sha and actual_sha != expected_sha:
                raise ValueError(f"upstream SHA-256 mismatch for {filename}")
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
        files.append({
            "filename": filename,
            "local_path": portable_path(destination),
            "source_url": f"{endpoint.rstrip('/')}/datasets/{repo_id}/resolve/{resolved_revision}/{filename}",
            "bytes": actual_size,
            "sha256": actual_sha,
            "upstream_sha256": expected_sha,
        })
        print(f"verified {filename}: {actual_size} bytes, sha256={actual_sha}", flush=True)
    manifest = {
        "dataset": key,
        "repo_id": repo_id,
        "repo_type": "dataset",
        "endpoint": endpoint,
        "transport": transport,
        "revision": resolved_revision,
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "files": files,
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                             encoding="utf-8")
    verify_manifest(manifest_path)
    return manifest
