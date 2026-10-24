# -*- coding: utf-8 -*-
"""ABGES 回归测试：基因型质控的等位基因频率/HWE 统计口径（freq_hwe_scope）。

背景（2026-10-12，plink-ng 源码实证）
--------------------------------------
PLINK2 源码（plink2.cc L2728 / L2836）证实：--maf 与 --hwe 默认【仅基于
founder（基础群）】计算，必须显式 --nonfounders 才纳入全群。育种家系数据
基础群通常仅数十只，沿用默认会导致：
  1) HWE 检验功效不足，剔除数恒为 0 —— 规则静默失效；
  2) MAF 基于小样本估频，最终位点集合与全群口径不一致（实测成员差 43 个）。

本测试锁定六点：
  1. all_samples 口径追加 --nonfounders；
  2. founders_only 口径不追加任何参数（沿用 PLINK2 默认）；
  3. 非法取值直接报错，不做静默回退；
  4. 基础群计数正确（.fam 父母列均为 0 者）；
  5. --nonfounders 只作用于 maf / hwe，不污染 mind / geno（缺失率不受口径影响）；
  6. 配置默认值为 all_samples，且 founders_only + 基础群过小会产出告警。
"""
import sys, os
from pathlib import Path

ROOT = str(Path(__file__).resolve().parents[2])
sys.path.insert(0, os.path.join(ROOT, 'tools', 'Python'))
sys.path.insert(0, os.path.join(ROOT, 'tools', 'PLINK'))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

import plink_tool as pt  # noqa: E402

PASS, FAIL = [], []


def check(name, fn):
    try:
        fn()
        PASS.append(name)
        print("  [PASS] %s" % name)
    except AssertionError as exc:
        FAIL.append((name, str(exc)))
        print("  [FAIL] %s -> %s" % (name, exc))
    except Exception as exc:      # noqa: BLE001
        FAIL.append((name, "%s: %s" % (type(exc).__name__, exc)))
        print("  [ERROR] %s -> %s: %s" % (name, type(exc).__name__, exc))


# ---------------------------------------------------------------
# 测试夹具：构造带家系结构的 .fam
# ---------------------------------------------------------------

def build_fam(tmp_dir, n_founder=30, n_offspring=270):
    """写出一个 .fam：前 n_founder 个为基础群，其余为后代。"""
    os.makedirs(tmp_dir, exist_ok=True)
    fam_path = os.path.join(tmp_dir, "case.fam")
    lines = []
    for idx in range(n_founder + n_offspring):
        if idx < n_founder:
            pat, mat = "0", "0"          # 父母为空 = founder
        else:
            pat = "per%d" % (idx % n_founder)
            mat = "per%d" % ((idx + 1) % n_founder)
        lines.append("FAM%d per%d %s %s 2 -9" % (idx, idx, pat, mat))
    with open(fam_path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    return os.path.join(tmp_dir, "case")


TMP = os.path.join(ROOT, "workspace", "intermediate", "test_gt_scope")


# ---------------------------------------------------------------
# 1–4：口径解析与基础群计数
# ---------------------------------------------------------------

def test_all_samples_flag():
    flags = pt.resolve_freq_hwe_scope_flags({"freq_hwe_scope": "all_samples"})
    assert flags == ["--nonfounders"], "all_samples 应追加 --nonfounders，实际 %r" % flags


def test_founders_only_flag():
    flags = pt.resolve_freq_hwe_scope_flags({"freq_hwe_scope": "founders_only"})
    assert flags == [], "founders_only 应沿用 PLINK2 默认，实际 %r" % flags


def test_invalid_scope_rejected():
    from abges_core import AbgesError
    try:
        pt.resolve_freq_hwe_scope_flags({"freq_hwe_scope": "whole_population"})
    except AbgesError as exc:
        assert "freq_hwe_scope" in str(exc), "错误信息应点名字段名"
        return
    raise AssertionError("非法 freq_hwe_scope 未被拦截，存在静默回退风险")


def test_count_founders():
    prefix = build_fam(TMP, n_founder=30, n_offspring=270)
    count = pt.count_founders(prefix)
    assert count == 30, "基础群计数错误，期望 30，实际 %d" % count


# ---------------------------------------------------------------
# 5：--nonfounders 只作用于 maf / hwe
# ---------------------------------------------------------------

def test_scope_flags_only_on_maf_hwe():
    """拦截底层调用，核对四步实际下发的参数。"""
    captured = []

    def fake_step(plink_args, output_prefix, **kwargs):
        captured.append(list(plink_args))
        return 0, output_prefix, "ok", 0.0

    original = pt.run_plink_step
    pt.run_plink_step = fake_step
    try:
        prefix = build_fam(TMP)
        for suffix in (".bed", ".bim"):
            with open(prefix + suffix, "wb") as handle:
                handle.write(b"\x00")
        # 伪造规模统计，避免依赖真实 plink 产物
        original_count = pt.count_genotype_scale
        pt.count_genotype_scale = lambda p: {"n_sample": 300, "n_snp": 2000}
        try:
            pt.run_genotype_qc(prefix, TMP,
                               thresholds={"freq_hwe_scope": "all_samples",
                                           "min_snp_after_qc": 1,
                                           "min_sample_after_qc": 1},
                               timestamp="ut_scope")
        finally:
            pt.count_genotype_scale = original_count
    finally:
        pt.run_plink_step = original

    assert len(captured) == 4, "应有 mind/geno/maf/hwe 四步，实际 %d" % len(captured)
    step_flag = {}
    for args in captured:
        for name in ("mind", "geno", "maf", "hwe"):
            if "--" + name in args:
                step_flag[name] = "--nonfounders" in args
    assert step_flag.get("mind") is False, "--mind 不应受口径影响"
    assert step_flag.get("geno") is False, "--geno 不应受口径影响"
    assert step_flag.get("maf") is True, "--maf 应追加 --nonfounders"
    assert step_flag.get("hwe") is True, "--hwe 应追加 --nonfounders"


# ---------------------------------------------------------------
# 6：默认口径与基础群告警
# ---------------------------------------------------------------

def test_default_scope_is_all_samples():
    config = pt._load_qc_thresholds()
    assert config.get("freq_hwe_scope") == "all_samples", \
        "配置默认口径应为 all_samples，实际 %r" % config.get("freq_hwe_scope")


def test_founders_only_warns_on_small_founder_set():
    """founders_only + 基础群 30 < 50 时，必须产出告警而非静默通过。"""
    prefix = build_fam(TMP, n_founder=30, n_offspring=270)

    captured = {}

    def fake_step(plink_args, output_prefix, **kwargs):
        return 0, output_prefix, "ok", 0.0

    original_step = pt.run_plink_step
    original_count = pt.count_genotype_scale
    pt.run_plink_step = fake_step
    pt.count_genotype_scale = lambda p: {"n_sample": 300, "n_snp": 2000}
    try:
        result = pt.run_genotype_qc(
            prefix, TMP,
            thresholds={"freq_hwe_scope": "founders_only",
                        "min_founders_warning": 50,
                        "min_snp_after_qc": 1, "min_sample_after_qc": 1},
            timestamp="ut_warn")
    finally:
        pt.run_plink_step = original_step
        pt.count_genotype_scale = original_count

    warnings = result.get("warnings", [])
    assert warnings, "基础群 30 < 50 且 founders_only 口径下必须告警，实际无告警"
    assert any("30" in w for w in warnings), "告警应写明实际基础群数量"
    assert result.get("n_founder_raw") == 30, "应记录基础群数量供上层留痕"


def main():
    print("\n基因型质控统计口径回归测试（freq_hwe_scope）")
    print("-" * 62)
    check("1. all_samples 口径追加 --nonfounders", test_all_samples_flag)
    check("2. founders_only 口径沿用 PLINK2 默认", test_founders_only_flag)
    check("3. 非法口径取值被拦截（无静默回退）", test_invalid_scope_rejected)
    check("4. 基础群计数正确", test_count_founders)
    check("5. --nonfounders 仅作用于 maf/hwe", test_scope_flags_only_on_maf_hwe)
    check("6a. 配置默认口径为 all_samples", test_default_scope_is_all_samples)
    check("6b. founders_only + 小基础群产出告警", test_founders_only_warns_on_small_founder_set)
    print("-" * 62)
    print("通过 %d / 失败 %d" % (len(PASS), len(FAIL)))
    for name, reason in FAIL:
        print("  失败项：%s -> %s" % (name, reason))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
