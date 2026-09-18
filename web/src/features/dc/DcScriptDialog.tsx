// DC 模式脚本生成对话框：触发 Hypium 脚本生成并预览 Python 源码。
//
// 弹窗必须用 createPortal 挂到 document.body：.panel 的 backdrop-filter 会让
// 该面板成为后代 position:fixed 元素的包含块，弹窗若留在面板内，z-index 只在
// 面板的层叠上下文里生效，会被右侧「操作日志」等相邻面板压在下面。

import { useCallback, useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { FileCode2, X } from "lucide-react";
import { useDcConsole } from "../../stores/dc-console";

export function DcScriptDialog() {
  const script = useDcConsole((state) => state.script);
  const generateScript = useDcConsole((state) => state.generateScript);
  // 只选原始数值 length（按值比较），避免 selector 返回新数组引用导致无限重渲染。
  const invocationCount = useDcConsole((state) => state.session?.invocations.length ?? 0);
  const [showDialog, setShowDialog] = useState(false);
  const [generating, setGenerating] = useState(false);

  const closeDialog = useCallback(() => setShowDialog(false), []);

  // 打开期间：Esc 关闭 + 锁定页面滚动
  useEffect(() => {
    if (!showDialog) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") closeDialog();
    };
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    window.addEventListener("keydown", onKeyDown);
    return () => {
      document.body.style.overflow = previousOverflow;
      window.removeEventListener("keydown", onKeyDown);
    };
  }, [showDialog, closeDialog]);

  const handleGenerate = async () => {
    setGenerating(true);
    try {
      await generateScript();
      setShowDialog(true);
    } finally {
      setGenerating(false);
    }
  };

  const handleCopy = () => {
    if (script?.python_text) {
      void navigator.clipboard.writeText(script.python_text);
    }
  };

  const handleDownload = () => {
    if (!script?.python_text) return;
    const blob = new Blob([script.python_text], { type: "text/x-python" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "dc_test.py";
    a.click();
    URL.revokeObjectURL(url);
  };

  const overlay =
    showDialog && script
      ? createPortal(
          <div className="dc-script-overlay" onClick={closeDialog} role="presentation">
            <div
              className="dc-script-dialog"
              role="dialog"
              aria-modal="true"
              aria-label="生成的 Hypium 脚本"
              onClick={(event) => event.stopPropagation()}
            >
              <div className="dc-script-dialog-header">
                <strong>生成的 Hypium 脚本</strong>
                <small>
                  {script.replay_eligible
                    ? `可作为验收脚本执行 · 断言 ${script.explicit_assertions ?? 0} 条`
                    : "诊断回放专用 · 未包含断言"}
                </small>
                <button type="button" className="secondary compact" onClick={closeDialog} aria-label="关闭">
                  <X size={14} />
                </button>
              </div>
              <div className="dc-script-dialog-stats">
                <span>可回放操作：{script.included_operations}</span>
                <span>省略操作：{script.omitted_operations.length}</span>
                <span>警告：{script.warnings.length}</span>
              </div>
              <pre className="dc-script-code">{script.python_text}</pre>
              <div className="dc-script-dialog-footer">
                <button type="button" className="secondary compact" onClick={handleCopy}>复制</button>
                <button type="button" className="secondary compact" onClick={handleDownload}>下载</button>
              </div>
            </div>
          </div>,
          document.body,
        )
      : null;

  return (
    <section className="panel dc-script">
      <div className="card-heading">
        <span>Hypium 脚本</span>
        <small>{script ? `${script.included_operations} 步可回放` : "未生成"}</small>
      </div>
      <div className="dc-script-actions">
        <button
          type="button"
          className="primary compact"
          onClick={() => void handleGenerate()}
          disabled={invocationCount === 0 || generating}
        >
          <FileCode2 size={14} />
          {generating ? "生成中..." : "生成脚本"}
        </button>
        {script && (
          <button type="button" className="secondary compact" onClick={() => setShowDialog(true)}>
            预览
          </button>
        )}
      </div>
      {script && script.warnings.length > 0 && (
        <div className="dc-script-warnings">
          {script.warnings.slice(0, 3).map((w, i) => (
            <small key={i}>⚠ {w}</small>
          ))}
        </div>
      )}
      {script && (
        <div className="dc-script-hint">
          <small>全部脚本（含本会话录制）可在顶部「Hypium 脚本」Tab 中查看与启动。</small>
        </div>
      )}

      {overlay}
    </section>
  );
}
