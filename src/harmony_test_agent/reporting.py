"""根据运行轨迹生成 HTML 报告与对应的结构化 JSON 报告。"""

from __future__ import annotations

import html
import json
from pathlib import Path

from .models import RunTrace
from .storage import ArtifactStore


class ReportBuilder:
    """将动作、脚本覆盖、回放证据和失败摘要组织为运行报告。"""

    def __init__(self, artifacts: ArtifactStore):
        self.artifacts = artifacts

    def build(self, trace: RunTrace) -> Path:
        """写入 HTML 与 JSON 报告，并返回 HTML 报告的绝对路径。"""
        run_dir = self.artifacts.run_dir(trace.run_id)
        report_path = run_dir / "reports" / "report.html"
        snapshots = {item.snapshot_id: item for item in trace.snapshots}
        cards: list[str] = []
        for action in trace.actions:
            snapshot = snapshots.get(action.after_snapshot_id or action.before_snapshot_id or "")
            image = self._image_markup(trace.run_id, run_dir, snapshot.image_path) if snapshot else ""
            assertion = ""
            if action.assertion:
                assertion_class = "ok" if action.assertion.passed else "bad"
                assertion = f'<p class="assertion {assertion_class}">{html.escape(action.assertion.message)}</p>'
            cards.append(
                f"""<article class="step"><h3>{html.escape(action.step_id)} · {html.escape(action.tool)}</h3>
<p>{html.escape(json.dumps(action.params, ensure_ascii=False))}</p>{assertion}{image}</article>"""
            )
        document = self._document(
            trace,
            "".join(cards),
            self._coverage_markup(trace),
            self._replay_markup(trace),
            self._failure_markup(trace),
        )
        report_path.write_text(document, encoding="utf-8")
        self.artifacts.write_json(run_dir / "reports" / "report.json", trace)
        return report_path.resolve()

    @staticmethod
    def _image_markup(run_id: str, run_dir: Path, image_path: Path) -> str:
        try:
            relative = image_path.resolve().relative_to(run_dir.resolve()).as_posix()
        except ValueError:
            return ""
        return (
            f'<img src="/api/runs/{html.escape(run_id, quote=True)}/artifacts/'
            f'{html.escape(relative, quote=True)}" alt="运行截图">'
        )

    @staticmethod
    def _coverage_markup(trace: RunTrace) -> str:
        generated = trace.generated
        if generated is None:
            return '<section class="summary"><h2>脚本覆盖范围</h2><p>未生成 Hypium 脚本。</p></section>'
        reasons = "".join(f"<li>{html.escape(reason)}</li>" for reason in generated.incomplete_reasons)
        return f"""<section class="summary"><h2>脚本覆盖范围</h2><div class="grid">
<div class="metric">用途<br><b>{html.escape(generated.purpose)}</b></div>
<div class="metric">可回放<br><b>{"是" if generated.replay_eligible else "否"}</b></div>
<div class="metric">源动作<br><b>{generated.source_action_count}</b></div>
<div class="metric">纳入脚本<br><b>{generated.included_action_count}</b></div>
<div class="metric">省略动作<br><b>{generated.omitted_action_count}</b></div></div>
{"<ul class='bad'>" + reasons + "</ul>" if reasons else ""}</section>"""

    @staticmethod
    def _replay_markup(trace: RunTrace) -> str:
        rows = []
        for replay in trace.replays:
            error = html.escape(replay.error.message) if replay.error else ""
            exit_code = replay.exit_code if replay.exit_code is not None else ""
            evidence = "<br>".join(html.escape(path) for path in replay.evidence_paths)
            rows.append(
                f"<tr><td>{replay.attempt}</td><td>{html.escape(replay.status)}</td><td>{exit_code}</td>"
                f"<td>{'是' if replay.timed_out else '否'}</td><td>{error}</td><td>{evidence}</td></tr>"
            )
        body = "".join(rows) or '<tr><td colspan="6">尚无回放结果</td></tr>'
        progress = f"完成 {trace.replay_completed} / {trace.replay_total}；通过 {trace.replay_passed}"
        return f"""<section class="summary"><h2>回放结果</h2>
<p>状态：<b>{html.escape(trace.replay_status)}</b>；{progress}</p>
<table><thead><tr><th>尝试</th><th>状态</th><th>退出码</th><th>超时</th><th>错误</th><th>证据</th></tr></thead>
<tbody>{body}</tbody></table></section>"""

    @staticmethod
    def _failure_markup(trace: RunTrace) -> str:
        failures = []
        if trace.agent_error or trace.error:
            failures.append(f"Agent：{trace.agent_error or trace.error}")
        failures.extend(f"回放 {item.attempt}：{item.error.message}" for item in trace.replays if item.error)
        content = "".join(f'<li class="bad">{html.escape(item)}</li>' for item in failures)
        return f'<section class="summary"><h2>失败摘要</h2><ul>{content or "<li>无失败</li>"}</ul></section>'

    @staticmethod
    def _document(trace: RunTrace, cards: str, coverage: str, replays: str, failures: str) -> str:
        css = """
body { font-family: Inter, "Microsoft YaHei", sans-serif; background: #08111f; color: #e6edf7;
  margin: 0; padding: 32px; }
main { max-width: 1180px; margin: auto; }
.summary, .step { background: #111d2e; border: 1px solid #263953; border-radius: 16px;
  padding: 20px; margin: 16px 0; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; }
.metric { background: #17263a; padding: 12px; border-radius: 10px; }
img { display: block; max-width: 420px; max-height: 620px; object-fit: contain; border-radius: 12px;
  border: 1px solid #324865; margin-top: 12px; }
.ok { color: #61d6a3; } .bad { color: #ff7c8e; } code { color: #86d8ff; }
table { width: 100%; border-collapse: collapse; margin-top: 12px; }
th, td { border-bottom: 1px solid #324865; padding: 10px; text-align: left; }
"""
        error = html.escape(trace.agent_error or trace.error or "")
        target = trace.resolved_target.model_dump(mode="json") if trace.resolved_target else {}
        profile = trace.profile_snapshot.model_dump(mode="json") if trace.profile_snapshot else {}
        discovery = trace.discovery_result or {}
        verification = trace.verification_result or {}
        gate_markup = f"""<section class="summary"><h2>Profile bootstrap</h2>
<p>阶段：<b>{html.escape(trace.phase)}</b> · Profile：<b>{html.escape(str(profile.get("status") or "none"))}</b>
· 临时结果：<b>{"是" if trace.provisional else "否"}</b></p>
<p>目标：<code>{html.escape(str(target.get("bundle_name") or trace.target_app_id))}</code>
· 探索页面：{len(discovery.get("pages", []))} · 验证轮次：{len(verification.get("rounds", []))}
· Profile Hypium 回放：{len(trace.profile_validation_replays)}</p></section>"""
        return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width">
<title>Harmony Test Agent · {html.escape(trace.run_id)}</title>
<style>{css}</style></head><body><main>
<h1>OpenHarmony 多模态测试 Agent 报告</h1>
<section class="summary"><p><code>{html.escape(trace.run_id)}</code></p>
<h2>{html.escape(trace.task)}</h2><div class="grid">
<div class="metric">状态<br><b>{html.escape(trace.state)}</b></div>
<div class="metric">Agent 结果<br><b>{html.escape(trace.agent_outcome)}</b></div>
<div class="metric">模型<br><b>{html.escape(trace.model_used)}</b></div>
<div class="metric">页面<br><b>{len(trace.graph.nodes)}</b></div>
<div class="metric">动作<br><b>{len(trace.actions)}</b></div>
<div class="metric">断言<br><b>{len(trace.assertions)}</b></div></div>
{"<p class='bad'>" + error + "</p>" if error else ""}</section>
{gate_markup}{coverage}{replays}{failures}{cards}
</main></body></html>"""
