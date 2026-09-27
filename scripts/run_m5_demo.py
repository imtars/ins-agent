"""Run four real M5 LangGraph routes through M4 tools and record their traces."""

import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess

from fastmcp import Client, ClientGroup
from sqlalchemy.ext.asyncio import create_async_engine

from packages.agent.graph import build_workflow, run_query
from packages.domain.hf_download import file_sha256
from packages.llm.client import select_chat_client
from services.mcp_data.server import create_server as data_server
from services.mcp_knowledge.server import build_real_server as knowledge_server

REPORT_PATH = Path("evaluation/reports/m5_demo.json")
DEMOS = {
    "sql": ("产品 product_001 有多少张保单？", "SQL"),
    "rag": ("合成产品 product_006 的等待期是多少天？请给出条款证据。", "RAG"),
    "both": ("产品 product_006 在 2026 年第二季度的已发生赔付率是多少？该产品等待期是多少天？", "BOTH"),
    "report": ("请生成 2026 年第二季度合成产品 product_006 的业务与条款综合简报："
               "列出已发生赔付率、理赔频率和等待期，附上数据和条款来源。", "REPORT"),
}


async def run(reader_url: str, milvus_uri: str, provider: str, only: str | None) -> dict:
    model = select_chat_client(provider)
    engine = create_async_engine(reader_url)
    try:
        knowledge = knowledge_server(milvus_uri)
        async with ClientGroup({"data": Client(data_server(engine)),
                                "knowledge": Client(knowledge)}) as tools:
            workflow = build_workflow(model, tools)
            records = []
            for name, (query, expected_route) in DEMOS.items():
                if only and name != only:
                    continue
                state = await run_query(workflow, query)
                record = {"name": name, "expected_route": expected_route,
                          "run_id": state["run_id"], "query": query,
                          "plan": state["plan"].model_dump(),
                          "trace": state["trace"],
                          "sql_results": [item.model_dump() for item in
                                          state.get("sql_results", [])],
                          "rag_results": [item.model_dump() for item in
                                          state.get("rag_results", [])],
                          "analysis": state["analysis"].model_dump(),
                          "verification": state["verification"].model_dump(),
                          "status": state["status"]}
                records.append(record)
                print(f"{name}: route={record['plan']['route']} "
                      f"sql={len(record['sql_results'])} rag={len(record['rag_results'])} "
                      f"status={record['status']} trace={record['trace']} "
                      f"issues={record['verification']['issues']}", flush=True)
                if (record["plan"]["route"] != expected_route
                        or bool(record["sql_results"]) != (expected_route != "RAG")
                        or bool(record["rag_results"]) != (expected_route != "SQL")
                        or record["trace"].count("synthesis") != 1
                        or record["trace"].count("verifier") != 1
                        or record["status"] == "BLOCK"):
                    raise RuntimeError(f"M5 demo route or verification failed: {name}")
    finally:
        await engine.dispose()
    return {"generated_at": datetime.now(timezone.utc).isoformat(),
            "provider": model.provider, "requested_model": model.model,
            "observed_response_model_ids": sorted({item for item in model.response_models
                                                   if item is not None}),
            "response_model_missing_count": model.response_models.count(None),
            "model_roles_called": model.roles_called,
            "json_parse_failures": model.parse_failures,
            "milvus_uri": milvus_uri, "case_count": len(records), "cases": records}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reader-url", default=os.environ.get("M3_READER_DATABASE_URL"))
    parser.add_argument("--milvus-uri", default=os.environ.get("MILVUS_URI",
                                                                "http://127.0.0.1:19530"))
    parser.add_argument("--provider", choices=("auto", "proxy", "deepseek"), default="auto")
    parser.add_argument("--case", choices=tuple(DEMOS))
    args = parser.parse_args()
    if not args.reader_url:
        raise ValueError("M3_READER_DATABASE_URL is required")
    report = asyncio.run(run(args.reader_url, args.milvus_uri, args.provider, args.case))
    if args.case:
        return  # Ad hoc pilot results are not written as the four-route acceptance report.
    report["base_git_commit"] = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], text=True).strip()
    report["git_worktree_dirty"] = bool(subprocess.check_output(
        ["git", "status", "--porcelain"], text=True).strip())
    report["code_sha256"] = {str(path): file_sha256(path) for path in (
        Path("packages/agent/models.py"), Path("packages/agent/agents/roles.py"),
        Path("packages/agent/graph.py"), Path("packages/llm/client.py"),
        Path("services/mcp_data/server.py"), Path("services/mcp_knowledge/server.py"),
        Path("scripts/run_m5_demo.py"))}
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
                           + "\n", encoding="utf-8")
    print(f"wrote {REPORT_PATH}")


if __name__ == "__main__":
    main()
