from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from harmony_test_agent.agents.providers import OpenAICompatibleProvider
from harmony_test_agent.config import Settings
from harmony_test_agent.devices import HarmonyDeviceAdapter
from harmony_test_agent.models import ToolName
from harmony_test_agent.storage import ArtifactStore

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("RUN_LIVE_TESTS") != "1", reason="set RUN_LIVE_TESTS=1 explicitly"),
]

TASK = "打开知乎++，进入搜索，输入 OpenHarmony，返回首页，打开一条内容详情，确认页面存在可见内容后返回首页。"


@pytest.mark.asyncio
async def test_real_provider_plans_and_analyzes_device_screenshot() -> None:
    settings = Settings(agent_provider="openai")
    if not settings.model_configured or not settings.vision_model_configured:
        pytest.fail("real model and vision model credentials are required")

    provider = OpenAICompatibleProvider(settings)
    artifacts = ArtifactStore(settings.resolved_runtime_dir)
    profile = artifacts.load_profile(settings.resolved_target_profile_path)
    plan = await asyncio.wait_for(provider.plan(TASK, profile, settings.agent_max_steps), timeout=120)

    assert plan.mock is False
    assert plan.model_used == settings.agent_model
    assert plan.steps
    assert plan.steps[-1].tool == ToolName.FINISH

    evidence_dir = settings.resolved_runtime_dir.parent / "validation" / "live-provider-smoke"
    device = HarmonyDeviceAdapter(
        settings.harmony_device,
        settings.hdc_path,
        settings.agent_action_timeout,
    )
    try:
        await asyncio.to_thread(device.connect)
        snapshot = await asyncio.to_thread(
            device.screenshot,
            evidence_dir / "screens",
            "live-provider-smoke",
            "device",
        )
    finally:
        device.close()

    observation = await asyncio.wait_for(provider.analyze(snapshot), timeout=120)
    decision = await asyncio.wait_for(provider.decide(plan.steps[0], snapshot), timeout=120)
    assert observation is not None
    assert decision.tool in ToolName
    assert observation.page_title or observation.summary
    assert all(
        0 <= element.bbox.left < element.bbox.right <= snapshot.width
        and 0 <= element.bbox.top < element.bbox.bottom <= snapshot.height
        for element in observation.elements
    )

    evidence_dir.mkdir(parents=True, exist_ok=True)
    result_path = evidence_dir / "result.json"
    result_path.write_text(
        json.dumps(
            {
                "provider": "openai-compatible",
                "model": provider.name,
                "mock": False,
                "plan": plan.model_dump(mode="json"),
                "snapshot": {
                    "path": str(snapshot.image_path),
                    "width": snapshot.width,
                    "height": snapshot.height,
                    "sha256": snapshot.image_sha256,
                    "hierarchy_elements": len(snapshot.elements),
                },
                "observation": observation.model_dump(mode="json"),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    assert Path(result_path).is_file()
