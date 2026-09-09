from harmony_test_agent.agents.orchestrator import AgentOrchestrator
from harmony_test_agent.models import PlannedStep, ToolDecision, ToolName


def test_premature_finish_is_replaced_with_planned_tool() -> None:
    step = PlannedStep(step_id="open", instruction="打开应用", tool=ToolName.OPEN_APP)
    decision = ToolDecision(tool=ToolName.FINISH, text="应用已经打开")

    constrained = AgentOrchestrator._constrain_finish_decision(step, decision)

    assert constrained.tool == ToolName.OPEN_APP
    assert "finish is only valid" in constrained.reasoning


def test_non_finish_adaptation_is_preserved() -> None:
    step = PlannedStep(step_id="back", instruction="返回首页", tool=ToolName.BACK)
    decision = ToolDecision(tool=ToolName.INSPECT_SCREEN)

    assert AgentOrchestrator._constrain_finish_decision(step, decision) is decision


def test_planned_finish_cannot_be_replaced_by_another_tool() -> None:
    step = PlannedStep(step_id="finish", instruction="结束", tool=ToolName.FINISH)
    decision = ToolDecision(tool=ToolName.INSPECT_SCREEN)

    constrained = AgentOrchestrator._constrain_finish_decision(step, decision)

    assert constrained.tool == ToolName.FINISH
