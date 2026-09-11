"""根据运行轨迹生成 HTML 报告与对应的结构化 JSON 报告。"""

from __future__ import annotations

import html
import json
from pathlib import Path

from .models import RunTrace
from .storage import ArtifactStore


class ReportBuilder:
    """将轨迹中的动作、断言和站内截图组织成可查看的运行报告。"""

    def __init__(self, artifacts: ArtifactStore):
        self.artifacts = artifacts

    def build(self, trace: RunTrace) -> Path:
        """写入 HTML 与 JSON 报告，并返回 HTML 报告的绝对路径。"""
        run_dir = self.artifacts.run_dir(trace.run_id)
        report_path = run_dir / "reports" / "report.html"
        cards: list[str] = []
        snapshots = {item.snapshot_id: item for item in trace.snapshots}
        for action in trace.actions:
            snapshot = snapshots.get(action.after_snapshot_id or action.before_snapshot_id or "")
            image = self._image_markup(trace.run_id, run_dir, snapshot.image_path) if snapshot else ""
            assertion = ""
            if action.assertion:
                assertion_class = "ok" if action.assertion.passed else "bad"
                assertion_message = html.escape(action.assertion.message)
                assertion = f'<p class="assertion {assertion_class}">{assertion_message}</p>'
            cards.append(
                f"""<article class="step">
<h3>{html.escape(action.step_id)} · {html.escape(action.tool)}</h3>
<p>{html.escape(json.dumps(action.params, ensure_ascii=False))}</p>
{assertion}{image}
</article>"""
            )
        document = self._document(trace, "".join(cards))
        report_path.write_text(document, encoding="utf-8")
        self.artifacts.write_json(run_dir / "reports" / "report.json", trace)
        return report_path.resolve()

    @staticmethod
    def _image_markup(run_id: str, run_dir: Path, image_path: Path) -> str:
        try:
            relative = image_path.resolve().relative_to(run_dir.resolve()).as_posix()
        except ValueError:
            return ""
        escaped_run_id = html.escape(run_id, quote=True)
        escaped_relative = html.escape(relative, quote=True)
        return f'<img src="/api/runs/{escaped_run_id}/artifacts/{escaped_relative}" alt="运行截图">'

    @staticmethod
    def _document(trace: RunTrace, cards: str) -> str:
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
"""
        error = html.escape(trace.error or "")
        target = trace.resolved_target.model_dump(mode="json") if trace.resolved_target else {}
        profile = trace.profile_snapshot.model_dump(mode="json") if trace.profile_snapshot else {}
        discovery = trace.discovery_result or {}
        verification = trace.verification_result or {}
        gate_markup = f"""<section class="summary"><h2>Profile bootstrap</h2>
<p>阶段：<b>{html.escape(trace.phase)}</b> · Profile：<b>{html.escape(str(profile.get('status') or 'none'))}</b>
· 临时结果：<b>{'是' if trace.provisional else '否'}</b></p>
<p>目标：<code>{html.escape(str(target.get('bundle_name') or trace.target_app_id))}</code>
· 探索页面：{len(discovery.get('pages', []))} · 验证轮次：{len(verification.get('rounds', []))}
· Profile Hypium 回放：{len(trace.profile_validation_replays)}</p></section>"""
        return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width">
<title>Harmony Test Agent · {html.escape(trace.run_id)}</title>
<style>{css}</style></head><body><main>
<h1>OpenHarmony 多模态测试 Agent 报告</h1>
<section class="summary"><p><code>{html.escape(trace.run_id)}</code></p>
<h2>{html.escape(trace.task)}</h2>
<div class="grid"><div class="metric">状态<br><b>{html.escape(trace.state)}</b></div>
<div class="metric">模型<br><b>{html.escape(trace.model_used)}</b></div>
<div class="metric">页面<br><b>{len(trace.graph.nodes)}</b></div>
<div class="metric">动作<br><b>{len(trace.actions)}</b></div>
<div class="metric">断言<br><b>{len(trace.assertions)}</b></div></div>
<p class="bad">{error}</p></section>
{gate_markup}
{cards}
</main></body></html>"""
