"""POST /api/runs 的 RunMode 历史兼容契约测试（2026-09-17 资产流水线重构 Phase 2）。

Phase 0 的 pydantic validator 在模型层已做降级；本文件补 API 层契约，
确保历史客户端发送 exploration/stability/reproduction 时：

1. 请求被接受（不再 422），
2. 落盘 trace 的 mode 归一化为 regression，
3. 单一 regression 仍是合法模式。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from harmony_test_agent.api import create_app
from harmony_test_agent.config import get_settings
from harmony_test_agent.models import RunMode
from harmony_test_agent.storage import RunRepository

LEGACY_MODES = ("exploration", "stability", "reproduction")


@pytest.fixture(autouse=True)
def _legacy_modes_disabled():
    """保证降级分支生效，不受本机 .env 的 ENABLE_LEGACY_RUN_MODES 影响。"""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _client(tmp_path: Path) -> TestClient:
    from harmony_test_agent.config import Settings

    settings = Settings(
        runtime_dir=tmp_path / "runs",
        database_path=tmp_path / "agent.db",
        target_profile_path=None,
        profiles_dir=tmp_path / "profiles",
        runtime_home=tmp_path / "runtime-home",
        agent_provider="mock",
    )
    return TestClient(create_app(settings))


@pytest.mark.parametrize("legacy", LEGACY_MODES)
def test_run_request_accepts_and_normalizes_legacy_mode(tmp_path: Path, legacy: str) -> None:
    with _client(tmp_path) as client:
        response = client.post(
            "/api/runs",
            json={
                "target": {"bundle_name": "com.example.notes"},
                "task": "legacy mode contract",
                "mode": legacy,
                "auto_generate": False,
                "auto_execute": False,
            },
        )

    assert response.status_code == 202, response.text
    run_id = response.json()["run_id"]
    repository = RunRepository(tmp_path / "agent.db")
    trace = repository.get_trace(run_id)
    assert trace is not None
    assert trace.mode == RunMode.REGRESSION


def test_run_request_accepts_explicit_regression(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        response = client.post(
            "/api/runs",
            json={
                "target": {"bundle_name": "com.example.notes"},
                "task": "regression mode contract",
                "mode": "regression",
                "auto_generate": False,
                "auto_execute": False,
            },
        )

    assert response.status_code == 202, response.text
    trace = RunRepository(tmp_path / "agent.db").get_trace(response.json()["run_id"])
    assert trace is not None
    assert trace.mode == RunMode.REGRESSION


def test_run_request_rejects_unknown_mode(tmp_path: Path) -> None:
    """只降级已知历史值；真正非法的模式仍然 422。"""
    with _client(tmp_path) as client:
        response = client.post(
            "/api/runs",
            json={
                "target": {"bundle_name": "com.example.notes"},
                "task": "invalid mode",
                "mode": "not-a-mode",
            },
        )

    assert response.status_code == 422
