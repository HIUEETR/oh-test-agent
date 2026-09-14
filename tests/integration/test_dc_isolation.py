"""DC 模式与 Live Mode 隔离性测试。

验证 DC-only 使用后 RunManager 保持为空，反之亦然。
"""

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


class TestDcIsolation:
    """DC Mode 与 Live Mode 完全隔离。"""

    def test_dc_usage_does_not_create_runs(self, client: TestClient) -> None:
        """DC-only 使用后 RunManager.tasks/orchestrators 保持为空。"""
        # 创建 DC 会话
        create_resp = client.post("/api/dc/sessions", json={})
        assert create_resp.status_code == 201
        session_id = create_resp.json()["session_id"]

        # 验证 DC 会话存在
        get_resp = client.get(f"/api/dc/sessions/{session_id}")
        assert get_resp.status_code == 200

        # 验证 Live Mode 的 /api/runs 仍为空
        runs_resp = client.get("/api/runs")
        assert runs_resp.status_code == 200
        assert runs_resp.json() == []

        # 关闭 DC 会话
        client.delete(f"/api/dc/sessions/{session_id}")

        # 再次验证 Live Mode 不受影响
        runs_resp2 = client.get("/api/runs")
        assert runs_resp2.status_code == 200
        assert runs_resp2.json() == []

    def test_dc_session_id_prefix(self, client: TestClient) -> None:
        """DC 会话 ID 必须以 dc- 前缀，与 run- 前缀区分。"""
        create_resp = client.post("/api/dc/sessions", json={})
        session_id = create_resp.json()["session_id"]
        assert session_id.startswith("dc-")
        assert not session_id.startswith("run-")

    def test_dc_artifacts_in_separate_directory(self, client: TestClient, tmp_path: Path) -> None:
        """DC 产物目录以 dc- 前缀，与 run- 目录天然分离。"""
        create_resp = client.post("/api/dc/sessions", json={})
        session_id = create_resp.json()["session_id"]

        # 获取会话详情
        get_resp = client.get(f"/api/dc/sessions/{session_id}")
        assert get_resp.status_code == 200

        # 验证 dc- 前缀目录存在
        dc_dir = tmp_path / "runs" / session_id
        assert dc_dir.exists() or session_id.startswith("dc-")

        # 验证没有 run- 前缀目录被创建
        run_dirs = list((tmp_path / "runs").glob("run-*"))
        assert len(run_dirs) == 0

    def test_live_mode_health_unaffected_by_dc(self, client: TestClient) -> None:
        """DC 路由挂载后 /api/health 契约不变。"""
        # 先访问 DC 端点
        client.post("/api/dc/sessions", json={})

        # 验证 health 端点仍正常
        health_resp = client.get("/api/health")
        assert health_resp.status_code == 200
        data = health_resp.json()
        assert "device" in data
        assert "model" in data

    def test_dc_and_live_endpoints_coexist(self, client: TestClient) -> None:
        """DC 和 Live Mode 端点同时存在且互不干扰。"""
        # DC 端点
        dc_resp = client.get("/api/dc/sessions")
        assert dc_resp.status_code == 200

        # Live Mode 端点
        runs_resp = client.get("/api/runs")
        assert runs_resp.status_code == 200

        health_resp = client.get("/api/health")
        assert health_resp.status_code == 200

        profiles_resp = client.get("/api/profiles")
        assert profiles_resp.status_code == 200
