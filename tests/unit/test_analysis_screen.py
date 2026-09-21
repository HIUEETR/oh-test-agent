"""``analysis/screen.py`` 单测：白屏 / 黑屏 / 噪声 / σ 与 blank_ratio 边界 / 同帧长度。"""

from __future__ import annotations

import random
from pathlib import Path

from PIL import Image

from harmony_test_agent.analysis.screen import (
    ScreenThresholds,
    detect_blank_screen,
    detect_identical_frames,
    screen_metrics,
)


def _flat(path: Path, value: int, size: tuple[int, int] = (300, 300)) -> Path:
    """生成纯色 PNG。"""
    Image.new("L", size, value).save(path, format="PNG")
    return path


def _pattern(path: Path, base: int, outlier: int, *, modulus: int, keep: int) -> Path:
    """生成 ``base`` 底色 + 每 ``modulus`` 个像素里前 ``keep`` 个替换成 ``outlier`` 的 PNG。"""
    width = height = 300
    image = Image.new("L", (width, height), base)
    image.putdata([outlier if index % modulus < keep else base for index in range(width * height)])
    image.save(path, format="PNG")
    return path


def test_pure_white_is_light_blank(tmp_path):
    path = _flat(tmp_path / "white.png", 255)
    finding = detect_blank_screen(path)
    assert finding is not None
    assert finding.kind.value == "white_screen"
    assert finding.severity == "warning"
    assert finding.source == "screenshot"
    assert finding.evidence["blank_kind"] == "light"
    assert finding.evidence["mean"] == 255.0
    assert finding.evidence["stddev"] == 0.0
    assert finding.evidence["blank_ratio"] == 1.0
    assert finding.evidence["image"] == "white.png"
    assert finding.evidence["thresholds"]["max_stddev"] == 8.0


def test_pure_black_is_dark_blank(tmp_path):
    finding = detect_blank_screen(_flat(tmp_path / "black.png", 0))
    assert finding is not None
    assert finding.evidence["blank_kind"] == "dark"
    assert finding.evidence["mean"] == 0.0


def test_flat_mid_gray_is_uniform_blank(tmp_path):
    finding = detect_blank_screen(_flat(tmp_path / "gray.png", 128))
    assert finding is not None
    assert finding.evidence["blank_kind"] == "uniform"


def test_noise_is_not_blank(tmp_path):
    rng = random.Random(20260210)
    image = Image.new("L", (200, 200))
    image.putdata([rng.randrange(256) for _ in range(200 * 200)])
    path = tmp_path / "noise.png"
    image.save(path, format="PNG")
    metrics = screen_metrics(path)
    assert metrics["stddev"] > 50
    assert detect_blank_screen(path) is None


def test_stddev_boundary_gate(tmp_path):
    under = _pattern(tmp_path / "sigma-under.png", 128, 178, modulus=50, keep=1)
    over = _pattern(tmp_path / "sigma-over.png", 128, 188, modulus=50, keep=1)
    under_metrics = screen_metrics(under)
    over_metrics = screen_metrics(over)
    assert under_metrics["blank_ratio"] == over_metrics["blank_ratio"] == 0.98
    assert under_metrics["stddev"] < 8.0 < over_metrics["stddev"]
    assert detect_blank_screen(under) is not None
    assert detect_blank_screen(over) is None


def test_blank_ratio_boundary_gate(tmp_path):
    at_gate = _pattern(tmp_path / "ratio-at.png", 200, 240, modulus=100, keep=3)
    below_gate = _pattern(tmp_path / "ratio-below.png", 200, 240, modulus=200, keep=7)
    at_metrics = screen_metrics(at_gate)
    below_metrics = screen_metrics(below_gate)
    assert at_metrics["blank_ratio"] == 0.97
    assert below_metrics["blank_ratio"] == 0.965
    # 两种情况的 σ 都在 8.0 之下，唯一被挡下的是 blank_ratio 门。
    assert at_metrics["stddev"] < 8.0
    assert below_metrics["stddev"] < 8.0
    finding = detect_blank_screen(at_gate)
    assert finding is not None
    assert finding.evidence["blank_kind"] == "light"
    assert detect_blank_screen(below_gate) is None


def test_thresholds_are_configurable(tmp_path):
    path = _flat(tmp_path / "white.png", 255)
    assert detect_blank_screen(path, ScreenThresholds(min_blank_ratio=1.01)) is None
    raised = detect_blank_screen(path, ScreenThresholds(light_mean_min=300.0))
    assert raised is not None
    assert raised.evidence["blank_kind"] == "uniform"
    dark = detect_blank_screen(path, ScreenThresholds(light_mean_min=300.0, dark_mean_max=400.0))
    assert dark is not None
    assert dark.evidence["blank_kind"] == "dark"


def test_screen_metrics_reports_downsampled_region(tmp_path):
    metrics = screen_metrics(_flat(tmp_path / "big.png", 200, size=(1080, 2400)))
    # thumbnail 到最长边 360 → 162x360，再裁掉顶部 4% / 底部 6% 行。
    assert metrics["width"] == 162
    assert metrics["height"] == 325
    assert metrics["sampled_pixels"] == 162 * 325


def test_detect_identical_frames_counts_trailing_run(tmp_path):
    white = _flat(tmp_path / "a.png", 255)
    white2 = _flat(tmp_path / "b.png", 255)
    white3 = _flat(tmp_path / "c.png", 255)
    noise = _pattern(tmp_path / "noise.png", 0, 255, modulus=2, keep=1)
    assert detect_identical_frames([noise, white, white2, white3])[0] == 3
    assert detect_identical_frames([white, noise, white2])[0] == 1
    assert detect_identical_frames([white, white2])[0] == 2
    assert detect_identical_frames([]) == (0, [])


def test_detect_identical_frames_returned_digests_match_tail(tmp_path):
    white = _flat(tmp_path / "a.png", 255)
    white2 = _flat(tmp_path / "b.png", 255)
    noise = _pattern(tmp_path / "noise.png", 0, 255, modulus=2, keep=1)
    run, digests = detect_identical_frames([noise, white, white2])
    assert run == 2
    assert len(digests) == 2
    assert digests[0] == digests[1]
    assert all(len(digest) == 64 for digest in digests)


def test_detect_identical_frames_recode_fallback(tmp_path):
    near_a = _flat(tmp_path / "near-a.png", 255)
    near_b = _flat(tmp_path / "near-b.png", 254)
    assert near_a.read_bytes() != near_b.read_bytes()
    assert detect_identical_frames([near_a, near_b])[0] == 2
    far = _flat(tmp_path / "far.png", 200)
    assert detect_identical_frames([near_a, far])[0] == 1


def test_detect_identical_frames_tolerates_missing_file(tmp_path):
    white = _flat(tmp_path / "a.png", 255)
    missing = tmp_path / "missing.png"
    assert detect_identical_frames([white, missing, white])[0] == 1
