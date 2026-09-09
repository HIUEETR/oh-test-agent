# Hypium 6.1.0.210 本机基线

更新时间：2026-09-09

## 1. 固定版本

- Python：3.14.0
- `hypium`：6.1.0.210
- `xdevice`：6.0.7.210（由 Hypium 依赖解析）
- Pydantic AI：1.73.0
- 已验证设备：`127.0.0.1:5555`，label=`phone`
- 已验证应用：`com.github.zhuoyi233.zhplus` / `EntryAbility`

版本由 `pyproject.toml` 和 `uv.lock` 固定。预检生成的机器可读基线位于：

```text
artifacts/preflight/hypium-baseline.json
```

## 2. Driver 模式

当前正式可执行链路使用 `UiDriver`：

```python
from hypium import BY, MatchPattern, UiDriver

with_driver = UiDriver.connect(
    device_sn="127.0.0.1:5555",
    report_path="...",
    log_level="info",
)
with_driver.stop_app("com.github.zhuoyi233.zhplus")
with_driver.start_app("com.github.zhuoyi233.zhplus", "EntryAbility")
with_driver.touch(BY.key("p2_home_titlebar_search"))
with_driver.input_text(BY.key("p2_search_input"), "OpenHarmony")
with_driver.check_component_exist(BY.key("p2_home_feed_list"), expect_exist=True)
with_driver.capture_screen("final.jpeg")
with_driver.close()
```

已验证 API 包括：

- `UiDriver.connect`
- `stop_app`
- `start_app`
- `wait`
- `touch`
- `input_text`
- `swipe`
- `go_back`
- `check_component_exist`
- `capture_screen`
- `close`

Hypium 的 `capture_screen` 使用 JPEG 文件；Agent 自身 HDC Snapshot 经 Pillow 校验后保存为 PNG。

## 3. Driver 与测试工程模式分离

当前仓库只声明 Driver 模式完成。DevEco Testing 测试工程模式需要真实且可复用的：

- `testcases` 工程目录；
- `config/user_config.xml`；
- 任务 JSON；
- Runner 命令；
- 成功与失败各一次可解释基线。

在上述材料完成前，测试工程模式保持：

```json
{
  "status": "not_validated"
}
```

不能把 Driver 模式的成功结果包装成测试工程模式成功。

## 4. 运行环境隔离

Hypium/xdevice 可能在导入和启动期间访问用户 Home 下的 `.xdevice`。本项目 Runner 只对子进程设置：

```text
HOME=<repo>/.runtime-user
USERPROFILE=<repo>/.runtime-user
PYTHONIOENCODING=utf-8
HARMONY_AGENT_REPORT_DIR=<run>/hypium/attempt-NN
```

因此：

- 不修改系统全局 Home；
- 不把 `.runtime-user` 交给 Git；
- 每次回放使用独立报告目录；
- `environment.json` 只保存上述白名单字段，不保存 API Key。

## 5. 确定性代码生成

生成器从成功 Action 的 `LocatorCandidate` 生成：

| 运行定位器 | Hypium 输出 |
| --- | --- |
| `key` | `BY.key(...)` |
| `id` | `BY.id(...)` |
| `text` | `BY.text(...)` |
| `type_text` | `BY.type(...).text(...)` |
| 动态长数字 key | `MatchPattern.STARTS_WITH` |
| 无稳定定位器但有 bbox | `driver.touch((x, y))`，同时告警 |

模型不能提供任意 Python 或未知 JSON 字段。生成脚本使用固定模板；配置写入已验证的 Driver 字段；元数据保存 Python/JSON SHA-256。

## 6. 坐标降级

最终验收 Run 中两个返回按钮没有稳定 key/id，但当前 Snapshot 提供了有效 bbox：

```text
step 9  → (108, 201)
step 15 → (102, 201)
```

生成器把它们写成：

```python
driver.touch((108, 201))  # coordinate fallback for 'ui-66c362215135'
driver.touch((102, 201))  # coordinate fallback for 'ui-502a19f48038'
```

同一告警同时存在于：

- 生成 Python 注释；
- 生成 JSON 的 `warnings`；
- `generation_metadata.json`；
- HTML 报告；
- Web 脚本页告警区。

运行时 `ui-...` 只在当前 Snapshot 内有效，不能错误生成 `BY.text('ui-...')`。

## 7. 动态 key

信息流卡片 key 末尾包含动态长数字，例如：

```text
p2_home_feed_card_answer_2075627677919211634
```

生成器确定性转换为：

```python
BY.key("p2_home_feed_card_answer_", MatchPattern.STARTS_WITH)
```

该行为会产生告警，提醒回放选择的是当前页面第一个匹配项，而不是绑定某个动态内容 ID。

## 8. 回放证据

每次回放目录包含：

```text
command.json
  完整参数、returncode、duration_ms、timed_out

environment.json
  隔离后的非敏感白名单环境

stdout.log / stderr.log / task_log.log
  Hypium/xdevice 原始输出

generated_result.json
  生成脚本写入的 passed/error

final.jpeg 或 failure.jpeg
  最终 UI 证据
```

CLI：

```powershell
.\.venv\Scripts\harmony-test-agent.exe execute --run-id <run-id> --attempts 3
```

## 9. 2026-09-09 真实验收

Run：`run-20260909T140205Z-e9ada52e`

```text
attempt-01  returncode=0  passed=true  duration≈29.2s
attempt-02  returncode=0  passed=true  duration≈26.3s
attempt-03  returncode=0  passed=true  duration≈26.9s
```

三次均完成：

1. 连接设备；
2. stop/start 知乎++；
3. 打开搜索；
4. 输入 `OpenHarmony`；
5. 收起键盘并返回首页；
6. 打开动态内容详情；
7. 断言详情内容；
8. 返回首页；
9. 断言首页信息流；
10. 保存最终截图并关闭 Driver。

此外，Web 脚本页的“连续回放 3 次”按钮实际调用 `POST /api/runs/{run_id}/execute?attempts=3`，API 返回 200，三次回放再次全部成功。

## 10. 返回码语义

- CLI `execute`：所有 attempt 均通过时返回 0；任一失败返回 1。
- 生成脚本：成功返回 0，异常返回 1，并写入 `generated_result.json`。
- Runner：同时检查进程返回码和生成结果；保留 stdout/stderr，不把日志中的 warning 文本单独等同于失败。
- 超时：`timed_out=true`，Run 状态进入 `failed_script`。
