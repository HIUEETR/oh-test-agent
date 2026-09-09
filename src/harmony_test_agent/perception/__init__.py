"""UI 层级归一化、元素定位与视觉结果融合能力的公共导出。"""

from .normalizer import find_element, normalize_layout, target_variants
from .service import PerceptionService

__all__ = ["PerceptionService", "find_element", "normalize_layout", "target_variants"]
