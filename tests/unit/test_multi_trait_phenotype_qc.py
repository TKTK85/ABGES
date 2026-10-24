# -*- coding: utf-8 -*-
"""ABGES 回归测试：多性状表型清洗（逐性状清洗 → ID 并集组装）与 PH007 口径。

背景（2026-10-12，先生指令）
----------------------------
1. 多性状 PBLUP 改为"先分别走 ABGES 项目表型清洗，最后组装计算"：
   每个性状独立跑完整 PH001–PH007，再按 ID 并集合并为一张宽表。
2. PH007 铁律修订：多性状由"强制 z-score"改为"不标准化，保持原始量纲"。

本测试锁定六点：
  1. 单性状路径行为完全不变（不回归）；
  2. 逐性状清洗后按 ID 并集组装，行集合正确、固定效应不丢；
  3. 各性状保留自身有效记录，未被其他性状的清洗动作牵连；
  4. 某性状脏数据 → 整任务中止并点名该性状（禁止静默降级）；
  5. PH007 multi 模式默认不标准化；multi_force_standardize=true 时恢复 z-score（恢复开关可用）；
  6. 变更流水按性状留痕（每条带 trait 标注，并有组装 MERGE 记录）。
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

TRAITS = ['ACL', 'EN']
N = 60


def _base_frame(n=N):
    """双性状良态数据：Gen×Batch 四组各 15 条，PH006 分组剔除有足够样本量。"""
    rows = []
    for i in range(n):
        rows.append({
            'ID': 'X%04d' % i,
            'Gen': str(1 + i % 2),
            'Batch': str(1 + (i // 2) % 2),
            'ACL': round(10.0 + 0.25 * (i % 20), 4),        # 10.0 ~ 26.0
            'EN': float(100 + (i * 2) % 150),               # 100 ~ 248
        })
    return pd.DataFrame(rows)


# ------------------------------------------------------------------
# 1. 单性状路径不回归
# ------------------------------------------------------------------
def case_single_path_unchanged():
    frame = _base_frame()
    qc = phenotype_tool.run_phenotype_qc(
        frame, traits=['ACL'], fixed_effects=['Gen', 'Batch'], covariates=[],
        mode='single')
    if isinstance(qc, phenotype_tool.MultiTraitQCResult):
        raise AssertionError('单性状不应返回 MultiTraitQCResult')
    if bool(qc.statistics.get('standardized')):
        raise AssertionError('单性状合同为 none，不应做标准化')
    untouched = bool(qc.frame['ACL'].equals(frame['ACL']))
    return 'standardized=False, 取值未被改动=%s' % untouched


# ------------------------------------------------------------------
# 2. ID 并集组装
# ------------------------------------------------------------------
def case_union_assembly():
    frame = _base_frame()
    # 人为制造差异：前 10 个个体 ACL 缺失（代表该性状无记录），后 5 个个体 EN 缺失
    frame.loc[0:9, 'ACL'] = None
    frame.loc[N - 5:N - 1, 'EN'] = None

    result = phenotype_tool.run_phenotype_qc_by_trait(
        frame, traits=TRAITS, fixed_effects=['Gen', 'Batch'], covariates=[],
        rename_detail={})

    if not isinstance(result, phenotype_tool.MultiTraitQCResult):
        raise AssertionError('应返回 MultiTraitQCResult，实得 %r' % type(result))
    rows_of_result = int(result.frame.shape[0])
    acl_ids = phenotype_tool.run_phenotype_qc(
        frame, traits=['ACL'], fixed_effects=['Gen', 'Batch'], covariates=[],
        mode='single').frame['ID'].astype(str).tolist()
    en_ids = phenotype_tool.run_phenotype_qc(
        frame, traits=['EN'], fixed_effects=['Gen', 'Batch'], covariates=[],
        mode='single').frame['ID'].astype(str).tolist()
    expected_union = len(set(acl_ids) | set(en_ids))
    if rows_of_result != expected_union:
        raise AssertionError('并集行数应为 %d，实得 %d'
                             % (expected_union, rows_of_result))
    # 固定效应/协变量在并集行上不得为空
    missing_fe = int(result.frame['Gen'].isna().sum()) + int(
        result.frame['Batch'].isna().sum())
    if missing_fe:
        raise AssertionError('固定效应列出现 %d 个空值' % missing_fe)
    return '并集行数=%d, 固定效应无缺失, 性状列=%s' % (
        rows_of_result, list(TRAITS))


# ------------------------------------------------------------------
# 3. 性状间互不牵连（各自保留自身有效记录）
# ------------------------------------------------------------------
def case_traits_keep_own_records():
    frame = _base_frame()
    frame.loc[0:9, 'ACL'] = None          # ACL 少 10 条
    frame.loc[N - 5:N - 1, 'EN'] = None   # EN 少 5 条

    result = phenotype_tool.run_phenotype_qc_by_trait(
        frame, traits=TRAITS, fixed_effects=['Gen', 'Batch'], covariates=[],
        rename_detail={})

    acl_valid = int(result.frame['ACL'].notna().sum())
    en_valid = int(result.frame['EN'].notna().sum())
    if acl_valid + en_valid != (N - 10) + (N - 5):
        raise AssertionError('逐性状有效记录数不符：ACL=%d EN=%d' % (acl_valid, en_valid))
    if result.trait_metrics['ACL']['valid_records'] != acl_valid:
        raise AssertionError('trait_metrics 与合并表不一致')
    return 'ACL有效=%d, EN有效=%d（互不影响）' % (acl_valid, en_valid)


# ------------------------------------------------------------------
# 4. 单性状脏数据 → 整任务中止，点名性状（禁止静默降级）
# ------------------------------------------------------------------
def case_dirty_trait_aborts_whole_task():
    frame = _base_frame()
    # 真实脏数据场景：列被混入非数值后 dtype 退化为 object
    frame['EN'] = frame['EN'].astype(object)
    frame.loc[3, 'EN'] = '一百二'   # 非数值：PH003 BLOCK
    try:
        phenotype_tool.run_phenotype_qc_by_trait(
            frame, traits=TRAITS, fixed_effects=['Gen', 'Batch'], covariates=[],
            rename_detail={})
        raise AssertionError('脏数据性状应整任务中止，但通过了')
    except core.AbgesError as exc:
        message = str(exc)
        if 'EN' not in message:
            raise AssertionError('中止信息未点名失败性状：%s' % message)
        return '已中止并点名性状：%s' % message[:46]


# ------------------------------------------------------------------
# 5. PH007：multi 默认不标准化；multi_force_standardize=true 可恢复
# ------------------------------------------------------------------
def case_ph007_multi_not_standardized():
    config = core.load_yaml_config(phenotype_tool.PHENOTYPE_QC_CONFIG)
    rules = config.get('rules', {})
    if rules.get('PH007', {}).get('multi_force_standardize') is not False:
        raise AssertionError('正式配置 PH007.multi_force_standardize 应为 false')
    if phenotype_tool._resolve_standardize_contract('multi', TRAITS, rules) is not False:
        raise AssertionError('multi 模式默认不应标准化')
    return 'multi 默认不标准化（配置与合同一致）'


def case_ph007_recovery_switch():
    rules = {'PH007': {'multi_force_standardize': True}}
    if phenotype_tool._resolve_standardize_contract('multi', TRAITS, rules) is not True:
        raise AssertionError('恢复开关置 true 时应重新启用标准化')
    rules_off = {'PH007': {'multi_force_standardize': False}}
    if phenotype_tool._resolve_standardize_contract('multi', TRAITS, rules_off) is not False:
        raise AssertionError('恢复开关置 false 时应关闭标准化')
    return '恢复开关双向可用'


def case_ph007_multi_mode_rejects_single_trait():
    try:
        phenotype_tool._resolve_standardize_contract('multi', ['ACL'], {})
        raise AssertionError('multi 模式只有 1 个性状时应报错')
    except core.AbgesError:
        return 'multi 单性状已阻断'


# ------------------------------------------------------------------
# 6. 变更流水按性状留痕 + 组装记录
# ------------------------------------------------------------------
def case_change_log_tagged_by_trait():
    frame = _base_frame()
    frame.loc[2, 'ACL'] = 9999.0     # 极端值，触发 PH006 组内剔除
    result = phenotype_tool.run_phenotype_qc_by_trait(
        frame, traits=TRAITS, fixed_effects=['Gen', 'Batch'], covariates=[],
        rename_detail={})
    tagged = [item for item in result.change_log if item.get('trait')]
    if not tagged:
        raise AssertionError('变更流水未按性状标注')
    traits_seen = sorted({item['trait'] for item in tagged})
    merge_records = [item for item in result.change_log
                     if item.get('action') == 'MERGE']
    if not merge_records:
        raise AssertionError('缺少组装（MERGE）留痕')
    # PH006 只应把极端值在 ACL 上置缺，EN 不受影响
    en_valid = int(result.frame['EN'].notna().sum())
    if en_valid != N:
        raise AssertionError('EN 被 ACL 的离群剔除牵连：EN 有效=%d' % en_valid)
    return '留痕性状=%s, MERGE=%d 条, EN 未受牵连' % (traits_seen, len(merge_records))


# ------------------------------------------------------------------
# pytest 用例包装（由脚本式 check() 自动转换，断言逻辑与判定条件均未改动）
# ------------------------------------------------------------------

def test_single_path_unchanged():
    check('单性状路径不回归', case_single_path_unchanged)

def test_union_assembly():
    check('ID 并集组装', case_union_assembly)

def test_traits_keep_own_records():
    check('各性状保留自身有效记录', case_traits_keep_own_records)

def test_dirty_trait_aborts_whole_task():
    check('脏数据性状整任务中止', case_dirty_trait_aborts_whole_task)

def test_ph007_multi_not_standardized():
    check('PH007 multi 默认不标准化', case_ph007_multi_not_standardized)

def test_ph007_recovery_switch():
    check('PH007 恢复开关双向可用', case_ph007_recovery_switch)

def test_ph007_multi_mode_rejects_single_trait():
    check('PH007 multi 单性状阻断', case_ph007_multi_mode_rejects_single_trait)

def test_change_log_tagged_by_trait():
    check('变更流水按性状留痕', case_change_log_tagged_by_trait)
