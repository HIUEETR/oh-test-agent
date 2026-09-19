"""DC 模式 API 契约测试：端点 happy path + 错误码 + 回归断言。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from harmony_test_agent.api.app import create_app
from harmony_test_agent.config import Settings
from harmony_test_agent.dc.models import TOOL_TIER, DcToolInvocation, DcToolName, utc_now

BUNDLE = "com.example.notes"
ABILITY = "MainAbility"


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    settings = Settings(
        runtime_dir=tmp_path / "runs",
        database_path=tmp_path / "agent.db",
        target_profile_path=None,
        profiles_dir=tmp_path / "profiles",
        runtime_home=tmp_path / "runtime-home",
        agent_provider="mock",
    )
    app = create_app(settings)
    # Mock 设备连接，避免测试中尝试连接真实 HDC 设备
    with (
        patch(
            "harmony_test_agent.dc.session.HarmonyDeviceAdapter.connect",
            return_value=None,
        ),
        patch(
            "harmony_test_agent.dc.session.HarmonyDeviceAdapter.health_check",
            return_value={"connected": True, "id": "mock-device"},
        ),
    ):
        yield TestClient(app, raise_server_exceptions=False)


def _recorded(
    tool: DcToolName,
    *,
    invocation_id: str,
    args: dict[str, object] | None = None,
    result_summary: str = "",
) -> DcToolInvocation:
    """构造一条成功的录制记录（脚本端点只要求录制非空，生成器只读 args/success）。"""
    return DcToolInvocation(
        invocation_id=invocation_id,
        turn_id="turn-1",
        tool=tool,
        tier=TOOL_TIER[tool],
        args=args or {},
        success=True,
        started_at=utc_now(),
        ended_at=utc_now(),
        duration_ms=3,
        page_path="pages/Page1",
        result_summary=result_summary,
    )


def _foreground_recording() -> DcToolInvocation:
    """成功的前台应用观测：摘要格式与 tools.py::tool_foreground_app 一致。"""
    return _recorded(
        DcToolName.FOREGROUND_APP,
        invocation_id="inv-fg",
        result_summary=f"bundle={BUNDLE}, ability={ABILITY}",
    )


def _click_recording() -> DcToolInvocation:
    return _recorded(DcToolName.CLICK, invocation_id="inv-click", args={"x": 10, "y": 20})


def _seed_script_session(client: TestClient, invocations: list[DcToolInvocation]) -> str:
    """创建真实 DC 会话并灌入录制记录。"""
    session = client.app.state.dc_manager.create()
    session.recorder.invocations.extend(invocations)
    return session.session_id


class TestDcSessionEndpoints:
    """会话 CRUD 端点。"""

    def test_create_session_returns_dc_prefix(self, client: TestClient) -> None:
        response = client.post("/api/dc/sessions", json={})
        assert response.status_code == 201
        data = response.json()
        assert data["session_id"].startswith("dc-")
        assert data["status"] in ("idle", "thinking", "acting")

    def test_list_sessions_empty(self, client: TestClient) -> None:
        response = client.get("/api/dc/sessions")
        assert response.status_code == 200
        assert isinstance(response.json(), list)

    def test_get_session_404(self, client: TestClient) -> None:
        response = client.get("/api/dc/sessions/dc-nonexistent")
        assert response.status_code == 404

    def test_close_session_404(self, client: TestClient) -> None:
        response = client.delete("/api/dc/sessions/dc-nonexistent")
        assert response.status_code == 404

    def test_create_and_get_session(self, client: TestClient) -> None:
        create_resp = client.post("/api/dc/sessions", json={"tier": 2})
        assert create_resp.status_code == 201
        session_id = create_resp.json()["session_id"]

        get_resp = client.get(f"/api/dc/sessions/{session_id}")
        assert get_resp.status_code == 200
        data = get_resp.json()
        assert data["session_id"] == session_id
        assert data["tier"] == 2

    def test_create_and_close_session(self, client: TestClient) -> None:
        create_resp = client.post("/api/dc/sessions", json={})
        session_id = create_resp.json()["session_id"]

        close_resp = client.delete(f"/api/dc/sessions/{session_id}")
        assert close_resp.status_code == 200
        assert close_resp.json()["status"] == "closed"

        # 再次获取应 404
        get_resp = client.get(f"/api/dc/sessions/{session_id}")
        assert get_resp.status_code == 404


class TestDcMessageEndpoints:
    """对话端点。"""

    def test_send_message_404(self, client: TestClient) -> None:
        response = client.post("/api/dc/sessions/dc-nonexistent/messages", json={"text": "hello"})
        assert response.status_code == 404

    def test_send_message_empty_text_422(self, client: TestClient) -> None:
        create_resp = client.post("/api/dc/sessions", json={})
        session_id = create_resp.json()["session_id"]
        response = client.post(f"/api/dc/sessions/{session_id}/messages", json={"text": ""})
        assert response.status_code == 422

    def test_stop_turn_404(self, client: TestClient) -> None:
        response = client.post("/api/dc/sessions/dc-nonexistent/stop")
        assert response.status_code == 404


class TestDcTierEndpoint:
    """层级变更端点。"""

    def test_set_tier(self, client: TestClient) -> None:
        create_resp = client.post("/api/dc/sessions", json={"tier": 1})
        session_id = create_resp.json()["session_id"]

        tier_resp = client.patch(f"/api/dc/sessions/{session_id}/tier", json={"tier": 3})
        assert tier_resp.status_code == 200
        assert tier_resp.json()["tier"] == 3

    def test_set_tier_invalid_422(self, client: TestClient) -> None:
        create_resp = client.post("/api/dc/sessions", json={})
        session_id = create_resp.json()["session_id"]
        response = client.patch(f"/api/dc/sessions/{session_id}/tier", json={"tier": 99})
        assert response.status_code == 422


class TestDcScriptEndpoints:
    """脚本生成端点。"""

    def test_generate_script_no_operations_409(self, client: TestClient) -> None:
        create_resp = client.post("/api/dc/sessions", json={})
        session_id = create_resp.json()["session_id"]
        response = client.post(f"/api/dc/sessions/{session_id}/script", json={})
        assert response.status_code == 409

    def test_get_script_not_generated_404(self, client: TestClient) -> None:
        create_resp = client.post("/api/dc/sessions", json={})
        session_id = create_resp.json()["session_id"]
        response = client.get(f"/api/dc/sessions/{session_id}/script")
        assert response.status_code == 404

    # ------------------------------------------------------------------
    # 改动 C1：脚本生成身份可省略，缺省时按会话录制推断
    # ------------------------------------------------------------------

    def test_generate_script_infers_identity_from_recordings(self, client: TestClient) -> None:
        """空 body（省略身份）时按录制推断 bundle/ability，不再是 com.example.app 占位。"""
        session_id = _seed_script_session(client, [_foreground_recording(), _click_recording()])

        response = client.post(f"/api/dc/sessions/{session_id}/script", json={})

        assert response.status_code == 200, response.text
        body = response.json()
        assert f"BUNDLE_NAME = {BUNDLE!r}" in body["python_text"]
        assert f"MAIN_ABILITY = {ABILITY!r}" in body["python_text"]
        # 推断出真实身份 ⇒ 不再是占位脚本
        assert not any("placeholder" in warning for warning in body["warnings"])

    def test_generate_script_without_body_infers_identity(self, client: TestClient) -> None:
        """完全不发请求体也走同一推断路径（端点 body 可选）。"""
        session_id = _seed_script_session(client, [_foreground_recording(), _click_recording()])

        response = client.post(f"/api/dc/sessions/{session_id}/script")

        assert response.status_code == 200, response.text
        assert f"BUNDLE_NAME = {BUNDLE!r}" in response.json()["python_text"]

    def test_generate_script_explicit_identity_wins_over_inference(self, client: TestClient) -> None:
        """显式提供的身份优先于录制推断。"""
        session_id = _seed_script_session(client, [_foreground_recording(), _click_recording()])

        response = client.post(
            f"/api/dc/sessions/{session_id}/script",
            json={"bundle_name": "com.explicit.app", "main_ability": "ExplicitAbility"},
        )

        assert response.status_code == 200, response.text
        text = response.json()["python_text"]
        assert "BUNDLE_NAME = 'com.explicit.app'" in text
        assert "MAIN_ABILITY = 'ExplicitAbility'" in text
        assert BUNDLE not in text

    def test_generate_script_without_identity_evidence_falls_back_to_placeholder(self, client: TestClient) -> None:
        """录制无身份线索且未显式提供 → 回退占位身份：脚本仍生成，但只作诊断脚本。"""
        session_id = _seed_script_session(client, [_click_recording()])

        response = client.post(f"/api/dc/sessions/{session_id}/script", json={})

        assert response.status_code == 200, response.text
        body = response.json()
        assert "BUNDLE_NAME = 'com.example.app'" in body["python_text"]
        assert any("placeholder" in warning for warning in body["warnings"])


class TestDcSSEEndpoint:
    """SSE 事件流端点。"""

    def test_events_404(self, client: TestClient) -> None:
        response = client.get("/api/dc/sessions/dc-nonexistent/events")
        assert response.status_code == 404


class TestDcAssertionToolsExposed:
    """Phase 3（2026-09-17）：断言工具必须进入 agent 可见的 L1 工具集。"""

    def test_dc_tools_list_includes_assertions(self, client: TestClient) -> None:
        response = client.get("/api/dc/tools", params={"tier": 1})

        assert response.status_code == 200, response.text
        body = response.json()
        names = {tool["name"] for tool in body["tools"]}
        assert {"assert_visible", "assert_not_visible", "assert_text"} <= names
        assert body["assertion_tools"] == ["assert_not_visible", "assert_text", "assert_visible"]
        # 断言是只读检查：不得标记设备副作用
        assert all(not tool["side_effect"] for tool in body["tools"] if tool["name"].startswith("assert_"))

    def test_dc_tools_list_defaults_to_all_tiers(self, client: TestClient) -> None:
        response = client.get("/api/dc/tools")

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["tier"] == 5
        assert body["total"] == 26
        assert len(body["tools"]) == 26

    def test_build_tools_returns_assertions(self) -> None:
        from harmony_test_agent.dc.models import DcToolTier
        from harmony_test_agent.dc.tools import build_tools

        names = {tool.name for tool in build_tools(DcToolTier.L1)}

        assert {"assert_visible", "assert_not_visible", "assert_text"} <= names

    def test_default_dc_session_exposes_assertion_tools(self, client: TestClient) -> None:
        """默认 tier（L2）会话同样能看到断言工具：L2 ⊇ L1。"""
        from harmony_test_agent.dc.models import DcToolTier
        from harmony_test_agent.dc.tools import build_tools

        names = {tool.name for tool in build_tools(DcToolTier.L2)}

        assert {"assert_visible", "assert_not_visible", "assert_text"} <= names

    def test_assertion_tools_are_not_replay_omitted(self) -> None:
        """断言工具必须可回放（不在 _NON_REPLAYABLE），否则脚本永远无法含检查点。"""
        from harmony_test_agent.dc.generator import _NON_REPLAYABLE, _REPLAYABLE_MAP
        from harmony_test_agent.dc.models import DcToolName

        for name in (DcToolName.ASSERT_VISIBLE, DcToolName.ASSERT_NOT_VISIBLE, DcToolName.ASSERT_TEXT):
            assert name in _REPLAYABLE_MAP
            assert name not in _NON_REPLAYABLE


class TestDcArtifactEndpoint:
    """产物下载端点。"""

    def test_artifact_404(self, client: TestClient) -> None:
        create_resp = client.post("/api/dc/sessions", json={})
        session_id = create_resp.json()["session_id"]
        response = client.get(f"/api/dc/sessions/{session_id}/artifacts/nonexistent.png")
        assert response.status_code == 404

    def test_artifact_path_traversal_blocked(self, client: TestClient) -> None:
        create_resp = client.post("/api/dc/sessions", json={})
        session_id = create_resp.json()["session_id"]
        response = client.get(f"/api/dc/sessions/{session_id}/artifacts/../../../etc/passwd")
        assert response.status_code == 404


class TestLiveModeRegression:
    """关键回归断言：DC 路由挂载后 Live Mode 端点契约不变。"""

    def test_health_endpoint_unchanged(self, client: TestClient) -> None:
        response = client.get("/api/health")
        assert response.status_code == 200
        data = response.json()
        assert "device" in data
        assert "model" in data

    def test_runs_endpoint_unchanged(self, client: TestClient) -> None:
        response = client.get("/api/runs")
        assert response.status_code == 200
        assert isinstance(response.json(), list)

    def test_profiles_endpoint_unchanged(self, client: TestClient) -> None:
        response = client.get("/api/profiles")
        assert response.status_code == 200
