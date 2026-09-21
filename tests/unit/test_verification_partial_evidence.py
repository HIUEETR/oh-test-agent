"""Phase 1.1 回归：验证循环在部分失败时必须保留已采证据（计划 R4）。

背景：原实现把「flow_failed → break」与「page identity mismatch → break」放在观测采集
**之前**。日历那一轮 page_index=0 身份不匹配即 break，导致 all_locators 为空、
stability.locators 为空，13 个真实定位器全部丢弃。
"""

from __future__ import annotations

from pathlib import Path

from test_profile_verification import FakeVerificationDevice, _discovery, _target

from harmony_test_agent.discovery import ProfileVerifier


class MismatchedHomeDevice(FakeVerificationDevice):
    """首页结构在验证期与探索期不同：page_index=0 必然身份不匹配。

    探索结果由普通设备产出（key=``page_N_control``），验证期把 key 换成
    ``boot_N_control``，既让结构身份不同、也让定位器重解析失败，用于同时覆盖
    「身份不匹配」与「core-flow 重放失败」两条曾经的 break 路径。
    """

    def snapshot_for_page(self, page: int, *, label: str = "fixture"):
        snapshot = super().snapshot_for_page(page, label=label)
        elements = [
            item.model_copy(
                update={
                    "key": f"boot_{page}_control",
                    "element_id": f"boot-control-{page}",
                    "content": f"boot control {page}",
                }
            )
            for item in snapshot.elements
        ]
        return snapshot.model_copy(update={"elements": elements}, deep=True)


def _verify(tmp_path: Path, device: FakeVerificationDevice) -> object:
    verifier = ProfileVerifier(
        device=device,  # type: ignore[arg-type]
        target=_target(),
        output_dir=tmp_path / "verification",
        run_id="verification-run",
        rounds=1,
    )
    return verifier.verify(_discovery(FakeVerificationDevice(tmp_path)))


def test_identity_mismatch_keeps_locator_evidence(tmp_path: Path) -> None:
    result = _verify(tmp_path, MismatchedHomeDevice(tmp_path))

    # 运行结论仍然是失败：failures 照记（逐轮 failures + 汇总 failures），passed 不放松。
    assert result.passed is False
    assert any("one or more device verification rounds failed" in item for item in result.failures)

    first_round = result.rounds[0]
    assert first_round.passed is False
    assert any("page identity mismatch" in item for item in first_round.failures)

    # 关键断言：已采证据不再被整体丢弃。
    assert first_round.locator_observations, "身份不匹配时仍必须采集当帧定位器观测"
    assert first_round.visited_page_identities, "已访问页身份必须保留"
    assert result.stability.locators, "部分失败轮次也必须产出真实定位器"
    assert {item.value for item in result.stability.locators} & {"boot_0_control", "boot_1_control"}


def test_page_zero_mismatch_does_not_stop_before_collecting(tmp_path: Path) -> None:
    """page_index=0 不匹配时继续登记后续页面帧，而不是整轮零证据。"""
    result = _verify(tmp_path, MismatchedHomeDevice(tmp_path))

    first_round = result.rounds[0]
    assert len(first_round.visited_page_identities) >= 2
    assert len(first_round.visited_page_signatures) == len(first_round.visited_page_identities)
    # 每一帧都登记了定位器观测（重放失败时当前帧仍算证据，不再整轮零观测）。
    assert "boot_0_control" in {item.value for item in first_round.locator_observations}


def test_matching_device_still_passes(tmp_path: Path) -> None:
    """放松 break 语义不能把失败轮次误判为通过。"""
    result = _verify(tmp_path, FakeVerificationDevice(tmp_path))

    assert result.passed is True
    assert result.failures == []
    assert result.stability.promotable_locator_count >= 3
