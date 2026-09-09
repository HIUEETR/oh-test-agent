"""将视觉模型观察结果按置信度和空间关系合并到设备快照。"""

from __future__ import annotations

import hashlib

from ..models import LocatorCandidate, LocatorKind, ScreenSnapshot, UIElement, VisionObservation


class PerceptionService:
    """以设备层级为基线，补充达到置信度阈值的视觉元素。"""

    def __init__(self, min_vision_confidence: float = 0.55):
        self.min_vision_confidence = min_vision_confidence

    def merge(self, snapshot: ScreenSnapshot, observation: VisionObservation | None) -> ScreenSnapshot:
        """原地合并视觉观察；越界或低置信度候选不会进入快照。"""
        if not observation:
            return snapshot
        snapshot.page_title = observation.page_title or snapshot.page_title
        snapshot.summary = observation.summary
        for item in observation.elements:
            if item.score < self.min_vision_confidence or not item.bbox.within(snapshot.width, snapshot.height):
                continue
            match = self._overlap_match(snapshot.elements, item.bbox.center)
            if match:
                match.score = max(match.score, item.score)
                if item.content and not match.content:
                    match.content = item.content
                match.metadata["vision_reason"] = item.reason
                continue
            identity = f"{item.content}|{item.type}|{item.bbox.model_dump_json()}"
            snapshot.elements.append(
                UIElement(
                    element_id="vlm-" + hashlib.sha1(identity.encode("utf-8")).hexdigest()[:12],
                    content=item.content,
                    type=item.type,
                    bbox=item.bbox,
                    clickable=item.clickable,
                    editable=item.editable,
                    score=item.score,
                    source="vlm",
                    locator_candidates=[
                        LocatorCandidate(
                            kind=LocatorKind.VLM_BBOX,
                            value=str(item.bbox.center),
                            score=item.score,
                        )
                    ],
                    metadata={"vision_reason": item.reason},
                )
            )
        return snapshot

    @staticmethod
    def _overlap_match(elements: list[UIElement], center: tuple[int, int]) -> UIElement | None:
        x, y = center
        candidates = [
            element
            for element in elements
            if element.bbox
            and element.bbox.left <= x <= element.bbox.right
            and element.bbox.top <= y <= element.bbox.bottom
        ]
        return min(candidates, key=lambda element: element.bbox.area if element.bbox else 0) if candidates else None
