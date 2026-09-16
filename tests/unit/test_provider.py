from harmony_test_agent.agents.providers import (
    OpenAICompatibleProvider,
    align_plan_with_task,
    usage_aware_chat_model_class,
)
from harmony_test_agent.config import Settings
from harmony_test_agent.models import PlannedStep, ToolName


def make_settings(*, disable_thinking: bool) -> Settings:
    return Settings(
        _env_file=None,
        openai_api_key="test-key",
        agent_model="test-model",
        agent_vision_model="test-vision-model",
        agent_provider="openai",
        agent_disable_thinking=disable_thinking,
    )


def test_provider_can_disable_thinking_for_structured_output() -> None:
    provider = OpenAICompatibleProvider(make_settings(disable_thinking=True))

    assert provider._model_settings() == {"extra_body": {"thinking": {"type": "disabled"}}}


def test_provider_does_not_send_vendor_specific_body_by_default() -> None:
    provider = OpenAICompatibleProvider(make_settings(disable_thinking=False))

    assert provider._model_settings() is None


def test_plan_alignment_removes_unrequested_search_submission() -> None:
    steps = [
        PlannedStep(step_id="1", instruction="输入关键词", tool=ToolName.INPUT_TEXT, text="OpenHarmony"),
        PlannedStep(step_id="2", instruction="点击搜索按钮", tool=ToolName.CLICK_ELEMENT, target="搜索按钮"),
        PlannedStep(step_id="3", instruction="等待搜索结果", tool=ToolName.WAIT, wait_seconds=2),
        PlannedStep(step_id="4", instruction="确认搜索结果", tool=ToolName.ASSERT_VISIBLE, target="搜索结果"),
        PlannedStep(step_id="5", instruction="返回首页", tool=ToolName.BACK),
        PlannedStep(step_id="6", instruction="确认首页", tool=ToolName.ASSERT_VISIBLE, target="首页"),
        PlannedStep(step_id="finish", instruction="结束", tool=ToolName.FINISH),
    ]

    aligned = align_plan_with_task("输入 OpenHarmony 后返回首页", steps, max_steps=20)

    assert [step.tool for step in aligned] == [
        ToolName.INPUT_TEXT,
        ToolName.BACK,
        ToolName.BACK,
        ToolName.ASSERT_VISIBLE,
        ToolName.FINISH,
    ]
    assert "键盘" in aligned[1].instruction


def test_plan_alignment_preserves_explicit_search_submission() -> None:
    steps = [
        PlannedStep(step_id="1", instruction="输入关键词", tool=ToolName.INPUT_TEXT),
        PlannedStep(step_id="2", instruction="提交搜索", tool=ToolName.CLICK_COORDINATE, coordinate=(10, 10)),
        PlannedStep(step_id="3", instruction="确认搜索结果", tool=ToolName.ASSERT_VISIBLE, target="搜索结果"),
        PlannedStep(step_id="finish", instruction="结束", tool=ToolName.FINISH),
    ]

    aligned = align_plan_with_task("输入关键词并提交搜索，确认搜索结果", steps, max_steps=4)

    assert [step.tool for step in aligned] == [
        ToolName.INPUT_TEXT,
        ToolName.CLICK_COORDINATE,
        ToolName.ASSERT_VISIBLE,
        ToolName.FINISH,
    ]


def test_plan_alignment_keeps_finish_within_step_limit() -> None:
    steps = [PlannedStep(step_id=str(index), instruction="检查", tool=ToolName.INSPECT_SCREEN) for index in range(5)]

    aligned = align_plan_with_task("检查页面", steps, max_steps=3)

    assert len(aligned) == 3
    assert aligned[-1].tool == ToolName.FINISH


def test_plan_alignment_does_not_add_keyboard_back_after_home_return() -> None:
    steps = [
        PlannedStep(step_id="1", instruction="输入关键词", tool=ToolName.INPUT_TEXT),
        PlannedStep(step_id="2", instruction="关闭软键盘", tool=ToolName.BACK),
        PlannedStep(step_id="3", instruction="返回首页", tool=ToolName.BACK),
        PlannedStep(step_id="4", instruction="打开详情", tool=ToolName.CLICK_ELEMENT, target="详情"),
        PlannedStep(step_id="5", instruction="从详情返回首页", tool=ToolName.BACK),
        PlannedStep(step_id="finish", instruction="结束", tool=ToolName.FINISH),
    ]

    aligned = align_plan_with_task("输入后返回首页，再打开详情并返回首页", steps, max_steps=20)

    assert [step.step_id for step in aligned] == ["1", "2", "3", "4", "5", "finish"]


def test_plan_alignment_treats_search_keyword_as_explicit_submission() -> None:
    steps = [
        PlannedStep(step_id="1", instruction="输入关键词", tool=ToolName.INPUT_TEXT),
        PlannedStep(step_id="2", instruction="提交搜索", tool=ToolName.CLICK_COORDINATE, coordinate=(10, 10)),
        PlannedStep(step_id="3", instruction="确认搜索结果", tool=ToolName.ASSERT_VISIBLE, target="搜索结果"),
        PlannedStep(step_id="finish", instruction="结束", tool=ToolName.FINISH),
    ]

    aligned = align_plan_with_task("搜索 OpenHarmony 并确认结果", steps, max_steps=4)

    assert [step.step_id for step in aligned] == ["1", "2", "3", "finish"]


# ---------------------------------------------------------------------------
# usage 映射：pydantic-ai 1.73.0 会丢掉自建 OpenAI 兼容端点的 token 与缓存计数
# ---------------------------------------------------------------------------

USAGE_RESPONSE = {
    "id": "chatcmpl-test",
    "object": "chat.completion",
    "created": 1789575000,
    "model": "test-model",
    "choices": [
        {
            "index": 0,
            "finish_reason": "stop",
            "message": {"role": "assistant", "content": "pong"},
        }
    ],
    "usage": {
        "prompt_tokens": 1721,
        "completion_tokens": 16,
        "total_tokens": 1737,
        "prompt_tokens_details": {"cached_tokens": 1536, "audio_tokens": 0, "video_tokens": 0},
        "completion_tokens_details": {"reasoning_tokens": 16, "image_tokens": 0},
        "cache_creation_input_tokens": 0,
    },
}


def _model() -> object:
    """构造 usage-aware 模型实例（不发起任何网络请求）。"""
    from pydantic_ai.providers.openai import OpenAIProvider

    settings = make_settings(disable_thinking=False)
    provider = OpenAIProvider(
        base_url=settings.openai_base_url,
        api_key=settings.openai_api_key.get_secret_value(),
    )
    return usage_aware_chat_model_class()("test-model", provider=provider)


def _map(response_payload: dict) -> object:
    from openai.types.chat import ChatCompletion

    model = _model()
    return model._map_usage(ChatCompletion.model_validate(response_payload))  # type: ignore[attr-defined]


def test_usage_mapping_recovers_tokens_and_cache_reads() -> None:
    """上游会把 input/output/cache 全部丢成 0，这里必须如实取到。"""
    usage = _map(USAGE_RESPONSE)

    assert usage.input_tokens == 1721
    assert usage.output_tokens == 16
    assert usage.cache_read_tokens == 1536
    # prompt_tokens 已包含缓存命中部分：不能用 input + cached 作为分母
    assert usage.cache_read_tokens < usage.input_tokens
    assert usage.details["reasoning_tokens"] == 16
    assert usage.details["cached_tokens"] == 1536


def test_usage_mapping_cache_hit_rate_semantics() -> None:
    """命中率 = cache_read / input（实测语义，锁定不被改成 input + cached）。"""
    usage = _map(USAGE_RESPONSE)

    assert abs(usage.cache_read_tokens / usage.input_tokens - 0.8925) < 0.001


def test_usage_mapping_handles_missing_details() -> None:
    """provider 不返回 *_details 时不抛错，缓存计数为 0。"""
    payload = dict(USAGE_RESPONSE)
    payload["usage"] = {"prompt_tokens": 100, "completion_tokens": 5, "total_tokens": 105}

    usage = _map(payload)

    assert usage.input_tokens == 100
    assert usage.output_tokens == 5
    assert usage.cache_read_tokens == 0
    assert usage.cache_write_tokens == 0


def test_usage_mapping_without_usage_falls_back_to_upstream() -> None:
    """没有 usage 字段时委托上游，不把「无数据」伪装成 0 成本。"""
    payload = dict(USAGE_RESPONSE)
    payload.pop("usage")

    usage = _map(payload)

    assert usage.input_tokens == 0
    assert usage.details == {}
