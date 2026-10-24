# -*- coding: utf-8 -*-
"""runners/report_sections.py 单元测试（P2-3 拆分后新增的可测点）。

只锁两件事：
  * 文案常量表的条目数与关键内容（导读页缩写表、闸门中文名、整合指标标签），
    防止拆分过程中漏搬或搬错；
  * build_exploration 的降级契约——引擎表型文件缺失时返回 (None, {})，
    绝不抛异常阻断报告生成。
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

from runners import report_sections as sc  # noqa: E402


def test_abbreviations_table():
    assert len(sc.ABBREVIATIONS) == 10
    flat = {item for row in sc.ABBREVIATIONS for item in row}
    for key in ("EBV", "BLUP", "PBLUP", "GBLUP", "SSGBLUP", "SNP", "SE", "CI"):
        assert key in flat
    for row in sc.ABBREVIATIONS:
        assert len(row) == 4, "缩写表每行应为（符号, 含义, 符号, 含义）四元组"


def test_gate_name_table():
    assert len(sc.GATE_NAME) == 6
    for gate in ("G1", "G2", "G3", "G4", "G5", "G6"):
        assert gate in sc.GATE_NAME
        assert sc.GATE_NAME[gate].endswith("闸门")


def test_integration_labels():
    assert len(sc.INTEG_LABELS) == 8
    keys = {key for key, _ in sc.INTEG_LABELS}
    assert {"phenotype_records", "pedigree_individuals",
            "genotype_individuals", "pheno_ped_overlap"} <= keys
    for key, label in sc.INTEG_LABELS:
        assert key and label, "标签表不得出现空键或空值"


def test_build_exploration_degrades_when_engine_file_missing():
    block, figures = sc.build_exploration("prefix", {}, {}, None)
    assert block is None
    assert figures == {}


def test_build_exploration_degrades_when_path_not_exists(tmp_path):
    missing = str(tmp_path / "no_such_file.txt")
    block, figures = sc.build_exploration("prefix", {}, {}, missing)
    assert block is None
    assert figures == {}


def test_explore_max_levels_guard():
    """高基数列（如孵化日期）不得进入分组统计，否则设计矩阵被撑爆。"""
    assert sc.EXPLORE_MAX_LEVELS == 50
