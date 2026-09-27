import hashlib
import json
from pathlib import Path

import pytest
import yaml

from packages.domain.hf_download import DATASETS, file_sha256, verify_manifest
from scripts import download_m2_models


def test_provenance_detects_modified_download(tmp_path: Path):
    file = tmp_path / "example.json"
    file.write_text("{}", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"files": [{"local_path": str(file), "bytes": 2,
        "sha256": hashlib.sha256(b"{}").hexdigest(),
        "upstream_sha256": hashlib.sha256(b"{}").hexdigest()}]}), encoding="utf-8")
    verify_manifest(manifest)
    assert file_sha256(file) == hashlib.sha256(b"{}").hexdigest()
    file.write_text("{ }", encoding="utf-8")
    with pytest.raises(ValueError, match="verification failed"):
        verify_manifest(manifest)


def test_dataset_file_lists_and_public_draft_status():
    assert len(DATASETS["insqabench"]["files"]) == 5
    assert len(DATASETS["insur_qa"]["files"]) == 2
    manifest = yaml.safe_load(Path("data/manifests/public_docs.yaml").read_text(encoding="utf-8"))
    documents = manifest["documents"]
    assert len(documents) == 5
    assert len({item["id"] for item in documents}) == 5
    assert all(item["status"] == "draft" for item in documents)
    assert all(len(item["sha256"]) == 64 and item["usage_note"] and
               item["license"] == "not_stated_on_source_page" and
               "retrieved_at" not in item for item in documents)
    assert all(item["source_page"].startswith("https://") and
               item["file_url"].startswith("https://www.iachina.cn/module/download/")
               for item in documents)


def test_model_verification_checks_actual_bytes_and_load_path(tmp_path, monkeypatch):
    model_dir = tmp_path / "models" / "fixture"
    model_dir.mkdir(parents=True)
    file = model_dir / "weights.bin"
    file.write_bytes(b"original")
    manifest_dir = tmp_path / "manifests"
    manifest_dir.mkdir()
    manifest_path = manifest_dir / "fixture_download.json"
    payload = {"model": "fixture", "repo_id": "BAAI/fixture", "revision": "fixed-revision",
               "files": [{"filename": "weights.bin", "local_path": str(file),
                          "bytes": file.stat().st_size, "sha256": file_sha256(file)}]}
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setitem(download_m2_models.MODELS, "fixture",
                        {"repo_id": "BAAI/fixture", "revision": "fixed-revision",
                         "files": ("weights.bin",)})
    assert download_m2_models.verify_local_model("fixture", tmp_path) == payload

    file.write_bytes(b"modified")
    with pytest.raises(ValueError, match="provenance verification failed"):
        download_m2_models.verify_local_model("fixture", tmp_path)
    file.write_bytes(b"original")

    other = tmp_path / "other" / "weights.bin"
    other.parent.mkdir()
    other.write_bytes(b"original")
    payload["files"][0]["local_path"] = str(other)
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="model file path mismatch"):
        download_m2_models.verify_local_model("fixture", tmp_path)
