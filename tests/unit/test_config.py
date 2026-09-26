from packages.domain.config import Settings


def test_default_model_and_fault_injection_disabled(monkeypatch):
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.delenv("FAULT_INJECTION_ENABLED", raising=False)
    settings = Settings(_env_file=None)

    assert settings.llm_model == "deepseek-flash"
    assert settings.fault_injection_enabled is False


def test_environment_override_and_secret_redaction(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "test-model")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-secret-value")
    monkeypatch.setenv("FAULT_INJECTION_ENABLED", "true")
    settings = Settings(_env_file=None)

    assert settings.llm_model == "test-model"
    assert settings.fault_injection_enabled is True
    assert settings.deepseek_api_key.get_secret_value() == "test-secret-value"
    assert "test-secret-value" not in repr(settings)
