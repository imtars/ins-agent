"""Evaluate real SQL generation against frozen reference results; no mock scores."""

import argparse
import asyncio
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import subprocess

from sqlalchemy.ext.asyncio import create_async_engine
import yaml

from evaluation.sql.prepare_cases import CASES_PATH, MANIFEST_PATH
from packages.domain.hf_download import file_sha256
from packages.persistence.synthetic_loader import database_snapshot
from packages.sql.agent import answer_question
from packages.sql.deepseek import DeepSeekSQLGenerator, SYSTEM
from packages.sql.runtime import execute_readonly

REPORT_PATH = Path("evaluation/reports/sql_evaluation.json")


def value_equal(expected, actual, tolerance: Decimal) -> bool:
    if expected is None or actual is None:
        return expected is actual
    try:
        return abs(Decimal(str(expected)) - Decimal(str(actual))) <= tolerance
    except InvalidOperation:
        return expected == actual


def rows_equal(expected: list[dict], actual: list[dict], tolerance: str,
               *, ordered: bool) -> bool:
    if len(expected) != len(actual):
        return False
    epsilon = Decimal(tolerance)

    def same(left: dict, right: dict) -> bool:
        if len(left) != len(right):
            return False
        # Output aliases are not part of the gold semantics. Match cell values,
        # including type and tolerance, independently of selected column names.
        remaining = list(right.values())
        for value in left.values():
            match = next((i for i, candidate in enumerate(remaining)
                          if value_equal(value, candidate, epsilon)), None)
            if match is None:
                return False
            remaining.pop(match)
        return True

    if ordered:
        return all(same(a, b) for a, b in zip(expected, actual, strict=True))
    unmatched = list(actual)
    for row in expected:
        match = next((i for i, candidate in enumerate(unmatched) if same(row, candidate)), None)
        if match is None:
            return False
        unmatched.pop(match)
    return not unmatched


async def checked_cases(engine) -> tuple[dict, dict]:
    casebook = yaml.safe_load(CASES_PATH.read_text(encoding="utf-8"))
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    if (casebook["dataset_sha256"] != manifest["dataset_sha256"]
            or casebook["case_count"] != len(casebook["cases"])
            or len({item["id"] for item in casebook["cases"]}) != len(casebook["cases"])):
        raise ValueError("casebook does not match frozen synthetic dataset")
    counts, hashes = await database_snapshot(engine)
    if counts != manifest["table_counts"] or hashes != manifest["table_sha256"]:
        raise ValueError("evaluation database differs from M1 synthetic manifest")
    for case in casebook["cases"]:
        if case["expected_status"] == "success":
            actual = await execute_readonly(engine, case["reference_sql"])
            if not rows_equal(case["expected_result"], actual, case["tolerance"],
                              ordered=case["ordered"]):
                raise ValueError(f"stale gold result: {case['id']}")
        elif case["expected_status"] != "refused" or case["reference_sql"] is not None:
            raise ValueError(f"invalid refusal case: {case['id']}")
    return casebook, manifest


async def evaluate(reader_url: str) -> dict:
    generator = DeepSeekSQLGenerator()
    engine = create_async_engine(reader_url)
    try:
        casebook, manifest = await checked_cases(engine)
        records = []
        for index, case in enumerate(casebook["cases"], start=1):
            answer = await answer_question(engine, generator, case["question"])
            correct = (answer.status == "refused" if case["expected_status"] == "refused"
                       else answer.status == "success" and rows_equal(
                           case["expected_result"], answer.rows or [], case["tolerance"],
                           ordered=case["ordered"]))
            records.append({"id": case["id"], "category": case["category"],
                            "expected_status": case["expected_status"],
                            "status": answer.status, "correct": bool(correct),
                            "rows": answer.rows, "attempts": [asdict(a) for a in answer.attempts]})
            if index % 10 == 0:
                print(f"evaluated {index}/{len(casebook['cases'])}", flush=True)
    finally:
        await engine.dispose()
    safe = [r for r in records if r["expected_status"] == "success"]
    unsafe = [r for r in records if r["category"] == "unsafe_request"]
    unknown = [r for r in records if r["category"] == "unanswerable"]
    repair = [r for r in safe if len(r["attempts"]) > 1]
    try:
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
        dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], text=True).strip())
    except (OSError, subprocess.CalledProcessError):
        commit, dirty = None, None
    report = {"generated_at": datetime.now(timezone.utc).isoformat(),
              "base_git_commit": commit, "git_worktree_dirty": dirty,
              "dataset_sha256": manifest["dataset_sha256"],
              "casebook_sha256": file_sha256(CASES_PATH),
              "code_sha256": {str(path): file_sha256(path) for path in (
                  Path("packages/sql/security.py"), Path("packages/sql/runtime.py"),
                  Path("packages/sql/agent.py"), Path("packages/sql/deepseek.py"),
                  Path("packages/sql/analytics.py"),
                  Path("evaluation/sql/prepare_cases.py"),
                  Path("packages/persistence/synthetic_loader.py"),
                  Path("evaluation/sql/run.py"))},
              "provider": "DeepSeek", "model": generator.model,
              "system_prompt_sha256": __import__("hashlib").sha256(SYSTEM.encode()).hexdigest(),
              "case_count": len(records), "safe_count": len(safe),
              "unsafe_count": len(unsafe), "unanswerable_count": len(unknown),
              "metrics": {
                  "execution_success": sum(r["status"] == "success" for r in safe) / len(safe),
                  "result_accuracy": sum(r["correct"] for r in safe) / len(safe),
                  "first_pass_result_accuracy": sum(r["correct"] and len(r["attempts"]) == 1
                                                    for r in safe) / len(safe),
                  "unsafe_request_refusal": sum(r["status"] == "refused" for r in unsafe) / len(unsafe),
                  "unanswerable_refusal": sum(r["status"] == "refused" for r in unknown) / len(unknown),
                  "repair_success": (sum(r["correct"] for r in repair) / len(repair)
                                     if repair else None),
                  "repair_attempted_cases": len(repair),
              },
              "cases": records}
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                           encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reader-url", default=os.environ.get("M3_READER_DATABASE_URL"))
    parser.add_argument("--check-gold", action="store_true")
    args = parser.parse_args()
    if not args.reader_url:
        raise ValueError("set M3_READER_DATABASE_URL")
    if args.check_gold:
        async def check():
            engine = create_async_engine(args.reader_url)
            try:
                casebook, _ = await checked_cases(engine)
                print(f"gold verified: {casebook['case_count']} cases")
            finally:
                await engine.dispose()
        asyncio.run(check())
    else:
        report = asyncio.run(evaluate(args.reader_url))
        print(json.dumps(report["metrics"], sort_keys=True))


if __name__ == "__main__":
    main()
