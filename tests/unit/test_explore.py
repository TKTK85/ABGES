# -*- coding: utf-8 -*-
"""
test_explore.py — 表型数据探索模块单元测试
============================================
覆盖：

1. test_describe_extended_adds_iqr_and_outliers — 描述统计扩展（IQR / 超3SD / 超3Q）
2. test_grouped_extended_has_sd_and_cv         — 分组统计扩展为 N/Mean/SD/CV
3. test_normality_normal_sample                — 正态样本判 normal
4. test_normality_skewed_sample                — 偏态样本判 skewed
5. test_normality_large_sample_ignores_p       — 大样本不看 P 值（按矩判定）
6. test_collinearity_detects_severe            — 强共线产生 severe 告警
7. test_collinearity_clean_no_severe           — 独立变量无严重告警、VIF 低
8. test_degraded_without_scipy                 — scipy 缺失降级不报错（仅偏度/峰度）
9. test_build_exploration_block                — 汇总入口结构完整
"""

import os
import sys

# 本机 python -S 启动会跳过 site 初始化，需手动注入 venv 的 site-packages

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..",
                                "tools", "Python"))

import numpy as np
import pandas as pd

import abges_explore


# 统一用默认阈值（避免依赖运行目录的 yaml 加载）
CFG = abges_explore._DEFAULTS


def _rng(seed=42):
    return np.random.default_rng(seed)


# ------------------------------------------------------------------
# 1. 描述统计扩展
# ------------------------------------------------------------------
def test_describe_extended_adds_iqr_and_outliers():
    rng = _rng()
    values = rng.normal(1800, 200, 1000)
    # 追加一个极端离群（远超 3SD）
    series = pd.Series(np.append(values, [1800 + 5000]))

    desc = abges_explore.describe_extended(series, CFG)

    # 17 项基础指标仍在
    assert desc["Mean"] is not None and desc["SD"] is not None
    # 扩展项
    assert desc["IQR"] is not None and desc["IQR"] > 0
    assert desc["Outlier3SD"] >= 1, "应至少检出 1 个超3SD 离群"
    assert desc["Outlier3Q"] >= 1, "应至少检出 1 个超3Q 极端离群"
    # 描述统计扩展：IQR/Outlier3SD/Outlier3Q 均正确产出
# ------------------------------------------------------------------
# 2. 分组统计扩展
# ------------------------------------------------------------------
def test_grouped_extended_has_sd_and_cv():
    rng = _rng(7)
    frame = pd.DataFrame({
        "BW": np.append(rng.normal(1800, 200, 60), rng.normal(2400, 250, 60)),
        "Gen": ["G1"] * 60 + ["G2"] * 60,
    })
    out = abges_explore.describe_grouped_extended(frame, "BW", ["Gen"])

    assert set(out.keys()) == {"Gen"}
    for level in ("G1", "G2"):
        stat = out["Gen"][level]
        assert stat["N"] == 60
        assert stat["Mean"] is not None
        assert stat["SD"] is not None, "分组统计应含 SD"
        assert stat["CV"] is not None, "分组统计应含 CV"
    # 分组统计扩展：N/Mean/SD/CV 齐全
# ------------------------------------------------------------------
# 3. 正态样本
# ------------------------------------------------------------------
def test_normality_normal_sample():
    rng = _rng(11)
    res = abges_explore.normality(pd.Series(rng.normal(1800, 200, 3000)), CFG)

    assert abs(res["Skew"]) < 0.5, "正态样本偏度应接近 0，实为 %s" % res["Skew"]
    assert abs(res["Kurtosis"]) < 1.0
    assert res["Verdict"] == "normal", "正态样本应判 normal，实为 %s（%s）" % (
        res["Verdict"], res["Basis"])
    # 正态样本判定：verdict=normal
# ------------------------------------------------------------------
# 4. 偏态样本
# ------------------------------------------------------------------
def test_normality_skewed_sample():
    rng = _rng(13)
    # 指数分布右偏，理论偏度 = 2
    res = abges_explore.normality(pd.Series(rng.exponential(2.0, 3000)), CFG)

    assert res["Skew"] > 1.0, "指数分布偏度应 >1，实为 %s" % res["Skew"]
    assert res["Verdict"] == "skewed", "明显右偏应判 skewed，实为 %s" % res["Verdict"]
    assert res["Advice"]
    # 偏态样本判定：verdict=skewed（|偏度|>1）
# ------------------------------------------------------------------
# 5. 大样本不看 P 值（回归锚点：SW 在大样本几乎必然拒绝，但矩形态良好仍判正态）
# ------------------------------------------------------------------
def test_normality_large_sample_ignores_p():
    rng = _rng(17)
    # n=8000 > sw_max_n(5000)，即使 SW 的 P 极小，只要矩形态良好也应判 normal
    res = abges_explore.normality(pd.Series(rng.normal(0, 1, 8000)), CFG)

    assert res["N"] == 8000
    assert res["Verdict"] == "normal", "大样本应按矩判定为 normal，实为 %s" % res["Verdict"]
    assert "大样本" in res["Basis"], "判定依据应说明走大样本规则，实为 %s" % res["Basis"]
    # 大样本不再产出 SW 结果（避免误读）
    assert res["SWTest.P"] is None, "大样本(n>5000)不应计算 SW 检验"
    # 大样本判定：不看 P 值，按偏度/峰度判 normal
# ------------------------------------------------------------------
# 6. 共线性：强相关
# ------------------------------------------------------------------
def test_collinearity_detects_severe():
    rng = _rng(23)
    base = rng.normal(0, 1, 600)
    frame = pd.DataFrame({
        "A": base,
        "B": base + rng.normal(0, 0.001, 600),   # 与 A 近乎恒等
        "C": rng.normal(0, 1, 600),
    })
    res = abges_explore.collinearity(frame, ["A", "B", "C"], CFG)

    assert res["VIF"], "应产出 VIF"
    severe = [w for w in res["Warnings"] if w["level"] == "severe"]
    assert severe, "近乎恒等的两列必须产生 severe 告警，实为 %s" % res["Warnings"]
    max_vif = max(v for v in res["VIF"].values() if v != float("inf"))
    assert max_vif > 10 or any(v == float("inf") for v in res["VIF"].values()), \
        "共线变量 VIF 应远超 10，实为 %s" % res["VIF"]
    assert res["Action"] == "warn", "默认行为应为仅告警"
    # 共线性检出：VIF 超标并产生 severe 告警（action=warn）
# ------------------------------------------------------------------
# 7. 共线性：独立变量
# ------------------------------------------------------------------
def test_collinearity_clean_no_severe():
    rng = _rng(29)
    frame = pd.DataFrame({k: rng.normal(0, 1, 600) for k in ("X", "Y", "Z")})
    res = abges_explore.collinearity(frame, ["X", "Y", "Z"], CFG)

    severe = [w for w in res["Warnings"] if w["level"] == "severe"]
    assert not severe, "独立变量不应有 severe 告警，实为 %s" % severe
    assert all(v < 5 for v in res["VIF"].values()), \
        "独立变量 VIF 应 <5，实为 %s" % res["VIF"]
    # 独立变量：无严重告警，VIF 均 <5
# ------------------------------------------------------------------
# 8. scipy 缺失降级
# ------------------------------------------------------------------
def test_degraded_without_scipy():
    rng = _rng(31)
    original = abges_explore._scipy_stats
    abges_explore._scipy_stats = lambda: None
    try:
        res = abges_explore.normality(pd.Series(rng.normal(1800, 200, 1000)), CFG)
    finally:
        abges_explore._scipy_stats = original

    # scipy 缺失不报错：偏度/峰度仍由 numpy 算出，且给出降级说明
    assert res["Skew"] is not None, "scipy 缺失时偏度仍应由 numpy 算出"
    assert res["Kurtosis"] is not None
    assert res["Degraded"] and "scipy" in res["Degraded"], \
        "应记录 scipy 缺失降级原因，实为 %s" % res["Degraded"]
    assert res["SWTest.P"] is None and res["KsTest.P"] is None
    # 降级后仍须给出可用判定（不能因 scipy 缺失而 Verdict 为空）
    assert res["Verdict"] in ("normal", "mild", "skewed", "insufficient")


# ------------------------------------------------------------------
# 9. 汇总入口
# ------------------------------------------------------------------
def test_build_exploration_block():
    rng = _rng(37)
    frame = pd.DataFrame({
        "ID": ["A%04d" % i for i in range(600)],
        "BW": rng.normal(1800, 200, 600),
        "EN": rng.normal(120, 15, 600),
        "Gen": ["G1"] * 300 + ["G2"] * 300,
        "Sex": ["M", "F"] * 300,
    })
    out = abges_explore.build_exploration(
        frame, traits=["BW", "EN"], config=CFG, group_columns=["Gen", "Sex"])

    assert out["Enabled"] is True
    assert set(out["Descriptors"].keys()) == {"BW", "EN"}
    assert set(out["Normality"].keys()) == {"BW", "EN"}
    assert "Correlation" in out["Collinearity"]
    assert "VIF" in out["Collinearity"]
    assert "ConditionNumber" in out["Collinearity"]
    assert isinstance(out["Warnings"], list)
    assert isinstance(out["Degraded"], list)
    # 分类固定效应须真实进入设计矩阵（pandas>=2.0 字符串列 StringDtype 易被漏判）
    assert any(str(k).startswith(("Gen_", "Sex_")) for k in out["Collinearity"]["VIF"]), \
        "分类列未进入设计矩阵：%s" % list(out["Collinearity"]["VIF"].keys())
    assert set(out["Groups"].keys()) == {"BW", "EN"}


if __name__ == "__main__":
    for name in sorted(k for k in globals() if k.startswith("test_")):
        print("%s -> %s" % (name, globals()[name]()))
    print("ALL PASS")
