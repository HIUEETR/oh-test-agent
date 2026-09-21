"""白屏 / 黑屏 / 同帧检测（基于 PIL 的轻量像素统计）。

判定算法（阈值必须可调，全部集中在 :class:`ScreenThresholds`）：

1. 转灰度 → 最长边缩到 ``max_side`` → 裁掉顶部 ``crop_top_ratio`` 与底部 ``crop_bottom_ratio``；
2. ``mean`` = 均值，``stddev`` = 总体标准差，
   ``blank_ratio`` = ``|p - mean| <= band`` 的像素占比；
3. **空白门**：``stddev < max_stddev 且 blank_ratio >= min_blank_ratio``；
   ``mean >= light_mean_min`` → ``blank_kind="light"``（白屏），
   ``mean <= dark_mean_max`` → ``"dark"``（黑屏），否则 ``"uniform"``。

深色主题与媒体密集页面会带来朴素白屏判定的误报，因此这里用 **σ + blank_ratio + mean
三重门**，并且分析结果永远是建议性的（不翻转 ``passed``）。
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import asdict, dataclass
from pathlib import Path

from PIL import Image

from ..models import AnomalyFinding, AnomalyKind

logger = logging.getLogger(__name__)

#: 重编码帧回退比较：平均绝对差 / 255 的允许上限。
MAX_RECODED_DIFF_RATIO = 0.005
#: 回退比较时的重采样边长。
RECODE_COMPARE_SIDE = 64


@dataclass(frozen=True)
class ScreenThresholds:
    """白屏 / 空白判定阈值。"""

    max_stddev: float = 8.0
    min_blank_ratio: float = 0.97
    light_mean_min: float = 200.0
    dark_mean_max: float = 40.0
    band: int = 6
    max_side: int = 360
    crop_top_ratio: float = 0.04
    crop_bottom_ratio: float = 0.06


#: 共享的默认阈值实例（冻结 dataclass，可安全复用）；同时满足 ruff B008。
DEFAULT_SCREEN_THRESHOLDS = ScreenThresholds()


def _crop_rows(height: int, thresholds: ScreenThresholds) -> tuple[int, int]:
    """计算有效像素行区间；裁剪后为空时退回全图。"""
    top = int(height * thresholds.crop_top_ratio)
    bottom = height - int(height * thresholds.crop_bottom_ratio)
    if bottom <= top:
        return 0, height
    return top, bottom


def screen_metrics(path: Path, thresholds: ScreenThresholds = DEFAULT_SCREEN_THRESHOLDS) -> dict:
    """返回降采样 + 裁剪后的灰度统计：mean / stddev / blank_ratio / width / height。"""
    with Image.open(path) as image:
        gray = image.convert("L")
        gray.thumbnail((thresholds.max_side, thresholds.max_side))
        width, height = gray.size
        top, bottom = _crop_rows(height, thresholds)
        region = gray.crop((0, top, width, bottom))
        values = list(region.tobytes())
    count = len(values)
    if count == 0:
        return {
            "mean": 0.0,
            "stddev": 0.0,
            "blank_ratio": 0.0,
            "width": region.width,
            "height": region.height,
            "sampled_pixels": 0,
        }
    mean = sum(values) / count
    variance = sum((value - mean) ** 2 for value in values) / count
    blank = sum(1 for value in values if abs(value - mean) <= thresholds.band)
    return {
        "mean": round(mean, 6),
        "stddev": round(variance**0.5, 6),
        "blank_ratio": round(blank / count, 6),
        "width": region.width,
        "height": region.height,
        "sampled_pixels": count,
    }


def _blank_kind(mean: float, thresholds: ScreenThresholds) -> str:
    """按均值把空白屏细分为 light / dark / uniform。"""
    if mean >= thresholds.light_mean_min:
        return "light"
    if mean <= thresholds.dark_mean_max:
        return "dark"
    return "uniform"


_BLANK_SUMMARY = {
    "light": "检测到白屏（整屏亮度均匀且偏亮）",
    "dark": "检测到黑屏（整屏亮度均匀且偏暗）",
    "uniform": "检测到空白屏（整屏像素均匀，疑似渲染缺失）",
}


def detect_blank_screen(path: Path, thresholds: ScreenThresholds = DEFAULT_SCREEN_THRESHOLDS) -> AnomalyFinding | None:
    """对单张截图做白屏判定；未过门时返回 ``None``。

    这里固定返回 ``warning``：当同一次执行里另有崩溃 / 卡死 finding 或硬 checkpoint
    失败佐证时，由 ``ExecutionAnalyzer`` 提升为 ``critical``。
    """
    metrics = screen_metrics(path, thresholds)
    if metrics["stddev"] >= thresholds.max_stddev or metrics["blank_ratio"] < thresholds.min_blank_ratio:
        return None
    blank_kind = _blank_kind(metrics["mean"], thresholds)
    return AnomalyFinding(
        kind=AnomalyKind.WHITE_SCREEN,
        severity="warning",
        summary_zh=_BLANK_SUMMARY[blank_kind],
        detail=(
            f"{path.name}: mean={metrics['mean']}, stddev={metrics['stddev']}, "
            f"blank_ratio={metrics['blank_ratio']}, kind={blank_kind}"
        ),
        source="screenshot",
        evidence={
            "image": path.name,
            "image_path": path.as_posix(),
            "blank_kind": blank_kind,
            "mean": metrics["mean"],
            "stddev": metrics["stddev"],
            "blank_ratio": metrics["blank_ratio"],
            "sampled_pixels": metrics["sampled_pixels"],
            "thresholds": asdict(thresholds),
        },
    )


def _sha256(path: Path) -> str | None:
    """计算文件字节摘要；读取失败返回 None。"""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        logger.debug("cannot hash frame %s: %s", path, exc)
        return None


def _frames_equal(left: Path, right: Path, left_digest: str | None, right_digest: str | None) -> bool:
    """先比字节摘要；摘要不同（含重编码）时回退到平均绝对差比较。"""
    if left_digest and right_digest and left_digest == right_digest:
        return True
    if not left_digest or not right_digest:
        return False
    try:
        with Image.open(left) as left_image, Image.open(right) as right_image:
            left_gray = left_image.convert("L")
            right_gray = right_image.convert("L")
            if left_gray.size != right_gray.size:
                right_gray = right_gray.resize(left_gray.size)
            left_gray.thumbnail((RECODE_COMPARE_SIDE, RECODE_COMPARE_SIDE))
            right_gray.thumbnail((RECODE_COMPARE_SIDE, RECODE_COMPARE_SIDE))
            left_values = list(left_gray.tobytes())
            right_values = list(right_gray.tobytes())
    except (OSError, ValueError) as exc:
        logger.debug("cannot compare frames %s / %s: %s", left, right, exc)
        return False
    count = min(len(left_values), len(right_values))
    if count == 0:
        return False
    total = sum(abs(left_values[index] - right_values[index]) for index in range(count))
    return (total / count) / 255.0 < MAX_RECODED_DIFF_RATIO


def detect_identical_frames(paths: list[Path]) -> tuple[int, list[str]]:
    """返回**末尾**连续相同帧的长度，以及该尾段各帧的 SHA256。

    末段长度为 1 表示最后一帧与其前一帧不同（单帧自成一"段"）；空输入返回 ``(0, [])``；
    读取失败的帧视为与前一帧不同（保守地截断尾段）。
    """
    if not paths:
        return 0, []
    digests = [_sha256(path) for path in paths]
    run = 1
    for index in range(len(paths) - 1, 0, -1):
        if _frames_equal(paths[index - 1], paths[index], digests[index - 1], digests[index]):
            run += 1
        else:
            break
    tail = digests[len(paths) - run :]
    return run, [digest or "" for digest in tail]


__all__ = [
    "DEFAULT_SCREEN_THRESHOLDS",
    "ScreenThresholds",
    "detect_blank_screen",
    "detect_identical_frames",
    "screen_metrics",
]
