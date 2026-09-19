// DC 工具层级下拉：紧凑触发器（L{tier} · 短标签）+ 向上弹出菜单（替代原 L1-L5 单选组面板）。
//
// 弹层必须用 createPortal 挂到 document.body：.panel 的 backdrop-filter 会让该面板
// 成为后代 position:fixed 元素的包含块，留在面板内的弹层会被相邻面板压住
// （同 DcScriptDialog → DcScriptPreview 的教训）。触发器位于会话输入区底部，
// 因此菜单向上弹出，避免超出视口下沿被裁掉。

import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Check, ChevronDown, Layers } from "lucide-react";
import { useDcConsole } from "../../stores/dc-console";
import { TIER_DESCRIPTIONS, type DcToolTier } from "../../api/dc-types";

const TIERS: DcToolTier[] = [1, 2, 3, 4, 5];

/** 短标签：取描述中「L{n} · 」之后、第一个「：」之前的部分（如「观测诊断」）。 */
function shortLabel(tier: DcToolTier): string {
  const head = TIER_DESCRIPTIONS[tier].split("：")[0];
  return head.replace(/^L\d+\s*·\s*/, "").trim();
}

export function DcTierMenu() {
  const activeSessionId = useDcConsole((state) => state.activeSessionId);
  const session = useDcConsole((state) => state.session);
  const changeTier = useDcConsole((state) => state.changeTier);

  // 无会话时按后端默认层级展示 L2，但触发器不可用。
  const currentTier = session?.tier ?? 2;
  const busy = session?.status === "thinking" || session?.status === "acting";
  const disabled = busy || !activeSessionId;

  const [open, setOpen] = useState(false);
  const [position, setPosition] = useState({ left: 0, bottom: 0 });
  const rootRef = useRef<HTMLDivElement>(null);
  const popRef = useRef<HTMLDivElement>(null);

  const close = useCallback(() => setOpen(false), []);

  const toggle = () => {
    if (disabled) return;
    if (open) {
      close();
      return;
    }
    const rect = rootRef.current?.getBoundingClientRect();
    if (rect) setPosition({ left: rect.left, bottom: window.innerHeight - rect.top + 6 });
    setOpen(true);
  };

  // 打开期间：Esc 关闭 + 点击外部关闭
  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: MouseEvent) => {
      const target = event.target as Node | null;
      if (target && (rootRef.current?.contains(target) || popRef.current?.contains(target))) return;
      close();
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") close();
    };
    window.addEventListener("mousedown", onPointerDown);
    window.addEventListener("keydown", onKeyDown);
    return () => {
      window.removeEventListener("mousedown", onPointerDown);
      window.removeEventListener("keydown", onKeyDown);
    };
  }, [open, close]);

  const handleSelect = (tier: DcToolTier) => {
    close();
    void changeTier(tier);
  };

  const title = !activeSessionId
    ? "创建会话后可调整"
    : busy
      ? "Agent 执行中不可调整"
      : TIER_DESCRIPTIONS[currentTier];

  return (
    <div className="dc-tier" ref={rootRef}>
      <button
        type="button"
        className="dc-tier-trigger"
        onClick={toggle}
        disabled={disabled}
        title={title}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={`工具层级 L${currentTier}`}
      >
        <Layers size={13} aria-hidden="true" />
        <span className="dc-tier-trigger-label">L{currentTier} · {shortLabel(currentTier)}</span>
        <ChevronDown size={13} aria-hidden="true" />
      </button>

      {open &&
        createPortal(
          <div
            className="dc-tier-pop"
            role="menu"
            aria-label="选择工具层级"
            ref={popRef}
            style={{ left: position.left, bottom: position.bottom }}
          >
            {TIERS.map((tier) => (
              <button
                key={tier}
                type="button"
                role="menuitem"
                aria-checked={tier === currentTier}
                className={`dc-tier-pop-item${tier === currentTier ? " active" : ""}`}
                onClick={() => handleSelect(tier)}
              >
                <span className="dc-tier-pop-label">{TIER_DESCRIPTIONS[tier]}</span>
                {tier === currentTier && <Check size={14} aria-hidden="true" />}
              </button>
            ))}
          </div>,
          document.body,
        )}
    </div>
  );
}
