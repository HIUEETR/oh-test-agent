"""RunMode 历史兼容测试（2026-09-17 资产流水线重构 Phase 0）。

重构把 RunMode 收缩为 ``regression`` 单值：``exploration`` / ``stability`` /
``reproduction`` 只保留为枚举成员，用于反序列化历史 ``trace.json`` 与承接历史
客户端请求，解析后一律降级为 ``regression``。
"""

from __future__ import annotations

import pytest

from harmony_test_agent.models import RunMode, RunRequest, RunTrace, TargetAppProfile

LEGACY_MODES = ("exploration", "stability", "reproduction")


@pytest.fixture(autouse=True)
def _legacy_modes_disabled():
    """确保降级分支生效：本文件不测 ``ENABLE_LEGACY_RUN_MODES=true`` 的回滚路径。"""
    from harmony_test_agent.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.parametrize("legacy", LEGACY_MODES)
def test_run_request_normalizes_legacy_mode(legacy: str):
    """POST /api/runs 含 mode=exploration 时静默归一化为 regression。"""
    request = RunRequest.model_validate({"task": "x", "mode": legacy})

    assert request.mode == RunMode.REGRESSION


def test_run_request_keeps_regression_and_rejects_unknown_mode():
    """只降级已知的历史值，其他取值仍由枚举校验报错。"""
    assert RunRequest.model_validate({"task": "x", "mode": "regression"}).mode == RunMode.REGRESSION

    with pytest.raises(ValueError):
        RunRequest.model_validate({"task": "x", "mode": "not-a-mode"})


@pytest.mark.parametrize("legacy", LEGACY_MODES)
def test_run_trace_deserializes_legacy_mode(legacy: str):
    """历史 trace.json 含 mode=stability 时可正常反序列化并降级。"""
    trace = RunTrace.model_validate(
        {
            "run_id": "run-legacy",
            "target_app_id": "zhihu-plus",
            "task": "legacy trace",
            "mode": legacy,
            "device_id": "127.0.0.1:5555",
        }
    )

    assert trace.mode == RunMode.REGRESSION


def test_run_trace_json_roundtrip_with_legacy_mode():
    """磁盘上的历史 trace.json 字符串同样走 validator。"""
    payload = (
        '{"run_id": "run-1", "target_app_id": "zhihu-plus", "task": "t",'
        ' "mode": "exploration", "device_id": "127.0.0.1:5555"}'
    )

    assert RunTrace.model_validate_json(payload).mode == RunMode.REGRESSION


def test_run_mode_enum_keeps_legacy_members_for_deserialization():
    """枚举成员保留（供旧数据读取），但值语义已标注 deprecated。"""
    assert [mode.value for mode in RunMode] == [
        "regression",
        "exploration",
        "stability",
        "reproduction",
    ]


def test_verified_profile_accepts_single_hypium_replay_id():
    """verified Profile 只需 >=1 个唯一 replay ID（1 轮门禁），不再强制 3 个。"""
    profile = TargetAppProfile(
        status="verified",
        target_app_id="demo",
        display_name="Demo",
        bundle_name="com.demo",
        stable_locator_inventory=[
            {"name": f"l{index}", "key": f"k{index}", "page_signature": f"page-{index}"} for index in range(3)
        ],
        assertion_inventory=[
            {"name": "a1", "kind": "visible", "target": "搜索"},
            {"name": "a2", "kind": "visible", "target": "首页"},
        ],
        provenance={
            "verified_at": "2026-09-17T00:00:00Z",
            "hypium_replay_run_ids": ["run-1:profile-attempt-1"],
            "evidence": {"verification_passed": True},
        },
    )

    assert profile.status == "verified"


@pytest.mark.parametrize(
    "replay_ids",
    [
        [],
        ["run-1:profile-attempt-1", "run-1:profile-attempt-1"],
    ],
)
def test_verified_profile_rejects_empty_or_duplicate_replay_ids(replay_ids: list[str]):
    with pytest.raises(ValueError):
        TargetAppProfile(
            status="verified",
            target_app_id="demo",
            display_name="Demo",
            bundle_name="com.demo",
            stable_locator_inventory=[
                {"name": f"l{index}", "key": f"k{index}", "page_signature": f"page-{index}"} for index in range(3)
            ],
            assertion_inventory=[
                {"name": "a1", "kind": "visible", "target": "搜索"},
                {"name": "a2", "kind": "visible", "target": "首页"},
            ],
            provenance={
                "verified_at": "2026-09-17T00:00:00Z",
                "hypium_replay_run_ids": replay_ids,
                "evidence": {"verification_passed": True},
            },
        )
