from pydantic import SecretStr

from harmony_test_agent.config import Settings


def test_settings_redacts_model_key():
    settings = Settings(openai_api_key=SecretStr("super-secret"), agent_model="model")
    serialized = str(settings.redacted())
    assert "super-secret" not in serialized
    assert settings.redacted()["openai_api_key"] == "configured"
