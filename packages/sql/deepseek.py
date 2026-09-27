"""OpenAI-compatible JSON SQL generation for the M3 evaluation."""

import json
from pathlib import Path

import httpx
import yaml

from packages.domain.config import get_settings

SYSTEM = """You generate PostgreSQL SELECT queries for a wholly synthetic insurance database.
Return JSON only: {"decision":"query","sql":"SELECT ..."} or
{"decision":"refuse","sql":null}. Refuse unsafe writes and questions not answerable
from the listed business columns. Use only the supplied schema. Never use a
generator manifest or assume real customer facts. Use one SELECT/CTE statement.
Translate natural-language category labels to stored codes using the supplied
business value dictionary before writing WHERE conditions.
For quarterly earned premium, allocate policy annual_premium by overlap days / 365;
policy end_date is exclusive. For claim count use claim_date in the interval.
For incurred claim amount include settled and open claims, excluding denied claims.
For payment totals use payment_date in the interval. Do not call paid amount
"incurred claims". Waiting-period-eligible exposure is not in the SQL schema;
if asked for that exact measure, refuse. JSON response required."""


class OpenAICompatibleSQLGenerator:
    def __init__(self, *, provider: str, model: str, base_url: str, api_key: str,
                 max_tokens: int = 1200):
        if not 1 <= max_tokens <= 16384:
            raise ValueError("max_tokens must be between 1 and 16384")
        self.provider = provider
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.max_tokens = max_tokens
        self.response_models: list[str | None] = []

    async def generate(self, question: str, schema: str, feedback: str | None,
                       attempt: int) -> dict:
        message = f"Schema:\n{schema}\n\nQuestion:\n{question}"
        if feedback:
            message += f"\n\nPrevious attempt failed. Error:\n{feedback}"
        payload = {"model": self.model,
                   "messages": [{"role": "system", "content": SYSTEM},
                                {"role": "user", "content": message}],
                   "response_format": {"type": "json_object"},
                   "temperature": 0, "max_tokens": self.max_tokens, "stream": False}
        async with httpx.AsyncClient(timeout=120, trust_env=False) as client:
            response = await client.post(f"{self.base_url}/chat/completions", json=payload,
                                         headers={"Authorization": f"Bearer {self.api_key}"})
            response.raise_for_status()
            body = response.json()
        response_model = body.get("model")
        self.response_models.append(response_model if isinstance(response_model, str)
                                    and response_model else None)
        choice = body["choices"][0]
        if choice["finish_reason"] != "stop":
            raise RuntimeError(f"LLM completion did not finish: {choice['finish_reason']}")
        return json.loads(choice["message"]["content"])


def select_sql_generator(provider: str = "auto", *,
                         proxy_config: Path = Path.home() / ".cli-proxy-api/config.yaml",
                         fallback_key_file: Path = Path("/home/xubei/projects/jobs/dsv4_key"),
                         max_tokens: int = 1200,
                         ) -> OpenAICompatibleSQLGenerator:
    """Choose one live provider for an entire run; never switch mid-evaluation."""
    if provider not in {"auto", "proxy", "deepseek"}:
        raise ValueError(f"unknown provider: {provider}")
    if provider in {"auto", "proxy"}:
        try:
            config = yaml.safe_load(proxy_config.read_text(encoding="utf-8"))
            key = config["api-keys"][0]
            port = int(config["port"])
            if not key or not 1 <= port <= 65535:
                raise ValueError("invalid local proxy configuration")
            base_url = f"http://127.0.0.1:{port}/v1"
            response = httpx.get(f"{base_url}/models",
                                 headers={"Authorization": f"Bearer {key}"},
                                 timeout=10, trust_env=False)
            response.raise_for_status()
            if "gpt-6-luna" not in {item["id"] for item in response.json()["data"]}:
                raise ValueError("local proxy does not offer gpt-6-luna")
            return OpenAICompatibleSQLGenerator(provider="CLIProxyAPI", model="gpt-6-luna",
                                                 base_url=base_url, api_key=key,
                                                 max_tokens=max_tokens)
        except (OSError, KeyError, IndexError, TypeError, ValueError,
                httpx.HTTPError) as exc:
            if provider == "proxy":
                raise RuntimeError(f"local CLIProxyAPI unavailable: {type(exc).__name__}") from exc

    settings = get_settings()
    key = (settings.deepseek_api_key.get_secret_value()
           if settings.deepseek_api_key else "")
    if not key and fallback_key_file.is_file():
        with fallback_key_file.open(encoding="utf-8") as file:
            key = file.readline().strip()
    if not key:
        raise ValueError("no available SQL evaluation provider or DeepSeek fallback key")
    return OpenAICompatibleSQLGenerator(provider="DeepSeek", model=settings.llm_model,
                                         base_url="https://api.deepseek.com", api_key=key,
                                         max_tokens=max_tokens)


class DeepSeekSQLGenerator(OpenAICompatibleSQLGenerator):
    """Compatibility wrapper for callers that explicitly require DeepSeek."""

    def __init__(self, *, model: str | None = None, base_url: str = "https://api.deepseek.com"):
        settings = get_settings()
        if not settings.deepseek_api_key or not settings.deepseek_api_key.get_secret_value():
            raise ValueError("DEEPSEEK_API_KEY is required for DeepSeekSQLGenerator")
        super().__init__(provider="DeepSeek", model=model or settings.llm_model,
                         base_url=base_url,
                         api_key=settings.deepseek_api_key.get_secret_value())
