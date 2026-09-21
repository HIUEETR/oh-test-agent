"""Cross-restart stability analysis for locators and application assertions.

INTERNAL CAPABILITY (2026-09-17 重构后): 不再通过 CLI/API/Web 直接暴露。

消费方：
- agents/orchestrator.py（经 discovery/verification.py 内部使用）
- dc/distill.py::DcProfileDistiller（DC 会话蒸馏 Profile 时复用稳定性分析）

禁止从 cli.py 或 web/ 反向依赖本模块。
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from enum import StrEnum

from pydantic import BaseModel, Field

from ..models import LocatorKind, ScreenSnapshot, UIElement

# 显式导出（2026-09-17 重构 §8.7）：verification 与 dc/distill 消费的公开能力。
__all__ = [
    "AssertionObservation",
    "LocatorObservation",
    "StabilityAnalyzer",
    "StabilityLevel",
    "StabilityReport",
    "StableAssertionEvidence",
    "StableLocatorEvidence",
]


class StabilityLevel(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class LocatorObservation(BaseModel):
    round_number: int = Field(ge=1)
    page_signature: str
    kind: LocatorKind
    value: str
    element_type: str = ""
    text: str = ""
    match_count: int = Field(default=1, ge=0)
    interactive: bool = False
    resolution: tuple[int, int] | None = None


class StableLocatorEvidence(BaseModel):
    name: str
    kind: LocatorKind
    value: str
    element_type: str = ""
    text: str = ""
    level: StabilityLevel
    rounds: tuple[int, ...]
    page_signatures: tuple[str, ...]
    unique_each_round: bool
    warning: str | None = None


class AssertionObservation(BaseModel):
    round_number: int = Field(ge=1)
    page_signature: str
    kind: str
    target: str
    passed: bool
    app_level: bool = True


class StableAssertionEvidence(BaseModel):
    kind: str
    target: str
    rounds: tuple[int, ...]
    page_signatures: tuple[str, ...]
    app_level: bool = True


class StabilityReport(BaseModel):
    locators: list[StableLocatorEvidence] = Field(default_factory=list)
    assertions: list[StableAssertionEvidence] = Field(default_factory=list)
    rejected_locators: dict[str, str] = Field(default_factory=dict)

    @property
    def promotable_locator_count(self) -> int:
        return sum(item.level in {StabilityLevel.HIGH, StabilityLevel.MEDIUM} for item in self.locators)

    @property
    def app_assertion_count(self) -> int:
        return sum(item.app_level for item in self.assertions)


class StabilityAnalyzer:
    """Accept only unique locator and assertion observations from N rounds.

    2026-09-17 重构：``required_rounds`` 从类常量改为构造参数。资产流水线精简后
    Profile 只做 1 轮设备验证，因此默认值仍为 3（向后兼容），调用方传入
    ``settings.profile_verification_rounds``。
    ``_reject_reason`` 与 ``analyze`` 都读取同一实例属性：只改轮次生成方而不改
    本类会导致所有定位器被拒绝（``promotable_locator_count == 0``）。
    """

    def __init__(self, required_rounds: int = 3) -> None:
        self.required_rounds = max(int(required_rounds), 1)

    def locator_observations(
        self, snapshot: ScreenSnapshot, round_number: int, page_signature: str
    ) -> list[LocatorObservation]:
        candidates: list[tuple[LocatorKind, str, UIElement]] = []
        for element in snapshot.elements:
            if element.key:
                candidates.append((LocatorKind.KEY, element.key, element))
            if element.id and element.id != element.key:
                candidates.append((LocatorKind.ID, element.id, element))
            if element.type and element.content:
                candidates.append((LocatorKind.TYPE_TEXT, f"{element.type}|{element.content}", element))
            elif element.content:
                candidates.append((LocatorKind.TEXT, element.content, element))
        counts = Counter((kind, value) for kind, value, _ in candidates)
        return [
            LocatorObservation(
                round_number=round_number,
                page_signature=page_signature,
                kind=kind,
                value=value,
                element_type=element.type,
                text=element.content,
                match_count=counts[(kind, value)],
                interactive=element.clickable or element.editable or element.scrollable,
                resolution=(snapshot.width, snapshot.height),
            )
            for kind, value, element in candidates
        ]

    def analyze(
        self,
        locators: list[LocatorObservation],
        assertions: list[AssertionObservation],
    ) -> StabilityReport:
        report = StabilityReport()
        grouped: dict[tuple[str, LocatorKind, str], list[LocatorObservation]] = defaultdict(list)
        for observation in locators:
            grouped[(observation.page_signature, observation.kind, observation.value)].append(observation)

        for (_, kind, value), items in grouped.items():
            rounds = sorted({item.round_number for item in items})
            key = f"{kind}:{value}"
            reason = self._reject_reason(kind, value, items, rounds)
            if reason:
                report.rejected_locators[key] = reason
                continue
            level = self._level(kind)
            report.locators.append(
                StableLocatorEvidence(
                    name=self._name(items[0]),
                    kind=kind,
                    value=value,
                    element_type=items[0].element_type,
                    text=items[0].text,
                    level=level,
                    rounds=tuple(rounds),
                    page_signatures=tuple(sorted({item.page_signature for item in items})),
                    unique_each_round=all(item.match_count == 1 for item in items),
                    warning="resolution-bound coordinate fallback" if level == StabilityLevel.LOW else None,
                )
            )

        assertion_groups: dict[tuple[str, str, str], list[AssertionObservation]] = defaultdict(list)
        for item in assertions:
            assertion_groups[(item.page_signature, item.kind, item.target)].append(item)
        for (_, kind, target), items in assertion_groups.items():
            rounds = sorted({item.round_number for item in items if item.passed and item.app_level})
            if len(rounds) == self.required_rounds:
                report.assertions.append(
                    StableAssertionEvidence(
                        kind=kind,
                        target=target,
                        rounds=tuple(rounds),
                        page_signatures=tuple(sorted({item.page_signature for item in items})),
                    )
                )
        report.locators.sort(key=lambda item: (item.level != StabilityLevel.HIGH, item.kind, item.name))
        return report

    def _reject_reason(
        self,
        kind: LocatorKind,
        value: str,
        items: list[LocatorObservation],
        rounds: list[int],
    ) -> str | None:
        if len(rounds) != self.required_rounds:
            return f"observed in {len(rounds)} of {self.required_rounds} rounds"
        if any(item.match_count != 1 for item in items):
            return "locator is not unique in every round"
        if not any(item.interactive for item in items) and kind not in {LocatorKind.TEXT, LocatorKind.TYPE_TEXT}:
            return "locator target is not interactive"
        if kind in {LocatorKind.KEY, LocatorKind.ID} and _dynamic_identifier(value):
            return "identifier appears dynamic"
        if kind in {LocatorKind.TEXT, LocatorKind.TYPE_TEXT} and _dynamic_text(value):
            return "text appears volatile or user generated"
        if kind in {LocatorKind.COORDINATE, LocatorKind.SPATIAL, LocatorKind.VLM_BBOX}:
            resolutions = {item.resolution for item in items}
            if len(resolutions) != 1:
                return "coordinate fallback changed resolution"
        return None

    @staticmethod
    def _level(kind: LocatorKind) -> StabilityLevel:
        if kind in {LocatorKind.KEY, LocatorKind.ID}:
            return StabilityLevel.HIGH
        if kind in {LocatorKind.TEXT, LocatorKind.TYPE_TEXT}:
            return StabilityLevel.MEDIUM
        return StabilityLevel.LOW

    @staticmethod
    def _name(item: LocatorObservation) -> str:
        semantic = item.text or item.value
        return re.sub(r"\s+", " ", semantic).strip()[:80]


def _dynamic_identifier(value: str) -> bool:
    return bool(
        re.search(r"(?:^|[_-])\d{6,}(?:$|[_-])", value)
        or re.search(r"[0-9a-f]{16,}", value, re.I)
        or re.search(r"(?:session|timestamp|nonce|random|uuid)", value, re.I)
    )


def is_dynamic_identifier(value: str) -> bool:
    """公开别名：任务期证据回收需要与验证期完全一致的动态标识判定（计划 3.3）。"""
    return _dynamic_identifier(value)


def dynamic_identifier_pattern(value: str) -> str:
    """把动态标识折叠为可复用前缀模式（``add_agenda_title-1789951623657`` → ``add_agenda_title-#``）。"""
    return re.sub(r"\d{6,}", "#", value)


__all__.extend(["dynamic_identifier_pattern", "is_dynamic_identifier"])


def _dynamic_text(value: str) -> bool:
    text = value.split("|", 1)[-1]
    return bool(
        len(text) > 80
        or re.search(r"\b\d{1,2}:\d{2}\b", text)
        or re.search(r"\b20\d{2}[-/]\d{1,2}[-/]\d{1,2}\b", text)
        or re.search(r"\b\d{4,}\b", text)
    )
