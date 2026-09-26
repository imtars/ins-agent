import hashlib
import json
from pathlib import Path

import pytest
import yaml

from packages.domain.hf_download import DATASETS, file_sha256, verify_manifest


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
    assert all(len(item["sha256"]) == 64 and item["retrieved_at"] for item in documents)
    assert all(item["source_page"].startswith("https://") and
               item["file_url"].startswith("https://www.iachina.cn/module/download/")
               for item in documents)
