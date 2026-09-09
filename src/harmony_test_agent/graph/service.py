from __future__ import annotations

import hashlib
import re
import uuid

from PIL import Image

from ..models import PageEdge, PageGraph, PageNode, ScreenSnapshot, ToolName


class PageGraphBuilder:
    def __init__(self, graph: PageGraph | None = None):
        self.graph = graph or PageGraph()

    def signature(self, snapshot: ScreenSnapshot) -> str:
        keys: list[str] = []
        for element in snapshot.elements:
            value = element.key or element.id
            if not value:
                continue
            value = re.sub(r"_\d{8,}$", "_*", value)
            if value not in keys:
                keys.append(value)
        keys.sort()
        text_tokens: list[str] = []
        for element in snapshot.elements:
            token = self._normalize_text(element.content or element.description)
            if token and token not in text_tokens:
                text_tokens.append(token)
        summary = self._normalize_text(snapshot.summary)
        raw = "|".join(
            [
                snapshot.page_path,
                ",".join(keys[:40]),
                ",".join(text_tokens[:20]),
                summary[:240],
                self._average_hash(snapshot),
            ]
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def add_snapshot(self, snapshot: ScreenSnapshot) -> tuple[PageNode, bool]:
        signature = self.signature(snapshot)
        for node in self.graph.nodes:
            if node.signature == signature:
                return node, False
        node = PageNode(
            node_id=f"page-{uuid.uuid4().hex[:10]}",
            signature=signature,
            page_path=snapshot.page_path,
            title=snapshot.page_title or snapshot.summary or snapshot.page_path,
            snapshot_id=snapshot.snapshot_id,
            image_path=snapshot.image_path,
            discovered_order=len(self.graph.nodes) + 1,
            element_count=len(snapshot.elements),
        )
        self.graph.nodes.append(node)
        return node, True

    def add_edge(
        self,
        source: PageNode,
        target: PageNode,
        action: ToolName,
        target_description: str = "",
    ) -> PageEdge | None:
        if source.node_id == target.node_id:
            return None
        for edge in self.graph.edges:
            if edge.source == source.node_id and edge.target == target.node_id and edge.action == action:
                return edge
        edge = PageEdge(
            edge_id=f"edge-{uuid.uuid4().hex[:10]}",
            source=source.node_id,
            target=target.node_id,
            action=action,
            target_description=target_description,
        )
        self.graph.edges.append(edge)
        return edge

    @staticmethod
    def _normalize_text(value: str) -> str:
        normalized = " ".join(value.casefold().split())
        return re.sub(r"\d+", "#", normalized)[:120]

    @staticmethod
    def _average_hash(snapshot: ScreenSnapshot) -> str:
        try:
            with Image.open(snapshot.image_path) as image:
                gray = image.convert("L").resize((8, 8))
                if hasattr(gray, "get_flattened_data"):
                    pixels = list(gray.get_flattened_data())
                else:
                    pixels = list(gray.getdata())
            average = sum(pixels) / len(pixels)
            bits = "".join("1" if pixel >= average else "0" for pixel in pixels)
            return f"{int(bits, 2):016x}"
        except Exception:
            return snapshot.image_sha256[:16]
