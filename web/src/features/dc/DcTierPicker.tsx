// DC 模式工具层级选择器：L1-L5 单选组，每层一行描述。

import { useDcConsole } from "../../stores/dc-console";
import { TIER_DESCRIPTIONS, type DcToolTier } from "../../api/dc-types";

const TIERS: DcToolTier[] = [1, 2, 3, 4, 5];

export function DcTierPicker() {
  const session = useDcConsole((state) => state.session);
  const changeTier = useDcConsole((state) => state.changeTier);
  const currentTier = session?.tier ?? 2;
  const busy = session?.status === "thinking" || session?.status === "acting";

  return (
    <section className="panel dc-tier-picker">
      <div className="card-heading">
        <span>工具层级</span>
        <small>L{currentTier}</small>
      </div>
      <div className="dc-tier-options" role="radiogroup" aria-label="选择工具层级">
        {TIERS.map((tier) => (
          <label key={tier} className={`dc-tier-option ${tier === currentTier ? "active" : ""}`}>
            <input
              type="radio"
              name="dc-tier"
              value={tier}
              checked={tier === currentTier}
              onChange={() => void changeTier(tier)}
              disabled={busy}
            />
            <span className="dc-tier-label">{TIER_DESCRIPTIONS[tier]}</span>
          </label>
        ))}
      </div>
    </section>
  );
}
