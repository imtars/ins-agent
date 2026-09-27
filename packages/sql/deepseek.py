"""Real DeepSeek JSON-mode SQL generator for the M3 evaluation run."""

import json

import httpx

from packages.domain.config import get_settings

SYSTEM = """You generate PostgreSQL SELECT queries for a wholly synthetic insurance database.
Return JSON only: {"decision":"query","sql":"SELECT ..."} or
{"decision":"refuse","sql":null}. Refuse unsafe writes and questions not answerable
from the listed business columns. Use only the supplied schema. Never use a
generator manifest or assume real customer facts. Use one SELECT/CTE statement.
For quarterly earned premium, allocate policy annual_premium by overlap days / 365;
policy end_date is exclusive. For claim count use claim_date in the interval.
For incurred claim amount include settled and open claims, excluding denied claims.
For payment totals use payment_date in the interval. Do not call paid amount
"incurred claims". Waiting-period-eligible exposure is not in the SQL schema;
if asked for that exact measure, refuse. JSON response required."""


class DeepSeekSQLGenerator:
    def __init__(self, *, model: str | None = None, base_url: str = "https://api.deepseek.com"):
        settings = get_settings()
        if not settings.deepseek_api_key or not settings.deepseek_api_key.get_secret_value():
            raise ValueError("DEEPSEEK_API_KEY is required for real SQL generation evaluation")
        self.api_key = settings.deepseek_api_key.get_secret_value()
        self.model = model or settings.llm_model
        self.base_url = base_url.rstrip("/")

    async def generate(self, question: str, schema: str, feedback: str | None,
                       attempt: int) -> dict:
        message = f"Schema:\n{schema}\n\nQuestion:\n{question}"
        if feedback:
            message += f"\n\nPrevious attempt failed. Error:\n{feedback}"
        payload = {"model": self.model,
                   "messages": [{"role": "system", "content": SYSTEM},
                                {"role": "user", "content": message}],
                   "response_format": {"type": "json_object"},
                   "temperature": 0, "max_tokens": 1200, "stream": False}
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(f"{self.base_url}/chat/completions", json=payload,
                                         headers={"Authorization": f"Bearer {self.api_key}"})
            response.raise_for_status()
            body = response.json()
        choice = body["choices"][0]
        if choice["finish_reason"] != "stop":
            raise RuntimeError(f"LLM completion did not finish: {choice['finish_reason']}")
        return json.loads(choice["message"]["content"])
