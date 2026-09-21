"""Phase 5.2/5.3 回归：截图与上传走快路径，元素列表按需截断。

实测（DC `screenshot` 工具）：9 次调用吃掉 153.5s，单次 11.5-19.7s——其中一半是「JPEG 快速
路径之后又完整截图一次只为拿元素表」。Live 侧 24s/次往返的主因之一则是全尺寸 PNG + 131 元素
全量列表。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from harmony_test_agent.agents.providers import OpenAICompatibleProvider
from harmony_test_agent.config import Settings
from harmony_test_agent.models import BoundingBox, ScreenSnapshot, UIElement


def _snapshot(tmp_path: Path, *, elements: int = 5) -> ScreenSnapshot:
    from PIL import Image

    png = tmp_path / "frame.png"
    Image.new("RGB", (120, 200), "navy").save(png)
    jpeg = tmp_path / "frame.device.jpeg"
    Image.new("RGB", (120, 200), "navy").save(jpeg, format="JPEG", quality=70)
    return ScreenSnapshot(
        snapshot_id="snap-fast",
        run_id="run-fast",
        image_path=png.resolve(),
        model_image_path=jpeg.resolve(),
        image_sha256="hash",
        width=120,
        height=200,
        page_path="pages/Home",
        elements=[
            UIElement(
                element_id=f"e{index}",
                content=f"item {index}",
                key=f"key_{index}",
                type="Button",
                bbox=BoundingBox(left=0, top=index * 10, right=50, bottom=index * 10 + 8),
                clickable=True,
                score=index / max(elements - 1, 1),
            )
            for index in range(elements)
        ],
    )


def _provider(**overrides: object) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        Settings(
            _env_file=None,
            openai_api_key="test-key",
            agent_model="test-model",
            agent_vision_model="test-vision-model",
            agent_provider="openai",
            **overrides,
        )
    )


def test_image_payload_prefers_device_jpeg(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)

    data, media_type = _provider()._image_payload(snapshot)

    assert media_type == "image/jpeg"
    assert data.startswith(b"\xff\xd8")  # JPEG SOI
    assert data == snapshot.model_image_path.read_bytes()


def test_image_payload_converts_png_when_no_device_jpeg(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path).model_copy(update={"model_image_path": None}, deep=True)

    data, media_type = _provider()._image_payload(snapshot)

    assert media_type == "image/jpeg"
    assert data.startswith(b"\xff\xd8")
    assert data != snapshot.image_path.read_bytes()


def test_image_payload_can_stay_png(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)

    data, media_type = _provider(model_image_format="png")._image_payload(snapshot)

    assert media_type == "image/png"
    assert data == snapshot.image_path.read_bytes()


def test_element_payload_is_truncated_and_ranked(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path, elements=40)

    payload = _provider(model_element_limit=10)._element_payload(snapshot, with_identity=True)

    assert len(payload) == 10
    # 按 score 降序：最高分的元素（最后一个）必须入选。
    assert payload[0]["element_id"] == "e39"
    assert "key" in payload[0] and "id" in payload[0]


def test_element_payload_without_identity_omits_keys(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path, elements=3)

    payload = _provider()._element_payload(snapshot, with_identity=False)

    assert all("key" not in entry and "id" not in entry for entry in payload)
    assert payload[0]["content"]


# ---------------------------------------------------------------------------
# DC 截图工具：复用同一帧，不再补一次完整截图
# ---------------------------------------------------------------------------


class _CollectingDevice:
    """实现快路径能力的设备替身：记录是否走过 snapshot_from_capture。"""

    def __init__(self, snapshot: ScreenSnapshot) -> None:
        self.snapshot = snapshot
        self.from_capture_calls = 0
        self.full_screenshot_calls = 0

    def snapshot_from_capture(self, image_path: Path, run_id: str, *, width: int, height: int, label: str = "screen"):
        self.from_capture_calls += 1
        del run_id, width, height, label
        resolved = image_path.resolve()
        return self.snapshot.model_copy(
            update={"image_path": resolved, "model_image_path": resolved, "snapshot_id": "snap-from-capture"},
            deep=True,
        )

    def screenshot(self, output_dir: Path, run_id: str, label: str = "screen"):  # pragma: no cover - 不应被调用
        self.full_screenshot_calls += 1
        return self.snapshot


def _tool_screenshot_harness(tmp_path: Path):
    """构造 DC 截图工具所需的最小 RunContext（放在函数里避免顶层 import 顺序问题）。"""
    from pydantic_ai import RunContext
    from pydantic_ai.usage import RunUsage

    from harmony_test_agent.dc.models import DcToolTier
    from harmony_test_agent.dc.tools import DcActionRecorder, DcSnapshotHolder, DcToolContext

    holder = DcSnapshotHolder()
    device = _CollectingDevice(_snapshot(tmp_path, elements=3))

    class _Hdc:
        def screenshot_jpeg(self, screens_dir: Path, name: str, on_phase=None):
            del on_phase
            screens_dir.mkdir(parents=True, exist_ok=True)
            path = screens_dir / f"{name}.jpeg"
            path.write_bytes(b"fake-jpeg")
            return path, b"fake-jpeg", 1080, 1920

    deps = DcToolContext(
        session_id="dc-fast",
        device=device,  # type: ignore[arg-type]
        hdc=_Hdc(),  # type: ignore[arg-type]
        safety=None,  # type: ignore[arg-type]
        recorder=DcActionRecorder(),
        artifacts=None,  # type: ignore[arg-type]
        session_dir=tmp_path,
        snapshot_holder=holder,
        tier=DcToolTier.L1,
    )
    # 截图工具只读取 deps；model/usage 是 RunContext 的形式参数，用占位值即可。
    ctx = RunContext(deps=deps, model=None, usage=RunUsage())  # type: ignore[arg-type]
    return ctx, device, holder


def test_dc_screenshot_tool_reuses_frame_without_full_capture(tmp_path: Path) -> None:
    from harmony_test_agent.dc.tools import tool_screenshot

    ctx, device, holder = _tool_screenshot_harness(tmp_path)

    summary = asyncio.run(tool_screenshot(ctx))

    assert device.from_capture_calls == 1
    assert device.full_screenshot_calls == 0
    assert holder.latest is not None
    assert holder.latest.image_path.suffix == ".jpeg"
    assert "elements" in summary or "Screenshot" in summary
