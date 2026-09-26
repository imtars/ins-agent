"""Download only verified public consultation drafts without redistributing them."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def portable_path(path: Path) -> str:
    absolute = path.resolve()
    try:
        return str(absolute.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(absolute)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("data/manifests/public_docs.yaml"))
    parser.add_argument("--output-dir", type=Path, default=Path("data/raw/public"))
    parser.add_argument("--provenance", type=Path, default=Path("data/manifests/public_docs_download.json"))
    args = parser.parse_args()
    documents = yaml.safe_load(args.source.read_text(encoding="utf-8"))["documents"]
    opener = build_opener(ProxyHandler({}))
    args.output_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for document in documents:
        url = document["file_url"]
        if urlsplit(url).hostname != "www.iachina.cn" or document["status"] != "draft":
            raise ValueError(f"unverified document source: {document['id']}")
        suffix = "." + document["file_type"]
        target = args.output_dir / f"{document['id']}{suffix}"
        with opener.open(Request(url, headers={"User-Agent": "insurance-agent-harness/0.1"}),
                         timeout=60) as response:
            content = response.read()
            resolved_url = response.geturl()
        magic = b"%PDF" if suffix == ".pdf" else b"PK\x03\x04"
        if not content.startswith(magic):
            raise ValueError(f"unexpected file type for {document['id']}")
        actual_sha = hashlib.sha256(content).hexdigest()
        expected_sha = document.get("sha256")
        if expected_sha and actual_sha != expected_sha:
            raise ValueError(f"source content changed for {document['id']}; review manifest")
        target.write_bytes(content)
        result = {"id": document["id"], "source_page": document["source_page"],
                  "file_url": url, "resolved_url": resolved_url,
                  "source_published_at": document["source_published_at"],
                  "status": document["status"], "retrieved_at": datetime.now(timezone.utc).isoformat(),
                  "local_path": portable_path(target), "bytes": len(content),
                  "sha256": actual_sha}
        results.append(result)
        print(f"verified {document['id']}: {len(content)} bytes, sha256={result['sha256']}", flush=True)
    args.provenance.parent.mkdir(parents=True, exist_ok=True)
    args.provenance.write_text(json.dumps({"documents": results}, ensure_ascii=False,
                                          sort_keys=True, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
