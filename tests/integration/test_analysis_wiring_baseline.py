"""接线断言：证明「缺口 1 — 接线全是洞」已闭合（Phase 1）。

本文件的断言在 Phase 0 全部失败（缺口确实存在），Phase 1 接线后转绿并长期守护。三条证据：

1. 全仓 ``HypiumRunner(...)`` 构造点是否传了 ``analysis_hook``（AST 静态扫描）；
2. ``POST /api/dc/scripts/run`` 的响应是否带 ``subject="dc_script"`` 的 ``analysis``；
3. ``POST /api/runs/{id}/execute`` 后 ``trace.replays[*].analysis`` 是否非 ``None``。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from harmony_test_agent.api.app import create_app
from harmony_test_agent.config import Settings
from harmony_test_agent.models import AnomalyKind, CommandResult, ExecutionAnalysis, ReplayResult

SRC = Path(__file__).resolve().parents[2] / "src" / "harmony_test_agent"

#: Phase 1 完成后的唯一合法形态：``grep -rn "HypiumRunner(" src/`` 只剩这两处。
#: ``runner/xdevice.py`` 的构造点只用于 environment，按设计不需要 hook。
ALLOWED_WITHOUT_HOOK = {
    ("runner", "factory.py"),
    ("runner", "xdevice.py"),
}


# --------------------------------------------------------------------------- 辅助


def hypium_runner_callsites() -> list[tuple[str, int, bool]]:
    """AST 扫描 ``src/`` 下全部 ``HypiumRunner(...)`` 构造点。

    返回 ``(相对文件路径, 行号, 是否传了 analysis_hook)``，按文件与行号排序。
    """
    found: list[tuple[str, int, bool]] = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name != "HypiumRunner":
                continue
            has_hook = any(keyword.arg == "analysis_hook" for keyword in node.keywords)
            relative = path.relative_to(SRC).as_posix()
            found.append((relative, node.lineno, has_hook))
    return sorted(found)


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        runtime_dir=tmp_path / "runs",
        database_path=tmp_path / "agent.db",
        target_profile_path=None,
        profiles_dir=tmp_path / "profiles",
        runtime_home=tmp_path / "runtime-home",
        agent_provider="mock",
    )


def _fake_analysis(subject: str = "hypium_replay", subject_id: str = "run-1#attempt-01") -> ExecutionAnalysis:
    return ExecutionAnalysis(
        subject=subject,  # type: ignore[arg-type]
        subject_id=subject_id,
        bundle_name="com.zhihu.hmos",
        device_id="SN1",
        healthy=True,
        findings=[],
    )


# ------------------------------------------------------- 缺口 1(a)：构造点无 hook


class TestRunnerConstructionSites:
    def test_every_hypium_runner_call_site_passes_an_analysis_hook(self) -> None:
        """除 factory/xdevice 外，所有构造点都必须传 ``analysis_hook``。"""
        missing = [
            f"{path}:{line}"
            for path, line, has_hook in hypium_runner_callsites()
            if not has_hook and (Path(path).parts[0], Path(path).name) not in ALLOWED_WITHOUT_HOOK
        ]

        assert missing == [], f"以下 HypiumRunner 构造点缺少 analysis_hook：{missing}"

    def test_only_the_factory_and_xdevice_construct_runners_directly(self) -> None:
        """验收：``grep -rn "HypiumRunner(" src/`` 只剩 factory 与 xdevice 两处。

        ``runner/xdevice.py`` 的构造点只用于 ``environment()``，按设计不需要 hook。
        """
        files = {path for path, _, _ in hypium_runner_callsites()}

        assert files == {"runner/factory.py", "runner/xdevice.py"}, files


# ------------------------------------------------- 缺口 1(b)：DC 路径零分析


class TestDcScriptRunAnalysis:
    def test_dc_script_run_response_carries_subject_dc_script_analysis(self, tmp_path: Path, monkeypatch) -> None:
        """DC 脚本执行的每个 result 都带 ``subject="dc_script"`` 的分析。"""
        import json

        settings = _settings(tmp_path)
        dc_run_id = "dc-20260101T000000Z-aaaa1111"
        generated = settings.resolved_runtime_dir / dc_run_id / "generated"
        generated.mkdir(parents=True, exist_ok=True)
        script = generated / "dc_test_dc_1.py"
        script.write_text("print('ok')\n", encoding="utf-8")
        script.with_suffix(".json").write_text(
            json.dumps({"purpose": "dc_recording", "replay_eligible": False}), encoding="utf-8"
        )

        captured: dict[str, object] = {}

        def fake_execute_diagnostic(self, python_path, attempt: int = 1, *, extra_env=None) -> ReplayResult:
            hook = getattr(self, "analysis_hook", None)
            attempt_dir = settings.resolved_runtime_dir / dc_run_id / "hypium" / f"attempt-{attempt:02d}"
            attempt_dir.mkdir(parents=True, exist_ok=True)
            result = ReplayResult(
                attempt=attempt,
                command=CommandResult(command="python", returncode=0, stdout="ok"),
                report_path=attempt_dir,
                passed=True,
                status="passed",
                evidence_paths=[f"hypium/attempt-{attempt:02d}/stdout.log"],
            )
            assert hook is not None, "DC 脚本执行路径的 HypiumRunner 必须带 analysis_hook"
            captured["analysis"] = hook(result, attempt_dir)
            if captured["analysis"] is not None:
                result = result.model_copy(update={"analysis": captured["analysis"]})
            return result

        monkeypatch.setattr(
            "harmony_test_agent.runner.hypium.HypiumRunner.execute_diagnostic",
            fake_execute_diagnostic,
        )
        monkeypatch.setattr(
            "harmony_test_agent.analysis.service.ExecutionAnalyzer.analyze_dc_script",
            lambda self, replay, **kwargs: _fake_analysis("dc_script", dc_run_id),
            raising=False,
        )

        with TestClient(create_app(settings), raise_server_exceptions=False) as client:
            response = client.post(
                "/api/dc/scripts/run",
                json={"script_id": f"{dc_run_id}/generated/{script.name}"},
            )

        assert response.status_code == 200, response.text
        results = response.json()["results"]
        assert results and results[0]["analysis"] is not None
        assert results[0]["analysis"]["subject"] == "dc_script"

    def test_subject_literal_dc_script_is_constructed_somewhere_in_src(self) -> None:
        """``subject="dc_script"`` 不能只是模型里的死字面量。"""
        matches = [
            path.relative_to(SRC).as_posix()
            for path in SRC.rglob("*.py")
            if "dc_script" in path.read_text(encoding="utf-8")
        ]

        # models.py 声明字面量；Phase 1 之后必须另有实现处（service.py 或 dc/）。
        assert any(path != "models.py" for path in matches), f"dc_script 只出现在 {matches}"


# ------------------------------------------- 缺口 1(c)：/api/runs/{id}/execute


def _seed_run(tmp_path: Path) -> tuple[Settings, str]:
    """造一个「已生成可回放脚本」的持久化 run，供 /execute 端点使用。"""
    from harmony_test_agent.models import GeneratedArtifact, RunState, RunTrace
    from harmony_test_agent.storage import ArtifactStore, RunRepository

    settings = _settings(tmp_path)
    artifacts = ArtifactStore(settings.resolved_runtime_dir)
    repository = RunRepository(settings.resolved_database_path)
    run_id = "run-20260101T000000Z-cccc3333"
    run_dir = artifacts.run_dir(run_id)
    generated_dir = run_dir / "generated"
    generated_dir.mkdir(parents=True, exist_ok=True)
    script = generated_dir / "test_run.py"
    script.write_text("print('noop')\n", encoding="utf-8")
    script.with_suffix(".json").write_text("{}\n", encoding="utf-8")
    trace = RunTrace(
        run_id=run_id,
        target_app_id="zhihu",
        task="回放分析接线",
        device_id="SN1",
        state=RunState.COMPLETED,
        generated=GeneratedArtifact(
            python_path=script,
            config_path=script.with_suffix(".json"),
            metadata_path=script.with_suffix(".json"),
            purpose="acceptance",
            replay_eligible=True,
        ),
    )
    repository.save_trace(trace)
    return settings, run_id


class TestRunExecuteAttachesAnalysis:
    def test_execute_populates_trace_replay_analysis(self, tmp_path: Path, monkeypatch) -> None:
        """``POST /api/runs/{id}/execute`` 后每个 replay 都有 ``analysis``。"""
        import time

        settings, run_id = _seed_run(tmp_path)

        def fake_execute(self, generated, attempt: int = 1) -> ReplayResult:
            hook = getattr(self, "analysis_hook", None)
            attempt_dir = settings.resolved_runtime_dir / run_id / "hypium" / f"attempt-{attempt:02d}"
            attempt_dir.mkdir(parents=True, exist_ok=True)
            result = ReplayResult(
                attempt=attempt,
                command=CommandResult(command="python", returncode=0, stdout="ok"),
                report_path=attempt_dir,
                passed=True,
                status="passed",
                evidence_paths=[f"hypium/attempt-{attempt:02d}/stdout.log"],
            )
            if hook is not None:
                analysis = hook(result, attempt_dir)
                if analysis is not None:
                    result = result.model_copy(update={"analysis": analysis})
            return result

        monkeypatch.setattr("harmony_test_agent.runner.hypium.HypiumRunner.execute", fake_execute)
        monkeypatch.setattr(
            "harmony_test_agent.analysis.service.ExecutionAnalyzer.analyze_replay",
            lambda self, replay, **kwargs: ExecutionAnalysis(
                subject="hypium_replay",
                subject_id=f"{run_id}#attempt-{replay.attempt:02d}",
                bundle_name="com.zhihu.hmos",
                device_id="SN1",
                healthy=True,
                findings=[],
            ),
        )

        with TestClient(create_app(settings), raise_server_exceptions=False) as client:
            response = client.post(f"/api/runs/{run_id}/execute")
            assert response.status_code in {200, 202}, response.text
            deadline = time.monotonic() + 30
            body: dict = {}
            while time.monotonic() < deadline:
                body = client.get(f"/api/runs/{run_id}").json()
                if body.get("replays"):
                    break
                time.sleep(0.05)

        replays = body.get("replays") or []
        assert replays, "回放未产生任何 attempt"
        assert all(item["analysis"] is not None for item in replays), replays


class TestAnalysisSubjectLiteral:
    def test_dc_script_analysis_subject_is_a_runtime_value(self) -> None:
        """G1：``dc_script`` 必须由真实代码路径构造，而不是只写在模型字面量里。"""
        service = (SRC / "analysis" / "service.py").read_text(encoding="utf-8")
        assert "analyze_dc_script" in service


@pytest.mark.parametrize("kind", list(AnomalyKind))
def test_anomaly_kinds_have_a_service_side_symptom_mapping(kind: AnomalyKind) -> None:
    """健全性基线：每个 AnomalyKind 都能进入缺陷汇报链路（Phase 4 反向映射的前置条件）。"""
    from harmony_test_agent.analysis.service import SYMPTOM_KINDS

    assert any(kind in kinds for kinds in SYMPTOM_KINDS.values()), kind
