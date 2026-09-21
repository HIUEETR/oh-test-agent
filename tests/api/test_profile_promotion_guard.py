"""Profile 晋级护栏：晋级证据只能来自 Profile 验证脚本。

「执行门禁放开」之后，任务脚本也可以是 ``purpose="acceptance"``；本文件钉住晋级链**不**因此
被污染：

1. ``POST /api/profiles/{id}/replay`` 只读 ``profile.provenance.generated_script_path``；
2. 该路径必须是 Profile 验证脚本（``test_*`` / ``dc_test_*``），否则 409；
3. 任务脚本的 ``promotion_eligible=False`` 与 ``promotion_blockers`` 会出现在
   ``GET /api/runs/{id}/script`` 响应里，调用方据此拒绝把它当晋级证据。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from harmony_test_agent.api.app import create_app
from harmony_test_agent.config import Settings
from harmony_test_agent.generation import HypiumGenerator
from harmony_test_agent.models import (
    ActionResult,
    AssertionDefinition,
    CommandResult,
    LocatorCandidate,
    LocatorKind,
    ProfileStatus,
    ReplayResult,
    RunState,
    RunTrace,
    StableLocator,
    TargetAppProfile,
    ToolName,
)
from harmony_test_agent.profiles import ProfileRegistry

TARGET_APP_ID = "com-github-zhuoyi233-zhplus"
BUNDLE_NAME = "com.github.zhuoyi233.zhplus"


def _profile(*, script_path: str) -> TargetAppProfile:
    return TargetAppProfile(
        status=ProfileStatus.CANDIDATE,
        target_app_id=TARGET_APP_ID,
        display_name="知乎++",
        bundle_name=BUNDLE_NAME,
        main_ability="EntryAbility",
        stable_locator_inventory=[
            StableLocator(
                name=f"locator-{index}",
                page_signature=f"page-{index}",
                key=f"key-{index}",
                observed_rounds=1,
                unique_match_rounds=1,
                evidence_snapshot_ids=[f"snap-{index}"],
            )
            for index in range(1, 4)
        ],
        assertion_inventory=[
            AssertionDefinition(
                name=f"assertion-{index}",
                kind="visible",
                target=f"key-{index}",
                page_signature=f"page-{index}",
                observed_rounds=1,
                evidence_snapshot_ids=[f"asnap-{index}"],
            )
            for index in range(1, 3)
        ],
        core_flows=[{"pages": ["page-1", "page-2", "page-3"], "steps": [], "interaction_types": ["click", "input"]}],
        provenance={
            "discovery_run_id": "run-profile",
            "evidence": {"verification_passed": True},
            "generated_script_path": script_path,
        },
    )


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        runtime_dir=tmp_path / "runs",
        database_path=tmp_path / "agent.db",
        target_profile_path=None,
        profiles_dir=tmp_path / "profiles",
        runtime_home=tmp_path / "runtime-home",
        cases_dir=tmp_path / "cases",
        agent_provider="mock",
        hypium_replay_attempts=1,
        profile_verification_rounds=1,
    )


def _seed_script(settings: Settings, name: str) -> Path:
    path = settings.resolved_runtime_dir / "run-profile-profile-validation" / "generated" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("print('noop')\n", encoding="utf-8")
    path.with_suffix(".json").write_text("{}\n", encoding="utf-8")
    return path


def _install_replay(monkeypatch) -> None:
    def fake_execute(self, generated, attempt: int = 1) -> ReplayResult:
        return ReplayResult(
            attempt=attempt,
            command=CommandResult(command="hypium", returncode=0),
            passed=True,
            status="passed",
            evidence_paths=[f"hypium/attempt-{attempt:02d}/stdout.log"],
        )

    monkeypatch.setattr("harmony_test_agent.api.app.HypiumRunner.execute", fake_execute)


def _task_trace() -> RunTrace:
    """一次实时模式任务运行：脚本可执行，但不是晋级证据。"""
    return RunTrace(
        run_id="run-task-script",
        target_app_id=TARGET_APP_ID,
        task="搜索 OpenHarmony",
        device_id="device-1",
        state=RunState.COMPLETED,
        agent_outcome="completed",
        live_mode=True,
        profile_snapshot=TargetAppProfile(
            target_app_id=TARGET_APP_ID,
            display_name="知乎++",
            bundle_name=BUNDLE_NAME,
            main_ability="EntryAbility",
        ),
        actions=[
            ActionResult(
                step_id="click",
                tool=ToolName.CLICK_ELEMENT,
                success=True,
                params={"target": "搜索"},
                locator=LocatorCandidate(kind=LocatorKind.KEY, value="p2_home_titlebar_search"),
            ),
            ActionResult(
                step_id="assert",
                tool=ToolName.ASSERT_VISIBLE,
                success=True,
                params={"target": "搜索"},
                locator=LocatorCandidate(kind=LocatorKind.KEY, value="p2_home_titlebar_search"),
            ),
            ActionResult(step_id="finish", tool=ToolName.FINISH, success=True),
        ],
    )


@pytest.fixture
def env(tmp_path: Path):
    settings = _settings(tmp_path)
    app = create_app(settings)
    with TestClient(app) as client:
        yield client, settings, app.state.manager


def test_replay_rejects_a_script_that_is_not_a_profile_validation_script(env, monkeypatch) -> None:
    client, settings, _ = env
    stale = settings.resolved_profiles_dir / "stale-profile.json"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text("{}\n", encoding="utf-8")
    registry = ProfileRegistry(settings.resolved_profiles_dir, promotion_replay_attempts=1, min_evidence_rounds=1)
    registry.save_candidate(_profile(script_path=str(stale)))
    _install_replay(monkeypatch)

    response = client.post(f"/api/profiles/{TARGET_APP_ID}/replay", json={"attempts": 1})

    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "profile has no valid validation script"


def test_replay_still_accepts_the_profile_validation_script(env, monkeypatch) -> None:
    client, settings, _ = env
    script = _seed_script(settings, "test_run_profile_profile_validation.py")
    registry = ProfileRegistry(settings.resolved_profiles_dir, promotion_replay_attempts=1, min_evidence_rounds=1)
    registry.save_candidate(_profile(script_path=str(script)))
    _install_replay(monkeypatch)

    response = client.post(f"/api/profiles/{TARGET_APP_ID}/replay", json={"attempts": 1})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["results"][0]["passed"] is True
    assert body["total_replays"] == 1
    # 晋级证据 ID 由 registry 统一命名（Profile 验证命名空间），不是任务脚本 run_id。
    stored = registry.read(TARGET_APP_ID)
    assert stored.provenance.hypium_replay_run_ids == ["run-profile:profile-attempt-1"]


def test_task_script_is_flagged_as_non_promotion_evidence(env) -> None:
    """任务脚本可执行但 promotion_eligible=False：响应里带 promotion_blockers 供调用方判断。"""
    client, _, manager = env
    trace = _task_trace()
    trace.generated = HypiumGenerator(manager.artifacts).generate(trace)
    manager.repository.save_trace(trace)

    detail = client.get("/api/runs/run-task-script/script").json()

    assert detail["acceptance_replay_enabled"] is True
    assert detail["promotion_eligible"] is False
    assert detail["promotion_blockers"] == ["live-mode trace is not Profile-promotion evidence"]
    # 该脚本的路径绝不会被写进任何 Profile 的 provenance：晋级只认验证脚本。
    registry = ProfileRegistry(
        manager.settings.resolved_profiles_dir, promotion_replay_attempts=1, min_evidence_rounds=1
    )
    assert registry.list() == []
