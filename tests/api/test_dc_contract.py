"""DC 模式 API 契约测试：端点 happy path + 错误码 + 回归断言。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from harmony_test_agent.api.app import create_app
from harmony_test_agent.config import Settings


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


class TestDcSSEEndpoint:
    """SSE 事件流端点。"""

    def test_events_404(self, client: TestClient) -> None:
        response = client.get("/api/dc/sessions/dc-nonexistent/events")
        assert response.status_code == 404


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
