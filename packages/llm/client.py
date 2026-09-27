"""Small OpenAI-compatible JSON client; prefer the local CLIProxyAPI Luna route."""

import json
from pathlib import Path
from typing import Protocol

import httpx
import yaml

from packages.domain.config import get_settings


class JsonModel(Protocol):
    async def complete_json(self, role: str, system: str, user: str,
                            *, max_tokens: int = 1200) -> dict: ...


class JsonChatClient:
    def __init__(self, *, provider: str, model: str, base_url: str, api_key: str):
        self.provider = provider
        self.model = model
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self.response_models: list[str | None] = []
        self.roles_called: list[str] = []
        self.parse_failures: list[str] = []

    async def complete_json(self, role: str, system: str, user: str,
                            *, max_tokens: int = 1200) -> dict:
        for attempt in range(2):
            request_user = user if attempt == 0 else (
                user + "\n\nYour previous response was not a single valid JSON object. "
                "Return exactly one complete JSON object, with no second object or prose.")
            payload = {"model": self.model,
                       "messages": [{"role": "system", "content": system},
                                    {"role": "user", "content": request_user}],
                       "response_format": {"type": "json_object"},
                       "temperature": 0, "max_tokens": max_tokens, "stream": False}
            if self.provider == "DeepSeek":
                payload["thinking"] = {"type": "enabled"}
                payload["reasoning_effort"] = "high"
                payload["max_tokens"] = max(max_tokens, 8192)
            async with httpx.AsyncClient(timeout=120, trust_env=False) as client:
                response = await client.post(f"{self.base_url}/chat/completions", json=payload,
                                             headers={"Authorization": f"Bearer {self._api_key}"})
                response.raise_for_status()
                body = response.json()
            model = body.get("model")
            self.response_models.append(model if isinstance(model, str) and model else None)
            self.roles_called.append(role)
            choice = body["choices"][0]
            if choice["finish_reason"] != "stop":
                raise RuntimeError(f"LLM completion did not finish: {choice['finish_reason']}")
            try:
                content = json.loads(choice["message"]["content"])
                if not isinstance(content, dict):
                    raise ValueError("LLM must return a JSON object")
                return content
            except (json.JSONDecodeError, ValueError) as exc:
                self.parse_failures.append(f"{role}:{type(exc).__name__}")
                if attempt == 1:
                    raise ValueError(f"{role} did not return a single JSON object") from exc
        raise AssertionError("unreachable")


def select_chat_client(provider: str = "auto", *,
                       proxy_config: Path = Path.home() / ".cli-proxy-api/config.yaml",
                       fallback_key_file: Path = Path("/home/xubei/projects/jobs/dsv4_key"),
                       ) -> JsonChatClient:
    if provider not in {"auto", "proxy", "deepseek"}:
        raise ValueError("provider must be auto, proxy, or deepseek")
    if provider in {"auto", "proxy"}:
        try:
            config = yaml.safe_load(proxy_config.read_text(encoding="utf-8"))
            key, port = config["api-keys"][0], int(config["port"])
            if not key or not 1 <= port <= 65535:
                raise ValueError("invalid local proxy configuration")
            base_url = f"http://127.0.0.1:{port}/v1"
            response = httpx.get(f"{base_url}/models",
                                 headers={"Authorization": f"Bearer {key}"},
                                 timeout=10, trust_env=False)
            response.raise_for_status()
            if "gpt-6-luna" not in {item["id"] for item in response.json()["data"]}:
                raise ValueError("local proxy does not offer gpt-6-luna")
            return JsonChatClient(provider="CLIProxyAPI", model="gpt-6-luna",
                                  base_url=base_url, api_key=key)
        except (OSError, KeyError, IndexError, TypeError, ValueError,
                httpx.HTTPError) as exc:
            if provider == "proxy":
                raise RuntimeError(f"local CLIProxyAPI unavailable: {type(exc).__name__}") from exc
    settings = get_settings()
    key = settings.deepseek_api_key.get_secret_value() if settings.deepseek_api_key else ""
    if not key and fallback_key_file.is_file():
        with fallback_key_file.open(encoding="utf-8") as file:
            key = file.readline().strip()
    if not key:
        raise ValueError("no available M5 model provider or DeepSeek fallback key")
    return JsonChatClient(provider="DeepSeek", model=settings.llm_model,
                          base_url="https://api.deepseek.com", api_key=key)
