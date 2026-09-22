"""``POST /api/dc/scripts/run`` 的执行结果分析契约（Phase 1：DC 零分析的修复）。

DC 脚本执行没有 trace 上下文，因此分析走 ``analyze_dc_script``：``subject="dc_script"``、
``subject_id="<session_id>#attempt-NN"``，``need_logs`` 仍按 ``status != "passed"`` 独立判定。
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from harmony_test_agent.analysis.service import ExecutionAnalyzer
from harmony_test_agent.api.app import create_app
from harmony_test_agent.config import Settings
from harmony_test_agent.models import AnomalyFinding, AnomalyKind, ExecutionAnalysis

DC_RUN_ID = "dc-20260101T000000Z-aaaa1111"
BUNDLE = "com.zhihu.hmos"

PASSING_SCRIPT = """\
import json
import os
from pathlib import Path

report = Path(os.environ["HARMONY_AGENT_REPORT_DIR"])
(report / "generated_result.json").write_text(json.dumps({"passed": True}), encoding="utf-8")
print("ok")
"""


def _settings(tmp_path: Path, **overrides) -> Settings:
    payload = {
        "runtime_dir": tmp_path / "runs",
        "database_path": tmp_path / "agent.db",
        "target_profile_path": None,
        "profiles_dir": tmp_path / "profiles",
        "runtime_home": tmp_path / "runtime-home",
        "agent_provider": "mock",
    }
    payload.update(overrides)
    return Settings(**payload)


def _write_script(settings: Settings, *, body: str = PASSING_SCRIPT) -> str:
    generated = settings.resolved_runtime_dir / DC_RUN_ID / "generated"
    generated.mkdir(parents=True, exist_ok=True)
    script = generated / "dc_test_dc_1.py"
    script.write_text(body, encoding="utf-8")
    script.with_suffix(".json").write_text(
        json.dumps({"purpose": "dc_recording", "replay_eligible": False}), encoding="utf-8"
    )
    return f"{DC_RUN_ID}/generated/{script.name}"


def _write_session_snapshot(settings: Settings, *, bundle_name: str = BUNDLE, device_id: str = "SN1") -> None:
    """写一份最小 ``dc_session.json``，让 DC 脚本执行能解析出应用身份。"""
    session_dir = settings.resolved_runtime_dir / DC_RUN_ID
    session_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "session_id": DC_RUN_ID,
        "device_id": device_id,
        "tier": 2,
        "continuation": {"last_foreground_app": bundle_name},
    }
    (session_dir / "dc_session.json").write_text(json.dumps(payload), encoding="utf-8")


def _analysis(**overrides) -> ExecutionAnalysis:
    payload = {
        "subject": "dc_script",
        "subject_id": f"{DC_RUN_ID}#attempt-01",
        "bundle_name": BUNDLE,
        "device_id": "SN1",
        "healthy": False,
        "findings": [
            AnomalyFinding(
                kind=AnomalyKind.PAGE_UNRESPONSIVE,
                severity="critical",
                summary_zh="动作后前台应用丢失，疑似崩溃",
                source="screenshot",
                phase="in_run",
            )
        ],
    }
    payload.update(overrides)
    return ExecutionAnalysis(**payload)  # type: ignore[arg-type]


@pytest.fixture
def client(tmp_path: Path):
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), raise_server_exceptions=False) as test_client:
        yield test_client, settings


class TestDcScriptAnalysisWiring:
    def test_each_result_carries_a_dc_script_analysis(self, client) -> None:
        test_client, settings = client
        script_id = _write_script(settings)
        _write_session_snapshot(settings)

        with mock.patch.object(ExecutionAnalyzer, "analyze_replay", return_value=_analysis()) as analyze:
            response = test_client.post("/api/dc/scripts/run", json={"script_id": script_id})

        assert response.status_code == 200, response.text
        results = response.json()["results"]
        assert len(results) == 1
        analysis = results[0]["analysis"]
        assert analysis is not None
        assert analysis["subject"] == "dc_script"
        assert analysis["subject_id"] == f"{DC_RUN_ID}#attempt-01"
        assert analysis["bundle_name"] == BUNDLE
        # 归属身份来自 dc_session.json 的 continuation.last_foreground_app
        assert analyze.call_args.kwargs["bundle_name"] == BUNDLE
        assert analyze.call_args.kwargs["run_dir"] == settings.resolved_runtime_dir / DC_RUN_ID

    def test_missing_identity_still_analyzes_without_attribution(self, client) -> None:
        """会话快照缺失时仍分析，只是没有归属信号（不阻塞 DC 脚本执行）。"""
        test_client, settings = client
        script_id = _write_script(settings)

        with mock.patch.object(ExecutionAnalyzer, "analyze_replay", return_value=_analysis(bundle_name="")) as analyze:
            response = test_client.post("/api/dc/scripts/run", json={"script_id": script_id})

        assert response.status_code == 200, response.text
        assert response.json()["results"][0]["analysis"]["subject"] == "dc_script"
        assert analyze.call_args.kwargs["bundle_name"] == ""

    def test_switch_off_restores_the_historic_zero_analysis_behaviour(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path, analysis_dc_scripts=False)
        _write_session_snapshot(settings)
        with TestClient(create_app(settings), raise_server_exceptions=False) as test_client:
            script_id = _write_script(settings)
            with mock.patch.object(ExecutionAnalyzer, "analyze_replay") as analyze:
                response = test_client.post("/api/dc/scripts/run", json={"script_id": script_id})

        assert response.status_code == 200, response.text
        assert response.json()["results"][0]["analysis"] is None
        analyze.assert_not_called()

    def test_analysis_never_flips_the_script_verdict(self, client) -> None:
        """红线：分析是 advisory，healthy=False 不改变 ``passed``。"""
        test_client, settings = client
        script_id = _write_script(settings)
        _write_session_snapshot(settings)

        with mock.patch.object(ExecutionAnalyzer, "analyze_replay", return_value=_analysis()):
            response = test_client.post("/api/dc/scripts/run", json={"script_id": script_id})

        result = response.json()["results"][0]
        assert result["analysis"]["healthy"] is False
        assert result["passed"] is True
        assert result["status"] == "passed"

    def test_response_exposes_the_resolved_bundle_name(self, client) -> None:
        test_client, settings = client
        script_id = _write_script(settings)
        _write_session_snapshot(settings, bundle_name=BUNDLE, device_id="SN7")

        with mock.patch.object(ExecutionAnalyzer, "analyze_replay", return_value=_analysis(device_id="SN7")) as analyze:
            body = test_client.post("/api/dc/scripts/run", json={"script_id": script_id}).json()

        assert body["bundle_name"] == BUNDLE
        assert analyze.call_args.kwargs["device_id"] == "SN7"


class TestAnalyzeDcScriptSubject:
    def test_subject_and_subject_id_are_dc_scoped(self, tmp_path: Path) -> None:
        """``analyze_dc_script`` 委托 ``analyze_replay`` 后改写 subject/subject_id。"""
        from harmony_test_agent.models import CommandResult, ReplayResult

        analyzer = ExecutionAnalyzer()
        attempt_dir = tmp_path / "runs" / DC_RUN_ID / "hypium" / "attempt-01"
        attempt_dir.mkdir(parents=True)
        replay = ReplayResult(
            attempt=1,
            command=CommandResult(command="python", returncode=0),
            report_path=attempt_dir,
            passed=True,
            status="passed",
        )

        analysis = analyzer.analyze_dc_script(
            replay,
            run_dir=tmp_path / "runs" / DC_RUN_ID,
            bundle_name=BUNDLE,
            device_id="SN1",
            session_id=DC_RUN_ID,
        )

        assert analysis.subject == "dc_script"
        assert analysis.subject_id == f"{DC_RUN_ID}#attempt-01"
        assert analysis.bundle_name == BUNDLE
        assert analysis.device_id == "SN1"
        # 分析本身写到了 attempt 目录旁边
        assert (attempt_dir / "analysis.json").is_file()
