"""Deterministic source checks before the LLM verifier may approve a draft."""

import json
import re
from decimal import Decimal, InvalidOperation

from packages.agent.models import AnalysisResult, RagArtifact, SqlArtifact, TaskPlan

NUMBER = re.compile(r"(?<![A-Za-z])\d[\d,]*(?:\.\d+)?")


def numbers(value: str) -> set[Decimal]:
    found = set()
    for raw in NUMBER.findall(value):
        try:
            found.add(Decimal(raw.replace(",", "")))
        except InvalidOperation:
            continue
    return found


def validate_claim_evidence(plan: TaskPlan, sql: list[SqlArtifact],
                            rag: list[RagArtifact], analysis: AnalysisResult) -> list[str]:
    """Require a current source, exact RAG quote, and source-backed numerals per claim."""
    issues = []
    sql_by_id = {item.artifact_id: item for item in sql}
    rag_by_id = {evidence.evidence_id: evidence for item in rag for evidence in item.evidence}
    if len(sql_by_id) != len(sql) or len(rag_by_id) != sum(len(item.evidence) for item in rag):
        issues.append("duplicate source ID in current artifacts")
    if not analysis.claims:
        issues.append("analysis has no factual claims")
    plan_numbers = numbers(json.dumps(plan.model_dump(), ensure_ascii=False))
    ungrounded_summary = numbers(analysis.summary) - plan_numbers
    if ungrounded_summary:
        issues.append("summary contains a result number without a cited claim")
    for index, claim in enumerate(analysis.claims, start=1):
        if not claim.text.strip() or len(claim.source_ids) != 1:
            issues.append(f"claim {index} needs text and exactly one source ID")
            continue
        source_id = claim.source_ids[0]
        if source_id in sql_by_id:
            if claim.evidence_quote is not None:
                issues.append(f"claim {index} SQL source must not carry a RAG quote")
            source = sql_by_id[source_id]
            source_numbers = numbers(json.dumps({"result": source.result,
                                                 "arguments": source.arguments},
                                                ensure_ascii=False))
            if not numbers(claim.text):
                issues.append(f"claim {index} SQL result needs an explicit number")
            elif numbers(claim.text) - source_numbers:
                issues.append(f"claim {index} contains a number absent from SQL artifact")
        elif source_id in rag_by_id:
            evidence = rag_by_id[source_id]
            quote = claim.evidence_quote
            if not quote or quote not in evidence.text or quote not in claim.text:
                issues.append(f"claim {index} needs an exact quote from its RAG evidence")
            source_numbers = numbers(evidence.text + " " + (evidence.product_code or ""))
            if numbers(claim.text) - source_numbers:
                issues.append(f"claim {index} contains a number absent from RAG evidence")
        else:
            issues.append(f"claim {index} cites an unknown source ID")
    return issues
