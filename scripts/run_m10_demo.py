"""Live M10 HTTP/JWT -> worker -> review -> publication acceptance."""

import argparse
import ast
import asyncio
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

from packages.persistence.auth import AuthStore
from scripts.run_m7_demo import DEMO_QUERY


REPORT = Path("evaluation/reports/m10_api_ui_demo.json")
SOURCES = ("apps/api/m10.py", "apps/api/run_control.py", "apps/api/m8.py",
           "apps/runner/worker.py", "packages/persistence/auth.py",
           "packages/persistence/events.py", "packages/persistence/reviews.py",
           "apps/web/src/App.vue", "scripts/run_m10_demo.py")


def port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def worker(env: dict, *, inject: bool = False) -> dict:
    worker_env = dict(env)
    if inject:
        worker_env.update(FAULT_INJECTION_ENABLED="1", FAULT_CASE="llm_timeout",
                          FAULT_STAGE="planner", FAULT_OCCURRENCES="1")
    result = subprocess.run([sys.executable, "-m", "apps.runner.worker",
                             "--lease-seconds", "30", "--provider", "deepseek"],
                            env=worker_env, capture_output=True, text=True, timeout=900)
    if result.returncode:
        raise RuntimeError(f"worker failed: {result.returncode} {result.stderr[-1800:]}")
    return ast.literal_eval(result.stdout.strip())


def response(client, method, path, *, token=None, **kwargs):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    result = client.request(method, path, headers=headers, **kwargs)
    return result


async def users(database, secret, suffix, analyst_password, reviewer_password):
    store = AuthStore(database, secret)
    await store.setup()
    await store.create_user(f"demo_analyst_{suffix}", analyst_password, "analyst")
    await store.create_user(f"demo_reviewer_{suffix}", reviewer_password, "reviewer")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=os.getenv("M10_DATABASE_URL"))
    parser.add_argument("--reader-url", default=os.getenv("M10_READER_DATABASE_URL"))
    parser.add_argument("--milvus-uri", default=os.getenv("M10_MILVUS_URI", "http://127.0.0.1:19530"))
    args = parser.parse_args()
    if not args.database_url or not args.reader_url:
        parser.error("M10_DATABASE_URL and M10_READER_DATABASE_URL are required")
    if subprocess.check_output(["git", "status", "--porcelain"], text=True).strip():
        raise RuntimeError("commit source before live acceptance")
    with Connection.connect(args.database_url) as conn:
        if conn.execute("SELECT to_regclass('agent_jobs')").fetchone()[0]:
            if conn.execute("SELECT count(*) FROM agent_jobs").fetchone()[0]:
                raise RuntimeError("use an empty dedicated M10 demo database")
    secret = secrets.token_urlsafe(48)
    suffix = secrets.token_hex(4)
    analyst_password = secrets.token_urlsafe(24)
    reviewer_password = secrets.token_urlsafe(24)
    asyncio.run(users(args.database_url, secret, suffix, analyst_password, reviewer_password))
    env = dict(os.environ)
    env.update(M10_DATABASE_URL=args.database_url,
               M10_READER_DATABASE_URL=args.reader_url,
               M10_JWT_SECRET=secret,
               M8_DATABASE_URL=args.database_url,
               M8_READER_DATABASE_URL=args.reader_url,
               M8_MILVUS_URI=args.milvus_uri)
    address = f"http://127.0.0.1:{port()}"
    with tempfile.TemporaryFile(mode="w+t") as log:
        process = subprocess.Popen([sys.executable, "-m", "uvicorn",
             "apps.api.m10:app_from_env", "--factory", "--host", "127.0.0.1",
             "--port", address.rsplit(":", 1)[1]], env=env, stdout=log,
             stderr=subprocess.STDOUT)
        try:
            with httpx.Client(base_url=address, timeout=300, trust_env=False) as client:
                for _ in range(120):
                    if process.poll() is not None:
                        log.seek(0)
                        raise RuntimeError(f"API failed: {log.read()[-1800:]}")
                    try:
                        if client.get("/api/health", timeout=2).status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(.25)
                else:
                    raise TimeoutError("M10 API did not start")
                a = response(client, "POST", "/api/auth/login", json={
                    "username": f"demo_analyst_{suffix}", "password": analyst_password})
                r = response(client, "POST", "/api/auth/login", json={
                    "username": f"demo_reviewer_{suffix}", "password": reviewer_password})
                a.raise_for_status(); r.raise_for_status()
                analyst_token, reviewer_token = a.json()["access_token"], r.json()["access_token"]
                created = response(client, "POST", "/api/runs", token=analyst_token,
                                   json={"query": DEMO_QUERY})
                created.raise_for_status()
                run_id = created.json()["run_id"]
                if response(client, "POST", f"/api/runs/{run_id}/review",
                            token=analyst_token, json={}).status_code != 403:
                    raise RuntimeError("analyst review was not denied")
                first = worker(env, inject=True)
                state = response(client, "GET", f"/api/runs/{run_id}", token=analyst_token).json()
                if state["status"] != "WAITING_APPROVAL" or state["trace"].count("planner") != 1:
                    raise RuntimeError(f"run did not reach review: {state}")
                if "sql" not in state["refs"] or "rag" not in state["refs"]:
                    raise RuntimeError("mixed route artifacts missing")
                events = response(client, "GET", f"/api/runs/{run_id}/events",
                                  token=analyst_token).json()["events"]
                with client.stream("GET", f"/api/runs/{run_id}/stream?after_id=0",
                                   headers={"Authorization": f"Bearer {analyst_token}"}) as stream:
                    stream.raise_for_status()
                    first_event_line = next(line for line in stream.iter_lines()
                                            if line.startswith("event: "))
                if first_event_line != "event: workflow.queued":
                    raise RuntimeError(f"SSE did not replay the first persisted event: {first_event_line}")
                if not any(e["event_type"] == "dependency.retry" and
                           e["payload"].get("error_code") == "dependency_timeout" for e in events):
                    raise RuntimeError("injected retry event was not persisted")
                if not any(e["event_type"] == "workflow.interrupted" for e in events):
                    raise RuntimeError("review interrupt event missing")
                artifacts = response(client, "GET", f"/api/runs/{run_id}/artifacts",
                                     token=analyst_token).json()["artifacts"]
                preview = response(client, "GET", f"/api/runs/{run_id}/report",
                                   token=analyst_token).json()
                if preview["published"] or not preview["analysis"]["claims"]:
                    raise RuntimeError("pre-approval report is invalid")
                ref = state["refs"]["analysis"]
                body = {"decision": "approve", "artifact_id": ref["artifact_id"],
                        "artifact_version": ref["version"], "content_hash": ref["content_hash"]}
                if response(client, "POST", f"/api/runs/{run_id}/review",
                            token=reviewer_token,
                            json={**body, "content_hash": "0" * 64}).status_code != 409:
                    raise RuntimeError("stale approval was not denied")
                approval = response(client, "POST", f"/api/runs/{run_id}/review",
                                    token=reviewer_token, json=body)
                approval.raise_for_status()
                if response(client, "POST", f"/api/runs/{run_id}/review",
                            token=reviewer_token, json=body).status_code != 409:
                    raise RuntimeError("duplicate review was not denied")
                second = worker(env)
                final = response(client, "GET", f"/api/runs/{run_id}", token=analyst_token).json()
                if final["status"] != "PUBLISHED" or final["publication"]["artifact_id"] != ref["artifact_id"]:
                    raise RuntimeError(f"publish failed: {final}")
                published = response(client, "GET", f"/api/runs/{run_id}/report",
                                     token=analyst_token).json()
                if not published["published"]:
                    raise RuntimeError("report remains unpublished")
                with Connection.connect(args.database_url) as conn:
                    count = conn.execute("SELECT count(*) FROM m8_publications WHERE run_id=%s",
                                         (run_id,)).fetchone()[0]
                if count != 1:
                    raise RuntimeError("publication is not unique")
        finally:
            process.terminate()
            try: process.wait(timeout=10)
            except subprocess.TimeoutExpired: process.kill(); process.wait()
    report = {"generated_at": datetime.now(timezone.utc).isoformat(),
              "base_git_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
              "git_worktree_dirty": False,
              "source_sha256": {p: hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in SOURCES},
              "run_id": run_id, "status": final["status"],
              "review_status": state["status"], "publication_count": count,
              "artifact_refs": state["refs"], "degraded_flags": state["degraded_flags"],
              "retry_events": [e for e in events if e["event_type"] == "dependency.retry"],
              "event_types": [e["event_type"] for e in events],
              "sse_first_event": first_event_line,
              "first_worker_model_ids": first["model_response_ids"],
              "second_worker_model_ids": second["model_response_ids"],
              "jwt_rbac": "analyst review 403; reviewer approved current artifact; stale/duplicate 409",
              "report_preview_published": preview["published"],
              "report_after_review_published": published["published"],
              "artifact_stages": sorted(artifacts)}
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"run_id": run_id, "status": final["status"],
                      "publication_count": count, "retry_events": len(report["retry_events"])},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
