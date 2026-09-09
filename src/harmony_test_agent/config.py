from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore", case_sensitive=False)

    openai_base_url: str = "https://api.openai.com/v1"
    openai_api_key: SecretStr | None = None
    agent_model: str | None = None
    agent_vision_model: str | None = None
    agent_provider: Literal["auto", "openai", "mock"] = "auto"
    agent_disable_thinking: bool = False
    hdc_path: str | None = None
    harmony_device: str = "127.0.0.1:5555"
    runtime_dir: Path = Field(default=Path("artifacts/runs"))
    database_path: Path = Field(default=Path("artifacts/agent.db"))
    target_profile_path: Path = Field(default=Path("profiles/zhihu-plus.json"))
    runtime_home: Path = Field(default=Path(".runtime-user"))
    agent_max_steps: int = Field(default=20, ge=1, le=100)
    agent_action_timeout: float = Field(default=30, gt=0, le=300)
    agent_model_timeout: float = Field(default=90, gt=0, le=300)
    agent_retry_limit: int = Field(default=2, ge=0, le=5)
    unchanged_screen_limit: int = Field(default=2, ge=1, le=5)
    vlm_min_confidence: float = Field(default=0.55, ge=0, le=1)

    @computed_field
    @property
    def resolved_runtime_dir(self) -> Path:
        return self._resolve(self.runtime_dir)

    @computed_field
    @property
    def resolved_database_path(self) -> Path:
        return self._resolve(self.database_path)

    @computed_field
    @property
    def resolved_target_profile_path(self) -> Path:
        return self._resolve(self.target_profile_path)

    @computed_field
    @property
    def resolved_runtime_home(self) -> Path:
        return self._resolve(self.runtime_home)

    @property
    def model_configured(self) -> bool:
        return bool(self.openai_api_key and self.agent_model)

    @property
    def vision_model_configured(self) -> bool:
        return bool(self.openai_api_key and (self.agent_vision_model or self.agent_model))

    def redacted(self) -> dict[str, object]:
        data = self.model_dump(exclude={"openai_api_key"}, mode="json")
        data["openai_api_key"] = "configured" if self.openai_api_key else "not_configured"
        return data

    @staticmethod
    def _resolve(value: Path) -> Path:
        return value if value.is_absolute() else ROOT / value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
