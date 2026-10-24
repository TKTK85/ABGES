# -*- coding: utf-8 -*-
"""
test_effect_screening.py — 数据驱动效应筛选模块单元测试
=====================================================
覆盖（与 docs/ABGES_数据驱动效应筛选方案_v1.0.md 六条判据对齐）：

1. test_strong_fixed_effect_keep_and_strong   — 强效应列判 keep 且 strong=True
2. test_noise_fixed_effect_dropped            — 无解释力列判 drop
3. test_single_level_fixed_effect_dropped     — 单水平列判 drop（不可估）
4. test_rare_level_fixed_effect_review        — 含稀有水平的列判 review（不自动剔除）
5. test_weak_covariate_dropped               — 协变量弱相关判 drop
6. test_category_dependency_choose_one       — 一一映射列判 choose_one（二选一）
7. test_degraded_without_scipy               — scipy 缺失降级不阻断（仅按 η² 判定）
8. test_no_applied_keys_emitted              — build 只诊断、不产出 Applied/AppliedChanges
"""

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..",
                                "tools", "Python"))

import numpy as np
import pandas as pd

import abges_effects
from abges_effects import (_DEFAULTS, screen_candidate, build_effect_screening)


CFG = dict(_DEFAULTS)


def _rng(seed=42):
    return np.random.default_rng(seed)


# ------------------------------------------------------------------
# 1. 强效应：keep + strong
# ------------------------------------------------------------------
def test_strong_fixed_effect_keep_and_strong():
    rng = _rng(1)
    n = 1200
    gen = rng.integers(0, 3, n)
    trait = gen * 8.0 + rng.normal(0, 1.0, n)   # 组间差 8、组内 sd=1 -> η² 极高
    frame = pd.DataFrame({"Trait": trait, "Gen": gen.astype(str)})
    res = build_effect_screening(frame, ["Trait"],
                                fixed_columns=["Gen"], config=CFG)

    assert res["Enabled"] is True
    cand = {c["column"]: c for c in res["Candidates"]}
    assert cand["Gen"]["verdict"] == "keep", cand["Gen"]
    assert cand["Gen"]["strong"] is True, "强效应应标记 strong"
    eta2 = (cand["Gen"]["evidence"] or {}).get("eta_squared")
    assert eta2 is not None and eta2 > CFG["eta_squared_strong"], \
        "强效应 η² 应超过强效应阈值 0.05，实为 %s" % eta2
    # 建议清单应包含 Gen
    assert "Gen" in res["SuggestedFixedEffects"]
    # 强效应：keep 且 strong=True，η²>0.05


# ------------------------------------------------------------------
# 2. 噪声列：drop
# ------------------------------------------------------------------
def test_noise_fixed_effect_dropped():
    rng = _rng(2)
    n = 1200
    gen = rng.integers(0, 3, n)          # 与性状无关的随机分组
    trait = rng.normal(0, 1, n)          # 独立噪声
    frame = pd.DataFrame({"Trait": trait, "Noise": gen.astype(str)})
    res = build_effect_screening(frame, ["Trait"],
                                fixed_columns=["Noise"], config=CFG)

    cand = {c["column"]: c for c in res["Candidates"]}
    assert cand["Noise"]["verdict"] == "drop", cand["Noise"]
    assert "Noise" in res["Recommendations"]["drop"]
    # 无解释力列：η² 低即 drop（P 仅展示，不参与判定）
    # drop 列不得进入建议清单
    assert "Noise" not in res["SuggestedFixedEffects"]


# ------------------------------------------------------------------
# 3. 单水平列：drop（设计矩阵不可估，与 PH005 同判据）
# ------------------------------------------------------------------
def test_single_level_fixed_effect_dropped():
    rng = _rng(3)
    frame = pd.DataFrame({"Trait": rng.normal(0, 1, 500),
                          "Single": ["X"] * 500})
    res = build_effect_screening(frame, ["Trait"],
                                fixed_columns=["Single"], config=CFG)

    cand = {c["column"]: c for c in res["Candidates"]}
    assert cand["Single"]["verdict"] == "drop", cand["Single"]
    assert "Single" in res["Recommendations"]["drop"]
    # 单水平不可估：drop（与 PH005 同口径）
    # 理由中应点明水平数不足
    assert any("水平数" in r for r in cand["Single"]["reasons"]), \
        cand["Single"]["reasons"]


# ------------------------------------------------------------------
# 4. 稀有水平：review（不自动剔除，须人工确认）
# ------------------------------------------------------------------
def test_rare_level_fixed_effect_review():
    rng = _rng(4)
    n = 500
    # B 水平仅 3 个样本 < min_level_n(5)，触发 review
    grp = (["A"] * (n - 3)) + (["B"] * 3)
    rng.shuffle(grp)
    trait = np.where(np.array(grp) == "A", 5.0, 5.0) + rng.normal(0, 1, n)
    frame = pd.DataFrame({"Trait": trait, "Rare": grp})
    res = build_effect_screening(frame, ["Trait"],
                                fixed_columns=["Rare"], config=CFG)

    cand = {c["column"]: c for c in res["Candidates"]}
    assert cand["Rare"]["verdict"] == "review", cand["Rare"]
    assert "Rare" in res["Recommendations"]["review"]
    # 稀有水平：review（不自动剔除，避免据失真 η² 误杀）
    # review 列仍保留在建议清单（仅提示，不剔除）
    assert "Rare" in res["SuggestedFixedEffects"]


# ------------------------------------------------------------------
# 5. 协变量弱相关：drop
# ------------------------------------------------------------------
def test_weak_covariate_dropped():
    rng = _rng(5)
    n = 1200
    cov = rng.normal(0, 1, n)
    trait = rng.normal(0, 1, n)          # 与 cov 独立
    frame = pd.DataFrame({"Trait": trait, "Cov": cov})
    res = build_effect_screening(frame, ["Trait"],
                                covariate_columns=["Cov"], config=CFG)

    cand = {c["column"]: c for c in res["Candidates"]}
    assert cand["Cov"]["verdict"] == "drop", cand["Cov"]
    r = (cand["Cov"]["evidence"] or {}).get("corr")
    assert r is not None and abs(r) < CFG["covariate_corr_min"], \
        "协变量 |r| 应 < 0.05，实为 %s" % r
    # 协变量弱相关：|r|<0.05 -> drop
    assert "Cov" not in res["SuggestedCovariates"]


# ------------------------------------------------------------------
# 6. 一一映射（共线性）：choose_one（二选一）
# ------------------------------------------------------------------
def test_category_dependency_choose_one():
    rng = _rng(6)
    n = 600
    a = rng.integers(0, 2, n).astype(str)
    b = a.copy()                          # A 与 B 一一对应
    frame = pd.DataFrame({"Trait": rng.normal(0, 1, n), "A": a, "B": b})
    # 共线性证据：CategoryDependency 严重告警
    coll = {"Warnings": [
        {"level": "severe", "kind": "CategoryDependency",
         "detail": "A 与 B 一一对应（A 的每个取值唯一映射到 B）"}]}
    res = screen_candidate(frame, "Trait", "A", role="fixed", config=CFG,
                          collinearity_block=coll, stats_mod=None)
    assert res["verdict"] == "choose_one", res
    # 一一映射列：choose_one（二选一，不自动剔除）
    assert any("二选一" in r for r in res["reasons"]), res["reasons"]


# ------------------------------------------------------------------
# 7. scipy 缺失降级：仅按 η² 判定，不阻断
# ------------------------------------------------------------------
def test_degraded_without_scipy():
    original = abges_effects._scipy
    abges_effects._scipy = lambda: None          # 模拟 scipy 缺失
    try:
        rng = _rng(7)
        n = 1200
        gen = rng.integers(0, 3, n)
        trait = rng.normal(0, 1, n)
        frame = pd.DataFrame({"Trait": trait, "Noise": gen.astype(str)})
        res = build_effect_screening(frame, ["Trait"],
                                    fixed_columns=["Noise"], config=CFG)
    finally:
        abges_effects._scipy = original

    # scipy 缺失不报错：噪声列仍据 η² 判 drop
    cand = {c["column"]: c for c in res["Candidates"]}
    assert cand["Noise"]["verdict"] == "drop", cand["Noise"]
    # 且应记录降级原因
    assert res["Degraded"], "scipy 缺失应记入 Degraded，实为 %s" % res["Degraded"]
    assert any("scipy" in d for d in res["Degraded"]), res["Degraded"]
    # 降级后：P 不可用，但仍给出可用结论（不空判）
    assert cand["Noise"]["evidence"].get("p") is None


# ------------------------------------------------------------------
# 8. build 只诊断、不产出 Applied/AppliedChanges（收口：纯诊断+提示）
# ------------------------------------------------------------------
def test_no_applied_keys_emitted():
    rng = _rng(9)
    n = 1200
    gen = rng.integers(0, 3, n)
    trait = gen * 8.0 + rng.normal(0, 1.0, n)
    noise = rng.integers(0, 3, n)
    frame = pd.DataFrame({"Trait": trait, "Gen": gen.astype(str),
                          "Noise": noise.astype(str)})
    res = build_effect_screening(frame, ["Trait"],
                                fixed_columns=["Gen", "Noise"], config=CFG)
    # 收口后：build 只产出诊断结论，绝不返回 Applied/AppliedChanges
    assert "Applied" not in res, "收口后不应再产出 Applied 键"
    assert "AppliedChanges" not in res, "收口后不应再产出 AppliedChanges 键"


if __name__ == "__main__":
    for name in sorted(k for k in globals() if k.startswith("test_")):
        print("%s -> %s" % (name, globals()[name]()))
    print("ALL PASS")
