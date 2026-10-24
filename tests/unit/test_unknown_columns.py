# -*- coding: utf-8 -*-
"""ABGES 未知 / 中文表头 → 英文代号 自动处理：单测 + 集成校验。

覆盖（2026-10-13，承接未知性状与固定效应自动处理方案_v1.1）：
  1. 三级优先级：注册表 name_cn → 人工词典 → pypinyin 兜底（含字母保底）。
  2. 已知英文代号保留（action=kept），未知英文保留（不静默改选）。
  3. 内容分类修正：低基数非数值列判为固定效应，而非整列排除；ID 类高唯一度排除。
  4. 映射表落盘 YAML 含完整条目与原始中文名。
  5. 集成 run_phenotype_qc：中文固定效应自动纳入 effective_fixed_effects 并留痕；
     中文连续列仅登记为候选、默认不自动入模（防模型污染）。
  6. 治理开关：auto_include_unknown_fixed_effects=false 时中文固定效应不纳入。
  7. 多性状逐性状清洗后未知列聚合（MultiTraitQCResult 对齐）。
  8. 不回归：全英文表头帧行为不变。
"""
import sys, os, copy, tempfile
from pathlib import Path

ROOT = str(Path(__file__).resolve().parents[2])
sys.path.insert(0, os.path.join(ROOT, 'tools', 'Python'))
sys.path.insert(0, ROOT)
os.chdir(ROOT)



def check(name, fn):
    """执行一项检查；失败即抛出，交由 pytest 逐项归因（不再静默记录）。"""
    detail = fn()
    if detail:
        print("PASS  %-52s %s" % (name, detail))
    return detail


import pandas as pd
import yaml
import abges_core as core
import abges_unknown as unknown_tool
import abges_phenotype as phenotype_tool

# 直接拿正式配置段，保证测试与运行期行为一致
_BASE_CFG = core.load_yaml_config(phenotype_tool.PHENOTYPE_QC_CONFIG)
_UNKNOWN_CFG = copy.deepcopy(_BASE_CFG.get("unknown_columns", {}))


def _cn_frame():
    """混合中文/英文/未知英文表头的合成帧。

    注意：中文列不与「其英文映射目标」同帧出现，否则会触发正常的碰撞规避
    （如 产蛋数→EN 但帧内已有 EN 列 → 退化拼音 CDS），那是正确的行为，
    不应在本用例中制造该场景。
    """
    rng = list(range(60))
    return pd.DataFrame({
        # 已知英文标准代号（应 kept，与任何中文列不冲突）
        "ID": ["C%04d" % i for i in rng],
        "ACL": [float(5 + (i % 12)) for i in rng],
        # 注册表 name_cn 命中（应 registry；目标 BW/Gen/EN 均不在帧内）
        "体重": [float(1500 + (i % 50)) for i in rng],
        "世代": ["G1"] * 30 + ["G2"] * 30,
        "产蛋数": [float(80 + (i % 45)) for i in rng],
        # 人工词典命中（应 dictionary；Cage 不在帧内）
        "笼位": ["C%d" % (i % 4) for i in rng],
        # 拼音兜底（应 pinyin）：养殖方式
        "养殖方式": ["Y%d" % (i % 3) for i in rng],
        # 未知英文（应 kept, unknown_english）
        "Region": ["R%d" % (i % 2) for i in rng],
        # 疑似 ID（唯一值高），应排除
        "个体流水号": ["S%06d" % i for i in rng],
    })


# ------------------------------------------------------------------
# 1–4. normalize_headers 四级优先级 + 分类 + 落盘
#
# 【S2 修正 2026-10-21】column-aliases.yaml 因 472 行一处中文引号未转义，
# 整份 478 行配置解析失败；而 `_load_column_alias_registry` 用
# `except Exception: return {}` 静默吞掉异常 → 别名注册表在生产中恒为空。
# 本用例原先断言"体重/世代/产蛋数 应走 registry（P2）、笼位 走 dictionary（P3）"，
# 正是按那个错误状态写的。配置修复后，这四列均已注册在别名表中，
# 应命中最高优先级 alias_registry（P1）；解析出的英文代号保持不变。
# ------------------------------------------------------------------
def case_normalize_three_tiers():
    frame = _cn_frame()
    new_frame, rename_map, report = unknown_tool.normalize_headers(
        frame, _UNKNOWN_CFG)
    by_orig = {e["original"]: e for e in report}

    # 已知英文保留
    assert by_orig["ID"]["source"] == "known", by_orig["ID"]
    assert by_orig["ID"]["action"] == "kept"
    assert by_orig["ACL"]["source"] == "known", by_orig["ACL"]
    # column-aliases.yaml 别名注册表（P1，最高优先级；代号与注册表口径一致）
    assert by_orig["体重"]["source"] == "alias_registry" and by_orig["体重"]["code"] == "BW", by_orig["体重"]
    assert by_orig["世代"]["source"] == "alias_registry" and by_orig["世代"]["code"] == "Gen", by_orig["世代"]
    assert by_orig["产蛋数"]["source"] == "alias_registry" and by_orig["产蛋数"]["code"] == "EN", by_orig["产蛋数"]
    assert by_orig["笼位"]["source"] == "alias_registry" and by_orig["笼位"]["code"] == "Cage", by_orig["笼位"]
    # 拼音兜底
    pe = by_orig["养殖方式"]
    assert pe["source"] == "pinyin", pe
    assert pe["code"] == "YZFS", "期望拼音首字母 YZFS，实得 %s" % pe["code"]
    # 未知英文保留
    assert by_orig["Region"]["source"] == "unknown_english"
    # 疑似 ID 排除
    assert by_orig["个体流水号"]["role"] == "excluded", by_orig["个体流水号"]

    # 分类：笼位/养殖方式 低基数 → fixed；产蛋数 连续 → trait_or_covariate
    assert by_orig["笼位"]["role"] == "fixed", by_orig["笼位"]
    assert by_orig["养殖方式"]["role"] == "fixed", by_orig["养殖方式"]
    assert by_orig["产蛋数"]["role"] == "trait_or_covariate", by_orig["产蛋数"]

    # 帧列名确已转换为英文（中文列消失）
    for cn in ("体重", "世代", "产蛋数", "笼位", "养殖方式"):
        assert cn not in new_frame.columns, "中文列未转换：%s" % cn
    for en in ("BW", "Gen", "EN", "Cage", "YZFS"):
        assert en in new_frame.columns, "缺少转换后的英文列：%s" % en
    return ("BW/Gen/EN/Cage/YZFS 均生成, ID 保留, 个体流水号 排除")


def case_map_dump():
    frame = _cn_frame()
    _, _, report = unknown_tool.normalize_headers(frame, _UNKNOWN_CFG)
    with tempfile.TemporaryDirectory() as d:
        path = unknown_tool.write_unknown_map(report, d, run_id="ut")
        assert path and os.path.exists(path), "映射表未落盘"
        with open(path, "r", encoding="utf-8") as fh:
            payload = yaml.safe_load(fh)
        entries = payload.get("entries", [])
        # 每条都保留原始中文名，可追溯
        assert any(e.get("original") == "体重" and e.get("code") == "BW" for e in entries)
        assert any(e.get("original") == "养殖方式" for e in entries)
    return "YAML 落盘含原始中文名 %d 条" % len(entries)


# ------------------------------------------------------------------
# 5–6. 集成 run_phenotype_qc：中文固定效应自动纳入 / 连续列候选
# ------------------------------------------------------------------
def _integration_frame():
    rng = list(range(60))
    return pd.DataFrame({
        "ID": ["C%04d" % i for i in rng],
        "Gen": ["G1"] * 30 + ["G2"] * 30,
        "ACL": [float(5 + (i % 10)) for i in rng],     # 目标性状（数值）
        "家系": ["F%d" % (i % 3) for i in rng],          # 中文固定效应 → Family
        "产蛋数": [float(80 + (i % 45)) for i in rng],  # 中文连续 → EN（候选）
    })


def case_integration_auto_include_fixed():
    df = _integration_frame()
    cfg = copy.deepcopy(_BASE_CFG)
    cfg["unknown_columns"]["map_output"] = tempfile.mkdtemp(prefix="abges_unk_")
    qc = phenotype_tool.run_phenotype_qc(
        df, traits=["ACL"], fixed_effects=["Gen"], covariates=[],
        mode="single", config=cfg)
    # 中文固定效应 家系 → Family 自动纳入
    assert "Family" in qc.effective_fixed_effects, qc.effective_fixed_effects
    assert qc.auto_included_fixed_effects == ["Family"], qc.auto_included_fixed_effects
    # rename_detail 记录中文→英文（复用既有语义，保留 _align_declared 能力）
    assert qc.rename_detail.get("家系") == "Family", qc.rename_detail
    # 留痕 + 告警（不静默）
    assert any(w.get("code") == "PH_AUTO_INCLUDED_FIXED" for w in qc.warnings), qc.warnings
    assert any(c.get("action") == "AUTO_INCLUDE_FIXED" for c in qc.change_log), qc.change_log
    # 中文连续列 产蛋数 → EN 仅登记候选，默认不自动入模
    assert "EN" not in qc.effective_covariates, qc.effective_covariates
    assert any(w.get("code") == "PH_AUTO_CANDIDATE_TRAIT" for w in qc.warnings), qc.warnings
    # 原始中文名在报告中可追溯
    assert any(e.get("original") == "家系" for e in qc.unknown_columns)
    return "Family 已纳入, 家系→Family 留痕, EN 仅候选未入模"


def case_switch_off_no_auto_include():
    df = _integration_frame()
    cfg = copy.deepcopy(_BASE_CFG)
    cfg["unknown_columns"]["auto_include_unknown_fixed_effects"] = False
    cfg["unknown_columns"]["map_output"] = tempfile.mkdtemp(prefix="abges_unk_")
    qc = phenotype_tool.run_phenotype_qc(
        df, traits=["ACL"], fixed_effects=["Gen"], covariates=[],
        mode="single", config=cfg)
    assert "Family" not in qc.effective_fixed_effects, qc.effective_fixed_effects
    assert qc.auto_included_fixed_effects == []
    # 仍完成转换与报告（只是不纳入建模）
    assert qc.rename_detail.get("家系") == "Family"
    return "开关关闭时 Family 未纳入, 但转换仍完成"


# ------------------------------------------------------------------
# 7. 多性状未知列聚合
# ------------------------------------------------------------------
def case_multitrait_aggregation():
    df = _integration_frame()
    cfg = copy.deepcopy(_BASE_CFG)
    cfg["unknown_columns"]["map_output"] = tempfile.mkdtemp(prefix="abges_unk_")
    result = phenotype_tool.run_phenotype_qc_by_trait(
        df, traits=["ACL", "EN"], fixed_effects=["Gen"], covariates=[],
        rename_detail={}, config=cfg)
    if not isinstance(result, phenotype_tool.MultiTraitQCResult):
        raise AssertionError("应返回 MultiTraitQCResult，实得 %r" % type(result))
    assert "Family" in result.auto_included_fixed_effects, result.auto_included_fixed_effects
    assert any(e.get("original") == "家系" for e in result.unknown_columns), result.unknown_columns
    assert any(e.get("original") == "产蛋数" for e in result.unknown_columns)
    # 转换后的 Family 列确实进了并集宽表
    assert "Family" in result.frame.columns, list(result.frame.columns)
    return "多性状聚合: Family 入模, 未知列 %d 条" % len(result.unknown_columns)


# ------------------------------------------------------------------
# 8. 不回归：全英文帧行为不变
# ------------------------------------------------------------------
def case_no_regression_all_english():
    df = pd.DataFrame({
        "ID": ["C%04d" % i for i in range(40)],
        "Gen": ["G1"] * 20 + ["G2"] * 20,
        "Batch": ["B1", "B2"] * 20,
        "ACL": [float(5 + (i % 9)) for i in range(40)],
    })
    qc = phenotype_tool.run_phenotype_qc(
        df, traits=["ACL"], fixed_effects=["Gen", "Batch"], covariates=[],
        mode="single")
    if qc.statistics.get("standardized"):
        raise AssertionError("单性状不应标准化")
    # 全英文帧不应产生任何未知列处理
    assert all(e.get("source") in ("known", "unknown_english")
               for e in qc.unknown_columns), qc.unknown_columns
    assert qc.auto_included_fixed_effects == []
    assert qc.effective_fixed_effects == ["Gen", "Batch"], qc.effective_fixed_effects
    return "全英文帧无未知列处理, 固定效应=Gen/Batch"


# ------------------------------------------------------------------
# pytest 用例包装（由脚本式 check() 自动转换，断言逻辑与判定条件均未改动）
# ------------------------------------------------------------------

def test_normalize_three_tiers():
    check("normalize 三级优先级+分类+落盘", case_normalize_three_tiers)

def test_map_dump():
    check("映射表 YAML 落盘含原始中文名", case_map_dump)

def test_integration_auto_include_fixed():
    check("集成: 中文固定效应自动纳入", case_integration_auto_include_fixed)

def test_switch_off_no_auto_include():
    check("开关: 关闭后中文固定效应不纳入", case_switch_off_no_auto_include)

def test_multitrait_aggregation():
    check("多性状未知列聚合", case_multitrait_aggregation)

def test_no_regression_all_english():
    check("不回归: 全英文帧行为不变", case_no_regression_all_english)
