"""Conservative PostgreSQL SELECT validator; database privileges are the final guard."""

import sqlglot
from sqlglot import exp

BUSINESS_TABLES = frozenset({"branches", "agents", "customers", "products",
                             "policies", "claims", "claim_payments"})
ALLOWED_FUNCTIONS = frozenset({
    "AND", "OR", "NOT", "CAST", "TRY_CAST", "CASE",
    "COUNT", "SUM", "AVG", "MIN", "MAX", "ROUND", "ABS", "CEIL", "FLOOR",
    "COALESCE", "NULLIF", "GREATEST", "LEAST", "DATE_TRUNC", "EXTRACT",
    "DATE_PART", "TO_CHAR", "LOWER", "UPPER", "LENGTH", "SUBSTRING",
})
FORBIDDEN_NODES = (exp.Insert, exp.Update, exp.Delete, exp.Create, exp.Drop,
                   exp.Alter, exp.Merge, exp.Command, exp.Copy, exp.Lock)


class UnsafeSQL(ValueError):
    pass


def validate_sql(sql: str) -> str:
    if not sql.strip() or len(sql) > 20_000:
        raise UnsafeSQL("empty or oversized SQL")
    try:
        statements = sqlglot.parse(sql, read="postgres", error_level="RAISE")
    except sqlglot.errors.SqlglotError as exc:
        raise UnsafeSQL(f"SQL parse failed: {exc}") from exc
    if len(statements) != 1 or not isinstance(statements[0], exp.Select):
        raise UnsafeSQL("exactly one SELECT or WITH SELECT is required")
    root = statements[0]
    if root.find(FORBIDDEN_NODES) or any(isinstance(node, exp.Lock) for node in root.walk()):
        raise UnsafeSQL("mutating or locking SQL is forbidden")
    ctes = {node.alias.lower() for node in root.find_all(exp.CTE)}
    business_seen = False
    for table in root.find_all(exp.Table):
        name = table.name.lower()
        if table.catalog or (table.db and table.db.lower() != "public"):
            raise UnsafeSQL("only public business tables may be queried")
        if name not in ctes:
            if name not in BUSINESS_TABLES:
                raise UnsafeSQL(f"table is not allowlisted: {name}")
            business_seen = True
    if not business_seen:
        raise UnsafeSQL("query must reference a business table")
    for func in root.find_all(exp.Func):
        name = (func.name if isinstance(func, exp.Anonymous) else func.sql_name()).upper()
        if name not in ALLOWED_FUNCTIONS:
            raise UnsafeSQL(f"function is not allowlisted: {name}")
    if any(isinstance(node, exp.Into) for node in root.walk()):
        raise UnsafeSQL("SELECT INTO is forbidden")
    return sql.strip().rstrip(";")
