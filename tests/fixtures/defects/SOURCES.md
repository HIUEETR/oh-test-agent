# `tests/fixtures/defects/` 样本来源说明

真机采集的原始产物体积大、且崩溃 / 卡死窗口难以在离线 CI 中稳定复现，因此这里的
`.txt` / `.png` / `.json` 都是**构造样本**。每个样本严格对齐 OpenHarmony 的产物格式，
来源与采集方式记录在本文件（而不是塞进 `.txt` 里 —— hilog 的崩溃模式正则会命中注释里的
`appfreeze` / `THREAD_BLOCK` / `LIFECYCLE_TIMEOUT` 等关键词，把说明文字变成假 finding）。

## hilog 样本

行格式对齐 `hdc shell hilog -x` 的
`MM-DD HH:MM:SS.mmm  PID  TID  LEVEL  DOMAIN/tag: message` 布局。

| 文件 | 来源 | 采集方式（真机复核用） | 应命中的模式 |
|---|---|---|---|
| `hilog_cppcrash.txt` | 构造样本 | `hdc shell aa force-stop <bundle>` 后 `hdc shell hilog -x \| Select-String cppcrash` | `\bcppcrash\b`、`Reason: Signal:SIGSEGV` → `CPP_CRASH`（critical） |
| `hilog_appfreeze.txt` | 构造样本 | 主线程长时间阻塞后 `hdc shell hilog -x \| Select-String appfreeze,THREAD_BLOCK` | `\bappfreeze\b` → `APP_FREEZE`；`THREAD_BLOCK_\d+S` / `LIFECYCLE_TIMEOUT` / `APP_INPUT_BLOCK` → `ANR`（均 critical） |
| `hilog_clean.txt` | 构造样本 | 正常页面的 hilog 尾部 | 无命中（用于断言「无命中不得产 finding」） |

归属判定：行内含被测 bundle（`com.zhihu.hmos`）或 pid 命中 `pidof` 标记行；
无法归属的行会降一级 severity（样本里刻意保留了一条 `com.huawei.hmos.settings` 的行用于覆盖该分支）。

## faultlog 样本

| 文件 | 来源 | 说明 |
|---|---|---|
| `faultlog_index.txt` | 构造样本 | `hdc shell ls /data/log/faultlog/faultlogger` 的输出形态；含 `cppcrash` / `jscrash` / `appfreeze` 三类、两个 bundle，用于验证 bundle 过滤与「最新 3 个」截断 |

faultlog 头部内容（`FAULTLOG_CPPCRASH_HEAD`）写在 `tests/defect_fixtures.py` 里，
与既有 `tests/fixtures/analysis/faultlog_cppcrash_head.txt` 同族。

## 截图样本

| 文件 | 来源 | 说明 |
|---|---|---|
| `blank_white.png` | 程序生成 | 1080×2340 纯白，`mean=255 / σ=0` → 白屏（light） |
| `blank_dark.png` | 程序生成 | 1080×2340 纯黑，`mean=0 / σ=0` → 黑屏（dark） |

正常页面的截图不落盘为 fixture：测试里用程序生成的高频纹理图（σ 远超白屏阈值），
避免把「白屏判定」的结论混进与本用例无关的断言。真机截图只在 Phase 6 验收时现场采集。

## 布局样本

| 文件 | 来源 | 说明 |
|---|---|---|
| `layout_out_of_bounds.json` | 按 ArkUI dump 格式构造 | 覆盖 L1 越界（右侧 / 顶部）+ 不可见越界节点不报 + 不可见节点不参与 |
| `layout_overlap.json` | 按 ArkUI dump 格式构造 | 覆盖 L2 同级文本重叠（IoU > 0.5） |

既有 `tests/fixtures/analysis/layout_raw.json` 同时覆盖 L1–L4 四条规则；
本目录的两个文件是**单规则聚焦**版本，便于定位失败原因。

真机复核（2026-09-21，`127.0.0.1:5555`）：对真实 ArkUI dump（1320×2232，55 元素）
跑 `detect_layout_anomalies` 得到 **0 findings**，越界 fixture 得到 4 findings
（`L1_out_of_bounds` + `L3_clipped_child`），即规则既不误报也能报。
