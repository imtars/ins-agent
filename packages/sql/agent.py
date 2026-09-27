"""M3 SQL generation, validation, bounded execution, and at most two repairs."""

from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncEngine

from packages.sql.runtime import execute_readonly, schema_context
from packages.sql.security import UnsafeSQL, validate_sql

MAX_REPAIRS = 2


class SQLGenerator(Protocol):
    async def generate(self, question: str, schema: str, feedback: str | None,
                       attempt: int) -> dict: ...


@dataclass
class SQLAttempt:
    number: int
    sql: str | None
    stage: str
    error: str | None = None


@dataclass
class SQLAnswer:
    status: str
    rows: list[dict] | None
    sql: str | None
    attempts: list[SQLAttempt]


async def answer_question(engine: AsyncEngine, generator: SQLGenerator,
                          question: str) -> SQLAnswer:
    if not question.strip():
        raise ValueError("question is empty")
    schema = await schema_context(engine)
    attempts = []
    feedback = None
    for number in range(MAX_REPAIRS + 1):
        proposal = await generator.generate(question, schema, feedback, number)
        if proposal.get("decision") == "refuse":
            attempts.append(SQLAttempt(number, None, "refused"))
            return SQLAnswer("refused", None, None, attempts)
        sql = proposal.get("sql")
        if proposal.get("decision") != "query" or not isinstance(sql, str):
            feedback = "Return JSON with decision=query and a SQL string, or decision=refuse."
            attempts.append(SQLAttempt(number, None, "invalid_proposal", feedback))
            continue
        try:
            safe_sql = validate_sql(sql)
        except UnsafeSQL as exc:
            feedback = str(exc)
            attempts.append(SQLAttempt(number, sql, "validation_failed", feedback))
            continue
        try:
            rows = await execute_readonly(engine, safe_sql)
        except Exception as exc:
            # The failed SQL remains in the trace; final status never hides repair attempts.
            feedback = f"PostgreSQL execution failed: {type(exc).__name__}: {str(exc)[:300]}"
            attempts.append(SQLAttempt(number, safe_sql, "execution_failed", feedback))
            continue
        attempts.append(SQLAttempt(number, safe_sql, "executed"))
        return SQLAnswer("success", rows, safe_sql, attempts)
    return SQLAnswer("failed", None, None, attempts)
