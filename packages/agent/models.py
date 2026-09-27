"""Validated role, tool, evidence, and approval outputs for the workflow."""

import hashlib
import operator
from typing import Annotated, Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class RoleOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True,
                              revalidate_instances="always")


class TaskPlan(RoleOutput):
    intent: str
    route: Literal["SQL", "RAG", "BOTH", "REPORT"]
    sql_tasks: list[str] = Field(default_factory=list)
    rag_tasks: list[str] = Field(default_factory=list)
    required_outputs: list[str] = Field(default_factory=list)

    @field_validator("route", mode="before")
    @classmethod
    def uppercase_route(cls, value):
        return value.upper() if isinstance(value, str) else value

    @field_validator("sql_tasks", "rag_tasks")
    @classmethod
    def nonempty_tasks(cls, value):
        if any(not task.strip() for task in value):
            raise ValueError("task text must be nonempty")
        return value


class QueryRowsOutput(RoleOutput):
    rows: list[dict]
    row_count: int = Field(ge=0)

    @model_validator(mode="after")
    def count_matches_rows(self):
        if self.row_count != len(self.rows):
            raise ValueError("row_count does not match rows")
        return self


class ClaimRateOutput(RoleOutput):
    metric: Literal["claims_per_in_force_policy_year"]
    value: str | None
    unit: str
    exposure_basis: Literal["in_force_days_not_waiting_period_adjusted"]
    start_date: str
    end_date: str
    product_code: str | None
    region: str | None
    claim_count: int
    in_force_policy_years: str


class LossRatioOutput(RoleOutput):
    metric: Literal["incurred_loss_ratio", "paid_loss_ratio"]
    value: str | None
    unit: Literal["ratio"]
    start_date: str
    end_date: str
    product_code: str | None
    region: str | None
    incurred_amount: str
    paid_amount: str
    earned_premium: str


class GrowthOutput(RoleOutput):
    metric: str
    previous: str
    current: str
    growth_rate: str | None
    previous_start: str
    previous_end: str
    current_start: str
    current_end: str
    product_code: str | None
    region: str | None
    unit: Literal["ratio"]


class StatisticsOutput(RoleOutput):
    group_by: Literal["all", "product_code", "region"]
    start_date: str
    end_date: str
    product_code: str | None
    region: str | None
    rows: list[dict]
    row_count: int = Field(ge=0)

    @model_validator(mode="after")
    def count_matches_rows(self):
        if self.row_count != len(self.rows):
            raise ValueError("row_count does not match rows")
        return self


SQL_TOOL_OUTPUTS = {
    "data_execute_readonly_query": QueryRowsOutput,
    "data_compute_claim_rate": ClaimRateOutput,
    "data_compute_loss_ratio": LossRatioOutput,
    "data_compute_growth": GrowthOutput,
    "data_group_statistics": StatisticsOutput,
}


class ToolAttempt(RoleOutput):
    number: int = Field(ge=0)
    status: Literal["failed", "success"]
    tool: str | None = None
    arguments: dict | None = None
    error: str | None = None

    @model_validator(mode="after")
    def has_outcome(self):
        if self.status == "success" and (not self.tool or self.arguments is None
                                         or self.error is not None):
            raise ValueError("successful attempt needs tool and arguments")
        if self.status == "failed" and not self.error:
            raise ValueError("failed attempt needs error")
        return self


class SqlArtifact(RoleOutput):
    artifact_id: str
    task: str
    tool: Literal["data_execute_readonly_query", "data_compute_claim_rate",
                  "data_compute_loss_ratio", "data_compute_growth",
                  "data_group_statistics"]
    arguments: dict
    result: dict
    attempts: list[ToolAttempt]

    @model_validator(mode="after")
    def validates_tool_result(self):
        output = SQL_TOOL_OUTPUTS[self.tool].model_validate(self.result)
        self.result = output.model_dump()
        if not self.attempts or self.attempts[-1].status != "success":
            raise ValueError("SQL artifact needs a successful final attempt")
        return self


class ToolProposal(RoleOutput):
    tool: Literal["data_execute_readonly_query", "data_compute_claim_rate",
                  "data_compute_loss_ratio", "data_compute_growth",
                  "data_group_statistics"]
    arguments: dict


class KnowledgeQuery(RoleOutput):
    query: str
    product_code: str | None = None


class Evidence(RoleOutput):
    evidence_id: str
    doc_id: str
    chunk_id: str
    title: str
    section: str
    page: int | None
    source_type: Literal["synthetic_product", "public_consultation_draft"]
    source_name: str
    source_url: str
    product_code: str | None
    content_hash: str
    text: str
    rerank_score: float

    @model_validator(mode="after")
    def provenance_matches_text(self):
        if self.evidence_id != f"{self.doc_id}:{self.section}:{self.chunk_id}":
            raise ValueError("evidence ID does not match source fields")
        if self.content_hash != hashlib.sha256(self.text.encode()).hexdigest():
            raise ValueError("evidence content hash mismatch")
        if self.source_type == "public_consultation_draft" and self.product_code:
            raise ValueError("public draft cannot bind synthetic product_code")
        return self


class SearchOutput(RoleOutput):
    query: str
    product_code: str | None
    evidence: list[Evidence]
    count: int = Field(ge=0)

    @model_validator(mode="after")
    def count_matches_evidence(self):
        if self.count != len(self.evidence):
            raise ValueError("knowledge count does not match evidence")
        return self


class RagArtifact(RoleOutput):
    artifact_id: str
    task: str
    query: str
    evidence: list[Evidence]


class AnalysisClaim(RoleOutput):
    text: str
    source_ids: list[str]
    evidence_quote: str | None = None


class AnalysisResult(RoleOutput):
    summary: str
    claims: list[AnalysisClaim]


class VerificationResult(RoleOutput):
    status: Literal["PASS", "REVISE", "BLOCK"]
    issues: list[str]

    @field_validator("status", mode="before")
    @classmethod
    def uppercase_status(cls, value):
        return value.upper() if isinstance(value, str) else value


class ApprovalResult(RoleOutput):
    run_id: str
    approval_id: str
    status: Literal["APPROVED", "REJECTED"]
    reviewer_id: str = Field(min_length=1)
    comment: str = ""


class PublicationReceipt(RoleOutput):
    run_id: str
    approval_id: str
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class RunState(TypedDict, total=False):
    run_id: str
    user_query: str
    plan: TaskPlan
    sql_results: list[SqlArtifact]
    rag_results: list[RagArtifact]
    analysis: AnalysisResult
    verification: VerificationResult
    approval: ApprovalResult
    publication: PublicationReceipt
    status: str
    trace: Annotated[list[str], operator.add]


class SqlSubState(TypedDict, total=False):
    plan: TaskPlan
    sql_results: list[SqlArtifact]


class RagSubState(TypedDict, total=False):
    plan: TaskPlan
    rag_results: list[RagArtifact]
