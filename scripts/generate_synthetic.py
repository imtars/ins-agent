"""Generate M1 operational rows and 12 matching synthetic product documents."""

import argparse
import asyncio
import json
from pathlib import Path

from packages.domain.config import get_settings
from packages.domain.synthetic import SyntheticConfig
from packages.persistence.synthetic_loader import generate_and_load


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=get_settings().database_url)
    parser.add_argument("--branches", type=int, default=12)
    parser.add_argument("--agents", type=int, default=180)
    parser.add_argument("--customers", type=int, default=10_000)
    parser.add_argument("--policies", type=int, default=30_000)
    parser.add_argument("--document-dir", type=Path, default=Path("data/synthetic/documents"))
    parser.add_argument("--manifest", type=Path, default=Path("data/synthetic/manifest.json"))
    args = parser.parse_args()
    config = SyntheticConfig(branches=args.branches, agents=args.agents,
                             customers=args.customers, policies=args.policies)
    manifest = asyncio.run(generate_and_load(
        args.database_url, config, args.document_dir, args.manifest
    ))
    print(json.dumps({"dataset_sha256": manifest["dataset_sha256"],
                      "table_counts": manifest["table_counts"],
                      "document_count": len(manifest["document_sha256"])},
                     ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
