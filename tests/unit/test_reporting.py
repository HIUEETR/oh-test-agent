from pathlib import Path

from harmony_test_agent.models import (
    CommandResult,
    GeneratedArtifact,
    ReplayError,
    ReplayResult,
    RunState,
    RunTrace,
)
from harmony_test_agent.reporting import ReportBuilder
from harmony_test_agent.storage import ArtifactStore


def test_report_renders_coverage_replays_and_failure_summary(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "runs")
    generated_dir = store.run_dir("run-report") / "generated"
    trace = RunTrace(
        run_id="run-report",
        target_app_id="app",
        task="报告任务",
        device_id="device",
        state=RunState.FAILED_SCRIPT,
        error="replay failed",
        agent_error="replay failed",
        generated=GeneratedArtifact(
            python_path=generated_dir / "test.py",
            config_path=generated_dir / "test.json",
            metadata_path=generated_dir / "generation_metadata.json",
            purpose="diagnostic",
            source_action_count=5,
            included_action_count=3,
            omitted_action_count=2,
            incomplete_reasons=["source trace is incomplete"],
        ),
        replays=[
            ReplayResult(
                attempt=1,
                command=CommandResult(command="python test.py", returncode=1),
                status="failed",
                exit_code=1,
                error=ReplayError(kind="script_error", message="assertion failed"),
            )
        ],
        replay_total=3,
    )

    path = ReportBuilder(store).build(trace)
    content = path.read_text(encoding="utf-8")

    assert "脚本覆盖范围" in content
    assert "纳入脚本" in content and ">3<" in content
    assert "回放结果" in content and "完成 1 / 3" in content
    assert "失败摘要" in content
    assert "Agent：replay failed" in content
    assert "回放 1：assertion failed" in content


def test_report_marks_quality_notes_as_warnings_not_blockers(tmp_path: Path) -> None:
    """质量提示用黄色 ``.warn``（非阻断），并单列「可执行 / 置信度」两格。"""
    store = ArtifactStore(tmp_path / "runs")
    generated_dir = store.run_dir("run-report-quality") / "generated"
    trace = RunTrace(
        run_id="run-report-quality",
        target_app_id="app",
        task="报告任务",
        device_id="device",
        state=RunState.COMPLETED,
        generated=GeneratedArtifact(
            python_path=generated_dir / "test.py",
            config_path=generated_dir / "test.json",
            metadata_path=generated_dir / "generation_metadata.json",
            purpose="acceptance",
            replay_eligible=True,
            confidence="medium",
            confidence_factors=["source trace has no successful explicit assertion"],
            incomplete_reasons=["source trace has no successful explicit assertion"],
            promotion_eligible=False,
            promotion_blockers=["live-mode trace is not Profile-promotion evidence"],
            source_action_count=5,
            included_action_count=3,
            omitted_action_count=2,
        ),
    )

    content = ReportBuilder(store).build(trace).read_text(encoding="utf-8")

    assert "可执行<br><b>是</b>" in content
    assert "置信度<br><b>中</b>" in content
    assert "质量提示（不影响执行）" in content
    assert "<ul class='warn'>" in content
    assert "<ul class='bad'>" not in content
    assert "不作为 Profile 晋级证据" in content
    assert "live-mode trace is not Profile-promotion evidence" in content
