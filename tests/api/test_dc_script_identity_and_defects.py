"""DC 脚本执行的两条门禁：身份一致性与「发现了就要记录」。

真机复盘 dc-20260922T171655Z-6fff3547（知乎++ 录制）：

* 脚本头部 ``BUNDLE_NAME`` 被写成会话开始时的遗留前台应用（网易云音乐），脚本体却是
  知乎++ 的步骤 ⇒ 回放第一步 ``Can't find component``，错误信息完全指不到真正原因；
* 那次执行的 ``analysis.json`` 已经判出 ``locator_stale``（critical），但它只留在
  ``hypium/attempt-01/`` 里：``defects`` 表为空、``dc_session.json.defects`` 为空，
  GUI 与 ``GET /api/defects`` 都看不到。
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
RECORDED_BUNDLE = "com.github.zhuoyi233.zhplus"
STALE_BUNDLE = "com.example.neteasymusic"

SCRIPT = """\
import json
import os
from pathlib import Path

BUNDLE_NAME = {bundle!r}
report = Path(os.environ["HARMONY_AGENT_REPORT_DIR"])
(report / "generated_result.json").write_text(json.dumps({{"passed": True}}), encoding="utf-8")
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


def _write_script(settings: Settings, *, bundle: str | None) -> str:
    generated = settings.resolved_runtime_dir / DC_RUN_ID / "generated"
    generated.mkdir(parents=True, exist_ok=True)
    script = generated / "dc_test_dc_1.py"
    body = SCRIPT.format(bundle=bundle) if bundle else SCRIPT.replace("BUNDLE_NAME = {bundle!r}\n", "")
    script.write_text(body, encoding="utf-8")
    script.with_suffix(".json").write_text(
        json.dumps({"purpose": "dc_recording", "replay_eligible": False, "bundle_name": bundle or ""}),
        encoding="utf-8",
    )
    return f"{DC_RUN_ID}/generated/{script.name}"


def _write_session_snapshot(settings: Settings, *, bundle_name: str = RECORDED_BUNDLE) -> Path:
    """写一份最小 ``dc_session.json``：录制身份 = 知乎++。"""
    session_dir = settings.resolved_runtime_dir / DC_RUN_ID
    session_dir.mkdir(parents=True, exist_ok=True)
    path = session_dir / "dc_session.json"
    path.write_text(
        json.dumps(
            {
                "session_id": DC_RUN_ID,
                "device_id": "SN1",
                "tier": 2,
                "continuation": {"last_foreground_app": bundle_name},
            }
        ),
        encoding="utf-8",
    )
    return path


def _analysis(
    *, kind: AnomalyKind = AnomalyKind.LOCATOR_STALE, summary: str = "脚本定位器在设备上已失效"
) -> ExecutionAnalysis:
    return ExecutionAnalysis(
        subject="dc_script",
        subject_id=f"{DC_RUN_ID}#attempt-01",
        bundle_name=RECORDED_BUNDLE,
        device_id="SN1",
        healthy=False,
        findings=[
            AnomalyFinding(
                kind=kind,
                severity="critical",
                summary_zh=summary,
                detail="第 42 行：driver.touch(BY.key('p2_search_clear'))",
                source="stdout",
                phase="post_hoc",
            )
        ],
    )


@pytest.fixture
def client(tmp_path: Path):
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), raise_server_exceptions=False) as test_client:
        yield test_client, settings


class TestScriptIdentityGate:
    def test_script_pointing_at_another_app_is_refused(self, client) -> None:
        """脚本驱动的应用与录制身份不一致 ⇒ 409，并明确说清两边分别是什么。"""
        test_client, settings = client
        _write_session_snapshot(settings)
        script_id = _write_script(settings, bundle=STALE_BUNDLE)

        response = test_client.post("/api/dc/scripts/run", json={"script_id": script_id})

        assert response.status_code == 409, response.text
        detail = response.json()["detail"]
        assert STALE_BUNDLE in detail
        assert RECORDED_BUNDLE in detail

    def test_matching_bundle_runs(self, client) -> None:
        test_client, settings = client
        _write_session_snapshot(settings)
        script_id = _write_script(settings, bundle=RECORDED_BUNDLE)

        response = test_client.post("/api/dc/scripts/run", json={"script_id": script_id})

        assert response.status_code == 200, response.text
        assert response.json()["results"][0]["passed"] is True

    def test_script_without_a_bundle_header_is_not_gated(self, client) -> None:
        """历史脚本没有 ``BUNDLE_NAME`` 时不做推断比较（保持向后兼容）。"""
        test_client, settings = client
        _write_session_snapshot(settings)
        script_id = _write_script(settings, bundle=None)

        response = test_client.post("/api/dc/scripts/run", json={"script_id": script_id})

        assert response.status_code == 200, response.text

    def test_no_recorded_identity_means_no_gate(self, client) -> None:
        """会话没有任何录制证据时不拦截（身份未知 ≠ 身份冲突）。"""
        test_client, settings = client
        script_id = _write_script(settings, bundle=STALE_BUNDLE)

        response = test_client.post("/api/dc/scripts/run", json={"script_id": script_id})

        assert response.status_code == 200, response.text


class TestScriptFindingsAreRecorded:
    def test_findings_reach_the_defect_library_and_the_session(self, client) -> None:
        test_client, settings = client
        _write_session_snapshot(settings)
        script_id = _write_script(settings, bundle=RECORDED_BUNDLE)

        with mock.patch.object(ExecutionAnalyzer, "analyze_replay", return_value=_analysis()):
            response = test_client.post("/api/dc/scripts/run", json={"script_id": script_id})

        assert response.status_code == 200, response.text
        defect_ids = response.json()["defect_ids"]
        assert defect_ids, "执行里发现的 finding 必须落库并回传 defect_id"

        listed = test_client.get("/api/defects").json()
        assert listed["total"] == 1
        assert listed["defects"][0]["kind"] == "locator_stale"
        assert listed["defects"][0]["defect_id"] == defect_ids[0]

        snapshot = json.loads((settings.resolved_runtime_dir / DC_RUN_ID / "dc_session.json").read_text("utf-8"))
        assert [item["kind"] for item in snapshot["defects"]] == ["locator_stale"]
        assert snapshot["defects"][0]["detail"].startswith("第 42 行")

    def test_healthy_execution_records_nothing(self, client) -> None:
        test_client, settings = client
        _write_session_snapshot(settings)
        script_id = _write_script(settings, bundle=RECORDED_BUNDLE)
        healthy = ExecutionAnalysis(
            subject="dc_script",
            subject_id=f"{DC_RUN_ID}#attempt-01",
            bundle_name=RECORDED_BUNDLE,
            device_id="SN1",
            healthy=True,
        )

        with mock.patch.object(ExecutionAnalyzer, "analyze_replay", return_value=healthy):
            response = test_client.post("/api/dc/scripts/run", json={"script_id": script_id})

        assert response.status_code == 200, response.text
        assert response.json()["defect_ids"] == []
        assert test_client.get("/api/defects").json()["total"] == 0

    def test_repeated_attempts_merge_into_one_defect(self, client) -> None:
        """同一会话重复执行同一失败：归并成一条缺陷（occurrences 累加）。"""
        test_client, settings = client
        _write_session_snapshot(settings)
        script_id = _write_script(settings, bundle=RECORDED_BUNDLE)

        with mock.patch.object(ExecutionAnalyzer, "analyze_replay", return_value=_analysis()):
            body = test_client.post("/api/dc/scripts/run", json={"script_id": script_id, "attempts": 2}).json()

        assert len(body["results"]) == 2
        assert len(body["defect_ids"]) == 1
        assert test_client.get("/api/defects").json()["total"] == 1
