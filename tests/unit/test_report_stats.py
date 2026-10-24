# -*- coding: utf-8 -*-
"""runners/report_stats.py 单元测试（P2-3 拆分后新增的可测点）。

拆分前这些统计判据硬编码在 docx_report.render_run_docx 的内部，
要改一个阈值必须动 1,400 行的渲染主体。抽出后此处逐条锁死：
  * 阈值默认值与 configs/qc/phenotype-qc.yaml 的 report_thresholds 一致；
  * 各判据的**分支**与**文案**都与迁移前的历史输出逐字一致；
  * 配置缺失/非法时静默回退默认值，绝不影响报告生成。
"""
import sys
import os
from pathlib import Path

ROOT = str(Path(__file__).resolve().parents[2])
for _p in (ROOT, os.path.join(ROOT, 'tools', 'Python'),
           os.path.join(ROOT, 'harness')):
    if _p not in sys.path:
        sys.path.insert(0, _p)
os.chdir(ROOT)

import pytest  # noqa: E402

from runners import report_stats as rs  # noqa: E402


# ----------------------------------------------------------------------
# 阈值加载：配置与默认值必须同值（否则等于悄悄改了报告结论）
# ----------------------------------------------------------------------
def test_config_matches_defaults_when_present():
    current = rs.reload_thresholds()
    drift = {k: (v, current.get(k)) for k, v in rs.DEFAULTS.items()
             if v != current.get(k)}
    assert drift == {}, "配置与默认值出现漂移：%s" % drift


def test_missing_config_falls_back_to_defaults(tmp_path):
    merged = rs.load_thresholds(str(tmp_path / "nonexistent.yaml"))
    assert merged == rs.DEFAULTS


@pytest.mark.parametrize("bad_yaml", [
    "report_thresholds: 不是字典\n",
    "::: 非法 YAML [[[\n",
    "",
])
def test_broken_config_falls_back_to_defaults(tmp_path, bad_yaml):
    path = tmp_path / "bad.yaml"
    path.write_text(bad_yaml, encoding="utf-8")
    assert rs.load_thresholds(str(path)) == rs.DEFAULTS


def test_config_overrides_defaults(tmp_path):
    path = tmp_path / "override.yaml"
    path.write_text("report_thresholds:\n  alpha: 0.10\n  h2_low: 0.20\n",
                    encoding="utf-8")
    merged = rs.load_thresholds(str(path))
    assert merged["alpha"] == 0.10
    assert merged["h2_low"] == 0.20
    # 未覆盖项保持默认
    assert merged["alpha_strong"] == rs.DEFAULTS["alpha_strong"]


def test_unknown_threshold_name_raises():
    with pytest.raises(KeyError):
        rs.get("this_threshold_does_not_exist")


# ----------------------------------------------------------------------
# P 值判定与显示（文案与历史实现逐字一致）
# ----------------------------------------------------------------------
@pytest.mark.parametrize("value, expected", [
    (0.0005, "极显著（P<0.001），必须校正"),
    (0.001, "显著（P<0.05），需校正"),       # 边界：等于 alpha_strong 不算极显著
    (0.049, "显著（P<0.05），需校正"),
    (0.05, "不显著（P≥0.05）"),              # 边界：等于 alpha 不算显著
    (0.9, "不显著（P≥0.05）"),
    (None, "—"),
    ("abc", "—"),
])
def test_p_sig(value, expected):
    assert rs.p_sig(value) == expected


@pytest.mark.parametrize("value, expected", [
    (0.00005, "<0.0001"),
    (0.0005, "<0.001"),
    (0.0234, "0.023"),
    (None, "—"),
])
def test_fmt_p(value, expected):
    assert rs.fmt_p(value) == expected


@pytest.mark.parametrize("value, expected", [
    (0.00005, "P<0.0001"),
    (0.0234, "P=0.023"),
])
def test_p_label(value, expected):
    assert rs.p_label(value) == expected


def test_is_significant():
    assert rs.is_significant(0.01) is True
    assert rs.is_significant(0.5) is False
    assert rs.is_significant(None) is False
    assert rs.is_significant("x") is False


# ----------------------------------------------------------------------
# 遗传力分档
# ----------------------------------------------------------------------
def test_h2_band_low():
    text = rs.h2_band(0.05)
    assert text.startswith("低遗传力（h²<0.10")
    assert "宜结合家系信息选择" in text


def test_h2_band_moderate():
    text = rs.h2_band(0.2)
    assert text.startswith("中等遗传力（0.10≤h²<0.30")


def test_h2_band_high_and_invalid():
    assert rs.h2_band(0.5).startswith("中高遗传力（h²≥0.30")
    assert rs.h2_band(None) is None
    assert rs.h2_band("n/a") is None


# ----------------------------------------------------------------------
# 残差形态
# ----------------------------------------------------------------------
@pytest.mark.parametrize("value, expected", [
    (0.2, "近似对称"),
    (-0.2, "近似对称"),
    (0.7, "轻度右偏"),
    (-0.7, "轻度左偏"),
    (1.5, "明显右偏"),
    (-1.5, "明显左偏"),
    (None, None),
])
def test_describe_skew(value, expected):
    assert rs.describe_skew(value) == expected


@pytest.mark.parametrize("value, expected", [
    (0.2, "峰度接近正态"),
    (0.9, "尾部相对正态偏厚/偏尖"),
    (None, None),
])
def test_describe_kurt(value, expected):
    assert rs.describe_kurt(value) == expected


def test_mean_is_zero():
    assert rs.mean_is_zero(0.0, 2.0) is True
    assert rs.mean_is_zero(0.0005, 2.0) is True     # 0.0005 < 0.001*2
    assert rs.mean_is_zero(0.5, 2.0) is False
    assert rs.mean_is_zero(None, 2.0) is False


def test_normality_verdict_strict():
    text, need = rs.normality_verdict(0.2, 0.3, n=1000)
    assert text == "残差近似对称、峰度接近正态，正态性假设成立"
    assert need is False


def test_normality_verdict_loose_does_not_raise_action():
    """轻度偏离在大样本下可接受 —— 与历史口径一致，不触发"建议核查"。"""
    text, need = rs.normality_verdict(0.8, 1.5, n=3000)
    assert "轻度偏离正态" in text and "大样本（n=3000）下可接受" in text
    assert need is False


def test_normality_verdict_obvious_raises_action():
    text, need = rs.normality_verdict(1.5, 3.0, n=3000)
    assert "明显偏离正态" in text
    assert need is True


def test_normality_verdict_without_shape_data():
    text, need = rs.normality_verdict(None, None, n=100)
    assert text == ""
    assert need is False


@pytest.mark.parametrize("gain, coef, keyword, need", [
    (0.005, 1.0, "无明显系统弯曲", False),
    (0.03, -1.0, "轻度 倒 U 型", False),
    (0.08, 1.0, "存在明显 U 型", True),
])
def test_quadratic_verdict(gain, coef, keyword, need):
    text, action = rs.quadratic_verdict(gain, coef)
    assert keyword in text
    assert action is need


def test_quadratic_verdict_without_gain():
    assert rs.quadratic_verdict(None, 1.0) == ("", False)


def test_trend_significance_text():
    assert rs.trend_significance_text(0.0001) == "遗传进展在统计上极显著（P<0.001）"
    assert rs.trend_significance_text(0.01) == "遗传进展在统计上显著（P<0.05）"
    assert rs.trend_significance_text(0.5) == "遗传进展在统计上不显著（P≥0.05）"
    assert rs.trend_significance_text(None) == "遗传进展在统计上不显著（P≥0.05）"


# ----------------------------------------------------------------------
# 枚举 -> 中文
# ----------------------------------------------------------------------
def test_verdict_cn_and_status_cn():
    assert rs.verdict_cn("pass") == "通过"
    assert rs.verdict_cn("blocked") == "阻断"
    assert rs.verdict_cn(None) == "—"
    assert rs.verdict_cn("some_unknown") == "some_unknown"
    assert rs.status_cn("partial") == "部分完成（存在告警或跳过项）"
    assert rs.status_cn(None) == "—"


def test_resolve_model():
    key, info = rs.resolve_model({"model_id": "pblup"}, "PBLUP")
    assert key == "pblup"
    assert info[2] is True and info[3] is False      # 用系谱、不用基因型
    key, _ = rs.resolve_model({}, "SSGBLUP")
    assert key == "ssblup"
    key, _ = rs.resolve_model({}, "GBLUP")
    assert key == "gblup"


# ----------------------------------------------------------------------
# fnum：不可转一律 None（不用 0 冒充，避免伪造结论）
# ----------------------------------------------------------------------
@pytest.mark.parametrize("value, expected", [
    ("1.5", 1.5), (3, 3.0), (None, None), ("abc", None), ("", None),
])
def test_fnum(value, expected):
    assert rs.fnum(value) == expected
