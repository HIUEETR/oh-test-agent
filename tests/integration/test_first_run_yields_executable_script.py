"""首次运行（磁盘无任何 Profile）必须产出**立即可执行**的脚本。

这是计划 G1/G2/G5 的端到端机器可验证形式：全新应用第一次跑任务就走
``BOOTSTRAP_ENABLED_ON_TASK_RUN=false`` ⇒ ``_enter_live_mode`` 的默认路径，
改造前这条路径产出的脚本永远 ``replay_eligible=False``（诊断产物），前端按钮禁用、
``POST /api/runs/{id}/execute`` 返回 409、``build_from_run`` 直接拒绝入库。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from PIL import Image

from harmony_test_agent.agents import AgentOrchestrator, MockAgentProvider
from harmony_test_agent.cases.library import CaseLibrary
from harmony_test_agent.config import Settings
from harmony_test_agent.devices import DeviceAdapter
from harmony_test_agent.models import (
    BoundingBox,
    CommandResult,
    RunRequest,
    RunState,
    ScreenSnapshot,
    TargetAppProfile,
    UIElement,
)
from harmony_test_agent.storage import ArtifactStore, RunRepository
from harmony_test_agent.storage.case_repository import CaseRepository
from harmony_test_agent.targets import ForegroundApp, InstalledApp

BUNDLE_NAME = "com.github.zhuoyi233.zhplus"
DISPLAY_NAME = "知乎++"
ABILITY_NAME = "EntryAbility"


def _installed() -> InstalledApp:
    return InstalledApp(
        bundle_name=BUNDLE_NAME,
        display_name=DISPLAY_NAME,
        abilities=(ABILITY_NAME,),
        main_ability=ABILITY_NAME,
        module_name="entry",
        version_name="1.0.0",
        version_code=1,
    )


class FirstRunDevice(DeviceAdapter):
    """全新应用的确定性离线设备：无 Profile、无前台查询能力（因此必然降级实时模式）。"""

    def __init__(self) -> None:
        self.state = "launcher"
        self.connected = False
        self.apps = [_installed()]
        self.foreground: ForegroundApp | None = None

    def connect(self) -> None:
        self.connected = True

    def health_check(self) -> dict[str, object]:
        return {"connected": self.connected, "id": "first-run-device", "resolution": [800, 1200]}

    def list_installed_apps(self) -> list[InstalledApp]:
        return list(self.apps)

    def inspect_app(self, bundle_name: str) -> InstalledApp:
        return next(item for item in self.apps if item.bundle_name == bundle_name)

    def current_foreground_app(self) -> ForegroundApp | None:
        return self.foreground

    def screenshot(self, output_dir: Path, run_id: str, label: str = "screen") -> ScreenSnapshot:
        output_dir.mkdir(parents=True, exist_ok=True)
        colors = {"launcher": "black", "home": "navy", "search": "teal", "search_input": "green", "detail": "purple"}
        image_path = output_dir / f"{label}_{self.state}.png"
        Image.new("RGB", (800, 1200), colors[self.state]).save(image_path)
        return ScreenSnapshot(
            snapshot_id=f"{label}-{self.state}",
            run_id=run_id,
            image_path=image_path.resolve(),
            image_sha256=hashlib.sha256(image_path.read_bytes()).hexdigest(),
            width=800,
            height=1200,
            page_path="pages/Index",
            elements=self._elements(),
        )

    def _elements(self) -> list[UIElement]:
        if self.state == "home":
            return [
                UIElement(
                    element_id="search",
                    content="搜索",
                    key="p2_home_titlebar_search",
                    type="Button",
                    clickable=True,
                    bbox=BoundingBox(left=20, top=20, right=180, bottom=80),
                ),
                UIElement(
                    element_id="detail",
                    content="一条内容详情",
                    key="p2_home_feed_card_answer_1",
                    type="Button",
                    clickable=True,
                    bbox=BoundingBox(left=200, top=250, right=600, bottom=600),
                ),
            ]
        if self.state in {"search", "search_input"}:
            return [
                UIElement(
                    element_id="input",
                    content="OpenHarmony" if self.state == "search_input" else "搜索输入框",
                    key="p2_search_input",
                    type="TextInput",
                    editable=True,
                    bbox=BoundingBox(left=80, top=100, right=700, bottom=180),
                )
            ]
        if self.state == "detail":
            return [
                UIElement(
                    element_id="content",
                    content="内容详情",
                    key="p2_answer_detail_page",
                    type="Column",
                    bbox=BoundingBox(left=10, top=80, right=790, bottom=1100),
                )
            ]
        return []

    def collect_ui_hierarchy(self) -> dict:
        return {}

    def collect_logs(self, output_path: Path) -> CommandResult:
        output_path.write_text("first-run logs", encoding="utf-8")
        return self._ok("logs")

    def open_app(self, profile: TargetAppProfile, reset: bool = False) -> CommandResult:
        self.state = "home"
        self.foreground = ForegroundApp(bundle_name=profile.bundle_name, ability_name=profile.main_ability)
        return self._ok("open_app")

    def click(self, x: int, y: int) -> CommandResult:
        self.state = "search" if x < 200 else "detail"
        return self._ok("click")

    def input_text(self, text: str, x: int | None = None, y: int | None = None) -> CommandResult:
        self.state = "search_input"
        return self._ok("input_text")

    def swipe(self, start: tuple[int, int], end: tuple[int, int], duration: float = 0.5) -> CommandResult:
        return self._ok("swipe")

    def back(self) -> CommandResult:
        self.state = "home"
        return self._ok("back")

    def wait(self, seconds: float) -> CommandResult:
        return self._ok("wait")

    def close(self) -> None:
        self.connected = False

    @staticmethod
    def _ok(name: str) -> CommandResult:
        return CommandResult(command=name, args=[name], returncode=0)


async def test_first_run_without_profile_yields_an_executable_script(tmp_path: Path) -> None:
    settings = Settings(
        agent_provider="mock",
        runtime_dir=tmp_path / "runs",
        database_path=tmp_path / "agent.db",
        target_profile_path=None,
        profiles_dir=tmp_path / "profiles",
        cases_dir=tmp_path / "cases",
        runtime_home=tmp_path / "home",
        unchanged_screen_limit=2,
    )
    library = CaseLibrary(
        CaseRepository(settings.resolved_database_path),
        settings.resolved_cases_dir,
        min_observed_rounds=settings.profile_verification_rounds,
    )
    orchestrator = AgentOrchestrator(
        settings,
        provider=MockAgentProvider(),
        repository=RunRepository(settings.resolved_database_path),
        artifacts=ArtifactStore(settings.resolved_runtime_dir),
        device_factory=lambda _: FirstRunDevice(),
        settle_seconds=0,
        launch_settle_seconds=0,
        case_library=library,
    )

    trace = await orchestrator.run(
        RunRequest(
            # 显式 bundle：全新应用在磁盘上还没有 Profile，身份只能来自设备查询。
            target={"bundle_name": BUNDLE_NAME},
            task="打开知乎++，进入搜索，输入 OpenHarmony，返回首页，打开一条内容详情，查看内容后返回首页",
            auto_generate=True,
        )
    )

    assert trace.state == RunState.COMPLETED, trace.error
    # 默认路径：任务型运行不前置完整探索 ⇒ 实时模式（没有走探索，因此 provisional 仍为 False）。
    assert trace.live_mode is True
    assert trace.provisional is False

    # G1：立即可用的脚本。
    generated = trace.generated
    assert generated is not None
    assert generated.purpose == "acceptance"
    assert generated.replay_eligible is True
    assert generated.runnable_blockers == []
    assert generated.python_path.exists()
    assert generated.python_path.name.startswith("test_")
    assert generated.python_path.parent.name == "generated"

    # G2：live_mode 只出现在晋级层，不出现在阻断执行/入库的判定里。
    assert generated.promotion_eligible is False
    assert "live-mode trace is not Profile-promotion evidence" in generated.promotion_blockers

    # 生成的 config / metadata 与内存产物一致（前端脚本面板直接读这两份文件）。
    config = json.loads(generated.config_path.read_text(encoding="utf-8"))
    assert config["purpose"] == "acceptance"
    assert config["replay_eligible"] is True
    metadata = json.loads(generated.metadata_path.read_text(encoding="utf-8"))
    assert metadata["purpose"] == "acceptance"
    assert metadata["replay_eligible"] is True
    assert metadata["incomplete_reasons"] == metadata["confidence_factors"]

    # 任务期证据回收把草稿 Profile 落盘，供后续运行复用。
    draft = settings.resolved_profiles_dir / "draft" / f"{trace.target_app_id}.json"
    assert draft.is_file(), sorted(path.name for path in settings.resolved_profiles_dir.rglob("*"))
    assert json.loads(draft.read_text(encoding="utf-8"))["stable_locator_inventory"]

    # G5：用例自动入库，不再被质量门禁拦下。
    cases = library.list()
    assert cases
    # 用例库按来源身份推导稳定 case_id（与生成器随机 case_id 不同），因此用列表项取记录。
    record = library.get(cases[0].case_id)
    assert record is not None
    assert record.promotion_eligible is False
    assert record.promotion_blockers == ["live-mode trace is not Profile-promotion evidence"]
