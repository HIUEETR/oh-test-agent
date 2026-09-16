import pytest
from pydantic import SecretStr, ValidationError

from harmony_test_agent.config import Settings


def test_settings_redacts_model_key():
    settings = Settings(openai_api_key=SecretStr("super-secret"), agent_model="model")
    serialized = str(settings.redacted())
    assert "super-secret" not in serialized
    assert settings.redacted()["openai_api_key"] == "configured"


def test_dc_model_budget_defaults_are_relaxed():
    """DC 单轮预算默认值：历史事故的 request_limit=30 被硬编码，必须已放宽且可配置。"""
    settings = Settings(_env_file=None)

    assert settings.dc_model_request_limit == 120
    assert settings.dc_model_tool_calls_limit == 200
    # 工具调用上限应 >= 请求上限：正常路径下模型每轮只调一个工具
    assert settings.dc_model_tool_calls_limit >= settings.dc_model_request_limit


def test_dc_max_turn_steps_is_gone():
    """死配置已移除：它从未被任何代码读取，语义与真实预算（请求/工具上限）不一致。"""
    assert "dc_max_turn_steps" not in Settings(_env_file=None).model_dump()


@pytest.mark.parametrize("field", ["dc_model_request_limit", "dc_model_tool_calls_limit"])
def test_dc_model_budget_rejects_non_positive(field: str):
    """不允许用 0/负数静默关闭预算保护。"""
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: 0})


def test_dc_model_budget_is_env_overridable(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DC_MODEL_REQUEST_LIMIT", "55")
    monkeypatch.setenv("DC_MODEL_TOOL_CALLS_LIMIT", "66")

    settings = Settings(_env_file=None)

    assert settings.dc_model_request_limit == 55
    assert settings.dc_model_tool_calls_limit == 66


def test_dc_context_window_default_and_bounds():
    """上下文窗口只用于展示占用比例，不参与限额判断。"""
    assert Settings(_env_file=None).dc_model_context_window == 128000

    with pytest.raises(ValidationError):
        Settings(_env_file=None, dc_model_context_window=10)


# ---------------------------------------------------------------------------
# 资产流水线精简 Flag（2026-09-17 重构）
# ---------------------------------------------------------------------------


def test_profile_verification_rounds_default_is_one():
    """默认 1 轮设备验证；3 轮旧行为只能显式开启。"""
    assert Settings(_env_file=None).profile_verification_rounds == 1


def test_hypium_replay_attempts_default_is_one():
    """主流程默认只内联 1 次 Hypium 回放。"""
    assert Settings(_env_file=None).hypium_replay_attempts == 1


def test_enable_legacy_run_modes_default_false():
    """历史 RunMode 默认不启用（读取历史 trace 时静默降级为 regression）。"""
    assert Settings(_env_file=None).enable_legacy_run_modes is False


@pytest.mark.parametrize("field", ["profile_verification_rounds", "hypium_replay_attempts"])
@pytest.mark.parametrize("value", [0, 4])
def test_pipeline_count_flags_are_bounded(field: str, value: int):
    """轮次/次数限定在 1..3：不允许 0（静默关闭门禁）或 >3（超出设备验收预算）。"""
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})


@pytest.mark.parametrize(
    ("env_name", "field"),
    [
        ("PROFILE_VERIFICATION_ROUNDS", "profile_verification_rounds"),
        ("HYPIUM_REPLAY_ATTEMPTS", "hypium_replay_attempts"),
    ],
)
def test_pipeline_count_flags_are_env_overridable(monkeypatch: pytest.MonkeyPatch, env_name: str, field: str):
    """环境变量是 Phase 1 的即时回滚手段，必须生效。"""
    monkeypatch.setenv(env_name, "3")

    assert getattr(Settings(_env_file=None), field) == 3


def test_enable_legacy_run_modes_is_env_overridable(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ENABLE_LEGACY_RUN_MODES", "true")

    assert Settings(_env_file=None).enable_legacy_run_modes is True
