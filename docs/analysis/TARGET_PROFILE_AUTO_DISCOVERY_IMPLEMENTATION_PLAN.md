# 无预置 TargetAppProfile 的应用发现、探索与自动生成实施计划

更新时间：2026-09-10  
适用仓库：`D:\Work\Code\worktrees\0476\mvp`  
计划状态：实现已完成，综合验证因模拟器离线暂缓  
目标：用户只提供应用名称或 `bundleName`，系统完成应用解析、启动、有界探索、Profile 生成与验证，并在同一次运行中继续原始测试任务。

## 1. 目标与交付边界

本计划将当前“必须预先存在 `profiles/<app-id>.json` 才能运行”的流程改造成：

```text
应用名称或 bundleName
  → 解析已安装应用
  → 复用并快速复验已有 verified Profile，或创建临时目标
  → 启动应用
  → 有界探索与页面图构建
  → 生成 draft Profile
  → 三轮独立重启验证
  → 生成 Hypium Driver 用例
  → 三次独立回放
  → 自动晋级 verified Profile
  → 执行用户原始测试任务
```

“无配置文件测试”的准确含义是：首次运行不要求磁盘中预先存在 Profile JSON。运行期仍会创建一个类型化的 `ResolvedTarget` 和临时 `DraftProfile`，用于保存已解析的应用身份、安全策略、启动方式和探索证据。只有完成门禁后，系统才原子写入正式 Profile。

第一阶段继续使用当前已验证的 Hypium Driver 模式。官方测试工程模式仍标记为 `not_validated`，不把直接执行 Driver Python 脚本描述成 DevEco Testing 测试工程执行。

## 2. 已确认的产品决策

以下决策来自本次需求访谈，实施时不再重新讨论：

1. 用户入口只要求应用名称或 `bundleName`；系统负责查找、启动、探索和生成 Profile。
2. 输入 `bundleName` 时精确匹配；输入应用名称时从已安装应用中匹配。唯一匹配直接使用，多匹配返回候选让用户选择，无匹配明确失败，不自动下载或安装应用。
3. 自动探索默认开启，也允许用户关闭或调整策略。
4. 普通导航、滑动、返回和固定测试文本输入默认允许；登录、授权、提交、发布和下载必须由用户逐项显式放行；支付、删除、卸载和清除数据始终禁止。
5. 探索边界为最多 20 个页面、每页最多 8 个候选动作、总时长 15 分钟。发现至少 3 个可重复页面、3 类交互、3 个稳定定位器和 2 个断言点后可以提前结束。
6. 重置先使用 `force-stop + aa start`；入口状态不一致时，只允许使用已验证的返回、关闭弹窗或首页 Tab 恢复。仍不一致则保留 draft，不晋级；禁止通过重装、清除数据或修改系统设置实现重置。
7. 已有 verified Profile 且应用版本、Ability 和稳定定位器快速复验通过时直接测试；否则自动重新探索。
8. Profile 记录 `versionName/versionCode`、签名摘要、发现时间和验证时间。版本变化触发快速复验，失败才执行完整探索。
9. 只提供应用名称时执行通用冒烟任务：“启动应用，探索可达页面，验证返回和重启恢复”。同时提供测试任务时，Profile 验证后在同一次运行中继续该任务。
10. 坐标可用于探索，但不成为高可信稳定定位器。只有三轮验证中页面签名和位置都稳定时，才允许以低可信、带分辨率约束和持续告警的形式进入候选数据。
11. 凭据只允许通过环境变量或 Secret 引用注入当前 Run，不写入 Profile、Trace、日志和说明性元数据。账号、手机号和密码不得由模型猜测。
12. 检测到系统权限页、浏览器、应用市场或其他 Bundle 时终止该路径并返回目标应用；只有用户显式允许对应权限时才处理系统授权弹窗。
13. 新 Profile 使用临时文件和 Schema 校验写入；验证失败时保留 draft 和失败证据。正式版本更新保留时间戳备份并支持自动回退；`locked=true` 的人工 Profile 禁止自动覆盖。
14. CLI、FastAPI 和 Web 共用同一套 `resolve → discover → verify → generate → replay → promote → test` 状态机和事件模型。
15. 探索失败的 draft 默认只能查看或人工修订。用户可显式发起“受限临时测试”，但高风险动作仍禁用，也不生成正式 Hypium 回归用例。
16. 稳定定位器至少在 3 次独立重启后的同一语义页面重复出现，优先级为 `key → id → text+type`。动态数字 ID、易变文本和纯坐标不能成为高可信定位器。
17. 任一验证轮次发生越界跳转、定位歧义、断言失败或重置失败时，不晋级。
18. Hypium 三次回放全部通过且至少包含一个应用级 UI 断言后，Profile 才标记为 `verified` 并成为当前正式版本。
19. 保留 `TARGET_PROFILE_PATH` 作为显式覆盖和旧入口兼容项；新入口不再因该文件缺失而直接失败。

## 3. 当前实现与差距

### 3.1 Profile 当前是硬依赖

当前执行链在设备连接前就读取固定文件：

- `src/harmony_test_agent/config.py` 将 `target_profile_path` 默认设置为 `profiles/zhihu-plus.json`。
- `src/harmony_test_agent/agents/orchestrator.py:69-71` 无条件加载该文件，并要求请求的 `target_app_id` 与文件一致。
- `src/harmony_test_agent/agents/providers.py` 的 `plan()` 接口接收完整 `TargetAppProfile`。
- `src/harmony_test_agent/runtime/tools.py` 从 Profile 取得稳定定位器，并用它启动应用。
- `src/harmony_test_agent/generation/hypium.py:20-38` 从当前 Profile 读取 bundle、Ability 和 reset strategy 生成脚本。
- `src/harmony_test_agent/api/app.py:211-216` 对历史 Run 重新生成时再次读取当前全局 Profile，因此 Profile 后续变化可能改变旧 Run 的生成结果。
- CLI 与 Web 默认值仍固定为 `zhihu-plus`；Web 的默认任务和展示文本也直接写了“知乎++”。

因此，不能只把 Profile 参数改成可空。需要把“应用解析结果”冻结到 Run，再让规划、执行、生成和回放消费同一份不可变快照。

### 3.2 已有能力可以复用

当前仓库已经具备自动探索的主要底座：

- `HarmonyDeviceAdapter` 可以连接设备、执行 HDC 命令、截图并调用 `uitest dumpLayout -a`。
- Layout 中通常可观察 `bundleName`、`abilityName`、`pagePath`、控件 `key/id/text/type/bounds`。
- `normalize_layout()` 已把 UI 层级转换为统一元素，并生成 key、id、text、type+text 定位候选。
- `PageGraphBuilder` 已根据页面路径、稳定 key、文本和图像摘要建立页面状态图。
- `ToolExecutor` 已支持点击、输入、滑动、返回、等待和断言。
- `SafetyPolicy` 已有敏感词和坐标边界检查，可扩展为分级策略。
- `HypiumGenerator` 和 `HypiumRunner` 已能从成功动作生成 Driver 脚本并保存独立回放证据。

### 3.3 必须同步修复的生成问题

自动晋级依赖可信回放，因此实施过程中必须同时修复：

1. 当前 `ASSERT_VISIBLE` 和 `ASSERT_TEXT` 都生成 `check_component_exist()`。文本断言应生成精确 `BY.text(...)`，或使用 `check_component(..., text=...)` 验证属性。
2. 当前没有 UI 断言时会退化为 `assert driver.device_sn`。该结果只能证明设备已连接，不得满足 Profile 晋级门槛。
3. 当前定位失败会从语义目标构造 `BY.text(target)`。这只能作为待验证候选，未经过运行观察和回放前不能标记为稳定定位器。
4. 动态 key/id 的前缀泛化属于项目启发式规则，需要在三轮回放中验证唯一性，不能仅因符合正则就视为稳定。

## 4. 官方资料约束

计划依据以下官方资料：

- [华为官方 Hypium Python 指南](https://developer.huawei.com/consumer/cn/doc/harmonyos-guides/hypium-python-guidelines)
- [Hypium 指南 Markdown 正文](https://developer.huawei.com/consumer/cn/doc/harmonyos-guides/hypium-python-guidelines.md)
- [华为官方 HDC 指南](https://developer.huawei.com/consumer/cn/doc/harmonyos-guides/hdc)
- [华为官方 bm 工具指南](https://developer.huawei.com/consumer/cn/doc/harmonyos-guides/bm-tool)
- [华为官方 aa 工具指南](https://developer.huawei.com/consumer/cn/doc/harmonyos-guides/aa-tool)

官方指南确认：

- `bm dump -a` 可查询设备已安装应用，`bm dump -n <bundleName>` 可查询指定应用信息，`-l` 支持按 label 查询。
- `aa start -b <bundleName> -a <abilityName>` 用于启动 Ability；`aa force-stop <bundleName>` 可用于当前项目的停止后重启流程。
- Hypium 支持 `BY.key/id/text/type`、匹配模式、坐标触摸、输入、滑动、启动、停止、返回、等待、截图和控件断言。
- 坐标定位是框架能力，但官方没有承诺绝对坐标可以跨版本、跨分辨率稳定复用。
- Driver 模式自行 `UiDriver.connect()` 和 `driver.close()`；测试工程模式由框架管理设备，两种模式不能混合。
- 官方当前推荐 Python 3.10；本项目锁定 Python 3.14 和 `hypium==6.1.0.210`，需要继续以本项目真机验证结果作为兼容证据，不能称为官方保证。

## 5. 目标架构

新增独立 `targets` 和 `discovery` 领域，将磁盘 Profile 从必需运行输入改为可复用、可晋级的测试资产。

```text
CLI / FastAPI / Web
  │ TargetRequest(app_name | bundle_name, task, policy)
  ▼
TargetResolver
  ├─ ProfileRegistry：查找 verified Profile
  ├─ InstalledAppCatalog：bm dump 解析与候选消歧
  └─ TargetProbe：启动并用 dumpLayout 交叉校验
  │ ResolvedTarget（冻结到 RunTrace）
  ▼
ProfileCoordinator
  ├─ 快速复验已有 Profile
  └─ 无可用 Profile → DiscoveryOrchestrator
       ├─ BoundedExplorer
       ├─ PageGraphBuilder
       ├─ ExplorationSafetyPolicy
       ├─ LocatorStabilityAnalyzer
       └─ AssertionCandidateAnalyzer
  │ DraftProfile + Evidence
  ▼
ProfileVerifier
  ├─ 3 次独立 stop/start/reset
  ├─ 页面、交互、定位器、断言门禁
  ├─ HypiumGenerator（Driver）
  └─ HypiumRunner × 3
  │ Candidate → Verified
  ▼
ProfileRegistry 原子晋级
  ▼
AgentOrchestrator 执行原始任务
```

### 5.1 关键设计原则

- Run 创建时冻结 `ResolvedTarget` 和安全策略，历史 Run 不再依赖当前全局 Profile。
- LLM 负责页面语义、候选动作排序和断言建议；应用身份解析、动作权限、探索上限、稳定性统计、晋级门禁和文件写入由确定性代码负责。
- 探索产生证据，不能直接声明稳定。稳定性由跨重启、跨回放的统计结果决定。
- Profile 文件是可重建资产；原始截图、布局、动作、页面图、验证结果和生成脚本是审计依据。

## 6. 领域模型与 Profile Schema

### 6.1 请求和解析模型

新增以下模型，名称可以在实现时调整，但职责必须保持：

```python
class TargetQuery(BaseModel):
    app_name: str | None = None
    bundle_name: str | None = None

class ExplorationPolicy(BaseModel):
    enabled: bool = True
    max_pages: int = 20
    max_actions_per_page: int = 8
    max_duration_seconds: int = 900
    fixed_input_text: str = "OpenHarmony"
    allow_login: bool = False
    allow_permission: bool = False
    allow_submit: bool = False
    allow_publish: bool = False
    allow_download: bool = False

class ResolvedTarget(BaseModel):
    target_app_id: str
    display_name: str
    bundle_name: str
    main_ability: str
    module_name: str | None
    version_name: str | None
    version_code: int | None
    signature_sha256: str | None
    device_id: str
    source: Literal["verified_profile", "installed_app", "explicit_override"]
    profile_snapshot: TargetAppProfile | None
```

请求必须要求 `app_name` 和 `bundle_name` 至少一个存在。两者同时存在时，以 `bundle_name` 精确匹配为主，并校验名称是否一致。

### 6.2 Profile 生命周期

```text
draft      探索中或探索失败，可查看和人工修订
candidate  自动探索及三轮设备验证通过，已持久化但尚未完成三次 Hypium 回放
verified   三次独立 Hypium 回放通过，成为当前正式 Profile
superseded 被新版本替换，保留历史记录
invalid    快速复验失败且无法自动恢复
```

验证通过时自动保存，不额外要求人工确认。为满足“全部验证后自动正式保存”和“Hypium 三次通过后才 verified”两个约束，系统先自动写入 `candidate` 文件；三次 Hypium 回放通过后，再原子更新为 `verified` 并切换当前版本。

### 6.3 Profile v2 建议字段

```json
{
  "schema_version": 2,
  "status": "verified",
  "locked": false,
  "target_app_id": "example-app",
  "display_name": "示例应用",
  "bundle_name": "com.example.app",
  "main_ability": "EntryAbility",
  "module_name": "entry",
  "app_version": {
    "version_name": "1.2.3",
    "version_code": 123,
    "signature_sha256": "..."
  },
  "device_compatibility": {
    "validated_device_types": ["phone"],
    "validated_resolutions": [[1320, 2232]]
  },
  "launch_strategy": {
    "kind": "hdc_aa_start",
    "command_template": "aa start -b {bundle_name} -a {main_ability}"
  },
  "reset_strategy": {
    "kind": "stop_start_then_navigation_restore",
    "clear_app_data": false,
    "recovery_actions": []
  },
  "test_data_strategy": {
    "fixed_input_text": "OpenHarmony",
    "secrets": []
  },
  "permission_and_popup_strategy": {
    "login": "explicit_only",
    "permission": "explicit_only",
    "submit": "explicit_only",
    "payment": "always_blocked",
    "delete": "always_blocked",
    "uninstall": "always_blocked",
    "clear_data": "always_blocked"
  },
  "stable_locator_inventory": [],
  "assertion_inventory": [],
  "core_flows": [],
  "known_limitations": [],
  "provenance": {
    "discovery_run_id": "...",
    "discovered_at": "...",
    "verified_at": "...",
    "hypium_replay_run_ids": [],
    "generator_version": "profile-discovery-v1"
  }
}
```

每个稳定定位器应额外保存：语义名称、页面签名、`key/id/text/type`、置信级别、三轮出现次数、唯一匹配次数、观测分辨率、来源、首次/末次观测、动态模式以及产生证据的 Snapshot ID。

### 6.4 RunTrace 冻结目标

`RunTrace` 新增：

- `target_query`
- `resolved_target`
- `profile_status_at_start`
- `profile_snapshot`
- `exploration_policy`
- `discovery_result`
- `verification_result`

`HypiumGenerator.generate()` 改为只接收 `RunTrace`，bundle、Ability、reset strategy 和定位器来自 Trace 内冻结的快照。这样删除、升级或替换外部 Profile 后，历史 Run 仍能确定性生成相同脚本。

## 7. 全过程状态机

扩展 `RunState`：

```text
created
resolving_target
waiting_target_selection
probing_target
profile_revalidating
discovering
profile_drafting
profile_verifying
script_generating
script_executing
profile_promoting
planning
executing
verifying
completed
```

新增失败状态：

```text
failed_target_resolution
failed_target_probe
failed_discovery
failed_profile_verification
failed_profile_promotion
```

对应事件至少包括：

- `target_candidates_found`
- `target_resolved`
- `target_started`
- `profile_found`
- `profile_revalidation_started/finished`
- `discovery_started/progress/finished`
- `discovery_path_blocked`
- `locator_candidate_observed`
- `profile_draft_saved`
- `profile_verification_round_finished`
- `hypium_replay_finished`
- `profile_promoted`
- `original_task_started`

当名称匹配多个应用时，API Run 进入 `waiting_target_selection`，返回候选及 continuation token。CLI 在交互终端显示候选并接收序号；非交互 CLI 返回结构化候选和非零退出码，由调用者使用 `--bundle-name` 重试。Web 展示候选选择框后调用继续接口。

## 8. 应用解析与启动

### 8.1 InstalledAppCatalog

在 `HarmonyDeviceAdapter` 上增加明确的只读能力：

```python
def list_installed_apps() -> list[InstalledApp]
def inspect_app(bundle_name: str) -> InstalledApp
def current_foreground_app() -> ForegroundApp | None
def start_app(bundle_name: str, ability_name: str, module_name: str | None) -> CommandResult
def stop_app(bundle_name: str) -> CommandResult
```

命令优先使用官方 `bm dump` 和 `aa` 能力：

- `hdc -t <device> shell bm dump -a`
- `hdc -t <device> shell bm dump -n <bundleName>`
- 必要时使用 `bm dump -l <label>` 辅助名称查找，但仍统一解析成本地 `InstalledApp` 列表后做唯一性判定。
- `hdc -t <device> shell aa start -b <bundleName> -a <abilityName>`
- `hdc -t <device> shell aa force-stop <bundleName>`

HDC/bm 输出需要保存原始文本、工具版本和解析器版本。不同系统版本字段可能变化，因此解析器必须采用 fixture 驱动并在无法确定时失败，不能猜测 Ability。

### 8.2 匹配规则

1. `bundleName`：完全相等，且必须存在于当前设备。
2. 应用名称：先规范化空格和大小写，再做 label 完全匹配；若无结果，可做前缀/包含候选，但永远不自动选择非唯一结果。
3. 过滤系统桌面、系统设置、场景管理、输入法和测试框架进程，除非用户显式提供其 bundle。
4. 启动后立即采集 `dumpLayout`，用其中的 `bundleName/abilityName` 与解析结果交叉校验。
5. 前台 Bundle 不一致时记录越界证据，停止当前路径并尝试一次返回目标 App；仍不一致则目标探测失败。

## 9. 有界自动探索

### 9.1 探索算法

采用带回溯的有界优先搜索：

```text
初始页入队
while 队列非空且未达页面/动作/时间上限：
  取优先级最高的未完成页面
  生成安全候选动作
  对每个候选（最多 8 个）：
    保存 before snapshot/layout/foreground bundle
    执行动作
    等待有界稳定窗口
    保存 after snapshot/layout/foreground bundle/hilog 摘要
    运行安全与越界检查
    更新页面图、定位器观测和断言候选
    回退到当前页或从入口重建路径
满足准入指标后提前结束
```

候选优先顺序：

1. 可点击且有稳定 `key/id` 的导航元素、Tab、菜单和列表项。
2. 有明确文本和类型的普通按钮。
3. 可编辑控件，输入固定无敏感文本，但不自动点击提交。
4. 可滚动区域的单次有界滑动。
5. VLM 识别且 hierarchy 缺失的候选。
6. 纯坐标候选只作为最后降级，并且不自动晋级为高可信定位器。

同一页面、同一元素、同一动作只探索一次；回到页面后用签名和动作历史去重。页面签名需继续结合 `pagePath`、稳定 key 集、归一化文本、截图感知哈希，并增加目标 Bundle 和窗口类型，避免把系统弹窗误判成应用页面。

### 9.2 动作分级

策略必须在执行器前生效，不能只靠 LLM Prompt：

- 默认允许：启动、普通导航点击、Tab、列表项、滑动、返回、等待、固定文本输入、只读断言。
- 显式放行：登录、系统授权、提交、发布、下载。
- 始终禁止：支付、购买确认、删除、卸载、清除数据、修改系统关键设置。

判定信号包括任务文本、元素文本/description/key/id、页面摘要、目标 Bundle、弹窗类型和动作组合。命中不确定敏感语义时停止该路径并记录 `blocked_uncertain`，不让模型自行解释为安全。

### 9.3 跨应用与弹窗

每个动作后读取前台 Bundle：

- 保持目标 Bundle：继续。
- 系统权限组件：只有 `allow_permission=true` 才进入专门处理器，否则记录并返回。
- 浏览器、应用市场或其他第三方 Bundle：记录越界，返回目标 App，该路径失败。
- 桌面：允许作为应用崩溃或退出证据，但不继续探索桌面。

### 9.4 探索产物

每次探索保存：

```text
artifacts/runs/<run-id>/discovery/
  installed-apps.raw.txt
  resolved-target.json
  policy.json
  pages.json
  transitions.json
  locator-observations.json
  assertion-candidates.json
  blocked-actions.json
  draft-profile.json
  summary.json
```

现有 `screens/`、`layouts/`、`logs/` 和 `trace.json` 继续复用。

## 10. Profile 生成和验证

### 10.1 稳定定位器筛选

定位器必须满足：

- 在同一语义页面的 3 次独立重启验证中均出现。
- 每轮匹配唯一，或具备可解释、可验证的组合选择器。
- 目标控件的可交互属性与用途一致。
- key/id 不包含明显会话、时间戳或长动态数字；若使用前缀模式，三轮均唯一匹配。
- 文本定位器不得依赖新闻标题、时间、计数、用户内容等动态数据。

分级：

- `high`：稳定唯一 key。
- `high`：稳定唯一 id。
- `medium`：稳定 `type + text` 或稳定文本。
- `low`：经过三轮验证的空间/坐标定位，带设备形态、分辨率、页面签名约束和告警。

正式 Profile 至少需要 3 个 `high/medium` 定位器；low 不计入晋级数量。

### 10.2 断言候选

至少筛选 2 个应用级 UI 断言点：

- 稳定控件存在或不存在。
- 稳定文本或控件属性符合预期。
- 页面返回后前一状态保持。

连接成功、设备序列号存在、截图成功不能计为应用级断言。动态内容“存在任意内容”只能作为弱断言，不能独自满足两项门槛。

### 10.3 三轮设备验证

每轮必须：

1. `force-stop` 目标应用。
2. 使用已解析 Ability 启动。
3. 校验入口 Bundle、Ability、页面签名。
4. 回放最小核心流程。
5. 校验至少 3 个稳定定位器和 2 个断言。
6. 执行返回和重启恢复。
7. 保存截图、Layout、命令、日志和结果。

任一轮失败，Profile 保持 draft，并记录失败阶段、实际页面签名、定位歧义、越界 Bundle 或断言差异。

### 10.4 Hypium 三次回放与晋级

设备验证通过后自动保存 `candidate`，从验证 Run Trace 生成 Hypium Driver 脚本。生成器必须：

- 从 Trace 内冻结的目标快照获取 bundle 和 Ability。
- 优先生成 `BY.key/id/type+text/text`。
- 正确区分存在断言和文本/属性断言。
- 无应用级 UI 断言时拒绝生成可晋级用例。
- 坐标降级持续写入 Python 注释、配置、元数据、报告和 Web 告警。

三次独立回放均要求进程返回码为 0、自定义结果 `passed=true`、至少一个应用级 UI 断言通过，并且未发生越界 Bundle。全部通过后：

1. 写入临时 Profile 文件。
2. 执行 Pydantic Schema 校验和跨字段校验。
3. 若现有 Profile `locked=true`，保存为候选并报告冲突，不覆盖。
4. 备份旧版本为 `profiles/history/<bundle>/<timestamp>.json`。
5. 原子替换 `profiles/<app-id>.json`。
6. 将状态更新为 `verified`，记录回放证据和校验摘要。

## 11. 原始任务续跑

Profile 晋级后，同一个顶层 Run 继续用户原始任务。建议顶层 Run 保存两个明确阶段：`bootstrap` 和 `task`，并分别统计模型调用、动作数、耗时和失败状态。

若用户只提供应用名称，则原始任务为通用冒烟任务。若用户提供业务测试任务，则重新规划该任务，但沿用已冻结的 verified Profile。探索动作不能直接被当成用户任务已完成；用户任务必须有自己的计划、动作、断言和结果。

探索失败时默认不续跑。用户显式选择“受限临时测试”后，可以使用 draft 的 `ResolvedTarget` 和空/候选定位器目录执行，但：

- 始终禁止高风险动作。
- 结果标记为 `provisional`。
- 不生成正式 Hypium 用例。
- 不写入或覆盖 verified Profile。

## 12. CLI、API 与 Web 改造

### 12.1 CLI

建议入口：

```powershell
uv run main.py run --app "应用名称" --task "测试任务"
uv run main.py run --bundle-name com.example.app --task "测试任务"
uv run main.py discover --app "应用名称"
uv run main.py profiles list
uv run main.py profiles show --bundle-name com.example.app
uv run main.py profiles verify --bundle-name com.example.app
```

主要参数：

- `--app` / `--bundle-name`：二选一或提供一致的两者。
- `--no-discovery`：没有 verified Profile 时直接失败。
- `--allow-login/permission/submit/publish/download`：逐项显式放行。
- `--max-pages`、`--max-actions-per-page`、`--discovery-timeout`。
- `--temporary-test`：探索失败后显式请求受限临时测试。

保留 `--target-app` 和 `TARGET_PROFILE_PATH` 一个兼容周期，并输出弃用提示。

### 12.2 API

新增或调整：

```text
GET  /api/targets?device_id=...
POST /api/targets/resolve
POST /api/runs
POST /api/runs/{run_id}/target-selection
GET  /api/profiles
GET  /api/profiles/{profile_id}
POST /api/profiles/{profile_id}/verify
POST /api/profiles/{profile_id}/lock
GET  /api/runs/{run_id}/discovery
```

`POST /api/runs` 示例：

```json
{
  "target": {"app_name": "示例应用"},
  "task": "打开设置页并验证版本信息",
  "mode": "regression",
  "discovery": {
    "enabled": true,
    "allow_login": false,
    "allow_permission": false
  }
}
```

历史字段 `target_app_id` 继续映射到 legacy Profile 查找。`/generate` 改为只读 Trace，不再读取 `settings.resolved_target_profile_path`。

### 12.3 Web

Web 控制台增加：

- 应用名称或 bundle 输入框。
- 已安装应用候选和消歧选择。
- 已有 Profile、版本、状态、锁定状态和快速复验结果。
- 探索策略开关及逐项权限设置。
- 探索进度：页面数、动作数、剩余时间、阻塞路径。
- 页面图、定位器候选、断言候选、draft/candidate/verified 差异。
- 三轮设备验证和三次 Hypium 回放结果。

移除固定“知乎++”和固定知乎任务。选择已有知乎 Profile 时可以由 Profile 提供示例任务，但不能作为全局默认值。

## 13. 文件与模块改造清单

建议新增：

```text
src/harmony_test_agent/targets/
  models.py
  catalog.py
  resolver.py
  profiles.py
  errors.py

src/harmony_test_agent/discovery/
  orchestrator.py
  explorer.py
  candidates.py
  locators.py
  assertions.py
  verifier.py
  policy.py

src/harmony_test_agent/profiles/
  coordinator.py
  promoter.py
```

修改重点：

- `models.py`：目标请求、解析结果、Profile 生命周期、探索策略、状态和事件。
- `config.py`：Profile 目录和旧单文件覆盖可选化。
- `devices/base.py`、`devices/harmony.py`：安装应用枚举、应用详情、前台应用和显式启动接口。
- `agents/orchestrator.py`：加入目标解析、Profile 协调和任务续跑阶段。
- `agents/providers.py`：规划接口改为接收最小 `PlanningContext`，不依赖完整磁盘 Profile。
- `runtime/safety.py`：实现分级、跨 Bundle 和永久禁止项。
- `runtime/tools.py`：依赖 `LocatorCatalog` 与 `ResolvedTarget`，支持空稳定定位器目录。
- `storage/artifacts.py`：Profile registry、draft/candidate/history、原子写和回退。
- `storage/repository.py`：保存 target snapshot、Profile 阶段和顶层 Run 阶段。
- `generation/hypium.py`：只依赖 Trace，修复断言语义和晋级门禁。
- `api/app.py`、`cli.py`、`web/src/App.tsx`、`web/src/types.ts`：统一新契约。

## 14. 数据迁移与兼容

1. 启动时扫描 `profiles/*.json`，把现有 `profiles/zhihu-plus.json` 作为 schema v1 导入；首次写入时迁移为 v2，原文件先备份。
2. `TARGET_PROFILE_PATH` 存在时注册为显式覆盖 Profile；缺失时服务仍能启动和执行发现流程。
3. `RunTrace.target_app_id` 暂时保留；新增 `resolved_target`。SQLite 的 `target_app_id NOT NULL` 可继续写确定性 app ID，同时增加 `target_json`，避免一次性破坏旧查询。
4. 读取旧 Trace 时可关联 legacy Profile，但生成结果必须记录补全来源；新 Trace 必须自包含。
5. Profile schema 迁移失败不能阻止服务启动，应把该文件标记为 invalid 并允许其他应用继续运行。

## 15. 测试计划

### 15.1 单元测试

- bm dump 的多版本 fixture 解析、名称唯一/多候选/无匹配。
- bundle 精确匹配、Ability 缺失和启动后交叉校验失败。
- Profile v1/v2 读取、锁定、原子替换、备份、回退和损坏文件处理。
- 分级安全策略所有允许、显式放行和永久禁止组合。
- 页面候选排序、动作去重、页面/动作/时间上限。
- 跨 Bundle、系统权限页、桌面和应用市场检测。
- 定位器三轮稳定性、唯一性、动态 key、易变文本和坐标降级。
- 断言候选与 Hypium 文本/属性断言生成。
- 无应用级断言时拒绝晋级。
- Trace 自包含生成：删除或修改外部 Profile 后脚本哈希保持一致。

### 15.2 API 与集成测试

- 旧 `target_app_id` 请求保持可用。
- 无 Profile 时通过应用名称自动发现。
- 多候选进入等待状态并可继续。
- 已有 verified Profile 快速复验后直接测试。
- 版本变化后快速复验失败并触发完整探索。
- 探索失败保留 draft，不续跑原任务。
- 三轮验证和三次 Hypium 回放全部通过后自动晋级。
- `locked=true` 不被覆盖。
- Profile 晋级后同一 Run 继续原始任务。
- 停止请求在解析、探索、验证和回放阶段均能安全终止。

### 15.3 Web 测试

- 应用输入、候选消歧、策略设置请求体正确。
- UI 不再发送固定 `zhihu-plus`。
- SSE 正确展示全部新阶段和错误。
- draft、candidate、verified 与锁定状态显示准确。
- 小屏和桌面布局可查看页面图、验证轮次和告警。

### 15.4 真机验收

至少选择：

- 现有知乎++ Profile，验证完全向后兼容。
- 一个从未存在 Profile 的应用，通过名称发现并自动晋级。
- 一个名称有多个候选的场景，验证不自动猜测。
- 一个无法稳定重置或包含越界跳转的应用，验证只保存 draft。

每个可晋级应用必须提供：

- 应用枚举和解析原始输出。
- 启动后 bundle/Ability 交叉校验。
- 至少 3 个页面、3 类交互、3 个稳定定位器、2 个应用级断言。
- 3 次独立设备验证。
- 3 次独立 Hypium Driver 回放。
- Profile 文件、历史备份、生成脚本、日志、截图和最终报告。

## 16. 分阶段实施顺序

### 阶段 1：目标模型和自包含 Trace

实现 `TargetQuery`、`ResolvedTarget`、Profile registry 和 Trace 快照；让生成器只依赖 Trace。保持旧 Profile 模式行为不变。

验收：现有知乎++单元、集成和生成测试通过；修改外部 Profile 后历史 Run 生成结果不变。

### 阶段 2：应用枚举、解析和启动探测

实现 bm/aa 适配器、名称消歧、Ability 解析和启动后交叉校验；接入 CLI/API。

验收：bundle 精确匹配、名称唯一匹配、多候选、无匹配和 Ability 不明确均有确定性结果。

### 阶段 3：分级安全和有界探索

实现探索策略、候选动作排序、跨 Bundle 防护、页面图搜索和证据存储。

验收：所有上限生效，永久禁止项无法通过 Prompt 或 API 绕过，陌生应用可生成完整 draft。

### 阶段 4：稳定性分析和 Profile v2

实现定位器/断言筛选、三轮重启验证、版本快速复验、draft/candidate/history/locked。

验收：稳定与不稳定 fixture 可区分；失败 Profile 不晋级；写入可原子回滚。

### 阶段 5：Hypium 门禁和自动晋级

修复断言生成语义，实现应用级断言门禁、三次回放、candidate 到 verified 的原子晋级。

验收：任一回放失败不晋级，三次全部通过后自动生成正式 Profile。

### 阶段 6：原始任务续跑与完整 UI

实现同一 Run 的 bootstrap/task 两阶段、Web 候选选择、探索进度、Profile 管理和受限临时测试。

验收：用户只提供应用名称即可走完全流程；若提供任务，晋级后自动继续并给出独立任务结果。

### 阶段 7：回归、文档和演示证据

更新 README、启动手册、架构、API、Profile schema 和验证报告；完成知乎++兼容回归及至少一个陌生 App 的全流程演示。

## 17. 完成标准

只有同时满足以下条件，才能宣告完成：

- 磁盘中没有对应 Profile 时，用户只输入应用名称即可创建 Run。
- 应用名称多匹配时系统绝不自动猜测。
- 系统能够确定性获得 bundle、Ability 和版本信息，并在启动后交叉校验。
- 探索严格遵守 20 页、每页 8 动作、15 分钟边界和分级安全策略。
- draft Profile 能追溯到每个字段的设备或运行证据。
- 三轮重启验证满足 3 页面、3 交互、3 稳定定位器和 2 应用级断言。
- 三次 Hypium Driver 回放全部通过后自动写入 verified Profile。
- 正式 Profile 可锁定、备份和回退，写入过程不会留下半文件。
- Profile 晋级后同一 Run 自动执行用户原始任务。
- 旧 `TARGET_PROFILE_PATH` 和知乎++流程保持兼容。
- CLI、API、Web、Trace、报告和文档对状态与失败原因的表述一致。
- 自动测试、Ruff、Web production build、真机 HDC/Hypium 验收全部通过。

## 18. 风险与控制

- **bm/aa 输出跨系统版本变化**：保存原始输出，使用版本化解析器和多版本 fixture，解析不确定时失败。
- **模型误判危险控件**：安全策略在 ToolExecutor 前确定性执行，永久禁止项不可配置解除。
- **动态页面导致伪稳定**：必须跨三次独立重启验证，并要求唯一匹配与页面签名一致。
- **网络内容变化**：易变文本不进入高可信定位器；准入门槛优先选择离线或可控内容流程。
- **坐标回放脆弱**：不计入正式稳定定位器数量，绑定设备与分辨率并持续告警。
- **自动探索循环或状态爆炸**：页面、动作、时间、同页同动作去重和回退深度同时限制。
- **Profile 自动覆盖人工调整**：`locked=true`、版本备份、临时写入和原子替换。
- **旧 Run 被新 Profile 污染**：新 Run 冻结完整目标快照，生成器只依赖 Trace。
- **Hypium 模式表述混淆**：继续明确 Driver 模式；测试工程模式保持 `not_validated`，除非另行完成官方工程模板和调度验收。

## 19. 实施后的用户体验

最终命令可以保持简单：

```powershell
uv run main.py run --app "示例应用" --task "打开设置并验证版本信息"
```

用户看到的阶段为：查找应用、确认唯一目标、启动探测、复用或生成 Profile、探索进度、三轮验证、三次 Hypium 回放、Profile 晋级、原始任务执行和报告。首次运行成本较高；后续同版本只做快速复验并直接测试。任何失败都保留可检查的 draft、截图、布局、命令、页面图和具体门禁结果。

## 20. 当前实现落点与待验证项

截至 2026-09-11，计划中的目标解析、分级安全、有界探索、Profile v2 生命周期、三轮流程验证、三次 Hypium Driver 回放、自动晋级、同 Run 原任务续跑、CLI/API/Web 管理入口和 Trace 自包含生成已经写入当前分支。探索队列保存入口到页面的动作路径，并在每个候选前通过 `stop/start + 路径回放 + 页面签名` 重建状态；验证阶段独立重启三次并回放同一条至少三页面、三交互路径；Profile 准入脚本从该路径生成，不再只验证入口页。

自动测试已补充以下范围，但因模拟器离线，按用户要求暂不执行：

- `bm` 多格式解析、唯一匹配、多候选和无匹配；
- 探索边界、危险动作、跨 Bundle 恢复和路径重建；
- 三轮核心流程、稳定定位器、动态 locator 拒绝；
- Registry 原子写、生命周期、锁定、备份、损坏文件和显式回退；
- Trace 自包含生成、候选等待继续、自动晋级、provisional 限制和 Web 契约。

收尾审查进一步加固了 Registry 准入资产校验、持久化 candidate 一致性、锁定 Profile 的元数据更新边界、文件名碰撞检测、invalid 历史版本显式回退、Legacy Profile provisional 限制、三页面快速复验和恢复失败记录。

模拟器恢复后一次完成静态检查、Python 自动测试、Web production build 和真机验收。若首次综合验证暴露问题，只修复失败项并执行对应复测，最终保留完整命令、退出码、日志、截图、布局、验证轮次与三次 Driver 回放报告。全部通过后再提交 `codex/target-profile-auto-discovery` 分支；当前保持未提交状态。

## 21. 为什么使用当前对话而不是 Codex 原生问答

本工作需要连续读取仓库、修改多个文件、协调子代理、保存计划和维护分支状态。当前 Codex 任务本身就是具备工作区与工具上下文的原生协作入口；另开普通问答会丢失当前工作树、未提交差异和设备状态，也不能直接完成实现。需要用户选择时仍通过 Codex 的结构化交互或 Web 候选选择接口集中提问；运行中的目标消歧不会由模型自行猜测。
