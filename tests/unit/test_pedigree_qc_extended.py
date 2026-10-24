# -*- coding: utf-8 -*-
"""ABGES 系谱 QC 扩展规则测试：PD014–PD010（含可选列存在/缺失两种情形）。

运行：python -S tests/unit/test_pedigree_qc_extended.py
依赖：tools/Python 下的 abges_* 模块 + D 盘 venv 的 site-packages。
"""
import sys, os, traceback, copy
from pathlib import Path

ROOT = str(Path(__file__).resolve().parents[2])
sys.path.insert(0, os.path.join(ROOT, 'tools', 'Python'))

import yaml  # noqa: E402
import pandas as pd  # noqa: E402
import abges_pedigree as ped  # noqa: E402



def check(name, fn):
    """执行一项检查；失败即抛出，交由 pytest 逐项归因（不再静默记录）。"""
    detail = fn()
    if detail:
        print("PASS  %-52s %s" % (name, detail))
    return detail


# ---- 加载真实 YAML 配置，并把会 BLOCK 小规模样本的规则放宽，避免测试被规模门控误伤 ----
_CFG_PATH = os.path.join(ROOT, 'configs', 'qc', 'pedigree-qc.yaml')
with open(_CFG_PATH, encoding='utf-8') as _f:
    CFG = yaml.safe_load(_f)
# 测试用极小系谱，关闭最小规模门禁与近交计算（非本次验证目标）
CFG['rules']['PD013']['min_individuals'] = 1
CFG['rules']['PD012']['compute_inbreeding'] = False


def _warn(res, rid):
    return [w for w in res.warnings if w.get('rule_id') == rid]


def _change(res, rid):
    return [c for c in res.change_log if c.get('rule_id') == rid]


# ===============================================================
# PD014 世代判定与置信声明（显式列 + 与拓扑冲突）
# ===============================================================
def t_pd014():
    df = pd.DataFrame({
        "ID": ["F0", "F1", "X1"],
        "Sire": ["NA", "F0", "F0"],
        "Dam": ["NA", "NA", "NA"],
        "Gen": [0, 1, 5],  # X1 显式=5，拓扑推断=1，应冲突
    })
    cfg = copy.deepcopy(CFG)
    cfg["generation_column_semantics"] = "genetic_generation"
    res = ped.run_pedigree_qc(df, config=cfg)
    assert res.generation.get("method") == "explicit", res.generation
    removed = {item["ID"] for item in res.exclusions if item.get("rule_id") == "PD014"}
    assert "X1" in removed, res.exclusions
    return "method=%s removed=%s" % (res.generation["method"], sorted(removed))


# ===============================================================
# PD003 翅号/编码唯一性与格式自洽（同世代内）
# ===============================================================
def t_pd015():
    df = pd.DataFrame({
        "ID": ["F1", "F2", "F3", "F4"],
        "Sire": ["NA", "F1", "F1", "F1"],
        "Dam": ["NA", "NA", "NA", "NA"],
        "Gen": [0, 1, 1, 1],
        "Wing": ["W001", "W002", "WX99", "W002"],  # W002 重复；W### 与 WA## 模式不一致
    })
    cfg = copy.deepcopy(CFG)
    cfg["late_founder_generation"] = 5
    res = ped.run_pedigree_qc(df, config=cfg)
    removed = {item["ID"] for item in res.exclusions if item.get("rule_id") == "PD003"}
    assert {"F2", "F4"}.issubset(removed), res.exclusions
    return "removed=%s" % sorted(removed)


# ===============================================================
# PD003 脏数据归一（全角转半角 + 仅大小写差异的潜在重复）
# ===============================================================
def t_pd016():
    df = pd.DataFrame({
        "ID": ["\uFF21\u0031", "a1", "B2"],  # Ａ1 全角 A
        "Sire": ["0", "0", "NA"],             # 0 -> NA
        "Dam": ["NA", "NA", "NA"],
    })
    cfg = copy.deepcopy(CFG)
    cfg["late_founder_generation"] = 5
    res = ped.run_pedigree_qc(df, config=cfg)
    assert _change(res, "PD003"), res.change_log
    removed = {item["ID"] for item in res.exclusions}
    assert {"A1", "a1"}.issubset(removed), res.exclusions
    return "changes=%d removed=%s" % (len(_change(res, "PD003")), sorted(removed))


# ===============================================================
# PD002 整行完全重复检测
# ===============================================================
def t_pd017():
    df = pd.DataFrame({
        "ID": ["C1", "C1", "C2"],
        "Sire": ["NA", "NA", "NA"],
        "Dam": ["NA", "NA", "NA"],
    })
    res = ped.run_pedigree_qc(df, config=CFG)
    assert _change(res, "PD002"), res.change_log
    return "dedup=%d" % len(_change(res, "PD002"))


# ===============================================================
# PD008 出生日期格式与越界校验
# ===============================================================
def t_pd018():
    df = pd.DataFrame({
        "ID": ["B1", "B2", "B3"],
        "Sire": ["NA", "NA", "NA"],
        "Dam": ["NA", "NA", "NA"],
        "Gen": [0, 0, 0],
        "BirthDate": ["1899-01-01", "notadate", "2020/05/10"],
    })
    res = ped.run_pedigree_qc(df, config=CFG)
    removed = {item["ID"] for item in res.exclusions if item.get("rule_id") == "PD008"}
    assert {"B1", "B2"}.issubset(removed), res.exclusions
    return "invalid=%s" % sorted(removed)


# ===============================================================
# PD015 血缘链追溯（须在 PD005 补建奠基者之前捕获断链）
# ===============================================================
def t_pd019():
    df = pd.DataFrame({
        "ID": ["A", "B", "C"],
        "Sire": ["NA", "A", "Z"],   # C 的父本 Z 不在系谱集合 -> PD005
        "Dam": ["NA", "NA", "NA"],
    })
    cfg = copy.deepcopy(CFG)
    cfg["late_founder_generation"] = 5
    res = ped.run_pedigree_qc(df, config=cfg)
    removed = {item["ID"]: item for item in res.exclusions}
    assert removed.get("C", {}).get("rule_id") == "PD005", res.exclusions
    assert "B" not in removed, res.exclusions
    return "excluded=%s" % sorted(removed)


# ===============================================================
# PD010 基础群分布合理性（双亲缺失个体散布于晚世代）
# ===============================================================
def t_pd020():
    df = pd.DataFrame({
        "ID": ["G0", "G1"],
        "Sire": ["NA", "G0"],
        "Dam": ["NA", "NA"],
        "Gen": [0, 1],  # G1 拓扑深度为 1 但仍有未知亲本
    })
    res = ped.run_pedigree_qc(df, config=CFG)
    removed = {item["ID"]: item for item in res.exclusions}
    assert removed.get("G1", {}).get("rule_id") == "PD010", res.exclusions
    return "excluded=%s" % sorted(removed)


# ===============================================================
# 可选列缺失时优雅跳过（PD014->topological；PD008/PD010 记 note）
# ===============================================================
def t_skip_when_absent():
    df = pd.DataFrame({
        "ID": ["A", "B", "C"],
        "Sire": ["NA", "A", "B"],
        "Dam": ["NA", "NA", "A"],
    })
    res = ped.run_pedigree_qc(df, config=CFG)
    notes = [n.get("rule_id") for n in res.notes]
    assert "PD008" in notes and "PD010" in notes, res.notes
    assert res.generation.get("method") == "topological", res.generation
    assert not res.lineage.get("break_chains"), res.lineage
    return "method=%s notes=%s" % (res.generation["method"], notes)


# ------------------------------------------------------------------
# pytest 用例包装（由脚本式 check() 自动转换，断言逻辑与判定条件均未改动）
# ------------------------------------------------------------------

def test_pd014():
    check("PD014 世代判定+冲突", t_pd014)

def test_pd015():
    check("PD003 翅号编码自洽", t_pd015)

def test_pd016():
    check("PD003 脏数据归一", t_pd016)

def test_pd017():
    check("PD002 整行重复剔除", t_pd017)

def test_pd018():
    check("PD008 出生日期越界", t_pd018)

def test_pd019():
    check("PD015 血缘链追溯断链", t_pd019)

def test_pd020():
    check("PD010 基础群分布", t_pd020)

def test_skip_when_absent():
    check("可选列缺失优雅跳过", t_skip_when_absent)
