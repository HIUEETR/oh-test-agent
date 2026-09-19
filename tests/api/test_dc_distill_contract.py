"""DC 蒸馏 API 契约测试：POST /api/dc/sessions/{id}/profile/distill。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from harmony_test_agent.api.app import create_app
from harmony_test_agent.config import Settings
from harmony_test_agent.dc.distill import DcProfileDistiller
from harmony_test_agent.dc.models import (
    TOOL_TIER,
    DcDistillResult,
    DcError,
    DcEventType,
    DcToolInvocation,
    DcToolName,
    utc_now,
)
from harmony_test_agent.models import BoundingBox, ScreenSnapshot, UIElement

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
    with (
        patch("harmony_test_agent.dc.session.HarmonyDeviceAdapter.connect", return_value=None),
        patch(
            "harmony_test_agent.dc.session.HarmonyDeviceAdapter.health_check",
            return_value={"connected": True, "id": "mock-device"},
        ),
    ):
        yield TestClient(app, raise_server_exceptions=False)


def _invocation(
    page_path: str,
    invocation_id: str,
    *,
    tool: DcToolName = DcToolName.CLICK,
    result_summary: str = "",
) -> DcToolInvocation:
    return DcToolInvocation(
        invocation_id=invocation_id,
        turn_id="turn-1",
        tool=tool,
        tier=TOOL_TIER[tool],
        args={"x": 10, "y": 20} if tool == DcToolName.CLICK else {},
        success=True,
        started_at=utc_now(),
        ended_at=utc_now(),
        duration_ms=3,
        page_path=page_path,
        result_summary=result_summary,
    )


def _foreground_invocation() -> DcToolInvocation:
    """灌入一条成功的前台应用观测（摘要格式与 tools.py::tool_foreground_app 一致）。"""
    return _invocation(
        "pages/Page3",
        "inv-fg",
        tool=DcToolName.FOREGROUND_APP,
        result_summary=f"bundle={BUNDLE}, ability={ABILITY}",
    )


def _seed_session(client: TestClient, *, pages: list[str], with_snapshot: bool = True) -> str:
    """创建一条真实 DC 会话并灌入指定页面覆盖的操作记录。"""
    session = client.app.state.dc_manager.create()
    session.recorder.invocations.extend(
        _invocation(page_path, f"inv-{index}") for index, page_path in enumerate(pages, 1)
    )
    if with_snapshot:
        session.snapshot_holder.record(
            ScreenSnapshot(
                snapshot_id="snap-1",
                run_id=session.session_id,
                image_path=Path("snap-1.png"),
                image_sha256="sha-1",
                width=1080,
                height=1920,
                page_path=pages[0],
                elements=[
                    UIElement(
                        element_id="home_search",
                        key="home_search",
                        content="搜索",
                        clickable=True,
                        bbox=BoundingBox(left=0, top=0, right=100, bottom=50),
                    )
                ],
            )
        )
    return session.session_id


def test_distill_endpoint_missing_bundle(client: TestClient) -> None:
    """空 body 且录制中无身份线索 → 422，detail 为逐字固定的推断失败文案。"""
    session_id = _seed_session(client, pages=["pages/Page1"])

    response = client.post(
        f"/api/dc/sessions/{session_id}/profile/distill",
        json={},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == (
        "cannot infer application identity from session recordings; pass bundle_name/main_ability explicitly"
    )


def test_distill_endpoint_infers_identity_from_recordings(client: TestClient) -> None:
    """空 body：身份由会话录制记录（FOREGROUND_APP 结果摘要）推断后交给蒸馏器。"""
    session_id = _seed_session(client, pages=["pages/Page1", "pages/Page2", "pages/Page3"])
    session = client.app.state.dc_manager.get(session_id)
    session.recorder.invocations.append(_foreground_invocation())
    expected = DcDistillResult(
        profile_id=f"dc-{session_id}",
        status="verified",
        pages_covered=3,
        stable_locators=3,
        assertions=1,
        replay_run_id=f"{session_id}:profile-attempt-1",
        replay_passed=True,
    )
    seen: list[tuple[str, str]] = []

    async def fake_distill(self, target_session, bundle_name, main_ability):
        seen.append((bundle_name, main_ability))
        return expected

    with patch.object(DcProfileDistiller, "distill", fake_distill):
        response = client.post(
            f"/api/dc/sessions/{session_id}/profile/distill",
            json={},
        )

    assert response.status_code == 200, response.text
    assert seen == [(BUNDLE, ABILITY)]
    assert response.json()["profile_id"] == expected.profile_id


def test_distill_endpoint_explicit_identity_wins_over_inference(client: TestClient) -> None:
    """显式参数优先于录制推断：录制里虽有身份，仍按 body 的值蒸馏。"""
    session_id = _seed_session(client, pages=["pages/Page1", "pages/Page2", "pages/Page3"])
    session = client.app.state.dc_manager.get(session_id)
    session.recorder.invocations.append(_foreground_invocation())
    seen: list[tuple[str, str]] = []

    async def fake_distill(self, target_session, bundle_name, main_ability):
        seen.append((bundle_name, main_ability))
        return DcDistillResult(
            profile_id=f"dc-{session_id}",
            status="candidate",
            pages_covered=3,
            stable_locators=2,
            assertions=0,
        )

    with patch.object(DcProfileDistiller, "distill", fake_distill):
        response = client.post(
            f"/api/dc/sessions/{session_id}/profile/distill",
            json={"bundle_name": "com.other.app", "main_ability": "OtherAbility"},
        )

    assert response.status_code == 200, response.text
    assert seen == [("com.other.app", "OtherAbility")]


def test_distill_endpoint_session_not_found(client: TestClient) -> None:
    response = client.post(
        "/api/dc/sessions/dc-does-not-exist/profile/distill",
        json={"bundle_name": BUNDLE, "main_ability": ABILITY},
    )

    assert response.status_code == 404


def test_distill_endpoint_no_operations_conflict(client: TestClient) -> None:
    session = client.app.state.dc_manager.create()

    response = client.post(
        f"/api/dc/sessions/{session.session_id}/profile/distill",
        json={"bundle_name": BUNDLE, "main_ability": ABILITY},
    )

    assert response.status_code == 409
    assert "no operations recorded" in response.json()["detail"]


def test_distill_endpoint_insufficient_pages_returns_422(client: TestClient) -> None:
    """会话只覆盖 1 页 → DcError → 422（比赛硬性要求 ≥3 页）。"""
    session_id = _seed_session(client, pages=["pages/Page1", "pages/Page1"])

    response = client.post(
        f"/api/dc/sessions/{session_id}/profile/distill",
        json={"bundle_name": BUNDLE, "main_ability": ABILITY},
    )

    assert response.status_code == 422
    assert "need >= 3" in response.json()["detail"]


def test_distill_endpoint_placeholder_identity_returns_422(client: TestClient) -> None:
    """占位应用身份被拒绝：不能把示例 bundle 蒸馏成 Profile。"""
    session_id = _seed_session(client, pages=["pages/Page1", "pages/Page2", "pages/Page3"])

    response = client.post(
        f"/api/dc/sessions/{session_id}/profile/distill",
        json={"bundle_name": "com.example.app", "main_ability": ABILITY},
    )

    assert response.status_code == 422
    assert "placeholder" in response.json()["detail"]


def test_distill_endpoint_returns_result_and_emits_sse_events(client: TestClient) -> None:
    """成功路径：返回 DcDistillResult，并推送 STARTED/FINISHED 事件。"""
    session_id = _seed_session(client, pages=["pages/Page1", "pages/Page2", "pages/Page3"])
    session = client.app.state.dc_manager.get(session_id)
    expected = DcDistillResult(
        profile_id=f"dc-{session_id}",
        status="verified",
        pages_covered=3,
        stable_locators=3,
        assertions=2,
        replay_run_id=f"{session_id}:profile-attempt-1",
        replay_passed=True,
    )

    async def fake_distill(self, target_session, bundle_name, main_ability):
        assert bundle_name == BUNDLE
        assert main_ability == ABILITY
        return expected

    with patch.object(DcProfileDistiller, "distill", fake_distill):
        response = client.post(
            f"/api/dc/sessions/{session_id}/profile/distill",
            json={"bundle_name": BUNDLE, "main_ability": ABILITY},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["profile_id"] == expected.profile_id
    assert body["status"] == "verified"
    assert body["replay_passed"] is True

    types = [event.type for event in session.bus.recent()]
    assert DcEventType.PROFILE_DISTILL_STARTED in types
    assert DcEventType.PROFILE_DISTILL_FINISHED in types
    assert DcEventType.PROFILE_DISTILL_FAILED not in types


def test_distill_failure_emits_failed_event_and_maps_to_422(client: TestClient) -> None:
    session_id = _seed_session(client, pages=["pages/Page1", "pages/Page2", "pages/Page3"])
    session = client.app.state.dc_manager.get(session_id)

    async def failing_distill(self, target_session, bundle_name, main_ability):
        raise DcError("device verification is unavailable")

    with patch.object(DcProfileDistiller, "distill", failing_distill):
        response = client.post(
            f"/api/dc/sessions/{session_id}/profile/distill",
            json={"bundle_name": BUNDLE, "main_ability": ABILITY},
        )

    assert response.status_code == 422
    assert "device verification is unavailable" in response.json()["detail"]

    types = [event.type for event in session.bus.recent()]
    assert DcEventType.PROFILE_DISTILL_FAILED in types
    assert DcEventType.PROFILE_DISTILL_FINISHED not in types
