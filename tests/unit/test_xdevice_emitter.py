"""``XDeviceEmitter`` 的目录布局、``testfile.json`` 一致性与发射代码契约。

覆盖计划 A6：``testcases/HarmonyAgentCases/{testfile.json,test_case.py}`` + ``testlist.txt``
+ 空 ``reports/`` / ``logs/``；``compile()`` 通过；``Step("N. 中文标题")`` 编号；
text/property/current_app 的 ``CHECK``+``ASSERT`` 成对；计数类压测发 ``@loop``、
soak 发显式循环；``_device_sn`` 优先级；import 只来自两个允许的模块。
"""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Callable
from pathlib import Path

import pytest

from harmony_test_agent.cases.spec import (
    BugReproSpec,
    CaseProvenance,
    CheckpointKind,
    CheckpointSpec,
    LocatorSpec,
    ScenarioKind,
    StepAction,
    StressSpec,
    TeardownSpec,
    TestCaseSpec,
    TestParamSpec,
    TestStepSpec,
)
from harmony_test_agent.generation import xdevice_case as xdevice_case_module
from harmony_test_agent.generation.checkpoints import DEFAULT_INDENT
from harmony_test_agent.generation.xdevice_case import (
    XDEVICE_CASE_CLASS,
    XDEVICE_CASE_FILE,
    XDEVICE_MAIN_METHOD,
    XDEVICE_STRESS_METHOD,
    XDEVICE_SUITE_NAME,
    XDeviceArtifact,
    XDeviceEmitter,
)
from harmony_test_agent.models import LocatorKind, StressKind

#: 生成文件里允许出现的 import 来源（除标准库外）。
ALLOWED_IMPORT_MODULES = {"devicetest.core.test_case", "hypium"}
STDLIB_IMPORT_MODULES = {"json", "os", "re", "time", "pathlib", "__future__"}

# pytest 会把导入的 pydantic ``Test*`` 类误当成测试类收集，这里显式排除。
TestCaseSpec.__test__ = False
TestParamSpec.__test__ = False
TestStepSpec.__test__ = False

SpecFactory = Callable[[], TestCaseSpec]


# ---------------------------------------------------------------------------
# fixture 构造
# ---------------------------------------------------------------------------


def key(value: str, label: str = "") -> LocatorSpec:
    """KEY 定位器；``label`` 决定中文显示标签。"""
    return LocatorSpec(kind=LocatorKind.KEY, value=value, target_label=label)


def coordinate(point: tuple[int, int]) -> LocatorSpec:
    """已解析为坐标的定位器（必须带解析边界与警告，见 IR 不变式）。"""
    return LocatorSpec(
        kind=LocatorKind.COORDINATE,
        coordinate=point,
        resolution_bound=(1080, 2340),
        warning="spatial locator resolved to a coordinate",
    )


def provenance(**overrides) -> CaseProvenance:
    data = {
        "source_kind": "live_run",
        "source_id": "run-20240101-000000",
        "profile_target_app_id": "com.zhihu.hmos",
    }
    data.update(overrides)
    return CaseProvenance(**data)


def core_flow_spec() -> TestCaseSpec:
    """普通核心流：点击 → 输入（数据驱动）→ 滑动 → 坐标兜底点击。"""
    return TestCaseSpec(
        case_id="case-20240101T000000Z-abcdef",
        slug="zhihu-core-search",
        title_zh="核心流程：首页搜索到结果页",
        scenario=ScenarioKind.CORE_FLOW,
        bundle_name="com.zhihu.hmos",
        main_ability="EntryAbility",
        device_sn="SN-IR-01",
        timeout_seconds=600,
        params=[TestParamSpec(name="search_text", default="OpenHarmony", description_zh="搜索关键词")],
        steps=[
            TestStepSpec(
                step_id="s1",
                index=1,
                action=StepAction.CLICK,
                title_zh="点击搜索入口",
                locator=key("p2_home_titlebar_search", "搜索入口"),
                checkpoints=[
                    CheckpointSpec(
                        kind=CheckpointKind.ELEMENT_EXISTS,
                        message_zh="搜索输入框应可见",
                        locator=key("p2_search_input", "搜索输入框"),
                        wait_seconds=5,
                    )
                ],
            ),
            TestStepSpec(
                step_id="s2",
                index=2,
                action=StepAction.INPUT_TEXT,
                title_zh="输入搜索关键词",
                locator=key("p2_search_input", "搜索输入框"),
                param_ref="search_text",
                checkpoints=[
                    CheckpointSpec(
                        kind=CheckpointKind.TEXT_EQUALS,
                        message_zh="输入框文本应为 OpenHarmony",
                        locator=key("p2_search_input", "搜索输入框"),
                        expected="OpenHarmony",
                    ),
                    CheckpointSpec(
                        kind=CheckpointKind.PROPERTY_EQUALS,
                        message_zh="输入框应可用",
                        locator=key("p2_search_input", "搜索输入框"),
                        property_name="enabled",
                        expected=True,
                    ),
                    CheckpointSpec(
                        kind=CheckpointKind.CURRENT_APP,
                        message_zh="前台应用仍为被测应用",
                        expected="com.zhihu.hmos",
                    ),
                ],
            ),
            TestStepSpec(
                step_id="s3",
                index=3,
                action=StepAction.SWIPE,
                title_zh="向上滑动结果列表",
                direction="up",
                checkpoints=[
                    CheckpointSpec(
                        kind=CheckpointKind.TEXT_CONTAINS,
                        message_zh="页面应包含搜索词",
                        expected="OpenHarmony",
                        soft=True,
                    )
                ],
            ),
            TestStepSpec(
                step_id="s4",
                index=4,
                action=StepAction.CLICK,
                title_zh="点击坐标兜底入口",
                locator=coordinate((540, 1200)),
            ),
        ],
        teardown=TeardownSpec(capture_final_screenshot=True, stop_app=True),
        provenance=provenance(),
    )


def bug_repro_spec() -> TestCaseSpec:
    """缺陷复现：页面特征锚点 + 前台应用哨兵。"""
    return TestCaseSpec(
        case_id="case-20240102T000000Z-abcde1",
        slug="zhihu-detail-white-screen",
        title_zh="缺陷复现：详情页进入后白屏",
        scenario=ScenarioKind.BUG_REPRODUCTION,
        bundle_name="com.zhihu.hmos",
        timeout_seconds=300,
        steps=[
            TestStepSpec(
                step_id="b1",
                index=1,
                action=StepAction.CLICK,
                title_zh="点击第一条结果",
                locator=key("p2_result_item", "第一条结果"),
                checkpoints=[
                    CheckpointSpec(
                        kind=CheckpointKind.PAGE_SIGNATURE,
                        message_zh="详情页锚点应可见",
                        page_path="detail",
                        anchors=[key("p2_detail_title", "详情标题")],
                    )
                ],
            ),
            TestStepSpec(
                step_id="b2",
                index=2,
                action=StepAction.BACK,
                title_zh="返回首页",
                checkpoints=[
                    CheckpointSpec(
                        kind=CheckpointKind.CURRENT_APP,
                        message_zh="应用未崩溃或退出",
                        expected="com.zhihu.hmos",
                    )
                ],
            ),
        ],
        bug_repro=BugReproSpec(
            symptom="进入详情页后白屏",
            symptom_kind="white_screen",
            expected="详情页正常渲染标题",
            actual="页面全白",
        ),
        provenance=provenance(source_kind="bug_report", source_id="BUG-1024"),
    )


def counted_stress_spec() -> TestCaseSpec:
    """纯计数压测：无时长预算、无内存采样 → ``@loop`` 装饰器。"""
    return TestCaseSpec(
        case_id="case-20240103T000000Z-abcde2",
        slug="zhihu-swipe-stress",
        title_zh="压测：连续上下滑动 20 次",
        scenario=ScenarioKind.STRESS,
        bundle_name="com.zhihu.hmos",
        timeout_seconds=1800,
        stress=StressSpec(
            kind=StressKind.CONTINUOUS_SWIPE,
            iterations=20,
            fail_break=True,
            fail_times=0,
            inter_iteration_wait_seconds=0.5,
            body_steps=[
                TestStepSpec(step_id="k1", index=1, action=StepAction.SWIPE, title_zh="向上滑动", direction="up"),
                TestStepSpec(step_id="k2", index=2, action=StepAction.SWIPE, title_zh="向下滑动", direction="down"),
            ],
            per_iteration_checkpoints=[
                CheckpointSpec(
                    kind=CheckpointKind.ELEMENT_EXISTS,
                    message_zh="首页锚点应可见",
                    locator=key("p2_home_titlebar_search", "搜索入口"),
                    wait_seconds=5,
                )
            ],
        ),
        provenance=provenance(source_kind="stress_request", source_id="stress-1"),
    )


def soak_stress_spec() -> TestCaseSpec:
    """soak 压测：时长预算 + 内存采样 → 显式 ``for``/deadline 循环。"""
    return TestCaseSpec(
        case_id="case-20240104T000000Z-abcde3",
        slug="zhihu-soak-enter-exit",
        title_zh="压测：页面进出长稳 10 分钟",
        scenario=ScenarioKind.STRESS,
        bundle_name="com.zhihu.hmos",
        timeout_seconds=3600,
        stress=StressSpec(
            kind=StressKind.SOAK,
            iterations=200,
            duration_budget_seconds=600,
            fail_break=True,
            sample_memory_every=5,
            memory_growth_threshold_kb=102400,
            step_log_interval=10,
            inter_iteration_wait_seconds=1.0,
            body_steps=[
                TestStepSpec(step_id="q1", index=1, action=StepAction.BACK, title_zh="返回首页"),
            ],
            per_iteration_checkpoints=[
                CheckpointSpec(
                    kind=CheckpointKind.ELEMENT_EXISTS,
                    message_zh="首页锚点应可见",
                    locator=key("p2_home_titlebar_search", "搜索入口"),
                    wait_seconds=5,
                )
            ],
        ),
        provenance=provenance(source_kind="stress_request", source_id="stress-2"),
    )


ALL_SPECS: list[SpecFactory] = [core_flow_spec, bug_repro_spec, counted_stress_spec, soak_stress_spec]


def emit(spec: TestCaseSpec, tmp_path: Path, *, version: int = 1) -> XDeviceArtifact:
    """把 spec 渲染到 ``tmp_path/xdevice``。"""
    return XDeviceEmitter().render(spec, tmp_path / "xdevice", version=version)


def step_numbers(source: str) -> list[int]:
    """按出现顺序取出 ``Step("N. ...")`` 里的编号。"""
    return [int(match) for match in re.findall(r'Step\("(\d+)\. ', source)]


# ---------------------------------------------------------------------------
# 目录布局与 testfile.json
# ---------------------------------------------------------------------------


def test_render_writes_plan_layout(tmp_path: Path) -> None:
    artifact = emit(core_flow_spec(), tmp_path)
    root = tmp_path / "xdevice"
    suite_dir = root / "testcases" / XDEVICE_SUITE_NAME

    assert root.is_dir()
    assert (suite_dir / XDEVICE_CASE_FILE).is_file()
    assert (suite_dir / "testfile.json").is_file()
    assert (root / "testlist.txt").read_text(encoding="utf-8") == f"{XDEVICE_SUITE_NAME}\n"
    assert (root / "reports").is_dir()
    assert (root / "logs").is_dir()
    assert list((root / "reports").iterdir()) == []
    assert list((root / "logs").iterdir()) == []

    assert artifact.root == root
    assert artifact.suite == XDEVICE_SUITE_NAME
    assert artifact.case_file == suite_dir / XDEVICE_CASE_FILE
    assert artifact.testfile_path == suite_dir / "testfile.json"
    assert artifact.testlist_path == root / "testlist.txt"
    assert artifact.reports_dir == root / "reports"
    assert artifact.logs_dir == root / "logs"
    assert artifact.class_name == XDEVICE_CASE_CLASS
    assert artifact.case_file.read_text(encoding="utf-8") == artifact.python_text


@pytest.mark.parametrize("factory", ALL_SPECS)
def test_generated_module_executes_against_the_installed_kits(tmp_path: Path, factory: SpecFactory) -> None:
    """生成文件的 import 必须真的能在已安装的 devicetest / hypium 上解析（不接触设备）。"""
    artifact = emit(factory(), tmp_path)
    namespace: dict = {"__name__": "generated_xdevice_case", "__file__": str(artifact.case_file)}
    exec(compile(artifact.python_text, str(artifact.case_file), "exec"), namespace)

    case_class = namespace[XDEVICE_CASE_CLASS]
    assert case_class.__name__ == XDEVICE_CASE_CLASS
    assert issubclass(case_class, namespace["TestCase"])
    assert callable(getattr(case_class, artifact.method))
    assert isinstance(namespace["PARAMS"], dict)


def test_render_creates_missing_parent_directories(tmp_path: Path) -> None:
    root = tmp_path / "deep" / "nested" / "xdevice"
    artifact = XDeviceEmitter().render(core_flow_spec(), root)
    assert artifact.case_file.is_file()
    assert artifact.reports_dir.is_dir()


def test_testfile_json_matches_emitted_code(tmp_path: Path) -> None:
    spec = core_flow_spec()
    artifact = emit(spec, tmp_path, version=3)
    testfile = json.loads(artifact.testfile_path.read_text(encoding="utf-8"))

    assert testfile == artifact.testfile
    # Phase 0 真机 spike（2026-09-21）实测：py_file 是相对 -tcpath 的路径，必须带 <suite>/；
    # 且用例文件名必须等于 TestCase 类名（devicetest Script-0203016）。
    relative_case = f"{XDEVICE_SUITE_NAME}/{XDEVICE_CASE_FILE}"
    assert testfile["driver"]["py_file"] == [relative_case]
    assert testfile["driver"]["type"] == "DeviceTest"
    assert testfile["driver"]["timeout"] == spec.timeout_seconds
    assert testfile["environment"] == []
    assert testfile["kits"] == []
    assert testfile["description"] == (f"harmony_test_agent generated case {spec.case_id} v3 (core_flow)")

    entry = testfile["tests"][0]
    assert entry["name"] == artifact.method == XDEVICE_MAIN_METHOD
    assert entry["class"] == XDEVICE_CASE_CLASS
    assert entry["suite"] == XDEVICE_SUITE_NAME
    assert entry["testfile"] == f"./{XDEVICE_SUITE_NAME}/{XDEVICE_CASE_FILE}"
    assert f"class {entry['class']}(TestCase):" in artifact.python_text
    assert f"    def {entry['name']}(self):" in artifact.python_text


@pytest.mark.parametrize("factory", ALL_SPECS)
def test_testfile_timeout_and_method_track_the_spec(tmp_path: Path, factory: SpecFactory) -> None:
    spec = factory()
    artifact = emit(spec, tmp_path)
    assert artifact.timeout_seconds == spec.timeout_seconds
    assert artifact.testfile["driver"]["timeout"] == spec.timeout_seconds
    assert artifact.testfile["tests"][0]["name"] == artifact.method
    assert f"    def {artifact.method}(self):" in artifact.python_text


# ---------------------------------------------------------------------------
# 四个场景都必须能编译
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("factory", ALL_SPECS)
def test_generated_source_compiles(tmp_path: Path, factory: SpecFactory) -> None:
    artifact = emit(factory(), tmp_path)
    compile(artifact.python_text, str(artifact.case_file), "exec")
    ast.parse(artifact.python_text)


def test_bug_repro_emits_page_signature_and_current_app(tmp_path: Path) -> None:
    artifact = emit(bug_repro_spec(), tmp_path)
    text = artifact.python_text
    assert "# page detail" in text
    assert "driver.check_component_exist(BY.key('p2_detail_title'), expect_exist=True, wait_time=5)" in text
    assert "bundle, ability = driver.current_app()" in text
    assert artifact.method == XDEVICE_MAIN_METHOD


# ---------------------------------------------------------------------------
# Step 编号与中文标题
# ---------------------------------------------------------------------------


def test_step_numbering_covers_steps_and_checkpoints(tmp_path: Path) -> None:
    artifact = emit(core_flow_spec(), tmp_path)
    numbers = step_numbers(artifact.python_text)
    # 4 个步骤 + 5 个检查点，共用同一套从 1 开始的编号。
    assert numbers == list(range(1, 10))

    text = artifact.python_text
    assert 'Step("1. 点击「搜索入口」")' in text
    assert 'Step("2. 检查点：「搜索输入框」应可见")' in text
    assert 'Step("3. 在「搜索输入框」输入「{search_text}」")' in text
    assert 'Step("7. 向上滑动页面")' in text
    assert 'Step("9. 点击「坐标 (540, 1200)」")' in text


def test_step_numbering_is_independent_per_method(tmp_path: Path) -> None:
    spec = core_flow_spec()
    spec.setup.pre_steps = [
        TestStepSpec(step_id="p1", index=1, action=StepAction.WAIT, title_zh="等待首页", wait_seconds=1.0)
    ]
    spec.teardown.post_steps = [
        TestStepSpec(step_id="t1", index=1, action=StepAction.BACK, title_zh="回到首页"),
    ]
    artifact = emit(spec, tmp_path)
    setup_section, rest = artifact.python_text.split("def test_main", 1)
    main_section, teardown_section = rest.split("def teardown", 1)
    assert step_numbers(setup_section) == [1]
    assert step_numbers("def test_main" + main_section) == list(range(1, 10))
    assert step_numbers(teardown_section) == [1]


def test_counted_stress_numbers_the_loop_body(tmp_path: Path) -> None:
    artifact = emit(counted_stress_spec(), tmp_path)
    assert step_numbers(artifact.python_text) == [1, 2, 3]
    text = artifact.python_text
    assert 'Step("1. 向上滑动页面")' in text
    assert 'Step("2. 向下滑动页面")' in text
    assert 'Step("3. 检查点：「搜索入口」应可见")' in text


# ---------------------------------------------------------------------------
# 检查点渲染
# ---------------------------------------------------------------------------


def test_text_property_current_app_render_check_and_assert_pairs(tmp_path: Path) -> None:
    artifact = emit(core_flow_spec(), tmp_path)
    lines = artifact.python_text.splitlines()

    assert artifact.python_text.count("CHECK(") == 3
    assert artifact.python_text.count("ASSERT(") == 3
    check_positions = [index for index, line in enumerate(lines) if "CHECK(" in line]
    assert len(check_positions) == 3
    for index in check_positions:
        assert lines[index + 1].strip().startswith("ASSERT(")

    text = artifact.python_text
    assert "actual = driver.get_component_property(BY.key('p2_search_input'), 'text')" in text
    assert "actual = driver.get_component_property(BY.key('p2_search_input'), 'enabled')" in text
    assert "bundle, ability = driver.current_app()" in text


def test_every_checkpoint_is_delegated_to_the_shared_renderer(tmp_path: Path, monkeypatch) -> None:
    """本 emitter 不得自己实现检查点渲染：每个检查点都必须走共享函数。"""
    calls: list[tuple[CheckpointKind, str]] = []
    real = xdevice_case_module.render_devicetest_checkpoint

    def spy(checkpoint, *, indent=DEFAULT_INDENT):
        calls.append((checkpoint.kind, indent))
        return real(checkpoint, indent=indent)

    monkeypatch.setattr(xdevice_case_module, "render_devicetest_checkpoint", spy)
    spec = core_flow_spec()
    emit(spec, tmp_path)

    expected = [cp.kind for step in spec.steps for cp in step.checkpoints]
    assert [kind for kind, _ in calls] == expected
    assert {indent for _, indent in calls} == {DEFAULT_INDENT}


def test_soft_checkpoint_uses_the_shared_devicetest_wrapper(tmp_path: Path) -> None:
    """``soft=True`` 由共享渲染器包成 ``try/except`` + ``MESSAGE``（不抛断言）。"""
    spec = bug_repro_spec()
    spec.steps[0].checkpoints.append(
        CheckpointSpec(
            kind=CheckpointKind.ELEMENT_EXISTS,
            message_zh="软检查点：广告位应可见",
            locator=key("p2_ad_banner", "广告位"),
            soft=True,
        )
    )
    artifact = emit(spec, tmp_path)
    text = artifact.python_text

    assert "        try:" in text
    assert "            driver.check_component_exist(BY.key('p2_ad_banner'), expect_exist=True)" in text
    assert "        except Exception as _exc:" in text
    assert "            MESSAGE(str(_exc))" in text
    compile(text, str(artifact.case_file), "exec")


def test_coordinate_fallback_is_annotated(tmp_path: Path) -> None:
    artifact = emit(core_flow_spec(), tmp_path)
    assert 'driver.touch((540, 1200))  # coordinate fallback for "坐标 (540, 1200)"' in artifact.python_text


def test_param_ref_renders_param_helper(tmp_path: Path) -> None:
    artifact = emit(core_flow_spec(), tmp_path)
    text = artifact.python_text
    assert "def param(name, default=None):" in text
    assert "driver.input_text(BY.key('p2_search_input'), param('search_text', 'OpenHarmony'))" in text
    assert "HARMONY_AGENT_CASE_PARAMS" in text


def test_param_helper_is_omitted_when_unused(tmp_path: Path) -> None:
    artifact = emit(bug_repro_spec(), tmp_path)
    assert "def param(name, default=None):" not in artifact.python_text


def test_teardown_closes_driver_and_captures_final_screenshot(tmp_path: Path) -> None:
    artifact = emit(core_flow_spec(), tmp_path)
    text = artifact.python_text
    assert 'driver.capture_screen(str(self._report_dir() / "final.jpeg"))' in text
    assert "driver.stop_app(BUNDLE_NAME)" in text
    assert "driver.close()" in text


# ---------------------------------------------------------------------------
# 压测形态
# ---------------------------------------------------------------------------


def test_counted_stress_uses_loop_decorator(tmp_path: Path) -> None:
    artifact = emit(counted_stress_spec(), tmp_path)
    text = artifact.python_text

    assert artifact.method == XDEVICE_STRESS_METHOD
    assert artifact.testfile["tests"][0]["name"] == XDEVICE_STRESS_METHOD
    assert "@loop(times=ITERATIONS, fail_break=True, fail_times=0, continues_fail=False)" in text
    assert f"    def {XDEVICE_STRESS_METHOD}(self):" in text
    assert "    def test_main(self):" not in text
    assert "from devicetest.core.test_case import ASSERT, CHECK, MESSAGE, Step, TestCase, loop" in text
    assert "ITERATIONS = 20" in text
    # ``@loop`` 内的测试方法会被执行 N 次，末次截图只能落在 teardown 里。
    assert "driver.capture_screen" not in text.split("def teardown")[0]
    assert "driver.capture_screen" in text.split("def teardown")[1]


def test_soak_stress_uses_explicit_loop(tmp_path: Path) -> None:
    artifact = emit(soak_stress_spec(), tmp_path)
    text = artifact.python_text

    assert artifact.method == XDEVICE_MAIN_METHOD
    assert "@loop(" not in text
    assert "from devicetest.core.test_case import ASSERT, CHECK, MESSAGE, Step, TestCase\n" in text
    assert "for _iteration in range(1, ITERATIONS + 1):" in text
    assert "if DEADLINE_SECONDS > 0 and time.monotonic() - started >= DEADLINE_SECONDS:" in text
    assert '"soak deadline reached after {_iteration - 1} iterations"' in text
    assert "if _iteration % STEP_LOG_INTERVAL == 0:" in text
    assert 'Step(f"第 {_iteration} 轮")' in text
    assert "self._sample_pss(driver)" in text
    assert "def _sample_pss(self, driver) -> int:" in text
    assert "ITERATIONS = 200" in text
    assert "DEADLINE_SECONDS = 600.0" in text
    assert "time.sleep(1.0)" in text
    compile(text, str(artifact.case_file), "exec")


# ---------------------------------------------------------------------------
# _device_sn 优先级
# ---------------------------------------------------------------------------


def test_device_sn_priority_is_env_then_device1_then_default(tmp_path: Path) -> None:
    spec = core_flow_spec()
    artifact = emit(spec, tmp_path)
    text = artifact.python_text

    assert f'DEFAULT_DEVICE_SN = "{spec.device_sn}"' in text
    env_position = text.index('os.environ.get("HARMONY_AGENT_DEVICE_SN", "")')
    device_position = text.index('getattr(self.device1, "device_sn", "")')
    default_position = text.index("return device_sn or DEFAULT_DEVICE_SN")
    assert env_position < device_position < default_position
    assert "if env_sn:" in text
    assert "            return env_sn" in text


def test_device_sn_default_is_empty_when_spec_has_none(tmp_path: Path) -> None:
    spec = bug_repro_spec()
    assert spec.device_sn is None
    artifact = emit(spec, tmp_path)
    assert 'DEFAULT_DEVICE_SN = ""' in artifact.python_text


# ---------------------------------------------------------------------------
# import 白名单
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("factory", ALL_SPECS)
def test_imports_only_come_from_devicetest_and_hypium(tmp_path: Path, factory: SpecFactory) -> None:
    artifact = emit(factory(), tmp_path)
    tree = ast.parse(artifact.python_text)

    from_modules: set[str] = set()
    imported_names: dict[str, set[str]] = {}
    plain_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            from_modules.add(module)
            imported_names[module] = {alias.name for alias in node.names}
        elif isinstance(node, ast.Import):
            plain_modules.update(alias.name for alias in node.names)

    assert from_modules == {"__future__", "devicetest.core.test_case", "hypium", "pathlib"}
    assert not from_modules - STDLIB_IMPORT_MODULES - ALLOWED_IMPORT_MODULES
    assert plain_modules == {"json", "os", "re", "time"}
    assert not plain_modules - STDLIB_IMPORT_MODULES
    assert imported_names["hypium"] == {"BY", "MatchPattern", "UiDriver"}
    devicetest_names = imported_names["devicetest.core.test_case"]
    assert {"ASSERT", "CHECK", "MESSAGE", "Step", "TestCase"} <= devicetest_names
    assert devicetest_names <= {"ASSERT", "CHECK", "MESSAGE", "Step", "TestCase", "loop"}
    assert "__future__" in imported_names


def test_loop_is_imported_only_when_the_decorator_is_emitted(tmp_path: Path) -> None:
    counted = emit(counted_stress_spec(), tmp_path / "counted")
    soak = emit(soak_stress_spec(), tmp_path / "soak")
    plain = emit(core_flow_spec(), tmp_path / "plain")

    assert "TestCase, loop" in counted.python_text
    assert "TestCase, loop" not in soak.python_text
    assert "TestCase, loop" not in plain.python_text
    assert "loop(" not in soak.python_text.splitlines()[0]


# ---------------------------------------------------------------------------
# 其余分支
# ---------------------------------------------------------------------------


def test_sampled_counting_stress_still_needs_an_explicit_loop(tmp_path: Path) -> None:
    """采样节奏 ``@loop`` 表达不了，即使 kind 不是 SOAK 也必须走显式循环。"""
    spec = counted_stress_spec()
    spec.stress.sample_memory_every = 5
    spec.stress.memory_growth_threshold_kb = 2048
    artifact = emit(spec, tmp_path)

    assert artifact.method == XDEVICE_MAIN_METHOD
    assert artifact.testfile["tests"][0]["name"] == XDEVICE_MAIN_METHOD
    text = artifact.python_text
    assert "@loop(" not in text
    assert "for _iteration in range(1, ITERATIONS + 1):" in text
    assert "DEADLINE_SECONDS = 0.0" in text
    assert "self._sample_pss(driver)" in text
    assert "MEMORY_SAMPLE_EVERY = 5" in text
    compile(text, str(artifact.case_file), "exec")


def test_setup_flags_and_start_app_step_are_respected(tmp_path: Path) -> None:
    spec = core_flow_spec()
    spec.setup.stop_app_first = False
    spec.setup.start_app = False
    spec.setup.pre_steps = [TestStepSpec(step_id="p1", index=1, action=StepAction.START_APP, title_zh="启动被测应用")]
    artifact = emit(spec, tmp_path)
    setup_section = artifact.python_text.split("def test_main")[0]

    assert "self.driver.stop_app(BUNDLE_NAME)" not in setup_section
    assert "self.driver.start_app(BUNDLE_NAME, MAIN_ABILITY)" not in setup_section
    assert "# start_app: com.zhihu.hmos (handled by case setup)" in setup_section
    assert "self.driver.wait(STARTUP_WAIT_SECONDS)" in setup_section


def test_toast_checkpoint_forces_start_listen_toast(tmp_path: Path) -> None:
    """``needs_listen_toast`` 兜底：即使 ``SetupSpec.listen_toast`` 为假也要监听。"""
    spec = core_flow_spec()
    assert spec.setup.listen_toast is False
    spec.steps[0].checkpoints.append(
        CheckpointSpec(kind=CheckpointKind.TOAST, message_zh="应弹出保存提示", expected="已保存")
    )
    artifact = emit(spec, tmp_path)

    assert "self.driver.start_listen_toast()" in artifact.python_text
    assert "driver.check_toast('已保存', fuzzy='equal', timeout=3)" in artifact.python_text


def test_render_is_repeatable_over_the_same_root(tmp_path: Path) -> None:
    first = emit(core_flow_spec(), tmp_path, version=2)
    second = emit(core_flow_spec(), tmp_path, version=2)

    assert first.python_text == second.python_text
    assert "version: 2" in second.python_text
    assert second.testfile["description"].endswith("v2 (core_flow)")
    assert second.python_text.count("class HarmonyAgentCase") == 1


def test_titles_with_quotes_and_backslashes_still_compile(tmp_path: Path) -> None:
    """标题里的双引号 / 反斜杠不得破坏生成的模块与类文档字符串。"""
    spec = core_flow_spec()
    spec.title_zh = '坏"标题" 含反斜杠 \\ 与连续引号 """"'
    artifact = emit(spec, tmp_path)
    compile(artifact.python_text, str(artifact.case_file), "exec")
    assert '\\"' in artifact.python_text
