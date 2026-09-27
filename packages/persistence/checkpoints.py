"""Explicit checkpoint deserialization allowlist for M7 graph state."""

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer


ALLOWED_STATE_MODELS = tuple(("packages.agent.models", name) for name in (
    "TaskPlan", "SqlArtifact", "RagArtifact", "Evidence", "ToolAttempt",
    "AnalysisResult", "AnalysisClaim", "VerificationResult", "ApprovalResult",
    "PublicationReceipt",
)) + (("packages.persistence.artifacts", "ArtifactRef"),
      ("packages.persistence.reviews", "ReviewDecision"))


def checkpoint_serializer() -> JsonPlusSerializer:
    return JsonPlusSerializer(allowed_msgpack_modules=ALLOWED_STATE_MODELS)
