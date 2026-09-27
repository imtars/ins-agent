"""Accept real M7 API process restart, checkpoint resume, and guarded publish."""

import argparse
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

from packages.persistence.approvals import psycopg_url
REPORT_PATH = Path("evaluation/reports/m7_durable_demo.json")
DEMO_QUERY = (
    "请生成 2026 年第二季度合成产品 product_006 的业务与条款综合简报，"
    "只列出已发生赔付率、理赔频率（次/在保保单年）以及疾病责任等待期，"
    "分别引用运营数据和产品条款。"
)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def start_api(port: int, env: dict, log_path: Path) -> subprocess.Popen:
    log = log_path.open("w", encoding="utf-8")
    try:
        proc = subprocess.Popen([
            sys.executable, "-m", "uvicorn", "apps.api.m7:app_from_env", "--factory",
            "--host", "127.0.0.1", "--port", str(port), "--no-access-log",
        ], env=env, stdout=log, stderr=subprocess.STDOUT)
    finally:
        log.close()
    return proc


def stop_api(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def wait_ready(client: httpx.Client, proc: subprocess.Popen) -> dict:
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"M7 API exited during startup: {proc.returncode}")
        try:
            response = client.get("/health", timeout=2)
            if response.status_code == 200:
                return response.json()
        except httpx.HTTPError:
            pass
        time.sleep(0.25)
    raise RuntimeError("M7 API did not become healthy")


def source_hashes() -> dict[str, str]:
    paths = (
        "packages/agent/graph.py", "packages/agent/contracts.py",
        "packages/agent/models.py", "packages/persistence/approvals.py",
        "packages/persistence/checkpoints.py", "apps/api/m7.py",
        "scripts/run_m7_demo.py",
    )
    return {path: hashlib.sha256(Path(path).read_bytes()).hexdigest()
            for path in paths}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint-url", default=os.getenv("M7_CHECKPOINT_DATABASE_URL"))
    parser.add_argument("--reader-url", default=os.getenv("M3_READER_DATABASE_URL"))
    parser.add_argument("--milvus-uri", default=os.getenv("MILVUS_URI",
                                                             "http://127.0.0.1:19530"))
    parser.add_argument("--provider", choices=("deepseek", "proxy"), default="deepseek")
    args = parser.parse_args()
    if not args.checkpoint_url or not args.reader_url:
        raise ValueError("checkpoint and reader PostgreSQL URLs are required")
    base_commit = subprocess.check_output(["git", "rev-parse", "HEAD"],
                                          text=True).strip()
    if subprocess.check_output(["git", "status", "--porcelain"], text=True).strip():
        raise RuntimeError("M7 acceptance requires a clean source baseline")
    token = secrets.token_urlsafe(32)
    env = {**os.environ, "M7_CHECKPOINT_DATABASE_URL": args.checkpoint_url,
           "M7_READER_DATABASE_URL": args.reader_url,
           "M7_MILVUS_URI": args.milvus_uri, "M7_PROVIDER": args.provider,
           "M7_REVIEW_TOKEN": token, "M7_REVIEWER_ID": "m7_local_reviewer",
           "PYTHONUNBUFFERED": "1"}
    port = free_port()
    first_log = Path(tempfile.gettempdir()) / f"ins_agent_m7_api_first_{port}.log"
    second_log = Path(tempfile.gettempdir()) / f"ins_agent_m7_api_second_{port}.log"
    first = start_api(port, env, first_log)
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=300,
                          trust_env=False) as client:
            first_health = wait_ready(client, first)
            created = client.post("/runs", json={"query": DEMO_QUERY})
            created.raise_for_status()
            start_record = created.json()
            run_id = start_record["run_id"]
            waiting = client.get(f"/runs/{run_id}")
            waiting.raise_for_status()
            waiting_record = waiting.json()
            if (start_record["status"] != "WAITING_APPROVAL"
                    or start_record["thread_id"] != run_id
                    or waiting_record["status"] != "WAITING_APPROVAL"
                    or waiting_record["next"] != ["human_review"]
                    or waiting_record["verification"]["status"] != "PASS"
                    or "publish" in waiting_record["trace"]):
                raise RuntimeError(
                    "run did not interrupt after a PASS verification: "
                    f"status={waiting_record['status']} "
                    f"next={waiting_record['next']} "
                    f"issues={waiting_record['verification']['issues']}")
            print(f"paused run {run_id} at human_review", flush=True)
    finally:
        stop_api(first)

    second = start_api(port, env, second_log)
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=300,
                          trust_env=False) as client:
            second_health = wait_ready(client, second)
            recovered = client.get(f"/runs/{run_id}")
            recovered.raise_for_status()
            recovered_record = recovered.json()
            if (recovered_record["status"] != "WAITING_APPROVAL"
                    or recovered_record["trace"] != waiting_record["trace"]
                    or second_health["model_response_count"] != 0):
                raise RuntimeError("new API process did not restore the paused checkpoint")
            denied = client.post(f"/runs/{run_id}/review",
                                 json={"decision": "approve"},
                                 headers={"X-Review-Token": "invalid-token"})
            if denied.status_code != 403:
                raise RuntimeError("unauthorized review was not refused")
            approved = client.post(f"/runs/{run_id}/review",
                                   json={"decision": "approve"},
                                   headers={"X-Review-Token": token})
            approved.raise_for_status()
            final = client.get(f"/runs/{run_id}")
            final.raise_for_status()
            final_record = final.json()
            final_health = client.get("/health").json()
            if (approved.json()["status"] != "PUBLISHED"
                    or final_record["status"] != "PUBLISHED"
                    or final_record["next"] != []
                    or final_record["trace"].count("human_review") != 1
                    or final_record["trace"].count("publish") != 1
                    or final_health["model_response_count"] != 0):
                raise RuntimeError("approved run did not publish exactly once without rerun")
            print(f"resumed and published run {run_id} after API restart", flush=True)
    finally:
        stop_api(second)

    with Connection.connect(psycopg_url(args.checkpoint_url)) as conn:
        row = conn.execute("""
            SELECT a.status, a.reviewer_id, p.content_hash,
                   (SELECT count(*) FROM agent_publications WHERE run_id = a.run_id)
            FROM agent_approvals a JOIN agent_publications p ON p.run_id = a.run_id
            WHERE a.run_id = %s
        """, (run_id,)).fetchone()
    if (row is None or row[0] != "APPROVED" or row[1] != "m7_local_reviewer"
            or row[2].strip() != final_record["publication"]["content_hash"]
            or row[3] != 1):
        raise RuntimeError("approval/publication rows do not match the final graph state")
    provenance = start_record["model_provenance"]
    if (provenance["response_count"] < 5
            or provenance["response_model_missing_count"] != 0
            or not provenance["observed_response_model_ids"]):
        raise RuntimeError("live model provenance is incomplete")
    report = {"generated_at": datetime.now(timezone.utc).isoformat(),
              "base_git_commit": base_commit, "git_worktree_dirty": False,
              "code_sha256": source_hashes(), "run_id": run_id,
              "query": DEMO_QUERY,
              "thread_id": start_record["thread_id"],
              "provider": provenance, "first_process_health": first_health,
              "second_process_initial_health": second_health,
              "second_process_final_health": final_health,
              "waiting": waiting_record, "recovered": recovered_record,
              "final": final_record, "unauthorized_review_status": denied.status_code,
              "approval_row_status": row[0], "approval_reviewer_id": row[1],
              "publication_row_count": row[3],
              "historical_m6_report_sha256": hashlib.sha256(Path(
                  "evaluation/reports/m6_contract_demo.json").read_bytes()).hexdigest()}
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2,
                                      sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {REPORT_PATH}", flush=True)


if __name__ == "__main__":
    main()
