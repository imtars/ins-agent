"""Minimal M5 role outputs; full handoff registry and enforcement belong to M6."""

import operator
from typing import Annotated, Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field, field_validator


class RoleOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")


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


class SqlArtifact(RoleOutput):
    artifact_id: str
    task: str
    tool: str
    arguments: dict
    result: dict
    attempts: list[dict]


class ToolProposal(RoleOutput):
    tool: Literal["data_execute_readonly_query", "data_compute_claim_rate",
                  "data_compute_loss_ratio", "data_compute_growth",
                  "data_group_statistics"]
    arguments: dict


class KnowledgeQuery(RoleOutput):
    query: str
    product_code: str | None = None


class RagArtifact(RoleOutput):
    artifact_id: str
    task: str
    query: str
    evidence: list[dict]


class AnalysisClaim(RoleOutput):
    text: str
    source_ids: list[str]


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


class RunState(TypedDict, total=False):
    run_id: str
    user_query: str
    plan: TaskPlan
    sql_results: list[SqlArtifact]
    rag_results: list[RagArtifact]
    analysis: AnalysisResult
    verification: VerificationResult
    status: str
    trace: Annotated[list[str], operator.add]


class SqlSubState(TypedDict, total=False):
    plan: TaskPlan
    sql_results: list[SqlArtifact]


class RagSubState(TypedDict, total=False):
    plan: TaskPlan
    rag_results: list[RagArtifact]
