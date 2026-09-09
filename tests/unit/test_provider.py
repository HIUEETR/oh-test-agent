from harmony_test_agent.agents.providers import OpenAICompatibleProvider, align_plan_with_task
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
