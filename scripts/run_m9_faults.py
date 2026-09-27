"""M9 acceptance: real DeepSeek/MCP recovery plus the deterministic fault suite."""

import argparse
import ast
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import Connection

from packages.agent.m8_graph import build_m8_workflow
from packages.persistence.approvals import psycopg_url
from packages.persistence.artifacts import ArtifactStore
from packages.persistence.checkpoints import checkpoint_serializer
from packages.persistence.jobs import JobQueue
from packages.persistence.reviews import ReviewStore
from scripts.run_m7_demo import DEMO_QUERY


REPORT = Path("evaluation/reports/m9_fault_injection.json")
SOURCES = ("packages/agent/faults.py", "packages/agent/agents/roles.py",
           "packages/agent/m8_graph.py", "packages/agent/models.py",
           "packages/knowledge/retrieval.py", "packages/persistence/jobs.py",
           "services/mcp_knowledge/server.py", "apps/runner/worker.py",
           "scripts/run_m9_faults.py", "tests/fault/test_durable_faults.py",
           "tests/fault/test_fault_policy.py",
           "tests/fault/test_reranker_fallback.py")


async def setup(database_url: str) -> str:
    queue = JobQueue(database_url)
    artifacts = ArtifactStore(database_url)
    reviews = ReviewStore(database_url, artifacts)
    await queue.setup()
    await artifacts.setup()
    await reviews.setup()
    async with AsyncPostgresSaver.from_conn_string(
            psycopg_url(database_url), serde=checkpoint_serializer()) as saver:
        await saver.setup()
    with Connection.connect(database_url) as conn:
        if conn.execute("SELECT count(*) FROM agent_jobs").fetchone()[0] != 0:
            raise RuntimeError("M9 demo requires an empty dedicated database")
    run_id = str(uuid4())
    await queue.enqueue(run_id, "START", {"query": DEMO_QUERY})
    return run_id


async def inspect(database_url: str, run_id: str):
    queue = JobQueue(database_url)
    artifacts = ArtifactStore(database_url)
    reviews = ReviewStore(database_url, artifacts)
    async with AsyncPostgresSaver.from_conn_string(
            psycopg_url(database_url), serde=checkpoint_serializer()) as saver:
        async def no_lease():
            return None

        graph = build_m8_workflow(None, None, artifacts, reviews,
                                   checkpointer=saver, assert_lease=no_lease)
        snapshot = await graph.aget_state({"configurable": {"thread_id": run_id}})
    return snapshot, await queue.latest(run_id)


def worker(env: dict) -> dict:
    process = subprocess.run([sys.executable, "-m", "apps.runner.worker",
                              "--lease-seconds", "30"], env=env,
                             capture_output=True, text=True, timeout=900)
    if process.returncode:
        raise RuntimeError(f"M9 live worker failed: {process.returncode} "
                           f"{process.stderr[-2000:]}")
    return ast.literal_eval(process.stdout.strip())


async def approve(database_url: str, run_id: str, cycle: int, artifact) -> str:
    artifacts = ArtifactStore(database_url)
    reviews = ReviewStore(database_url, artifacts)
    queue = JobQueue(database_url)
    decision = await reviews.decide(run_id, cycle, artifact, status="APPROVED",
                                     reviewer_id="m9_acceptance_reviewer")
    await queue.enqueue(run_id, "RESUME", {"approval_id": decision.approval_id})
    return decision.approval_id


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=os.getenv("M9_DATABASE_URL"))
    parser.add_argument("--reader-url", default=os.getenv("M9_READER_DATABASE_URL"))
    parser.add_argument("--test-database-url", default=os.getenv("M9_TEST_DATABASE_URL"))
    parser.add_argument("--milvus-uri", default=os.getenv("M9_TEST_MILVUS_URI",
                                                        "http://127.0.0.1:19530"))
    args = parser.parse_args()
    if not args.database_url or not args.reader_url or not args.test_database_url:
        parser.error("demo, reader, and fault-test database URLs are required")
    base = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    if subprocess.check_output(["git", "status", "--porcelain"], text=True).strip():
        raise RuntimeError("M9 acceptance requires clean committed source")
    run_id = asyncio.run(setup(args.database_url))
    common = {**os.environ, "M8_DATABASE_URL": args.database_url,
              "M8_READER_DATABASE_URL": args.reader_url,
              "M8_MILVUS_URI": args.milvus_uri, "M8_PROVIDER": "deepseek",
              "PYTHONUNBUFFERED": "1"}
    injected = {**common, "FAULT_INJECTION_ENABLED": "1",
                "FAULT_CASE": "llm_timeout", "FAULT_STAGE": "planner",
                "FAULT_OCCURRENCES": "1"}
    first = worker(injected)
    snapshot, job = asyncio.run(inspect(args.database_url, run_id))
    if (first["status"] != "WAITING_APPROVAL" or first["fault_fired"] != 1
            or first["retry_events"] != [{"boundary": "llm:planner",
                                           "error_code": "dependency_timeout",
                                           "attempt": 1}]
            or not first["model_response_ids"]
            or any(item != "deepseek-flash" for item in first["model_response_ids"])
            or job["status"] != "WAITING_APPROVAL"
            or snapshot.values["status"] != "PASS"
            or snapshot.next != ("human_review",)
            or not any(task.interrupts for task in snapshot.tasks)
            or snapshot.values["trace"].count("planner") != 1):
        raise RuntimeError("live injected timeout did not recover to review")
    print(f"live timeout recovered to review: {run_id}", flush=True)
    approval_id = asyncio.run(approve(args.database_url, run_id,
        snapshot.values["cycle"], snapshot.values["refs"]["analysis"]))
    resumed = {**common, "FAULT_INJECTION_ENABLED": "0"}
    second = worker(resumed)
    final, final_job = asyncio.run(inspect(args.database_url, run_id))
    if (second["status"] != "COMPLETED" or second["model_response_ids"]
            or final.values["status"] != "PUBLISHED"
            or final.values["publication"]["approval_id"] != approval_id
            or final_job["status"] != "COMPLETED"
            or final.values["trace"].count("publish") != 1):
        raise RuntimeError("approved recovery did not publish exactly once")
    with Connection.connect(args.database_url) as conn:
        publication_count = conn.execute(
            "SELECT count(*) FROM m8_publications WHERE run_id=%s", (run_id,)
        ).fetchone()[0]
        stage_versions = {stage: versions for stage, versions in conn.execute("""
            SELECT stage, array_agg(version ORDER BY version)
            FROM run_artifacts WHERE run_id=%s GROUP BY stage
        """, (run_id,)).fetchall()}
    if (publication_count != 1 or stage_versions != {
            "sql": [1], "rag": [1], "analysis": [1], "verification": [1]}):
        raise RuntimeError("live PostgreSQL publication/artifact rows are inconsistent")

    fault_env = {**os.environ, "M9_TEST_DATABASE_URL": args.test_database_url,
                 "M9_TEST_MILVUS_URI": args.milvus_uri,
                 "FAULT_INJECTION_ENABLED": "0"}
    suite = subprocess.run([sys.executable, "-m", "pytest", "-q", "tests/fault"],
                           env=fault_env, capture_output=True, text=True, timeout=600)
    if suite.returncode or "passed" not in suite.stdout or "skipped" in suite.stdout:
        raise RuntimeError(f"fault suite failed: {suite.stdout[-2000:]} "
                           f"{suite.stderr[-1000:]}")
    summary = suite.stdout.strip().splitlines()[-1]
    report = {"generated_at": datetime.now(timezone.utc).isoformat(),
              "base_git_commit": base, "git_worktree_dirty": False,
              "code_sha256": {path: hashlib.sha256(Path(path).read_bytes()).hexdigest()
                              for path in SOURCES},
              "live_run_id": run_id, "thread_id": run_id,
              "live_fault_case": "llm_timeout once at planner",
              "live_first_worker": first, "live_resume_worker": second,
              "live_status_before_review": snapshot.values["status"],
              "live_final_status": final.values["status"],
              "live_trace": final.values["trace"],
              "stage_versions": stage_versions,
              "publication_row_count": publication_count,
              "fault_suite_summary": summary,
              "fault_suite_uses_stub_llm_for_deterministic_cases": True,
              "reranker_fault_uses_real_milvus_bge": True}
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True,
                                 indent=2) + "\n", encoding="utf-8")
    print(f"M9 acceptance PASS ({summary}); wrote {REPORT}", flush=True)


if __name__ == "__main__":
    main()
