# -*- coding: utf-8 -*-
"""ABGES 特征化测试（characterization tests）：锁住 runners/ 的现行行为。

背景（审计 S3/S7/S9/M10，先生拍板 2026-10-21）：
    P0/P1/P3 各批次修改集中在 runners/（breeding_insights 的群体信息来源、
    report_figures 的异常留痕、pblup _stage_reporter 的 Markdown 报告与 DOCX 口径）。
    而该目录补用例前覆盖率仅 0–12%（gblup / ssgblup / report_figures 均为 0%），
    在如此薄弱的安全网下再动 4,047 行结构（抽取基类）风险过高。

因此本文件先行「锁行为」：把本轮已修复的每一项都固化为可执行断言。
后续抽取 base_stage_runner 时，本文件即为回归判据。

覆盖范围：
    0. _resolve_pre_standardized（三份 runner 的标准化声明合同）
    1. P0-3  breeding_insights 群体信息来源 = 引擎表型 TXT
    2. P0    降级必须留痕（来源全失效时 population_degraded 非空且记 ERROR）
    3. P1-2  report_figures.missing_figures 缺图识别
    4. P1-4  pblup _stage_reporter 已接线 _render_markdown_report
    5. P3-4  pblup DOCX 失败为软依赖降级（不再抛 E_DOCX）
"""
import ast
import io
import os
import sys
from pathlib import Path

import pytest  # noqa: E402

ROOT = str(Path(__file__).resolve().parents[2])
for _p in (ROOT, os.path.join(ROOT, 'tools', 'Python'),
           os.path.join(ROOT, 'harness')):
    if _p not in sys.path:
        sys.path.insert(0, _p)
os.chdir(ROOT)          # gate-rules.yaml 等按项目相对路径加载

from runners import breeding_insights as bi          # noqa: E402
from runners import report_figures as rf             # noqa: E402


# ----------------------------------------------------------------------
# 夹具：合成「引擎表型 TXT」（列：ID / Gen / Batch / EN）
# ----------------------------------------------------------------------
def _write_engine_txt(path, rows):
    with io.open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("ID\tGen\tBatch\tEN\n")
        for rid, gen, batch, en in rows:
            handle.write("%s\t%s\t%s\t%s\n" % (rid, gen, batch, en))


def _rand(ids):
    """.rand 解析结果：ID + 加性效应列 PA。"""
    return {"rand": [{"ID": rid, "PA": "1.0"} for rid in ids]}


# 6 个在群个体（其中 A3/A5 的 EN 缺失）+ 1 个仅系谱个体 B1
ROWS = [
    ("A1", "1", "b1", "10"),
    ("A2", "1", "b1", "12"),
    ("A3", "2", "b2", ""),      # 在群但自身无有效记录
    ("A4", "2", "b2", "15"),
    ("A5", "3", "b3", ""),      # 在群但自身无有效记录
    ("A6", "3", "b3", "18"),
]
ALL_IDS = ["A1", "A2", "A3", "A4", "A5", "A6", "B1"]


def _all_runners():
    """返回三份 stage_runner 模块。

    注意：三处同名实现的入参形态并不统一（历史漂移）：
      · pblup  : 形参 analysis_traits，字符串 / 列表都认
      · gblup  : 形参 traits，**只认列表**——传字符串会被按字符拆开，
                 进而误报「声明未覆盖本次分析性状」
      · ssgblup: 形参 trait，**只认单个字符串**（该 runner 强制单性状）
    抽取基类时应统一为「字符串与列表都兼容」。
    """
    from runners import pblup_stage_runner as p
    from runners import gblup_stage_runner as g
    from runners import ssgblup_stage_runner as s
    return (p, g, s)


def _pre_std(mod, declared, trait="EN"):
    """按各 runner 真实契约适配后调用 _resolve_pre_standardized。"""
    task = {"traits": [trait], "pre_standardized": declared}
    if mod.__name__.endswith("ssgblup_stage_runner"):
        return mod._resolve_pre_standardized(task, trait)
    return mod._resolve_pre_standardized(task, [trait])


# ----------------------------------------------------------------------
# 0. _resolve_pre_standardized：标准化声明合同
# ----------------------------------------------------------------------
def test_pre_standardized_absent_returns_none():
    """未声明 pre_standardized → None，表示按原始观测值处理（三份同口径）。"""
    for mod in _all_runners():
        for declared in (None, {}, []):
            assert _pre_std(mod, declared) is None


def test_pre_standardized_wrong_type_raises_e013():
    """声明必须是对象；格式错误如实报错，不做猜测式兼容。"""
    for mod in _all_runners():
        for bad in ("z-score", ["EN"], 3):
            with pytest.raises(Exception) as exc:
                _pre_std(mod, bad)
            assert getattr(exc.value, "code", "") == "E013"


def test_pre_standardized_empty_traits_raises_e013():
    """已声明但 traits 为空 → 报错，避免「声明了却毫无作用」的假象。"""
    for mod in _all_runners():
        with pytest.raises(Exception) as exc:
            _pre_std(mod, {"scope": []})
        assert getattr(exc.value, "code", "") == "E013"


def test_pre_standardized_uncovered_trait_raises_e013():
    """声明范围未覆盖本次分析性状 → 报错，不静默降级。"""
    for mod in _all_runners():
        with pytest.raises(Exception) as exc:
            _pre_std(mod, {"traits": ["BW"]})
        assert getattr(exc.value, "code", "") == "E013"


def test_pre_standardized_happy_path():
    """正常解析 traits / scope / method，并统一成字符串列表。"""
    task = {"pre_standardized": {"traits": ["EN"],
                                 "scope": "all", "method": "z-score"}}
    for mod in _all_runners():
        if mod.__name__.endswith("ssgblup_stage_runner"):
            out = mod._resolve_pre_standardized(task, "EN")
        else:
            out = mod._resolve_pre_standardized(task, ["EN"])
        assert out["traits"] == ["EN"]
        assert out["scope"] == ["all"]        # 单字符串被包成列表
        assert out["method"] == "z-score"


# ----------------------------------------------------------------------
# 1. P0-3：群体信息来源 = 引擎表型 TXT
# ----------------------------------------------------------------------
def test_population_source_prefers_engine_phenotype(tmp_path):
    """P0-3 修复回归：必须能读到引擎表型 TXT 的 Gen 列。

    修复前该分支用 pd.read_csv 去读原始 .xlsx，必然抛 UnicodeDecodeError，
    异常又被 `except Exception: pass` 吞掉 → generation_available 恒 False。
    """
    eng = tmp_path / "engine_phenotype.txt"
    _write_engine_txt(str(eng), ROWS)

    result = bi.population_and_trend(_rand(ALL_IDS), "EN", None, str(eng))

    assert result["population_source"] == "engine_phenotype"
    assert result["generation_available"] is True
    assert result["population_degraded"] is None


def test_population_partition_is_closed(tmp_path):
    """三路分群之和必须等于预测总数。

    修复前 raw_ids 恒空 → 部分在群个体被误判为「仅系谱」。
    """
    eng = tmp_path / "engine_phenotype.txt"
    _write_engine_txt(str(eng), ROWS)

    result = bi.population_and_trend(_rand(ALL_IDS), "EN", None, str(eng))

    assert result["prediction_n"] == len(ALL_IDS)
    assert result["partition_closed"] is True
    counts = [grp[1]["n"] for grp in result["groups"]]
    assert sum(counts) == result["prediction_n"]
    # A1/A2/A4/A6 有有效 EN；A3/A5 在群但缺失；B1 仅系谱
    assert counts == [4, 2, 1]


# ----------------------------------------------------------------------
# 2. P0：降级必须留痕
# ----------------------------------------------------------------------
def test_population_degrades_to_raw_phenotype_when_engine_absent(tmp_path):
    """引擎 TXT 不可用时应降级到原始表型，且仍视为可用。"""
    raw = tmp_path / "raw_phenotype.txt"
    _write_engine_txt(str(raw), ROWS)

    result = bi.population_and_trend(_rand(ALL_IDS), "EN", str(raw), None)

    assert result["population_source"] == "raw_phenotype"
    assert result["generation_available"] is True


def test_population_records_degradation_when_no_source(caplog):
    """两个来源都不可用：必须记录降级原因（不得静默 pass）。"""
    with caplog.at_level("ERROR", logger="runners.breeding_insights"):
        result = bi.population_and_trend(
            _rand(ALL_IDS), "EN", "/nonexistent/raw.xlsx",
            "/nonexistent/engine.txt")

    assert result["population_source"] is None
    assert result["population_degraded"], "降级原因必须留痕"
    # 用 getMessage() 取已格式化文案，避免 % 惰性格式化误判
    assert any("群体信息来源不可用" in rec.getMessage()
               for rec in caplog.records)


# ----------------------------------------------------------------------
# 3. P1-2：缺图识别
# ----------------------------------------------------------------------
def test_missing_figures_detects_absent_keys():
    expected = ["fig1_variance_h2", "fig2_ebv_dist", "fig3_genetic_trend"]
    assert rf.missing_figures(expected, {"fig1_variance_h2": "a.png"}) == \
        ["fig2_ebv_dist", "fig3_genetic_trend"]
    assert rf.missing_figures(expected, {k: "x" for k in expected}) == []
    assert rf.missing_figures(expected, None) == expected


# ----------------------------------------------------------------------
# 4/5. P1-4 / P3-4：pblup _stage_reporter 结构哨兵
# ----------------------------------------------------------------------
def _pblup_tree():
    path = os.path.join(ROOT, "runners", "pblup_stage_runner.py")
    return ast.parse(io.open(path, encoding="utf-8").read())


def _func(tree, name):
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError("未找到函数 %s" % name)


def test_pblup_reporter_uses_compact_delivery_artifacts():
    """PBLUP delivery should use the shared concise report and Excel builder."""
    fn = _func(_pblup_tree(), "_stage_reporter")
    calls = [n.func.attr for n in ast.walk(fn)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and isinstance(n.func.value, ast.Name)
             and n.func.value.id == "delivery_artifacts"]
    assert "render_markdown" in calls
    assert "create_workbook" in calls


def test_pblup_docx_failure_is_soft_dependency():
    """P3-4 口径统一（先生拍板：软依赖）。

    修复前 pblup 抛 E_DOCX 硬失败，gblup/ssgblup 仅记 warning；
    且该处注释原文写的就是「软依赖：缺失/异常则降级」——注释与代码自相矛盾。
    """
    path = os.path.join(ROOT, "runners", "pblup_stage_runner.py")
    text = io.open(path, encoding="utf-8").read()
    # 排除注释/文档字符串，只在「代码行」里找硬失败
    code_lines = []
    for node in ast.parse(text).body:
        if isinstance(node, ast.FunctionDef) and node.name == "_stage_reporter":
            for sub in ast.walk(node):
                if isinstance(sub, ast.ExceptHandler):
                    for stmt in sub.body:
                        code_lines.append(ast.dump(stmt))
    joined = "\n".join(code_lines)
    assert "E_DOCX" not in joined, "pblup 的 DOCX 异常处理分支仍会抛 E_DOCX"
    assert "DOCX_REPORT_FAILED" in joined, \
        "DOCX 失败分支必须追加 DOCX_REPORT_FAILED 告警（可追溯）"
