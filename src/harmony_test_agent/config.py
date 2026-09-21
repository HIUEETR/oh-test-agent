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
    agent_model_retry_limit: int = Field(default=1, ge=0, le=3)
    """单步模型决策超时后的重试次数；重试沿用同一帧并附加「只输出工具决策」的收敛提示。"""
    agent_model_retry_backoff_seconds: float = Field(default=3.0, ge=0, le=30)
    """瞬时 provider 故障（429/5xx/网关限流）的退避秒数；单测可设为 0 免等待。"""
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

    profile_harvest_enabled: bool = True
    """收尾时把任务期真实定位器证据累加回 Profile（计划 3.3）。

    单测可通过 ``PROFILE_HARVEST_ENABLED=false`` 关闭，避免把产物写进仓库 ``profiles/``。"""

    # ------------------------------------------------------------------
    # Bootstrap 预算与 Profile 门禁阈值（计划 4.1/4.2）
    # ------------------------------------------------------------------
    bootstrap_enabled_on_task_run: bool = False
    """任务型运行（带明确 task 且非 bootstrap_only）是否前置完整探索。

    默认 False：Profile 由任务期证据回收（PROFILE_HARVEST）作为副产物建立，
    完整探索通过 ``POST /api/profiles/{id}/verify``（bootstrap_only=True）显式触发。
    显式打开可恢复「同一次运行先探索晋级再执行任务」的旧行为。"""

    bootstrap_max_pages: int = Field(default=8, ge=1, le=20)
    bootstrap_max_actions_per_page: int = Field(default=6, ge=1, le=8)
    bootstrap_max_duration_seconds: int = Field(default=300, ge=30, le=900)
    bootstrap_advisor_enabled: bool = True
    bootstrap_settle_timeout_seconds: int = Field(default=0, ge=0, le=30)
    """任务期每帧采集前的稳定轮询预算（秒）。

    ``ExplorationPolicy.settle_timeout_seconds`` 默认 1s：真机实测该轮询会再付一次
    ``dumpLayout + cat``（≈6s/帧），而每步已经在动作后固定 ``settle_seconds`` 等待，
    因此任务期默认关闭轮询（0）；需要更严格的过渡帧过滤时用 env 调回。"""
    """探索预算上界必须落在 ``ExplorationPolicy`` 的字段约束内（tests/unit/test_discovery.py 钉住）。"""

    profile_min_stable_locators: int = Field(default=3, ge=1, le=20)
    profile_min_page_states: int = Field(default=3, ge=1, le=20)
    profile_min_assertions: int = Field(default=2, ge=1, le=10)
    profile_min_interaction_kinds: int = Field(default=2, ge=1, le=3)
    """Profile 准入门禁阈值；单页应用调试时可把 ``profile_min_page_states`` 调到 1。"""

    # ------------------------------------------------------------------
    # Live 执行效率（计划 5.1/5.2/5.4）
    # ------------------------------------------------------------------
    merge_observe_and_decide: bool = True
    """把每步的 analyze + decide 合并为一次视觉请求；关闭即回退两次调用。"""

    model_element_limit: int = Field(default=80, ge=10, le=500)
    """送模型的元素列表上限（按分数排序取前 N），降低单次请求延迟。"""

    model_observation_element_limit: int = Field(default=5, ge=0, le=50)
    """合并观测里允许模型回吐的视觉元素上限。

    Live 的合并请求把「元素表」当**输入**给出，若再让模型把整页元素逐条生成进
    ``elements`` 输出，输出 token 会成为单次延迟的主因（真机实测一次回吐 41 条，
    16 步模型总耗时 478s）。这里只保留极少数「层级里没有、只在画面上可见」的控件。"""

    model_image_format: Literal["jpeg", "png"] = "jpeg"
    """送模型的截图格式；jpeg 显著降低上传字节数与延迟。"""

    live_decision_history_steps: int = Field(default=4, ge=0, le=10)
    """Live 决策注入的跨步历史条数；0 关闭（回退到只带最近一次失败反馈）。"""

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
    # token 级流式输出：开启后模型侧按增量发 message_delta，前端逐字显示。
    # 关闭即回退整块输出（每个 part 仍发全量 THINKING/AGENT_TEXT，行为与旧版一致）。
    dc_token_streaming: bool = True
    # 轮次成功结束后自动推断目标身份并发 CASE_SUGGESTED（计划 7）：关掉即回到「用户手填 bundle」。
    dc_auto_resolve_identity: bool = True

    # ------------------------------------------------------------------
    # 用例库 / 官方 xdevice harness / 执行结果分析配置
    # ------------------------------------------------------------------
    cases_dir: Path = Field(default=Path("artifacts/cases"))
    """用例库落盘根目录（用例 IR、双引擎产物、执行证据）。"""

    xdevice_timeout_seconds: float = Field(default=900, gt=0, le=7200)
    """官方 xdevice 用例执行的子进程超时上限。"""

    case_analysis_enabled: bool = True
    """执行结果分析总开关；分析仅作附加信息，永不翻转用例 passed。"""

    stress_max_iterations: int = Field(default=2000, ge=1, le=5000)
    """压力测试用例允许的最大循环轮数（IR 静态安全门禁的硬上限）。"""

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

    @computed_field
    @property
    def resolved_cases_dir(self) -> Path:
        """返回用例库落盘根目录的绝对路径。"""
        return self._resolve(self.cases_dir)

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
