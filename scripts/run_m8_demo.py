"""Live M8 acceptance: crash, lease takeover, RAG replay, bound approval."""

import argparse
import ast
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time

import httpx
from psycopg import Connection

from scripts.run_m7_demo import DEMO_QUERY


REPORT = Path("evaluation/reports/m8_runner_replay.json")
SOURCES = ("packages/agent/m8_graph.py", "packages/persistence/artifacts.py",
           "packages/persistence/jobs.py", "packages/persistence/reviews.py",
           "packages/persistence/checkpoints.py", "apps/api/m8.py",
           "apps/runner/worker.py", "scripts/run_m8_demo.py")


def port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def worker(env: dict, *, crash: bool = False) -> dict:
    command = [sys.executable, "-m", "apps.runner.worker", "--lease-seconds", "4"]
    if crash:
        command.append("--crash-after-planner")
    result = subprocess.run(command, env=env, capture_output=True, text=True,
                            timeout=900)
    if crash:
        if result.returncode != 73:
            raise RuntimeError(f"worker crash failpoint did not fire: {result.returncode} "
                               f"{result.stderr[-1200:]}")
        return {"exit_code": result.returncode}
    if result.returncode:
        raise RuntimeError(f"worker failed: {result.returncode} {result.stderr[-2500:]}")
    output = ast.literal_eval(result.stdout.strip())
    if output.get("status") not in {"WAITING_APPROVAL", "COMPLETED"}:
        raise RuntimeError("worker did not report a completed execution state")
    return {"exit_code": 0, "result": output}


def wait_api(client: httpx.Client, process: subprocess.Popen):
    for _ in range(360):
        if process.poll() is not None:
            raise RuntimeError(f"API exited at startup: {process.returncode}")
        try:
            if client.get("/health", timeout=2).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(.25)
    raise TimeoutError("M8 API did not start")


def get(client: httpx.Client, run_id: str) -> dict:
    response = client.get(f"/runs/{run_id}")
    response.raise_for_status()
    return response.json()


def review(client: httpx.Client, run_id: str, record: dict, token: str,
           decision: str, targets: list[str] | None = None) -> dict:
    ref = record["refs"]["analysis"]
    response = client.post(f"/runs/{run_id}/review", json={
        "decision": decision, "artifact_id": ref["artifact_id"],
        "artifact_version": ref["version"], "content_hash": ref["content_hash"],
        "rerun_targets": targets or []}, headers={"X-Review-Token": token})
    response.raise_for_status()
    return response.json()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=os.getenv("M8_DATABASE_URL"))
    parser.add_argument("--reader-url", default=os.getenv("M8_READER_DATABASE_URL"))
    parser.add_argument("--milvus-uri", default="http://127.0.0.1:19530")
    args = parser.parse_args()
    if not args.database_url or not args.reader_url:
        parser.error("database and reader URLs required")
    base_commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    if subprocess.check_output(["git", "status", "--porcelain"], text=True).strip():
        raise RuntimeError("M8 acceptance requires clean committed source")
    token = secrets.token_urlsafe(32)
    env = {**os.environ, "M8_DATABASE_URL": args.database_url,
           "M8_READER_DATABASE_URL": args.reader_url,
           "M8_MILVUS_URI": args.milvus_uri, "M8_PROVIDER": "deepseek",
           "M8_REVIEW_TOKEN": token, "M8_REVIEWER_ID": "m8_local_reviewer",
           "PYTHONUNBUFFERED": "1"}
    api_port = port()
    log_path = Path(tempfile.gettempdir()) / f"ins_agent_m8_api_{api_port}.log"
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen([sys.executable, "-m", "uvicorn",
            "apps.api.m8:app_from_env", "--factory", "--host", "127.0.0.1",
            "--port", str(api_port), "--no-access-log"], env=env,
            stdout=log, stderr=subprocess.STDOUT)
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{api_port}",
                          timeout=300, trust_env=False) as client:
            wait_api(client, process)
            created = client.post("/runs", json={"query": DEMO_QUERY})
            created.raise_for_status()
            run_id = created.json()["run_id"]
            crashed = worker(env, crash=True)
            crashed_state = get(client, run_id)
            if (crashed_state["job"]["attempt"] != 1
                    or crashed_state["trace"] != ["planner"]
                    or crashed_state["next"] not in
                        (["fork"], ["sql"], ["rag"])):
                raise RuntimeError(f"planner checkpoint not durable: {crashed_state}")
            print(f"worker crashed after durable planner checkpoint: {run_id}", flush=True)
            time.sleep(4.3)
            recovered_worker = worker(env)
            waiting = get(client, run_id)
            if (waiting["status"] != "WAITING_APPROVAL"
                    or waiting["job"]["attempt"] != 2
                    or waiting["trace"].count("planner") != 1
                    or waiting["next"] != ["human_review"]
                    or waiting["refs"]["sql"]["version"] != 1
                    or waiting["refs"]["rag"]["version"] != 1):
                raise RuntimeError(f"recovered run failed acceptance: {waiting}")
            denied = client.post(f"/runs/{run_id}/review", json={
                "decision": "approve", "artifact_id": waiting["refs"]["analysis"]["artifact_id"],
                "artifact_version": 1,
                "content_hash": waiting["refs"]["analysis"]["content_hash"]},
                headers={"X-Review-Token": "wrong"})
            if denied.status_code != 403:
                raise RuntimeError("invalid review token was accepted")
            review(client, run_id, waiting, token, "reject", ["rag"])
            replay_worker = worker(env)
            revised = get(client, run_id)
            if (revised["status"] != "WAITING_APPROVAL"
                    or revised["cycle"] != 2
                    or revised["refs"]["sql"] != waiting["refs"]["sql"]
                    or revised["refs"]["rag"]["version"] != 2
                    or revised["refs"]["analysis"]["version"] != 2
                    or revised["trace"].count("planner") != 1
                    or revised["trace"].count("data_analyst") != 1
                    or revised["trace"].count("knowledge_researcher") != 2):
                raise RuntimeError(f"targeted replay failed acceptance: {revised}")
            stale = client.post(f"/runs/{run_id}/review", json={
                "decision": "approve", "artifact_id": waiting["refs"]["analysis"]["artifact_id"],
                "artifact_version": 1,
                "content_hash": waiting["refs"]["analysis"]["content_hash"]},
                headers={"X-Review-Token": token})
            if stale.status_code != 409:
                raise RuntimeError("stale artifact approval was accepted")
            review(client, run_id, revised, token, "approve")
            publish_worker = worker(env)
            final = get(client, run_id)
            if (final["status"] != "PUBLISHED"
                    or final["publication"]["artifact_id"] != revised["refs"]["analysis"]["artifact_id"]
                    or final["publication"]["content_hash"] != revised["refs"]["analysis"]["content_hash"]
                    or final["trace"].count("publish") != 1):
                raise RuntimeError(f"bound publish failed acceptance: {final}")
            for name, record in (("recovery", recovered_worker),
                                 ("replay", replay_worker)):
                ids = record["result"]["model_response_ids"]
                if not ids or any(item != "deepseek-flash" for item in ids):
                    raise RuntimeError(f"{name} model provenance is incomplete")
            if publish_worker["result"]["model_response_ids"]:
                raise RuntimeError("publish worker unexpectedly called the model")
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
    with Connection.connect(args.database_url) as conn:
        artifact_rows = conn.execute("""
            SELECT stage, array_agg(version ORDER BY version)
            FROM run_artifacts WHERE run_id = %s GROUP BY stage ORDER BY stage
        """, (run_id,)).fetchall()
        review_rows = conn.execute("""
            SELECT status, cycle, artifact_version FROM m8_reviews
            WHERE run_id = %s ORDER BY cycle
        """, (run_id,)).fetchall()
        publication_rows = conn.execute("""
            SELECT artifact_id, artifact_version, content_hash
            FROM m8_publications WHERE run_id = %s
        """, (run_id,)).fetchall()
    stage_versions = {stage: versions for stage, versions in artifact_rows}
    if (stage_versions != {"analysis": [1, 2], "rag": [1, 2],
                           "sql": [1], "verification": [1, 2]}
            or review_rows != [("REJECTED", 1, 1), ("APPROVED", 2, 2)]
            or len(publication_rows) != 1
            or str(publication_rows[0][0]) != revised["refs"]["analysis"]["artifact_id"]
            or publication_rows[0][1] != 2
            or publication_rows[0][2].strip() != revised["refs"]["analysis"]["content_hash"]):
        raise RuntimeError("PostgreSQL artifact/review/publication rows failed acceptance")
    report = {"generated_at": datetime.now(timezone.utc).isoformat(),
        "base_git_commit": base_commit, "git_worktree_dirty": False,
        "code_sha256": {path: hashlib.sha256(Path(path).read_bytes()).hexdigest()
                        for path in SOURCES}, "run_id": run_id,
        "thread_id": run_id, "query": DEMO_QUERY, "crashed": crashed,
        "crashed_state": crashed_state, "recovered_worker": recovered_worker,
        "waiting": waiting, "replay_worker": replay_worker,
        "revised": revised, "publish_worker": publish_worker,
        "final": final, "unauthorized_status": denied.status_code,
        "stale_approval_status": stale.status_code,
        "stage_versions": stage_versions,
        "review_rows": review_rows,
        "publication_row_count": len(publication_rows)}
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2,
                                 sort_keys=True) + "\n", encoding="utf-8")
    print(f"M8 acceptance PASS; wrote {REPORT}", flush=True)


if __name__ == "__main__":
    main()
