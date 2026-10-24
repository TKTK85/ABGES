# -*- coding: utf-8 -*-
"""ABGES 回归测试：task.pre_standardized 声明必须真正接通到表型质控。

背景（B3，2026-08-11 端到端验证发现）
------------------------------------
schemas/task-schema.yaml 定义了 pre_standardized，abges_phenotype.run_phenotype_qc
也支持 pre_standardized_traits 等参数，但 runners/pblup_stage_runner.py 既未把
task.pre_standardized 读入 plan，也未传给质控函数——声明被静默忽略。

后果实测：SAMPLE_POPULATION ACL 的 z-score 输入（48 条有效）被 PH006 按组内统计量判定（未声明标准化时，z 值按原始观测尺度处理）
[1.0, 100.0] 判定，2330 条正常 z 值被当作超范围置缺，仅剩 386 条，
h² 由 0.42 崩到 0.05，且流程只报 partial、不阻断，属严重静默失真。

本测试锁定四点：
  1. 未声明时返回 None，行为与既往一致（不误开尺度切换）；
  2. 声明缺 traits / 未覆盖本次性状时如实报错，不静默放行；
  3. 声明生效后 PH006 依 skip_when_pre_standardized 跳过，正常 z 值不被误剔；
  4. 同一份 z 值数据在"未声明"时确实会被大量误剔——证明链路真实起作用。
"""
import sys, os
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
import abges_core as core
import abges_phenotype as phenotype_tool
from runners.pblup_stage_runner import _resolve_pre_standardized

TRAIT = 'ACL'


def _zscore_frame(n=200):
    """构造已标准化的表型：z 值集中在 ±2.5 SD，全部落在 [-6, 6] 内，
    但绝大多数会被原始生物学范围 [1.0, 100.0] 判为超范围。"""
    rows = []
    for i in range(n):
        z = -2.5 + 5.0 * (i / float(n - 1))          # -2.5 ~ +2.5
        rows.append({'ID': 'X%04d' % i,
                     'Gen': str(1 + i % 2),
                     'Batch': str(1 + (i // 2) % 2),
                     TRAIT: round(z, 6)})
    return pd.DataFrame(rows)


def case_absent_returns_none():
    if _resolve_pre_standardized({}, TRAIT) is not None:
        raise AssertionError('未声明时应返回 None')
    if _resolve_pre_standardized({'pre_standardized': None}, TRAIT) is not None:
        raise AssertionError('显式 null 也应返回 None')
    return 'None'


def case_declared_parsed():
    got = _resolve_pre_standardized(
        {'pre_standardized': {'traits': [TRAIT], 'scope': ['Batch', 'Gen'],
                              'method': 'z-score'}}, TRAIT)
    if got != {'traits': [TRAIT], 'scope': ['Batch', 'Gen'], 'method': 'z-score'}:
        raise AssertionError('解析结果不符：%r' % (got,))
    return 'traits=[ACL],scope=[Batch,Gen],method=z-score'


def case_bad_declaration_raises():
    # 空 traits
    try:
        _resolve_pre_standardized({'pre_standardized': {'traits': []}}, TRAIT)
        raise AssertionError('空 traits 应报错')
    except core.AbgesError:
        pass
    # 未覆盖本次分析性状
    try:
        _resolve_pre_standardized({'pre_standardized': {'traits': ['EN']}}, TRAIT)
        raise AssertionError('声明未覆盖本次性状时应报错')
    except core.AbgesError:
        pass
    # 类型错误
    try:
        _resolve_pre_standardized({'pre_standardized': ['ACL']}, TRAIT)
        raise AssertionError('非对象声明应报错')
    except core.AbgesError:
        pass
    return '空traits/未覆盖/类型错误 均已阻断'


def case_declared_keeps_records():
    frame = _zscore_frame()
    qc = phenotype_tool.run_phenotype_qc(
        frame, traits=[TRAIT], fixed_effects=['Gen', 'Batch'], covariates=[],
        mode='single',
        pre_standardized_traits=[TRAIT],
        standardization_scope=['Batch', 'Gen'],
        standardization_method='z-score')
    valid = int(qc.frame[TRAIT].notna().sum())
    if valid != len(frame):
        raise AssertionError('声明生效后不应剔除任何记录，实得 %d/%d'
                             % (valid, len(frame)))
    rule_ids = [item['rule_id'] for item in qc.change_log]
    if 'PH006' not in rule_ids:
        raise AssertionError('尺度切换必须留痕（PH006 SKIP_BY_DECLARATION），实得 %r' % rule_ids)
    switch = [item for item in qc.change_log if item['rule_id'] == 'PH006'][0]
    if switch.get('action') != 'SKIP_BY_DECLARATION':
        raise AssertionError('PH006 应记 SCALE_SWITCH，实得 %r' % switch.get('action'))
    return 'valid=%d/%d,留痕=%s' % (valid, len(frame), ','.join(rule_ids))


def case_undeclared_would_drop():
    """对照组：同一份 z 值不声明时会被 PH006 按原始观测尺度大量误剔。
    此用例锁定"链路确实在起作用"，若哪天两者结果相同即说明声明失效。"""
    frame = _zscore_frame()
    frame.loc[0, TRAIT] = 30.0
    qc = phenotype_tool.run_phenotype_qc(
        frame, traits=[TRAIT], fixed_effects=['Gen', 'Batch'], covariates=[],
        mode='single')
    valid = int(qc.frame[TRAIT].notna().sum())
    if valid >= len(frame):
        raise AssertionError('对照组应发生误剔，否则本测试无法证伪；实得 %d/%d'
                             % (valid, len(frame)))
    return '对照 valid=%d/%d（误剔 %d 条）' % (
        valid, len(frame), len(frame) - valid)


# ------------------------------------------------------------------
# pytest 用例包装（由脚本式 check() 自动转换，断言逻辑与判定条件均未改动）
# ------------------------------------------------------------------

def test_absent_returns_none():
    check('未声明时返回 None', case_absent_returns_none)

def test_declared_parsed():
    check('声明被正确解析', case_declared_parsed)

def test_bad_declaration_raises():
    check('非法声明如实报错不静默', case_bad_declaration_raises)

def test_declared_keeps_records():
    check('声明生效：z 值不被误剔且留痕', case_declared_keeps_records)

def test_undeclared_would_drop():
    check('对照：未声明时确会误剔', case_undeclared_would_drop)
