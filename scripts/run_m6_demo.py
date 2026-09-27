"""Accept four real M6 routes and record node/evidence contract provenance."""

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess

from packages.agent.contracts import NODE_CONTRACTS, validate_registry
from packages.agent.evidence import validate_claim_evidence
from packages.agent.models import AnalysisResult, RagArtifact, SqlArtifact, TaskPlan
from packages.domain.hf_download import file_sha256
from scripts.run_m5_demo import run

REPORT_PATH = Path("evaluation/reports/m6_contract_demo.json")
MAIN_NODES = {"planner", "fork", "sql_only", "rag_only", "sql_parallel",
              "rag_parallel", "synthesis", "verifier"}
SUBGRAPH_NODES = {"sql_subgraph.data_analyst", "rag_subgraph.knowledge_researcher"}


def schema_sha256(value: dict) -> str:
    data = json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode()
    return hashlib.sha256(data).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reader-url", default=os.environ.get("M3_READER_DATABASE_URL"))
    parser.add_argument("--milvus-uri", default=os.environ.get("MILVUS_URI",
                                                                "http://127.0.0.1:19530"))
    parser.add_argument("--provider", choices=("auto", "proxy", "deepseek"), default="auto")
    args = parser.parse_args()
    if not args.reader_url:
        raise ValueError("M3_READER_DATABASE_URL is required")
    validate_registry(MAIN_NODES, SUBGRAPH_NODES)
    report = asyncio.run(run(args.reader_url, args.milvus_uri, args.provider, None))
    report["model_request_parameters"] = (
        {"thinking": {"type": "enabled"}, "reasoning_effort": "high",
         "minimum_max_tokens": 8192}
        if report["provider"] == "DeepSeek" else
        {"thinking": "provider_default", "max_tokens": "role_specific"})
    for case in report["cases"]:
        issues = validate_claim_evidence(
            TaskPlan.model_validate(case["plan"]),
            [SqlArtifact.model_validate(item) for item in case["sql_results"]],
            [RagArtifact.model_validate(item) for item in case["rag_results"]],
            AnalysisResult.model_validate(case["analysis"]))
        case["deterministic_evidence_issues"] = issues
        if issues:
            raise RuntimeError(f"M6 evidence contract failed: {case['name']}: {issues}")
    report["contract_registry"] = [{
        "name": name,
        "requires": sorted(contract.requires),
        "optional": sorted(contract.optional),
        "produces": sorted(contract.produces),
        "upstream": list(contract.upstream),
        "input_schema_sha256": schema_sha256(contract.input_schema),
        "output_schema_sha256": schema_sha256(contract.output_schema),
    } for name, contract in sorted(NODE_CONTRACTS.items())]
    report["base_git_commit"] = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], text=True).strip()
    report["git_worktree_dirty"] = bool(subprocess.check_output(
        ["git", "status", "--porcelain"], text=True).strip())
    report["code_sha256"] = {str(path): file_sha256(path) for path in (
        Path("packages/agent/models.py"), Path("packages/agent/contracts.py"),
        Path("packages/agent/evidence.py"), Path("packages/agent/agents/roles.py"),
        Path("packages/agent/graph.py"), Path("scripts/run_m6_demo.py"),
        Path("services/mcp_data/server.py"), Path("services/mcp_knowledge/server.py"))}
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2,
                                      sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {REPORT_PATH}")


if __name__ == "__main__":
    main()
