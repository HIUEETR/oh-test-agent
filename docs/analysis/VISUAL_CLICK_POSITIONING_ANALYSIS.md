# UI hierarchy 与 VLM 点击定位分析

## 结论

当前系统实现了“UI hierarchy 优先，VLM 补盲”的运行时定位：HDC `dumpLayout` 生成带 bbox 的标准元素，VLM 对截图返回视觉元素，重叠时增强 hierarchy 元素，不重叠时新增 `source="vlm"` 且 locator kind 为 `VLM_BBOX` 的元素。Agent 决策拿到当前元素及 bbox 后，可以按 `element_id` 选择元素，运行时点击该元素 bbox 的中心点。对应代码见 `src/harmony_test_agent/perception/normalizer.py:58-114`、`src/harmony_test_agent/perception/service.py:16-53`、`src/harmony_test_agent/agents/providers.py:330-345`、`src/harmony_test_agent/runtime/tools.py:56-63`。

这条链路存在一个确定的生成缺口：运行时 `CLICK_ELEMENT` 命中纯 VLM 元素时，`ActionResult.locator` 会保留 `VLM_BBOX`；Hypium 生成器只把 `SPATIAL` locator 转成坐标，对 `VLM_BBOX` 没有分支，随后 `_selector()` 也不识别 `VLM_BBOX`，最终退化为 `BY.text(target)`。此处 `target` 通常是运行时 `vlm-...` element ID，不是屏幕文本，因此生成脚本可能无法重放。生成元数据的 `coordinate_fallbacks` 也只统计 `CLICK_COORDINATE` 和 `SPATIAL`，会漏报 `VLM_BBOX`。证据见 `src/harmony_test_agent/generation/hypium.py:50-60`、`src/harmony_test_agent/generation/hypium.py:88-100`、`src/harmony_test_agent/generation/hypium.py:130-163`。

小按钮、图标按钮和软键盘 action key 是最高风险对象。它们可能缺少 hierarchy 节点，VLM bbox 面积小，中心点又可能落在透明 padding、相邻图标或系统手势区域。当前实现只校验 bbox 在屏幕内和最低置信度，没有最小尺寸、边缘安全区、触达面积、遮挡、相邻目标间距、坐标变换或点击后目标状态验证，不能把“bbox 合法”解释为“点击可靠”。

## 当前 UI hierarchy 链路

设备适配器执行 `uitest dumpLayout -a`，解析输出路径后读取 JSON（`src/harmony_test_agent/devices/harmony.py:105-118`）。每次证据截图随后保存 hierarchy，并用截图实际宽高标准化元素（`src/harmony_test_agent/devices/harmony.py:121-143`）。这保证 bbox 与拉回图像使用同一像素坐标系，但代码没有显式记录设备方向、display scale、系统栏 inset 或截图裁剪信息。

标准化器只保留可见且 enabled 的节点，过滤一组系统节点前缀，解析 `[left,top][right,bottom]`，拒绝超出截图边界的 bbox。元素包含 key、id、text、description、type、clickable、editable、scrollable、selected、source 和 locator candidates（`src/harmony_test_agent/perception/normalizer.py:36-45`、`src/harmony_test_agent/perception/normalizer.py:58-114`）。locator 优先顺序在候选数组中是 key、id、text、type+text（`src/harmony_test_agent/perception/normalizer.py:87-95`）。

`find_element()` 会把自然语言目标拆成语义变体，并把 Profile 中的稳定定位器值加入候选。过滤 clickable/editable 后按精确、前缀、包含和少量别名打分，最后偏好有 key 和 bbox 的元素（`src/harmony_test_agent/perception/normalizer.py:117-135`、`src/harmony_test_agent/perception/normalizer.py:138-206`）。如果没有 locator candidate，则构造 `SPATIAL` 候选。输入框另有“第一个带 bbox 的 editable 元素”回退（`src/harmony_test_agent/runtime/tools.py:107-132`）。

静态代码没有建立 parent/child、兄弟关系、z-order、可见裁剪区域或 ancestor clickable 传播。原始 hierarchy 字符串只进入 metadata（`src/harmony_test_agent/perception/normalizer.py:109-111`），后续定位没有使用。任务书要求的“可交互属性 + 空间关系”优先级（`docs/PROJECT_REQUIREMENTS.md:725-733`）因此只部分实现：有属性过滤和 bbox，缺少“某文本右侧按钮”“列表第一项中的更多”等结构化空间约束。

## 当前 VLM 融合与点击链路

视觉 prompt 要求返回页面标题、摘要和 hierarchy 缺失的可操作元素，尤其是软键盘 action key 与纯图标控件，并要求给出基于原图像素的 bbox 和置信度（`src/harmony_test_agent/agents/providers.py:29-33`）。请求发送实际 PNG 二进制和最多 120 个 hierarchy 元素（`src/harmony_test_agent/agents/providers.py:285-309`）。决策 prompt 规定优先使用当前 `element_id`；截图中明确但元素列表缺失时才使用 `click_coordinate`（`src/harmony_test_agent/agents/providers.py:36-44`）。

融合时，低于 `VLM_MIN_CONFIDENCE` 或越界的视觉元素被丢弃。视觉 bbox 中心落入某个 hierarchy bbox 时，选择面积最小的 hierarchy 元素，仅更新 score、空 content 和 `vision_reason`；不加入 `VLM_BBOX` locator。没有重叠时创建纯 VLM 元素，元素 ID 由 content、type 和 bbox 计算，locator 为 bbox 中心字符串（`src/harmony_test_agent/perception/service.py:16-65`）。

这套重叠规则是“中心点包含”，没有 IoU、类别一致性或文本一致性。例如小图标中心位于一个覆盖整行的大 clickable 容器中时会融合到容器；多个嵌套节点同时包含中心时只选面积最小节点，未检查该节点是否 clickable。反之，VLM 框与 hierarchy 框高度重叠但中心刚好越界时会产生重复元素。

运行时 `CLICK_ELEMENT` 解析到元素后要求 bbox，并点击中心点；`CLICK_COORDINATE` 直接点击模型坐标（`src/harmony_test_agent/runtime/tools.py:56-63`）。安全策略能拒绝屏幕外坐标，任务书也要求坐标降级告警（`docs/PROJECT_REQUIREMENTS.md:764-771`），但当前 `CLICK_ELEMENT` 对 VLM bbox 不追加运行时 warning；只有生成阶段的部分坐标降级会产生 warning。

## 小按钮和图标控件风险

小目标首先受到模型量化误差影响。对于 1320×2232 截图，一个 24×24 像素目标若 bbox 每边偏差 6 像素，中心仍可能位于目标内，但有效点击区域只剩很小；若控件视觉图标小、实际 hit target 大，VLM 可能框图标而非 hit target；若相邻按钮间距小，中心偏差会点击邻项。当前 `BoundingBox.within()` 只检查完整位于屏幕且宽高为正（`src/harmony_test_agent/models.py:119-141`），没有最小面积或目标间距门禁。

其次是缩放和方向。前端显示时通过 CSS 缩放图片（`web/src/styles.css:92-93`）；如果未来允许用户在 Web 图上点选，必须把 DOM 坐标按图像实际 rendered rect、object-fit 留白和设备原始宽高反算。目前 Web 类型未包含 bbox（`web/src/types.ts:20-36`），也没有这个转换。运行时模型坐标来自原 PNG，不受 Web 缩放影响，但设备旋转、截图尺寸变化或系统栏变化会让历史坐标失效。

第三是遮挡和时序。Orchestrator 默认动作后等待 0.8 秒再截图（`src/harmony_test_agent/agents/orchestrator.py:48-56`、`src/harmony_test_agent/agents/orchestrator.py:190-195`），没有检测目标在点击瞬间是否被 toast、弹窗或键盘覆盖。小按钮点击后若页面哈希不变，只会累计 unchanged count；这能停止连续无变化动作，却不能证明点中了预期控件（`src/harmony_test_agent/agents/orchestrator.py:211-221`）。

建议对纯视觉点击设置更严格门禁：bbox 短边至少 24 px 且面积至少占屏幕 `0.0001`；中心距离屏幕左右/底部系统手势区小于安全阈值时拒绝自动点击；与第二候选中心距离小于 1.5 倍目标短边时要求 hierarchy 或二次确认；置信度阈值对点击高于只读识别阈值；点击后必须满足页面签名变化或该 step 的显式断言，否则标记 `failed_action`。具体阈值需要真实设备标定，不能把这些建议值视作已验证参数。

## `VLM_BBOX` 到 Hypium 的确定缺口

运行时流程如下：

```text
VisionElement.bbox
  → UIElement(source="vlm", locator=VLM_BBOX, element_id="vlm-...")
  → model returns click_element(target="vlm-...")
  → ToolExecutor resolves element and clicks bbox.center
  → ActionResult(locator=VLM_BBOX, before_snapshot_id=...)
```

前四步由 `src/harmony_test_agent/perception/service.py:25-51` 和 `src/harmony_test_agent/runtime/tools.py:56-63` 支持。生成流程当前却是：

```text
CLICK_ELEMENT + locator VLM_BBOX
  → not SPATIAL
  → _selector(locator, target)
  → VLM_BBOX is unhandled
  → BY.text("vlm-<hash>")
```

`_runtime_element_coordinate()` 已能从 before Snapshot 按 target element ID 找到 bbox 中心（`src/harmony_test_agent/generation/hypium.py:130-144`），所以修复不需要新的数据模型。生成器应把 `{SPATIAL, VLM_BBOX, COORDINATE}` 统一视为坐标降级；其中 `VLM_BBOX` 的 warning 明确记录 content、bbox、中心、截图尺寸、confidence 和 source。生成 metadata 同时记录 `locator_kind="vlm_bbox"`、`stability="degraded"` 和 source snapshot ID。任务书要求 bbox 坐标在代码、Web 和报告告警（`docs/PROJECT_REQUIREMENTS.md:764-771`），当前元数据只有计数和 warning，尚未满足结构化稳定性字段。

建议生成代码形态：

```python
# degraded locator: VLM_BBOX, source snapshot step_03_before, screen 1320x2232
# visual target: "搜索" bbox=[1180, 92, 1244, 156], confidence=0.91
driver.touch((1212, 124))
```

这仍然只是固定分辨率回放。更稳妥的生成方式是优先尝试同一 Snapshot 中与 VLM 框高 IoU 的稳定 hierarchy locator；找不到时才生成坐标。禁止把 `vlm-...` 当作 `BY.text()`，因为运行时 element ID 不存在于 Hypium 页面树。

## 实施计划

第一步修复生成器。将坐标降级判定集中成一个函数，同时用于脚本渲染、warning 和 metadata 计数；覆盖 `VLM_BBOX`。生成前校验目标 Snapshot、元素、bbox 与当前分辨率均存在，缺一项则拒绝生成该动作并产生不可回放错误，不能静默输出文本选择器。

第二步增强融合。用 IoU、中心包含、类型和可点击性组合评分替代单一中心包含；记录 `vision_bbox`、`hierarchy_bbox`、IoU 和融合原因。纯 VLM 元素同时记录 image SHA-256 和 screenshot dimensions，确保生成器能审计坐标来源。

第三步增加小目标门禁。为 `VisionElement` 或融合结果计算短边、面积占比、边缘距离和最近邻距离。对风险高的目标让模型重新分析裁剪图，或返回 `failed_element` 请求人工确认；不要靠重复点击试错。

第四步增加动作后验证。PlannedStep 已有 expected 字段时要求断言；视觉点击至少比较 after Snapshot，并区分“页面变化”“目标状态出现”“无变化”。对无变化动作不自动重试视觉坐标，避免同一点重复触发危险操作。

第五步补齐 Web 标注。TypeScript Snapshot 元素加入 bbox、score、locator kind 和 warnings；在原图尺寸坐标系绘制 SVG overlay，支持筛选 hierarchy、VLM、融合元素和点击点。画布反算只用于展示或人工选择，真正执行前仍由后端按当前 Snapshot 校验。

## 验证矩阵

| 场景 | 静态/自动验证 | 必须真实复验的内容 | 通过条件 |
| --- | --- | --- | --- |
| hierarchy 稳定 key 按钮 | normalizer、locator 优先级、Hypium selector 单测 | 当前应用 key 是否稳定 | 运行与回放均使用 `BY.key/id` |
| hierarchy 无 key 但有文本 | exact text/type+text 单测 | 多个同名元素的歧义 | 唯一命中或明确失败 |
| 纯 VLM 大按钮 | `VLM_BBOX` 生成坐标与 warning 单测 | 真实点击与动作后状态 | 不生成 `BY.text("vlm-...")`，动作有验证 |
| 纯 VLM 小图标 | 尺寸、边缘、近邻门禁单测 | 不同分辨率与动态布局 | 风险门禁触发或连续多轮命中正确目标 |
| 软键盘 action key | VLM prompt 与 bbox 边界单测 | 键盘版本、语言、显示状态 | 点击后输入/页面状态符合预期 |
| Web overlay | 坐标缩放组件测试 | 375/1440 视口和 DPR | 框与原图目标对齐，误差不超过 2 CSS px |
| Hypium 三次回放 | 生成 artifact 检查 | 模拟器重置后三次真实运行 | 独立报告，三次断言通过，无状态污染 |

仓库文档历史声明 UI hierarchy、VLM bbox 和真实 Hypium 三次回放已经通过（`docs/REFACTOR_CHECKLIST.md:27-44`、`docs/REFACTOR_CHECKLIST.md:59-70`）。本次没有调用 HDC、真实模型或 Hypium，因此只能确认静态链路和上述生成缺口，不能确认小按钮实际命中率，也不能复验历史三次回放是否覆盖过纯 `VLM_BBOX` 元素。
