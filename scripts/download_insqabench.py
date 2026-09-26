"""Download five named InsQABench JSON files with provenance."""

import argparse
import os
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default="https://huggingface.co")
    parser.add_argument("--revision")
    parser.add_argument("--transport", choices=("direct", "system"), default="direct",
                        help="direct ignores proxy settings; system permits configured proxy")
    parser.add_argument("--output-dir", type=Path, default=Path("data/raw/benchmarks/insqabench"))
    parser.add_argument("--manifest", type=Path, default=Path("data/manifests/insqabench_download.json"))
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    from packages.domain.hf_download import download_dataset, verify_manifest
    if args.verify:
        verify_manifest(args.manifest)
        print("provenance verified")
        return
    if args.transport == "direct":
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            os.environ.pop(key, None)
    os.environ["HF_HUB_DISABLE_XET"] = "1"
    import httpx
    from huggingface_hub import set_client_factory
    set_client_factory(lambda: httpx.Client(trust_env=args.transport == "system",
                                            follow_redirects=True, timeout=60))
    download_dataset(key="insqabench", output_dir=args.output_dir,
                     manifest_path=args.manifest, endpoint=args.endpoint,
                     revision=args.revision, transport=args.transport)


if __name__ == "__main__":
    main()
