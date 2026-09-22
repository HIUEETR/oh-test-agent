// DC 脚本预览弹窗：受控展示已生成的 Hypium 脚本源码（生成入口在 DcComposerActions）。
//
// 弹窗必须用 createPortal 挂到 document.body：.panel 的 backdrop-filter 会让
// 该面板成为后代 position:fixed 元素的包含块，弹窗若留在面板内，z-index 只在
// 面板的层叠上下文里生效，会被相邻面板压在下面。

import { useCallback, useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { RotateCcw, X } from "lucide-react";
import { useDcConsole } from "../../stores/dc-console";

export function DcScriptPreview({ open, onClose }: { open: boolean; onClose: () => void }) {
  const script = useDcConsole((state) => state.script);
  const generateScript = useDcConsole((state) => state.generateScript);
  const [generating, setGenerating] = useState(false);

  // 无脚本时即使 open 也不渲染：空弹窗会让用户以为生成失败
  const visible = open && Boolean(script);

  // 打开期间：Esc 关闭 + 锁定页面滚动
  useEffect(() => {
    if (!visible) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    window.addEventListener("keydown", onKeyDown);
    return () => {
      document.body.style.overflow = previousOverflow;
      window.removeEventListener("keydown", onKeyDown);
    };
  }, [visible, onClose]);

  const handleRegenerate = async () => {
    setGenerating(true);
    try {
      await generateScript();
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

  const closeDialog = useCallback(() => onClose(), [onClose]);

  if (!visible || !script) return null;

  return createPortal(
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
              ? `可立即执行 · 断言 ${script.explicit_assertions ?? 0} 条${
                  (script.explicit_assertions ?? 0) === 0 ? "（无检查点，建议补充断言）" : ""
                }`
              : "不可执行 · 缺少可回放动作或身份为占位值"}
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
          <button
            type="button"
            className="secondary compact"
            onClick={() => void handleRegenerate()}
            disabled={generating}
          >
            <RotateCcw size={14} />{generating ? "生成中..." : "重新生成"}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
