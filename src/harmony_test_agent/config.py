"""集中读取环境配置，并将相对运行路径基于仓库根目录解析为绝对路径。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, SecretStr, computed_field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """声明测试代理可由环境变量覆盖的配置，并提供解析后的路径与脱敏视图。"""

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
    target_profile_path: Path | None = Field(default=Path("profiles/zhihu-plus.json"))
    profiles_dir: Path = Field(default=Path("profiles"))
    runtime_home: Path = Field(default=Path(".runtime-user"))
    agent_max_steps: int = Field(default=20, ge=1, le=100)
    agent_action_timeout: float = Field(default=30, gt=0, le=300)
    agent_model_timeout: float = Field(default=90, gt=0, le=300)
    agent_retry_limit: int = Field(default=2, ge=0, le=5)
    agent_step_recovery_limit: int = Field(default=2, ge=0, le=5)
    unchanged_screen_limit: int = Field(default=2, ge=1, le=5)
    vlm_min_confidence: float = Field(default=0.55, ge=0, le=1)
    harmony_cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: [
            "http://127.0.0.1:5173",
            "http://localhost:5173",
            "http://127.0.0.1:15173",
            "http://localhost:15173",
        ]
    )

    @field_validator("harmony_cors_origins", mode="before")
    @classmethod
    def parse_cors_origins(cls, value: object) -> object:
        """Accept a comma-separated environment value or a native list."""
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @computed_field
    @property
    def resolved_runtime_dir(self) -> Path:
        """返回用于保存每次运行产物的绝对目录。"""
        return self._resolve(self.runtime_dir)

    @computed_field
    @property
    def resolved_database_path(self) -> Path:
        """返回运行数据库的绝对路径。"""
        return self._resolve(self.database_path)

    @computed_field
    @property
    def resolved_target_profile_path(self) -> Path | None:
        """返回旧版显式 Profile 覆盖路径；未配置时不构成运行硬依赖。"""
        return self._resolve(self.target_profile_path) if self.target_profile_path else None

    @computed_field
    @property
    def resolved_profiles_dir(self) -> Path:
        """返回 Profile Registry 根目录。"""
        return self._resolve(self.profiles_dir)

    @computed_field
    @property
    def resolved_runtime_home(self) -> Path:
        """返回 Hypium 运行用户目录的绝对路径。"""
        return self._resolve(self.runtime_home)

    @property
    def model_configured(self) -> bool:
        """判断文本规划模型所需的密钥和模型名是否齐备。"""
        return bool(self.openai_api_key and self.agent_model)

    @property
    def vision_model_configured(self) -> bool:
        """判断视觉分析是否可以使用专用模型或回退模型。"""
        return bool(self.openai_api_key and (self.agent_vision_model or self.agent_model))

    def redacted(self) -> dict[str, object]:
        """返回可安全展示的配置字典，并隐藏 API 密钥内容。"""
        data = self.model_dump(exclude={"openai_api_key"}, mode="json")
        data["openai_api_key"] = "configured" if self.openai_api_key else "not_configured"
        return data

    @staticmethod
    def _resolve(value: Path) -> Path:
        return value if value.is_absolute() else ROOT / value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """返回进程内缓存的配置实例。"""
    return Settings()
