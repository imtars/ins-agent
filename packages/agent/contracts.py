"""M6 node registry and fail-closed handoff validation."""

from dataclasses import dataclass
from typing import Awaitable, Callable, Mapping

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from packages.agent.models import (AnalysisResult, RagArtifact, SqlArtifact,
                                   TaskPlan, VerificationResult)


class ContractViolation(ValueError):
    """A node or tool produced data that cannot cross a handoff boundary."""


class BoundaryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True,
                              revalidate_instances="always")


class PlannerInput(BoundaryModel):
    user_query: str = Field(min_length=1)


class PlannerOutput(BoundaryModel):
    plan: TaskPlan
    trace: list[str]


class BranchInput(BoundaryModel):
    plan: TaskPlan


class SqlSubgraphOutput(BoundaryModel):
    sql_results: list[SqlArtifact]


class RagSubgraphOutput(BoundaryModel):
    rag_results: list[RagArtifact]


class SqlBranchOutput(SqlSubgraphOutput):
    trace: list[str]


class RagBranchOutput(RagSubgraphOutput):
    trace: list[str]


class ForkOutput(BoundaryModel):
    trace: list[str]


class SynthesisInput(BoundaryModel):
    plan: TaskPlan
    sql_results: list[SqlArtifact] = Field(default_factory=list)
    rag_results: list[RagArtifact] = Field(default_factory=list)

    @model_validator(mode="after")
    def required_route_artifacts(self):
        if self.plan.route in {"SQL", "BOTH", "REPORT"} and not self.sql_results:
            raise ValueError("route requires SQL artifacts")
        if self.plan.route in {"RAG", "BOTH", "REPORT"} and not self.rag_results:
            raise ValueError("route requires RAG artifacts")
        if self.plan.route == "SQL" and self.rag_results:
            raise ValueError("SQL route received RAG artifacts")
        if self.plan.route == "RAG" and self.sql_results:
            raise ValueError("RAG route received SQL artifacts")
        if len(self.sql_results) != len(self.plan.sql_tasks):
            raise ValueError("SQL artifact count does not match plan")
        if len(self.rag_results) != len(self.plan.rag_tasks):
            raise ValueError("RAG artifact count does not match plan")
        return self


class SynthesisOutput(BoundaryModel):
    analysis: AnalysisResult
    trace: list[str]


class VerifierInput(SynthesisInput):
    analysis: AnalysisResult


class VerifierOutput(BoundaryModel):
    verification: VerificationResult
    status: str
    trace: list[str]

    @model_validator(mode="after")
    def status_matches_verdict(self):
        if self.status != self.verification.status:
            raise ValueError("status differs from verification result")
        if self.status == "PASS" and self.verification.issues:
            raise ValueError("PASS cannot contain issues")
        return self


@dataclass(frozen=True)
class NodeContract:
    name: str
    input_model: type[BoundaryModel]
    output_model: type[BoundaryModel]
    upstream: tuple[str, ...] = ()

    @property
    def requires(self) -> frozenset[str]:
        return frozenset(name for name, field in self.input_model.model_fields.items()
                         if field.is_required())

    @property
    def optional(self) -> frozenset[str]:
        return frozenset(self.input_model.model_fields) - self.requires

    @property
    def produces(self) -> frozenset[str]:
        return frozenset(self.output_model.model_fields)

    @property
    def input_schema(self) -> dict:
        return self.input_model.model_json_schema()

    @property
    def output_schema(self) -> dict:
        return self.output_model.model_json_schema()

    def validate_input(self, state: Mapping) -> dict:
        fields = self.requires | self.optional
        payload = {key: state[key] for key in fields if key in state}
        try:
            result = self.input_model.model_validate(payload)
        except ValidationError as exc:
            raise ContractViolation(f"{self.name} input contract: {exc.errors()[0]['msg']}") from exc
        return {key: getattr(result, key) for key in fields}

    def validate_output(self, value: object) -> dict:
        try:
            result = self.output_model.model_validate(value)
        except ValidationError as exc:
            raise ContractViolation(f"{self.name} output contract: {exc.errors()[0]['msg']}") from exc
        return {key: getattr(result, key) for key in self.produces}


NODE_CONTRACTS = {
    "planner": NodeContract("planner", PlannerInput, PlannerOutput),
    "fork": NodeContract("fork", BranchInput, ForkOutput, ("planner",)),
    "sql_only": NodeContract("sql_only", BranchInput, SqlBranchOutput, ("planner",)),
    "rag_only": NodeContract("rag_only", BranchInput, RagBranchOutput, ("planner",)),
    "sql_parallel": NodeContract("sql_parallel", BranchInput, SqlBranchOutput, ("fork",)),
    "rag_parallel": NodeContract("rag_parallel", BranchInput, RagBranchOutput, ("fork",)),
    "synthesis": NodeContract("synthesis", SynthesisInput, SynthesisOutput,
                              ("planner", "sql_only", "rag_only",
                               "sql_parallel", "rag_parallel")),
    "verifier": NodeContract("verifier", VerifierInput, VerifierOutput, ("synthesis",)),
    "sql_subgraph.data_analyst": NodeContract("sql_subgraph.data_analyst", BranchInput,
                                              SqlSubgraphOutput, ("planner",)),
    "rag_subgraph.knowledge_researcher": NodeContract(
        "rag_subgraph.knowledge_researcher", BranchInput, RagSubgraphOutput, ("planner",)),
}


def validate_registry(main_nodes: set[str], subgraph_nodes: set[str]) -> None:
    registered = set(NODE_CONTRACTS)
    actual = main_nodes | subgraph_nodes
    if registered != actual:
        raise ContractViolation(f"registry nodes differ: missing={sorted(actual-registered)}, "
                                f"extra={sorted(registered-actual)}")
    if "analysis" not in NODE_CONTRACTS["verifier"].requires:
        raise ContractViolation("verifier must consume AnalysisResult")
    if NODE_CONTRACTS["verifier"].input_model.model_fields["analysis"].annotation \
            is not AnalysisResult:
        raise ContractViolation("verifier analysis input has wrong schema")

    def upstream_outputs(name: str, seen: set[str] | None = None) -> dict[str, set[object]]:
        seen = set() if seen is None else seen
        if name in seen:
            raise ContractViolation("contract registry contains a dependency cycle")
        result: dict[str, set[object]] = {}
        for upstream in NODE_CONTRACTS[name].upstream:
            if upstream not in NODE_CONTRACTS:
                raise ContractViolation(f"unknown producer: {upstream}")
            parent = NODE_CONTRACTS[upstream]
            for field in parent.produces:
                result.setdefault(field, set()).add(
                    parent.output_model.model_fields[field].annotation)
            for field, types in upstream_outputs(upstream, seen | {name}).items():
                result.setdefault(field, set()).update(types)
        return result

    for name, contract in NODE_CONTRACTS.items():
        available = upstream_outputs(name)
        for field in contract.requires:
            if name == "planner" and field == "user_query":
                continue  # The user query is the one external graph input.
            expected = contract.input_model.model_fields[field].annotation
            if expected not in available.get(field, set()):
                raise ContractViolation(f"{name} requires {field} without a typed producer")


def checked_node(name: str, node: Callable[[dict], Awaitable[dict]]):
    contract = NODE_CONTRACTS[name]

    async def invoke(state: dict) -> dict:
        checked = contract.validate_input(state)
        result = await node({**state, **checked})
        return contract.validate_output(result)

    return invoke
