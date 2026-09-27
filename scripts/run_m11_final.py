"""Rerun frozen evaluations in clean disposable Git worktrees, preserving history."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "evaluation/reports/m11_final.json"
PYTHON = ROOT / ".venv/bin/python"
SUITES = (
    ("rag", ("-m", "evaluation.rag.run"),
     {"evaluation/reports/rag_ablation.json": "m11_rag_ablation.json",
      "evaluation/reports/rag_ablation.md": "m11_rag_ablation.md",
      "evaluation/reports/rag_rankings.jsonl": "m11_rag_rankings.jsonl"}),
    ("sql", ("-m", "evaluation.sql.run", "--provider", "deepseek",
             "--max-tokens", "8192"),
     {"evaluation/reports/sql_evaluation.json": "m11_sql_evaluation.json"}),
    ("workflow", ("-m", "scripts.run_m6_demo", "--provider", "deepseek"),
     {"evaluation/reports/m6_contract_demo.json": "m11_workflow_demo.json"}),
    ("replay", ("-m", "scripts.run_m8_demo"),
     {"evaluation/reports/m8_runner_replay.json": "m11_replay_demo.json"}),
    ("fault", ("-m", "scripts.run_m9_faults"),
     {"evaluation/reports/m9_fault_injection.json": "m11_fault_injection.json"}),
)
REQUIRED_TEST_ENV = ("M1_TEST_DATABASE_URL", "M2_TEST_MILVUS_URI",
                     "M3_TEST_READER_DATABASE_URL", "M4_TEST_MILVUS_URI",
                     "M7_TEST_DATABASE_URL", "M8_TEST_DATABASE_URL",
                     "M9_TEST_DATABASE_URL", "M10_TEST_DATABASE_URL")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def command(args: list[str], *, cwd: Path, env: dict, timeout: int) -> dict:
    print("running:", " ".join(args), flush=True)
    start = time.monotonic()
    process = subprocess.Popen(args, cwd=cwd, env=env, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True, bufsize=1)
    tail = []
    try:
        for line in process.stdout:
            print(line, end="", flush=True)
            tail.append(line)
            if len(tail) > 300:
                tail.pop(0)
            if time.monotonic() - start > timeout:
                process.kill()
                raise TimeoutError(f"command exceeded {timeout}s")
        code = process.wait(timeout=10)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
    if code:
        raise RuntimeError(f"command exited {code}: {''.join(tail[-15:])}")
    return {"elapsed_seconds": round(time.monotonic() - start, 2),
            "output_tail": "".join(tail[-5:]).strip()}


def link_inputs(worktree: Path):
    paths = ("data/raw/benchmarks", "data/models", "data/processed")
    for name in paths:
        target = worktree / name
        target.mkdir(parents=True, exist_ok=True)
        for source in (ROOT / name).iterdir():
            (target / source.name).symlink_to(source,
                                              target_is_directory=source.is_dir())
    public = ROOT / "data/raw/public"
    for source in public.iterdir():
        if source.name != ".gitkeep" and source.is_file():
            (worktree / "data/raw/public" / source.name).symlink_to(source)


def isolated_suite(name: str, args: tuple[str, ...], outputs: dict[str, str],
                   *, base: str, env: dict, staging: Path) -> dict:
    with tempfile.TemporaryDirectory(prefix=f"ins-agent-m11-{name}-") as temp:
        worktree = Path(temp) / "checkout"
        subprocess.run(["git", "worktree", "add", "--detach", "--quiet",
                        str(worktree), base], cwd=ROOT, check=True)
        try:
            link_inputs(worktree)
            actual_env = dict(env, PYTHONPATH=str(worktree), PYTHONUNBUFFERED="1")
            result = command([str(PYTHON), *args], cwd=worktree,
                             env=actual_env, timeout=7200)
            files = {}
            for source_name, target_name in outputs.items():
                source = worktree / source_name
                if not source.is_file():
                    raise RuntimeError(f"{name} did not generate {source_name}")
                target = staging / target_name
                shutil.copyfile(source, target)
                files[target_name] = sha256(target)
            report_path = next((staging / filename for filename in files
                                if filename.endswith(".json")), None)
            if report_path:
                payload = json.loads(report_path.read_text(encoding="utf-8"))
                if payload.get("base_git_commit") != base or payload.get("git_worktree_dirty"):
                    raise RuntimeError(f"{name} report has no clean-source provenance")
            return {"command": [str(PYTHON), *args],
                    "elapsed_seconds": result["elapsed_seconds"], "files_sha256": files,
                    "output_tail": result["output_tail"]}
        finally:
            subprocess.run(["git", "worktree", "remove", "--force", str(worktree)],
                           cwd=ROOT, check=True)


def require_empty_database(url: str):
    from psycopg import Connection
    with Connection.connect(url) as conn:
        table = conn.execute("SELECT to_regclass('agent_jobs')").fetchone()[0]
        if table and conn.execute("SELECT count(*) FROM agent_jobs").fetchone()[0]:
            raise RuntimeError("M11 live demo database must be empty")


def verify_rag_rankings(report: dict, ranking_path: Path) -> None:
    rows = [json.loads(line) for line in ranking_path.read_text().splitlines()]
    if len(rows) != report["query_count"] or sha256(ranking_path) != report["rankings_sha256"]:
        raise RuntimeError("RAG rankings count or SHA-256 mismatch")
    for mode, expected in report["results"].items():
        values = {key: 0.0 for key in ("Recall@1", "Recall@5", "Recall@10",
                                       "MRR@10", "Hit@10")}
        for row in rows:
            positives = set(row["positive_ids"])
            ranked = row["rankings"][mode]
            if not positives or len(ranked) != len(set(ranked)):
                raise RuntimeError("invalid RAG ranking row")
            for k in (1, 5, 10):
                values[f"Recall@{k}"] += len(positives.intersection(ranked[:k])) / len(positives)
            values["MRR@10"] += next((1 / index for index, value in enumerate(ranked[:10], 1)
                                       if value in positives), 0)
            values["Hit@10"] += bool(positives.intersection(ranked[:10]))
        for key, total in values.items():
            if abs(total / len(rows) - expected[key]) > 1e-12:
                raise RuntimeError(f"RAG {mode} {key} does not match rankings")


def verify_sql_summary(report: dict) -> None:
    records = report["cases"]
    if len(records) != report["case_count"]:
        raise RuntimeError("SQL case count mismatch")
    safe = [row for row in records if row["expected_status"] == "success"]
    unsafe = [row for row in records if row["category"] == "unsafe_request"]
    unknown = [row for row in records if row["category"] == "unanswerable"]
    metrics = report["metrics"]
    recomputed = {
        "execution_success": sum(row["status"] == "success" for row in safe) / len(safe),
        "result_accuracy": sum(row["correct"] for row in safe) / len(safe),
        "first_pass_result_accuracy": sum(row["correct"] and len(row["attempts"]) == 1
                                          for row in safe) / len(safe),
        "unsafe_request_refusal": sum(row["status"] == "refused" for row in unsafe) / len(unsafe),
        "unanswerable_refusal": sum(row["status"] == "refused" for row in unknown) / len(unknown),
    }
    if any(abs(metrics[key] - value) > 1e-12 for key, value in recomputed.items()):
        raise RuntimeError("SQL summary differs from per-case records")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reader-url", default=os.getenv("M3_READER_DATABASE_URL"))
    parser.add_argument("--milvus-uri", default=os.getenv("MILVUS_URI", "http://127.0.0.1:19530"))
    parser.add_argument("--replay-database-url", default=os.getenv("M11_REPLAY_DATABASE_URL"))
    parser.add_argument("--fault-database-url", default=os.getenv("M11_FAULT_DATABASE_URL"))
    parser.add_argument("--fault-test-database-url", default=os.getenv("M9_TEST_DATABASE_URL"))
    args = parser.parse_args()
    if not all((args.reader_url, args.replay_database_url,
                args.fault_database_url, args.fault_test_database_url)):
        parser.error("reader, replay, fault-demo, and fault-test URLs are required")
    if not PYTHON.is_file():
        raise RuntimeError("run uv sync --locked before M11 evaluation")
    missing = [key for key in REQUIRED_TEST_ENV if not os.getenv(key)]
    if missing:
        raise RuntimeError(f"full integration environment is missing: {', '.join(missing)}")
    if os.getenv("M1_VERIFY_DOWNLOADS") != "1":
        raise RuntimeError("M1_VERIFY_DOWNLOADS=1 is required for final verification")
    if subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True).strip():
        raise RuntimeError("commit source before M11 evaluation")
    require_empty_database(args.replay_database_url)
    require_empty_database(args.fault_database_url)
    base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT,
                                   text=True).strip()
    env = dict(os.environ, M3_READER_DATABASE_URL=args.reader_url,
               M8_READER_DATABASE_URL=args.reader_url,
               M9_READER_DATABASE_URL=args.reader_url,
               M8_MILVUS_URI=args.milvus_uri,
               M9_TEST_MILVUS_URI=args.milvus_uri,
               MILVUS_URI=args.milvus_uri,
               M8_DATABASE_URL=args.replay_database_url,
               M9_DATABASE_URL=args.fault_database_url,
               M9_TEST_DATABASE_URL=args.fault_test_database_url)
    completed = {}
    with tempfile.TemporaryDirectory(prefix="ins-agent-m11-results-") as temp:
        staging = Path(temp)
        for name, cmd, outputs in SUITES:
            print(f"\n=== M11 {name} ===", flush=True)
            completed[name] = isolated_suite(name, cmd, outputs, base=base,
                                              env=env, staging=staging)
        print("\n=== M11 full pytest ===", flush=True)
        completed["pytest"] = command([str(PYTHON), "-m", "pytest", "-q"],
                                      cwd=ROOT, env=env, timeout=3600)
        summary = completed["pytest"]["output_tail"]
        if not re.search(r"\b\d+ passed\b", summary) or "skipped" in summary:
            raise RuntimeError(f"full pytest was not an unskipped pass: {summary}")
        print("\n=== M11 npm ci/build ===", flush=True)
        npm_env = dict(env)
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                    "http_proxy", "https_proxy", "all_proxy"):
            npm_env.pop(key, None)
        completed["web"] = command(["npm", "ci", "--registry=https://registry.npmmirror.com",
                                    "--fetch-retries=0", "--fetch-timeout=12000"],
                                   cwd=ROOT / "apps/web", env=npm_env, timeout=600)
        completed["web_build"] = command(["npm", "run", "build"],
                                         cwd=ROOT / "apps/web", env=npm_env, timeout=600)
        rag = json.loads((staging / "m11_rag_ablation.json").read_text())
        sql = json.loads((staging / "m11_sql_evaluation.json").read_text())
        workflow = json.loads((staging / "m11_workflow_demo.json").read_text())
        replay = json.loads((staging / "m11_replay_demo.json").read_text())
        fault = json.loads((staging / "m11_fault_injection.json").read_text())
        if rag["query_count"] != 256 or sql["case_count"] != 110:
            raise RuntimeError("evaluation case count changed")
        verify_rag_rankings(rag, staging / "m11_rag_rankings.jsonl")
        verify_sql_summary(sql)
        if (workflow["case_count"] != 4
                or [c["plan"]["route"] for c in workflow["cases"]] !=
                    ["SQL", "RAG", "BOTH", "REPORT"]
                or any(c["status"] != "PASS" or c["deterministic_evidence_issues"]
                       for c in workflow["cases"])):
            raise RuntimeError("four-route workflow did not PASS")
        if (replay["final"]["status"] != "PUBLISHED"
                or replay["publication_row_count"] != 1
                or replay["stage_versions"] != {"analysis": [1, 2], "rag": [1, 2],
                                                "sql": [1], "verification": [1, 2]}):
            raise RuntimeError("targeted replay did not publish")
        if (fault["live_final_status"] != "PUBLISHED"
                or fault["publication_row_count"] != 1
                or fault["live_first_worker"]["retry_events"] != [{
                    "boundary": "llm:planner", "error_code": "dependency_timeout",
                    "attempt": 1}]):
            raise RuntimeError("fault recovery did not publish exactly once")
        report = {"generated_at": datetime.now(timezone.utc).isoformat(),
                  "base_git_commit": base, "git_worktree_dirty": False,
                  "runner_sha256": sha256(ROOT / "scripts/run_m11_final.py"),
                  "suites": completed,
                  "rag": {"query_count": rag["query_count"], "results": rag["results"],
                          "rankings_sha256": rag["rankings_sha256"]},
                  "sql": {"case_count": sql["case_count"], "provider": sql["provider"],
                          "requested_model": sql["requested_model"],
                          "generation_max_tokens": sql["generation_max_tokens"],
                          "observed_response_model_ids": sql["observed_response_model_ids"],
                          "response_model_missing_count": sql["response_model_missing_count"],
                          "metrics": sql["metrics"]},
                  "workflow": {"routes": [c["plan"]["route"] for c in workflow["cases"]],
                               "statuses": [c["status"] for c in workflow["cases"]],
                               "model_ids": workflow["observed_response_model_ids"]},
                  "replay": {"final_status": replay["final"]["status"],
                             "run_id": replay["run_id"]},
                  "fault": {"final_status": fault["live_final_status"],
                            "publication_row_count": fault["publication_row_count"],
                            "fault_suite_summary": fault["fault_suite_summary"]},
                  "limitations": ["SQL and workflow use fixed regression/demo questions, not unseen holdouts.",
                                  "Faults are controlled injections, not observed service incidents.",
                                  "Wall time is environment-specific; provider token usage was not recorded."]}
        for path in staging.iterdir():
            shutil.copyfile(path, ROOT / "evaluation/reports" / path.name)
        REPORT.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True,
                                     indent=2) + "\n", encoding="utf-8")
    print(f"M11 final evaluation PASS; wrote {REPORT}", flush=True)


if __name__ == "__main__":
    main()
