"""Central configuration. No service connects to external systems at import time."""

from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = (
        "postgresql+asyncpg://insurance_app:change-me-local-only"
        "@localhost:5432/insurance_harness"
    )
    llm_provider: str = "deepseek"
    llm_model: str = "deepseek-flash"
    deepseek_api_key: SecretStr | None = None
    milvus_uri: str = "http://localhost:19530"
    fault_injection_enabled: bool = False


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
