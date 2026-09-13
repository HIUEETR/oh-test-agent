# Web 控制台（v2 重写版）

OpenHarmony 多模态测试 Agent 的现代化控制台：浅色玻璃拟态界面，实时呈现大模型的
思考、输入与输出，覆盖「采集 → 感知 → 规划 → 探索 → 脚本 → 回放 → 报告」完整闭环。
旧版控制台冻结在 `../web-legacy/`，仅供追溯，不再维护。

## 技术栈

- React 19 + TypeScript 5.9（strict）+ Vite 7
- 状态管理：zustand（单一 console store，按选择器订阅）
- 页面关系图：@xyflow/react（React Flow）
- 图标：lucide-react；Markdown 渲染：marked + dompurify；类名：clsx
- 测试：vitest + @testing-library/react（jsdom）
- 无路由库：单控制台应用，使用 `?run_id=&tab=` 查询参数导航并同步地址栏

## 目录结构

```
web/src/
├── app/                  App 壳、深链解析（deep-links）、运行时编排（use-run-runtime）
├── api/                  client（fetch 封装/并发锁）、sse（EventSource 封装）、types（后端契约）
├── stores/               console.ts：全部业务状态与异步动作（zustand）
├── features/
│   ├── launcher/         目标解析、任务、模式、探索策略、启动/停止
│   ├── health/           设备/Provider/Hypium 健康面板
│   ├── runs/             当前运行卡 + 历史运行（左栏列表与完整表格）
│   ├── pipeline/         闭环流水线状态条
│   ├── live/             设备画面、思考流、事件日志（payload 查看器）、元素表
│   ├── advisor/          顾问对话视图（LLM 输入/输出留痕 + 逐页结论）
│   ├── graph/            页面关系图（trace.graph 优先，探索页面图回退）
│   ├── script/           Hypium 脚本与验收回放
│   ├── profiles/         Profile 资产（复验/锁定/回退/失效）
│   └── report/           HTML 报告探测/内嵌/下载
├── components/ui/        通用原语（Badge/StatusDot/EmptyState/RetryImage/JsonViewer/Markdown…）
├── utils/                thought-aggregator（事件→思考块）、pipeline、markdown、artifact、format
└── styles/               tokens.css（设计令牌）+ global.css（组件样式）
```

## 核心概念

- **思考流（ThoughtStream）**：`utils/thought-aggregator.ts` 把 SSE 事件流聚合为
  计划 / 感知 / 决策步骤 / 断言 / 顾问 / 页面 / 通知七类思考块；`discovery_progress`
  事件按 `payload.stage` 分发（`advisor` 进思考流，`advisor_turn` 留痕进顾问对话视图）。
- **顾问对话（AdvisorPanel）**：读取 `/api/runs/{id}/discovery` 的 `advisor_log`
  （每轮 LLM 调用的输入 payload 与输出 verdict），旧运行回退展示 `advisor_verdicts` 逐页结论。
- **页面关系图**：任务阶段 `trace.graph` 优先；探索型运行回退用 `discovery.pages/transitions`
  构建页面状态图。
- **SSE**：原生 EventSource 按事件名订阅（`api/types.ts` 的 `RUN_EVENT_TYPES` 镜像后端
  EventType），重连自动携带 `Last-Event-ID` 续传；终态后延迟 3 秒收流以保证历史回放完整。

## 设计令牌

`styles/tokens.css` 定义全部颜色/圆角/字体变量：冷调晨蓝灰底 + OpenHarmony 品牌蓝
（#0A59F7）主色 + 玻璃面板配方（`backdrop-filter` + 内高光 + 柔和投影）。组件样式只允许
引用令牌变量，为后续双主题扩展留出空间。正文字体优先 HarmonyOS Sans SC / MiSans，
与 OpenHarmony 主题呼应；不加载外部字体 CDN，离线可用。

## 开发与测试

```powershell
# 方式一：双端一体（推荐，注入代理环境变量）
uv run main.py dev

# 方式二：手动双终端
uv run main.py serve            # API :8000
cd web; npm run dev             # Web :5173，/api 代理到 127.0.0.1:8000

cd web
npm run test      # vitest 单元/组件测试
npm run build     # tsc -b + vite 生产构建
```

测试覆盖：思考流聚合规则、Markdown 渲染（含 XSS）、产物 URL 归一化、深链解析、
流水线推导，以及思考流/事件日志/流水线条的组件冒烟。
