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

    # ------------------------------------------------------------------
    # 资产流水线精简 Flag（2026-09-17 重构）
    # ------------------------------------------------------------------
    profile_verification_rounds: int = Field(default=1, ge=1, le=3)
    """Profile 设备验证轮次；默认 1 轮，可通过 PROFILE_VERIFICATION_ROUNDS=3 恢复旧行为。"""

    hypium_replay_attempts: int = Field(default=1, ge=1, le=3)
    """主流程内联 Hypium 回放次数；默认 1 次，剩余由 POST /api/profiles/{id}/replay 异步追加。"""

    enable_legacy_run_modes: bool = False
    """是否允许 RunMode 使用 exploration/stability/reproduction；默认关闭，历史 trace 读取时静默降级为 regression。"""

    # ------------------------------------------------------------------
    # 直流模式（DC Mode）配置
    # ------------------------------------------------------------------
    dc_max_sessions: int = Field(default=8, ge=1, le=64)
    dc_idle_ttl_seconds: int = Field(default=1800, ge=60, le=86400)
    dc_history_turns: int = Field(default=12, ge=2, le=50)
    dc_event_buffer_size: int = Field(default=500, ge=50, le=5000)
    dc_default_tier: int = Field(default=2, ge=1, le=5)
    # 注入 prompt 的 UI 树摘要元素上限。默认值必须高到足以覆盖「底部弹窗/表单」类页面：
    # 105 个元素的页面里导航栏+侧边栏+背景月历就占了前 ~52 个，取 60 会把表单字段全部截断，
    # 模型因此只能盲点坐标、反复截图（见 docs/analysis/DC_RUN_20260915_FIX_PLAN.md 的
    # 「dc-20260916T160556Z-d23f9684 归因」一节）。
    dc_ui_tree_top_k: int = Field(default=200, ge=10, le=500)
    # 单轮模型请求上限（pydantic-ai 按「模型 HTTP 请求次数」计数，DC 每轮一个工具 ⇒ 约等于工具往返数）。
    # 与 dc_turn_timeout 的关系：按实测 ~14s/请求，120 次远大于 600s 轮次预算，默认先撞轮次墙钟
    # （错误码 turn_timeout，语义清晰），请求上限只作为防跑飞兜底。模型更慢时应同步调大
    # AGENT_MODEL_TIMEOUT / DC_TURN_TIMEOUT。
    dc_model_request_limit: int = Field(default=120, ge=1, le=1000)
    # 单轮工具调用上限；正常路径下模型每轮只调一个工具，故应 >= dc_model_request_limit。
    dc_model_tool_calls_limit: int = Field(default=200, ge=1, le=2000)
    # 展示用上下文窗口大小（仅用于把「单次请求 input_tokens」换算成占用百分比）。
    # 不参与任何限额判断：填错只会让百分比不准，不会影响预算与超时。
    dc_model_context_window: int = Field(default=128000, ge=1024, le=4000000)
    dc_screenshot_cache_frames: int = Field(default=3, ge=1, le=10)
    # 轮次总预算：必须 >= 单次模型超时，超时后轮次进入 failed/needs_attention
    dc_turn_timeout: float = Field(default=600, gt=0, le=3600)
    # 进度心跳间隔（工具与模型等待期间）
    dc_progress_interval: float = Field(default=1.5, ge=0.2, le=30)
    # 工具开始/结束时写 checkpoint 的最小间隔
    dc_checkpoint_interval: float = Field(default=2.0, ge=0, le=60)
    # 关闭/淘汰会话时等待轮次收尾的上限
    dc_close_timeout: float = Field(default=8.0, gt=0, le=120)

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
