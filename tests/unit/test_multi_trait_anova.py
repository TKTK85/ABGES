# -*- coding: utf-8 -*-
"""ABGES 回归测试：多性状逐性状 .anova（固定效应 F 检验）解析。

背景（2026-10-13，先生指令"多性状 .anova 要支持"）
----------------------------------------------------
HIBLUP v1.7.0 起，多性状模式产出【逐性状】 .anova：prefix.TRAIT.anova
（v1.6.0 仅单性状产出 prefix.anova）。迁移来的 hiblup_tool.py 原逻辑为
`if not is_multi` 才解析，且查找无性状名的 prefix.anova，
导致多性状的 F 检验被静默忽略。

本测试锁定六点：
  1. 多性状逐性状解析，anova_by_trait 覆盖全部性状；
  2. anova 合并列表每条带 trait 字段，可区分性状；
  3. **anova 必须是列表**——下游 anova_table() 遍历并调 rec.get()，
     若为 dict 会遍历字符串键而崩溃（兼容性铁律）；
  4. 单性状行为不回归：anova 为列表、trait 为空串、报告层据此不加"性状"列；
  5. 文件缺失时静默不阻断（.anova 属可选信息）；
  6. downstream anova_table 多性状带 trait、单性状为空串。
"""
import sys, os
from pathlib import Path

ROOT = str(Path(__file__).resolve().parents[2])
sys.path.insert(0, os.path.join(ROOT, 'tools', 'Python'))
sys.path.insert(0, os.path.join(ROOT, 'tools', 'HIBLUP'))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

import hiblup_tool as ht                      # noqa: E402
from runners import breeding_insights as bi   # noqa: E402
from abges_core import resolve_temp_dir       # noqa: E402

PASS, FAIL = [], []


def check(name, fn):
    try:
        fn()
        PASS.append(name)
        print("  [PASS] %s" % name)
    except AssertionError as exc:
        FAIL.append((name, str(exc)))
        print("  [FAIL] %s -> %s" % (name, exc))
    except Exception as exc:                  # noqa: BLE001
        FAIL.append((name, "%s: %s" % (type(exc).__name__, exc)))
        print("  [ERROR] %s -> %s: %s" % (name, type(exc).__name__, exc))


# HIBLUP 实测表头（v1.6.0 与 v1.7.0 一致），残差行 F/P 为 "."
ANOVA_HEADER = "Factors\tDf\tSumSq\tMeanSq\tF\tPr(>F)\n"
ANOVA_BODY = "Gen\t1\t10.0\t10.0\t2.0\t0.20\n" \
             "Batch\t2\t20.0\t10.0\t1.0\t0.30\n" \
             "e\t296\t16167.2\t54.6189\t.\t.\n"

WORK = os.path.join(resolve_temp_dir(), "test_multi_trait_anova")


def write_anova(prefix, trait=None):
    """写出一个 .anova 文件：trait 为 None 时为单性状命名。"""
    os.makedirs(WORK, exist_ok=True)
    name = prefix + (("." + trait + ".anova") if trait else ".anova")
    with open(os.path.join(WORK, name), "w", encoding="utf-8") as handle:
        handle.write(ANOVA_HEADER + ANOVA_BODY)
    return os.path.join(WORK, prefix)


# ---------------------------------------------------------------
# 1–3：多性状逐性状解析
# ---------------------------------------------------------------

def test_multi_trait_anova_by_trait():
    prefix = write_anova("mt", "EN")
    write_anova("mt", "ACL")
    parsed = ht.parse_hiblup_output(prefix, method="pblup", mode="multi",
                                    trait_names=["EN", "ACL"])
    by_trait = parsed.get("anova_by_trait") or {}
    assert set(by_trait) == {"EN", "ACL"}, \
        "逐性状 .anova 应覆盖全部性状，实际 %s" % sorted(by_trait)
    assert len(by_trait["EN"]) == 3 and len(by_trait["ACL"]) == 3, \
        "每个性状应解析出 3 行（2 个固定效应 + 残差）"


def test_multi_trait_merged_has_trait_field():
    prefix = write_anova("mt2", "EN")
    write_anova("mt2", "ACL")
    parsed = ht.parse_hiblup_output(prefix, method="pblup", mode="multi",
                                    trait_names=["EN", "ACL"])
    merged = parsed.get("anova") or []
    assert len(merged) == 6, "合并列表应为 6 行，实际 %d" % len(merged)
    traits = {rec.get("trait") for rec in merged}
    assert traits == {"EN", "ACL"}, "合并列表每条应带 trait 字段，实际 %s" % traits


def test_anova_must_be_list():
    """兼容性铁律：anova 必须是列表，下游 anova_table 会遍历并调 rec.get()。"""
    prefix = write_anova("mt3", "EN")
    write_anova("mt3", "ACL")
    parsed = ht.parse_hiblup_output(prefix, method="pblup", mode="multi",
                                    trait_names=["EN", "ACL"])
    assert isinstance(parsed.get("anova"), list), \
        "anova 必须是列表，实际类型 %s" % type(parsed.get("anova")).__name__


# ---------------------------------------------------------------
# 4：单性状不回归
# ---------------------------------------------------------------

def test_single_trait_not_regressed():
    prefix = write_anova("st", None)
    parsed = ht.parse_hiblup_output(prefix, method="pblup", mode="single",
                                    trait_names=["EN"])
    anova = parsed.get("anova") or []
    assert isinstance(anova, list) and len(anova) == 3, \
        "单性状 anova 应为 3 行列表，实际 %r" % (len(anova),)
    assert "trait" not in anova[0], \
        "单性状不应污染 trait 字段，实际 %s" % sorted(anova[0])
    # 单性状也提供 anova_by_trait，便于下游统一取值
    assert list(parsed.get("anova_by_trait") or {}) == ["EN"], \
        "单性状 anova_by_trait 应含该性状，实际 %s" % list(parsed.get("anova_by_trait") or {})


# ---------------------------------------------------------------
# 5：缺文件静默不阻断
# ---------------------------------------------------------------

def test_missing_anova_not_blocking():
    os.makedirs(WORK, exist_ok=True)
    parsed = ht.parse_hiblup_output(os.path.join(WORK, "none"),
                                    method="pblup", mode="multi",
                                    trait_names=["EN", "ACL"])
    assert parsed.get("anova") == [], "缺 .anova 时 anova 应为空列表"
    assert (parsed.get("anova_by_trait") or {}) == {"EN": [], "ACL": []}, \
        "缺 .anova 时逐性状应为性状键 + 空列表，不阻断流程"
    # .anova 属可选信息，不应记入 missing_files
    assert not [f for f in (parsed.get("missing_files") or []) if ".anova" in f], \
        "缺 .anova 不应记入 missing_files（可选信息，避免误报）"


# ---------------------------------------------------------------
# 6：下游 anova_table
# ---------------------------------------------------------------

def test_anova_table_multi_and_single():
    prefix = write_anova("at", "EN")
    write_anova("at", "ACL")
    parsed_multi = ht.parse_hiblup_output(prefix, method="pblup", mode="multi",
                                          trait_names=["EN", "ACL"])
    rows_multi = bi.anova_table(parsed_multi)
    assert len(rows_multi) == 6, "多性状 anova_table 应为 6 行，实际 %d" % len(rows_multi)
    assert {r["trait"] for r in rows_multi} == {"EN", "ACL"}, \
        "多性状 anova_table 应带 trait"
    # 残差行 F/P 为 "."，按缺失处理
    resid = [r for r in rows_multi if r["factor"] == "e"]
    assert resid and resid[0]["f"] is None, "残差行 F 应为 None（'.' 按缺失处理）"

    prefix_single = write_anova("at2", None)
    parsed_single = ht.parse_hiblup_output(prefix_single, method="pblup",
                                           mode="single", trait_names=["EN"])
    rows_single = bi.anova_table(parsed_single)
    assert len(rows_single) == 3, "单性状 anova_table 应为 3 行，实际 %d" % len(rows_single)
    assert all(r["trait"] == "" for r in rows_single), \
        "单性状 trait 应为空串，报告层据此不加'性状'列，版式不变"


def main():
    print("\n多性状逐性状 .anova 回归测试")
    print("-" * 62)
    check("1. 多性状逐性状解析覆盖全部性状", test_multi_trait_anova_by_trait)
    check("2. 合并列表每条带 trait 字段", test_multi_trait_merged_has_trait_field)
    check("3. anova 必须是列表（下游兼容铁律）", test_anova_must_be_list)
    check("4. 单性状行为不回归", test_single_trait_not_regressed)
    check("5. 缺 .anova 静默不阻断、不误报", test_missing_anova_not_blocking)
    check("6. 下游 anova_table 多/单性状均正确", test_anova_table_multi_and_single)
    print("-" * 62)
    print("通过 %d / 失败 %d" % (len(PASS), len(FAIL)))
    for name, reason in FAIL:
        print("  失败项：%s -> %s" % (name, reason))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
