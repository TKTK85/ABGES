# -*- coding: utf-8 -*-
"""
ABGES 表型质量控制模块
======================
严格实现 configs/qc/phenotype-qc.yaml 中 PH001–PH007 规则。

约定：
  - 阈值一律来自配置文件，本模块不硬编码任何正式阈值。
  - 每条被剔除/修改的记录都进 change_log，附 rule_id。
  - 处理前后统计强制产出，缺失即视为无效处理。
  - 绝不做无依据填补（PH004 forbid_imputation）。
"""

import abges_core as core
import abges_dataio as dataio
import abges_unknown as unknown_tool


PHENOTYPE_QC_CONFIG = "configs/qc/phenotype-qc.yaml"
TRAIT_REGISTRY_CONFIG = "configs/traits/trait-registry.yaml"


class PhenotypeQCResult(object):
    """表型 QC 结果容器，承载数据、变更流水、统计与告警。"""

    def __init__(self):
        self.frame = None
        self.change_log = []
        self.warnings = []
        self.blocks = []
        self.human_confirmations = []
        self.statistics = {}
        self.rename_detail = {}
        self.effective_fixed_effects = []
        self.effective_covariates = []
        self.dropped_fixed_effects = []
        # 声明存在但源数据中缺失（被静默丢弃）的固定效应列——强制约束机制据此判定建模退化
        self.missing_fixed_effects = []
        # 未知 / 中文表头 → 英文代号 自动处理结果（2026-10-13 新增）
        self.unknown_columns = []                 # normalize_headers 报告（每列一条）
        self.auto_included_fixed_effects = []    # 自动纳入的固定效应代号
        self.auto_included_covariates = []       # 自动纳入的协变量代号
        # 输入状态声明的生效记录（供审计与报告，声明未生效时为空）
        self.input_state = {}

    def log_change(self, rule_id, action, affected, reason, detail=None):
        """记录一次数据变更，affected 为受影响记录数。"""
        self.change_log.append({
            "rule_id": rule_id,
            "action": action,
            "affected": int(affected),
            "reason": reason,
            "detail": detail or {},
        })

    def to_dict(self):
        return {
            "change_log": self.change_log,
            "warnings": self.warnings,
            "blocks": self.blocks,
            "human_confirmations": self.human_confirmations,
            "statistics": self.statistics,
            "rename_detail": self.rename_detail,
            "effective_fixed_effects": self.effective_fixed_effects,
            "effective_covariates": self.effective_covariates,
            "dropped_fixed_effects": self.dropped_fixed_effects,
            "unknown_columns": self.unknown_columns,
            "auto_included_fixed_effects": self.auto_included_fixed_effects,
            "auto_included_covariates": self.auto_included_covariates,
            "input_state": self.input_state,
        }


# ===============================================================
# 一、性状登记表查询
# ===============================================================

def load_trait_registry():
    """加载性状登记表，返回 {trait_id: 定义}。"""
    registry = core.load_yaml_config(TRAIT_REGISTRY_CONFIG)
    catalog = {}
    for section in ("identifiers", "fixed_effect_candidates",
                    "covariate_candidates", "traits"):
        for entry in registry.get(section, []) or []:
            catalog[entry["trait_id"]] = dict(entry, _section=section)
    return catalog


# ===============================================================
# 二、读取与标准化
# ===============================================================

def load_phenotype(file_path, sheet=None, config=None):
    """
    读取表型文件并完成表头标准化。
    返回 (DataFrame, 读取信息, 重命名明细, 未识别列)。
    """
    config = config or core.load_yaml_config(PHENOTYPE_QC_CONFIG)
    input_config = config.get("input", {})
    sheet_index = sheet if sheet is not None else input_config.get("sheet", 0)
    frame, info = dataio.read_table(file_path, sheet=sheet_index,
                                    delimiter=input_config.get("delimiter"))
    frame, rename_detail, unmapped = dataio.normalize_header(
        frame, config.get("header_map", {}))
    return frame, info, rename_detail, unmapped


# ===============================================================
# 三、统计
# ===============================================================

def _coefficient_of_variation(mean_value, sd_value):
    """
    变异系数（%）。均值接近 0 时 CV 失去统计意义——标准化数据的均值为
    1e-17 量级，直接相除会得到 1e+18 这类天文数字并污染报告。
    此类情形返回 None，由报告层显式呈现"不适用"，不放任溢出。
    """
    if not mean_value:
        return None
    if sd_value and abs(mean_value) < abs(sd_value) * 1e-6:
        return None
    return round(sd_value / mean_value * 100, 4)


def describe_continuous(series):
    """
    连续变量描述统计，字段与 phenotype-qc.yaml statistics 段一致。
    """
    import pandas as pd

    numeric = pd.to_numeric(series, errors="coerce")
    total = int(len(numeric))
    valid = numeric.dropna()
    valid_n = int(valid.shape[0])
    stats = {
        "N": total,
        "ValidN": valid_n,
        "Missing": total - valid_n,
        "MissingPct": round((total - valid_n) / total * 100, 4) if total else None,
    }
    if valid_n == 0:
        for key in ("Mean", "SD", "Variance", "CV", "Min", "P1", "P5", "P25",
                    "Median", "P75", "P95", "P99", "Max"):
            stats[key] = None
        return stats
    mean_value = float(valid.mean())
    sd_value = float(valid.std(ddof=1)) if valid_n > 1 else 0.0
    stats.update({
        "Mean": round(mean_value, 6),
        "SD": round(sd_value, 6),
        "Variance": round(sd_value ** 2, 6),
        "CV": _coefficient_of_variation(mean_value, sd_value),
        "Min": round(float(valid.min()), 6),
        "P1": round(float(valid.quantile(0.01)), 6),
        "P5": round(float(valid.quantile(0.05)), 6),
        "P25": round(float(valid.quantile(0.25)), 6),
        "Median": round(float(valid.median()), 6),
        "P75": round(float(valid.quantile(0.75)), 6),
        "P95": round(float(valid.quantile(0.95)), 6),
        "P99": round(float(valid.quantile(0.99)), 6),
        "Max": round(float(valid.max()), 6),
    })
    return stats


def describe_grouped(frame, trait, group_columns):
    """按分组字段给出每组的样本量与均值，用于判断组间不平衡。"""
    import pandas as pd

    result = {}
    for column in group_columns:
        if column not in frame.columns:
            continue
        numeric = pd.to_numeric(frame[trait], errors="coerce")
        grouped = numeric.groupby(frame[column].astype(str))
        result[column] = {
            str(level): {
                "N": int(sub.notna().sum()),
                "Mean": round(float(sub.mean()), 6) if sub.notna().any() else None,
            }
            for level, sub in grouped
        }
    return result


def build_statistics_block(frame, traits, config):
    """产出一份完整统计块（整体 + 分组）。"""
    group_candidates = (config.get("statistics", {})
                        .get("group_by_candidates", []))
    block = {"n_records": int(frame.shape[0]), "traits": {}}
    for trait in traits:
        if trait not in frame.columns:
            continue
        block["traits"][trait] = {
            "overall": describe_continuous(frame[trait]),
            "by_group": describe_grouped(frame, trait, group_candidates),
        }
    return block


# ===============================================================
# 四、主流程：QC 全套规则
# ===============================================================

def run_phenotype_qc(frame, traits, config=None, fixed_effects=None,
                     covariates=None, mode="single", rename_detail=None,
                     pre_standardized_traits=None, standardization_scope=None,
                     standardization_method=None):
    """
    执行 PH001–PH007 全套规则。

    参数：
      frame          标准化表头后的原始表型
      traits         目标性状列表
      fixed_effects  固定效应列（None 时取配置默认）
      covariates     协变量列（None 时取配置默认）
      mode           single | multi，影响 PH007 标准化

      pre_standardized_traits
                     声明"输入中已由上游完成标准化"的性状列表。
                     被声明的性状：PH006 依 skip_when_pre_standardized
                     决定是否跳过。
                     声明不取消任何规则，只修正判定尺度，且每次生效
                     都会写入 change_log 与 warnings，不会静默放行。
                     为 None 时取配置 input_state 中的默认值。
      standardization_scope / standardization_method
                     标准化的分组字段与方法，仅作审计记录。

    返回 PhenotypeQCResult。BLOCK 类问题以 AbgesError 抛出。
    """
    import pandas as pd

    config = config or core.load_yaml_config(PHENOTYPE_QC_CONFIG)
    rules = config.get("rules", {})
    catalog = load_trait_registry()
    result = PhenotypeQCResult()
    result.rename_detail = rename_detail or {}

    declared_std, std_scope, std_method = _resolve_input_state(
        config, pre_standardized_traits, standardization_scope,
        standardization_method)

    working = frame.copy()
    id_column = config.get("input", {}).get("id_column", "ID")

    # ---------- 未知 / 中文表头 → 英文代号 自动转换（第一阶段：更名） ----------
    # 先生定调：凡中文表头一律转换为英文；英文表头保持不变交由显式请求逻辑处理。
    # 集成在 PH001 之前，使下游（PH005 有效水平、PH006 分组、引擎命令组装）统一用英文引用。
    # 注意：复用 result.rename_detail 语义（原名→标准名），并保留 _align_declared 对齐能力。
    unknown_cfg = config.get("unknown_columns", {}) or {}
    unknown_report = []
    if unknown_cfg.get("enabled", True):
        raw_registry = core.load_yaml_config(TRAIT_REGISTRY_CONFIG)
        working, auto_rename, unknown_report = unknown_tool.normalize_headers(
            working, unknown_cfg, registry=raw_registry)
        result.rename_detail.update(auto_rename)
        result.unknown_columns = unknown_report

    # ---------- PH001 ID 列存在性 ----------
    if id_column not in working.columns:
        raise core.AbgesError("PH001", "标准化后缺少 ID 列",
                              {"available": list(working.columns)})

    # ---------- PH001 性状列存在性 ----------
    missing_traits = [trait for trait in traits if trait not in working.columns]
    if missing_traits:
        raise core.AbgesError("PH001", "请求的性状列不存在：%s" % missing_traits,
                              {"available": list(working.columns)})

    # ---------- PH007 标准化 ----------
    standardize = _resolve_standardize_contract(mode, traits, rules)

    # 处理前统计
    result.statistics["before"] = build_statistics_block(working, traits, config)

    # ---------- PH002 ID 缺失 ----------
    working[id_column] = dataio.normalize_identifier_series(working[id_column])
    id_missing_mask = working[id_column].isin(["NA", ""]) | working[id_column].isna()
    if id_missing_mask.any():
        removed = int(id_missing_mask.sum())
        working = working[~id_missing_mask].copy()
        result.log_change("PH002", "EXCLUDE", removed, "ID 为空或无效")

    # ---------- PH002 ID 重复 ----------
    duplicate_mask = working[id_column].duplicated(keep=False)
    if duplicate_mask.any():
        duplicate_ids = sorted(working.loc[duplicate_mask, id_column].unique().tolist())
        policy = rules.get("PH002", {}).get("duplicate_policy", "flag")
        if policy == "deduplicate":
            keep = rules.get("PH002", {}).get("keep", "first")
            before_rows = working.shape[0]
            working = working.drop_duplicates(subset=[id_column], keep=keep).copy()
            result.log_change("PH002", "DEDUPLICATE",
                              before_rows - working.shape[0],
                              "同一 ID 多条记录，按 keep=%s 去重" % keep,
                              {"duplicate_ids": duplicate_ids[:50]})
        else:
            result.warnings.append({
                "rule_id": "PH002",
                "message": "存在重复 ID %d 个，按 flag 策略保留全部记录" % len(duplicate_ids),
                "sample": duplicate_ids[:20],
            })

    # ---------- 有效效应列解析（含声明名 → 标准化名别名对齐） ----------
    # 背景：load_phenotype 阶段 header_map 会把数据列标准化（如 批次→Batch、
    # 产蛋数300日龄→EN），而任务 yaml 声明的还是原始列名，导致 PH001 误判
    # "列不存在"而静默丢效应（2026-08-24 实验一图C 16 任务分叉的根因）。
    # 修复：声明名先经 rename_detail 别名映射到标准化名再匹配；仍未命中才告警。
    default_fixed = config.get("fixed_effect_cols", [])
    default_covariate = config.get("covariate_cols", [])
    requested_fixed = fixed_effects if fixed_effects is not None else default_fixed
    requested_covariate = covariates if covariates is not None else default_covariate
    reverse_rename = {str(k).strip().lower(): v
                      for k, v in result.rename_detail.items()}

    def _align_declared(name):
        """声明名 → 标准化名：直接命中 > rename_detail 别名 > 原名。"""
        if name in working.columns:
            return name
        target = reverse_rename.get(str(name).strip().lower())
        if target is not None and target in working.columns:
            return target
        return name

    aligned_fixed = {}
    aligned_covariate = {}
    for name in requested_fixed:
        aligned = _align_declared(name)
        aligned_fixed[name] = aligned
        if aligned != name:
            result.log_change(
                "PH001", "ALIAS_ALIGNED", 1,
                "固定效应声明名 %s 经表头标准化映射对齐为 %s" % (name, aligned),
                {"declared": name, "standardized": aligned})
    for name in requested_covariate:
        aligned = _align_declared(name)
        aligned_covariate[name] = aligned
        if aligned != name:
            result.log_change(
                "PH001", "ALIAS_ALIGNED", 1,
                "协变量声明名 %s 经表头标准化映射对齐为 %s" % (name, aligned),
                {"declared": name, "standardized": aligned})
    requested_fixed = [aligned_fixed[n] for n in requested_fixed]
    requested_covariate = [aligned_covariate[n] for n in requested_covariate]

    present_fixed = [name for name in requested_fixed if name in working.columns]
    present_covariate = [name for name in requested_covariate if name in working.columns]
    for name in requested_fixed:
        if name not in working.columns:
            result.missing_fixed_effects.append(name)
            result.warnings.append({
                "rule_id": "PH001",
                "message": "固定效应列不存在，已跳过：%s" % name,
            })
    for name in requested_covariate:
        if name not in working.columns:
            result.warnings.append({
                "rule_id": "PH001",
                "message": "协变量列不存在，已跳过：%s" % name,
            })

    # ---------- PH003 性状与协变量数值化（WARNING，仅提醒，不私自置 NA）----------
    # 行为契约（2026-08-17 PH003 修订）：
    #   - 发现无法转数值的取值 → 仅记 WARNING 与原值清单，**禁止自行置换为 NA**；
    #   - 若任一性状列存在脏数据，立即 AbgesError 阻断后续流程，待人工清洗后再跑；
    #   - 协变量列仅记 WARNING 不阻断（不参与建模主路径）。
    for column in list(traits):
        converted, bad_values = dataio.to_numeric_column(working[column], column)
        if bad_values:
            # 仅记 WARNING，绝不覆盖原值
            result.warnings.append({
                "rule_id": "PH003",
                "code": "PH003_DIRTY_VALUES",
                "message": ("列 %s 存在 %d 个无法转数值的取值（PH003 WARNING）："
                            "已保留原值，禁止私自置 NA，请人工清洗后重跑"
                            % (column, len(bad_values))),
                "column": column,
                "sample": bad_values[:20],
                "action_taken": "保留原值，阻断后续流程，需人工处置",
            })
            result.log_change("PH003", "WARNING", len(bad_values),
                              "列 %s 存在无法转数值的取值，已保留原值待人工处置"
                              % column,
                              {"column": column, "bad_values": bad_values[:20]})
            # 阻断：脏数据进 HIBLUP 会因非数值整体失败，不如在 PH003 处显式阻断
            raise core.AbgesError(
                "PH003",
                "性状 %s 存在 %d 个无法转数值取值，已保留原值；"
                "按 PH003 WARNING 契约需人工清洗后再评估" % (
                    column, len(bad_values)),
                {"column": column,
                 "bad_values_sample": bad_values[:20],
                 "total": len(bad_values)})
        working[column] = converted
    for column in present_covariate:
        converted, bad_values = dataio.to_numeric_column(working[column], column)
        if bad_values:
            result.warnings.append({
                "rule_id": "PH003",
                "code": "PH003_DIRTY_VALUES",
                "message": ("协变量列 %s 存在 %d 个无法转数值的取值：已保留原值，"
                            "请人工清洗" % (column, len(bad_values))),
                "column": column,
                "sample": bad_values[:20],
            })
            result.log_change("PH003", "WARNING", len(bad_values),
                              "协变量列 %s 存在无法转数值的取值" % column,
                              {"column": column, "bad_values": bad_values[:20]})
        working[column] = converted

    # ---------- PH006 分组异常值剔除 ----------
    outlier_rule = rules.get("PH006", {})
    skip_std = bool(outlier_rule.get("skip_when_pre_standardized", True))
    ph008_traits = list(traits)
    if declared_std and skip_std:
        exempt = [t for t in traits if t in declared_std]
        ph008_traits = [t for t in traits if t not in declared_std]
        if exempt:
            reason = ("性状 %s 已声明按 %s 分组标准化，组内离群统计已由上游施加，"
                      "本轮不再二次剔除" % (exempt, std_scope or "未声明分组"))
            result.log_change("PH006", "SKIP_BY_DECLARATION", 0, reason,
                              {"traits": exempt, "scope": std_scope,
                               "threshold": outlier_rule.get("threshold")})
            result.warnings.append({
                "rule_id": "PH006",
                "code": "PH006_SKIPPED",
                "message": reason,
                "action_taken": "按任务声明跳过 PH006 组内离群剔除，已记入 change_log",
            })
    working = _remove_group_outliers(working, ph008_traits, present_fixed,
                                     outlier_rule, result)

    # ---------- PH004 缺失保持 ----------
    if rules.get("PH004", {}).get("forbid_imputation", True):
        result.warnings.append({
            "rule_id": "PH004",
            "code": "PH004_NO_IMPUTATION",
            "message": "缺失值保持 NA，未做任何填补（由引擎在建模时排除）",
            "action_taken": "按 forbid_imputation 保持 NA，未填补任何取值",
        })

    # ---------- PH005 固定效应有效水平 ----------
    min_levels = rules.get("PH005", {}).get("min_levels", 2)
    effective_fixed = []
    for column in present_fixed:
        level_count = int(working[column].dropna().astype(str).str.strip().nunique())
        if level_count < min_levels:
            result.dropped_fixed_effects.append(
                {"column": column, "levels": level_count, "code": "E022"})
            result.warnings.append({
                "rule_id": "PH005",
                "code": "E022",
                "message": "固定效应 %s 有效水平数=%d(<%d) 不可估，命令层将剔除"
                           % (column, level_count, min_levels),
            })
        else:
            effective_fixed.append(column)
    result.effective_fixed_effects = effective_fixed
    result.effective_covariates = present_covariate

    # ---------- 未知 / 中文表头 自动纳入建模（第二阶段：纳入，须在 PH005 之后） ----------
    # 必须在 PH005 设定 effective_* 之后执行：PH005 会整体重赋值这两个字段，
    # 若提前纳入会被覆盖。固定效应按「中文来源 + 内容判为固定效应」自动纳入；
    # 连续型中文列默认仅登记候选、不静默入模（防模型污染）。
    if unknown_report:
        min_levels_unk = rules.get("PH005", {}).get("min_levels", 2)
        _auto_include_unknown_columns(working, result, unknown_report,
                                      unknown_cfg, min_levels_unk)
        # 仅当确实存在被转换/发现的未知列时才落盘映射表（不污染纯已知数据）
        if any(e.get("source") not in ("known", "unknown_english")
               for e in unknown_report):
            map_out = unknown_cfg.get("map_output")
            if map_out:
                try:
                    run_id = core.make_timestamp()
                except Exception:
                    run_id = None
                unknown_tool.write_unknown_map(unknown_report, map_out, run_id=run_id)

    # ---------- PH008 数值列重复/错位检测 ----------
    _ph008_check(working, rules, result)

    # ---------- PH009 派生比率恒等检测 ----------
    _ph009_check(working, rules, result)

    # ---------- PH010 分类列水平编码一致性 ----------
    _ph010_check(working, present_fixed, rules, result)

    # ---------- 清洗下降比例统计（原 PH008 样本量充足性门控已于 2026-08-17 删除）----------
    # 仅保留 drop_rate 统计值供 G2-03 等闸门参考；不再触发人工确认或 BLOCK 阻断。
    before_rows = result.statistics["before"]["n_records"]
    after_rows = int(working.shape[0])
    drop_rate = (before_rows - after_rows) / before_rows if before_rows else 0.0


    # ---------- PH007 标准化执行 ----------
    if standardize:
        for trait in traits:
            values = working[trait]
            mean_value = values.mean()
            sd_value = values.std(ddof=1)
            if sd_value and sd_value > 0:
                working[trait] = (values - mean_value) / sd_value
                result.log_change("PH007", "STANDARDIZE",
                                  int(values.notna().sum()),
                                  "PH007 标准化合同生效（z-score）：%s" % trait,
                                  {"mean": round(float(mean_value), 6),
                                   "sd": round(float(sd_value), 6)})
            else:
                raise core.AbgesError("PH007",
                                      "性状 %s 标准差为 0，无法标准化" % trait)

    result.statistics["after"] = build_statistics_block(working, traits, config)
    result.statistics["standardized"] = bool(standardize)
    result.statistics["drop_rate"] = round(drop_rate, 6)
    result.input_state = {
        "pre_standardized_traits": sorted(declared_std),
        "standardization_scope": std_scope,
        "standardization_method": std_method,
        "ph008_skipped_traits": sorted(
            set(traits) & declared_std) if (
                declared_std and rules.get("PH006", {}).get(
                    "skip_when_pre_standardized", True)) else [],
    }
    result.statistics["input_state"] = dict(result.input_state)
    result.frame = working
    return result


class MultiTraitQCResult(object):
    """多性状逐性状清洗后组装的结果容器（鸭子类型，兼容 PhenotypeQCResult）。

    2026-10-12 新增（先生指令）：多性状 PBLUP 先逐性状各自跑一遍完整
    PH001–PH007，再按 ID 组装。字段名与 PhenotypeQCResult 保持一致，
    使 build_engine_phenotype 与下游 runner 无需为多性状额外分支；
    额外提供 per_trait / trait_metrics / change_log_by_trait 等逐性状明细。
    """

    def __init__(self, traits, join_column="ID", join_mode="union"):
        self.frame = None
        self.change_log = []
        self.warnings = []
        self.blocks = []
        self.human_confirmations = []
        self.statistics = {}
        self.rename_detail = {}
        self.effective_fixed_effects = []
        self.effective_covariates = []
        self.dropped_fixed_effects = []
        self.missing_fixed_effects = []
        self.input_state = {}
        # 未知 / 中文表头 → 英文代号 自动处理结果（与 PhenotypeQCResult 对齐）
        self.unknown_columns = []
        self.auto_included_fixed_effects = []
        self.auto_included_covariates = []
        # ---- 逐性状明细 ----
        self.traits = list(traits)
        self.join_column = join_column
        self.join_mode = join_mode
        self.per_trait = {}
        self.statistics_by_trait = {}
        self.change_log_by_trait = {}
        self.trait_metrics = {}
        self.failed_traits = []

    def log_change(self, rule_id, action, affected, reason, detail=None):
        """记录一次数据变更，affected 为受影响记录数。"""
        self.change_log.append({
            "rule_id": rule_id,
            "action": action,
            "affected": int(affected),
            "reason": reason,
            "detail": detail or {},
        })

    def to_dict(self):
        return {
            "change_log": self.change_log,
            "warnings": self.warnings,
            "blocks": self.blocks,
            "human_confirmations": self.human_confirmations,
            "statistics": self.statistics,
            "rename_detail": self.rename_detail,
            "effective_fixed_effects": self.effective_fixed_effects,
            "effective_covariates": self.effective_covariates,
            "dropped_fixed_effects": self.dropped_fixed_effects,
            "unknown_columns": self.unknown_columns,
            "auto_included_fixed_effects": self.auto_included_fixed_effects,
            "auto_included_covariates": self.auto_included_covariates,
            "input_state": self.input_state,
            "traits": self.traits,
            "join_mode": self.join_mode,
            "join_column": self.join_column,
            "trait_metrics": self.trait_metrics,
            "change_log_by_trait": self.change_log_by_trait,
            "failed_traits": self.failed_traits,
        }


def run_phenotype_qc_by_trait(frame, traits, config=None, fixed_effects=None,
                              covariates=None, rename_detail=None,
                              pre_standardized_traits=None,
                              standardization_scope=None,
                              standardization_method=None,
                              join_column="ID", join_mode="union"):
    """
    多性状表型清洗：逐性状各自完整跑一遍单性状 PH001–PH007，再组装成宽表。

    为什么逐性状清洗而不是一次传入全部性状（2026-10-12，先生指令）：
      1. 故障隔离：某性状脏数据 / 缺列只影响该性状自身，不连坐整条任务；
      2. 留痕可追溯：change_log 与 before/after 统计按性状独立留存；
      3. 零改动复用已验证的单性状清洗路径，run_phenotype_qc 本身不动。

    join_mode="union"（先生 2026-10-12 拍板，现行唯一实现）：
      以 ID 并集为行，各性状保留自身清洗后的有效记录，其余性状列置 NA。
      这是多性状 BLUP 的标准用法——借性状间遗传相关利用缺失信息；
      若取交集（仅所有性状都有效的个体）会白白丢弃可利用信息。

    失败策略（禁止静默降级）：
      任一性状触发 BLOCK 级问题即整任务中止，AbgesError 列出失败性状与原因；
      不自动把失败性状剔除后继续跑——如需降级必须由先生显式声明。

    返回 MultiTraitQCResult。
    """
    import pandas as pd

    trait_list = list(traits or [])
    if len(trait_list) < 2:
        raise core.AbgesError(
            "PH001", "逐性状组装要求至少 2 个性状，实得 %d 个" % len(trait_list),
            {"traits": trait_list})
    if join_mode != "union":
        raise core.AbgesError(
            "PH001", "不支持的组装方式：%r（现行仅实现 ID 并集）" % join_mode,
            {"allowed": ["union"]})
    if join_column not in frame.columns:
        raise core.AbgesError(
            "PH001", "缺少个体标识列：%s" % join_column,
            {"available": list(frame.columns)})

    result = MultiTraitQCResult(trait_list, join_column=join_column,
                                join_mode=join_mode)
    result.rename_detail = rename_detail or {}

    # ---------- 一、逐性状独立清洗 ----------
    for trait in trait_list:
        try:
            per_trait_result = run_phenotype_qc(
                frame, traits=[trait], config=config,
                fixed_effects=fixed_effects, covariates=covariates,
                mode="single", rename_detail=rename_detail,
                pre_standardized_traits=pre_standardized_traits,
                standardization_scope=standardization_scope,
                standardization_method=standardization_method)
        except core.AbgesError as exc:
            # BLOCK 级问题：如实上报并中止，不静默剔除该性状继续运行
            result.failed_traits.append(
                {"trait": trait, "code": exc.code, "message": str(exc)})
            raise core.AbgesError(
                "PH001",
                "性状 %s 清洗未通过，多性状任务整任务中止：%s" % (trait, exc),
                {"trait": trait, "failed_traits": result.failed_traits})
        if per_trait_result.blocks:
            result.failed_traits.append(
                {"trait": trait, "code": "BLOCK",
                 "message": "; ".join(str(item) for item in per_trait_result.blocks)})
            raise core.AbgesError(
                "PH001",
                "性状 %s 存在未处理的阻断级质量问题，多性状任务整任务中止"
                % trait,
                {"trait": trait, "blocks": per_trait_result.blocks})

        result.per_trait[trait] = per_trait_result
        result.statistics_by_trait[trait] = {
            "before": per_trait_result.statistics.get("before"),
            "after": per_trait_result.statistics.get("after"),
            "standardized": bool(per_trait_result.statistics.get("standardized")),
            "drop_rate": per_trait_result.statistics.get("drop_rate"),
        }
        result.change_log_by_trait[trait] = list(per_trait_result.change_log)
        # 逐性状变更流水并入总表，每条标注来源性状
        for entry in per_trait_result.change_log:
            merged_entry = dict(entry)
            merged_entry["trait"] = trait
            result.change_log.append(merged_entry)
        # 修复 F-2：告警合并须置于 for trait 循环体内（与 change_log 一致），
        # 原代码错置于循环体外，仅末性状告警入库，其余性状告警静默丢失。
        for warning in per_trait_result.warnings:
            merged_warning = dict(warning)
            merged_warning["trait"] = trait
            result.warnings.append(merged_warning)

    # ---------- 未知列自动处理汇总（逐性状 → 多性状聚合，按 original 去重） ----------
    _unknown_reports = []
    _auto_fixed = []
    _auto_cov = []
    for trait in trait_list:
        per = result.per_trait[trait]
        if per.unknown_columns:
            _unknown_reports.extend(per.unknown_columns)
        if per.auto_included_fixed_effects:
            _auto_fixed.extend(per.auto_included_fixed_effects)
        if per.auto_included_covariates:
            _auto_cov.extend(per.auto_included_covariates)
    _seen = set()
    _deduped = []
    for entry in _unknown_reports:
        key = entry.get("original")
        if key in _seen:
            continue
        _seen.add(key)
        _deduped.append(entry)
    result.unknown_columns = _deduped
    result.auto_included_fixed_effects = list(dict.fromkeys(_auto_fixed))
    result.auto_included_covariates = list(dict.fromkeys(_auto_cov))

    # ---------- 二、ID 并集组装 ----------
    indexed = {}
    for trait in trait_list:
        per_frame = result.per_trait[trait].frame.set_index(join_column)
        # PH002 策略为 flag 时可能残留重复 ID，按首次出现去重后再对齐
        per_frame = per_frame[~per_frame.index.duplicated(keep="first")]
        per_frame.index = per_frame.index.astype(str)
        indexed[trait] = per_frame

    union_ids = []
    seen = set()
    source_order = [str(value) for value in frame[join_column].tolist()]
    for trait in trait_list:
        for value in indexed[trait].index.tolist():
            if value not in seen:
                seen.add(value)
                union_ids.append(value)
    # 行序：优先保持原始输入表顺序，其后补上仅存在于清洗结果中的个体
    source_set = set(source_order)
    ordered_ids = [value for value in source_order if value in seen]
    ordered_ids.extend([value for value in union_ids if value not in source_set])

    merged = pd.DataFrame(index=pd.Index(ordered_ids, name=join_column))
    trait_set = set(trait_list)
    for column in trait_list:
        merged[column] = indexed[column][column].reindex(ordered_ids)

    # 固定效应 / 协变量与性状无关，取同一 ID 上先到且非空的取值
    ancillary = []
    for trait in trait_list:
        for name in (list(result.per_trait[trait].effective_fixed_effects)
                     + list(result.per_trait[trait].effective_covariates)):
            if name not in ancillary and name != join_column and name not in trait_set:
                ancillary.append(name)
    for name in ancillary:
        column_series = None
        for trait in trait_list:
            if name not in indexed[trait].columns:
                continue
            candidate = indexed[trait][name].reindex(ordered_ids)
            column_series = (candidate if column_series is None
                             else column_series.combine_first(candidate))
        if column_series is not None:
            merged[name] = column_series

    merged = merged.reset_index()

    # ---------- 三、汇总口径 ----------
    input_rows = int(frame.shape[0])
    after_rows = int(merged.shape[0])
    drop_rate = (input_rows - after_rows) / input_rows if input_rows else 0.0

    before_traits = {}
    after_traits = {}
    for trait in trait_list:
        block_before = (result.statistics_by_trait[trait].get("before") or {})
        block_after = (result.statistics_by_trait[trait].get("after") or {})
        before_traits.update((block_before.get("traits") or {}).copy())
        after_traits.update((block_after.get("traits") or {}).copy())

    for trait in trait_list:
        series = merged[trait]
        variance = series.var()
        result.trait_metrics[trait] = {
            "valid_records": int(series.notna().sum()),
            "trait_variance": float(variance) if variance == variance else 0.0,
        }

    result.frame = merged
    result.statistics = {
        "before": {"n_records": input_rows, "traits": before_traits},
        "after": {"n_records": after_rows, "traits": after_traits},
        "standardized": any(item["standardized"]
                            for item in result.statistics_by_trait.values()),
        "drop_rate": round(drop_rate, 6),
        "by_trait": result.statistics_by_trait,
    }
    result.input_state = dict(
        result.per_trait[trait_list[0]].input_state or {})

    # 固定效应 / 协变量：逐性状应完全一致，出现分歧时如实告警
    for attribute_name in ("effective_fixed_effects", "effective_covariates",
                           "dropped_fixed_effects"):
        collected = []
        differing = []
        for trait in trait_list:
            value = list(getattr(result.per_trait[trait], attribute_name) or [])
            if collected and value != collected[-1]:
                differing.append(trait)
            if value not in collected:
                collected.append(value)
        merged_values = []
        for value in collected:
            for name in value:
                if name not in merged_values:
                    merged_values.append(name)
        setattr(result, attribute_name, merged_values)
        if differing:
            result.warnings.append({
                "rule_id": "PH005",
                "code": "MULTI_TRAIT_EFFECT_DIVERGENCE",
                "message": ("不同性状的有效%s不一致（分歧性状：%s），"
                            "已按并集下发；多性状模型要求同一套效应结构，请复核"
                            % (attribute_name, differing)),
                "action_taken": "按并集处理并如实告警，未静默取舍",
            })

    for trait in trait_list:
        for item in result.per_trait[trait].dropped_fixed_effects:
            if item not in result.dropped_fixed_effects:
                result.dropped_fixed_effects.append(item)

    result.log_change(
        "PH001", "MERGE", after_rows,
        "多性状逐性状清洗后按 ID 并集组装（%s）" % "+".join(trait_list),
        {"join_mode": join_mode,
         "join_column": join_column,
         "input_rows": input_rows,
         "per_trait_valid_records": {trait: result.trait_metrics[trait]["valid_records"]
                                     for trait in trait_list}})
    return result


def _resolve_input_state(config, pre_standardized_traits,
                         standardization_scope, standardization_method):
    """
    解析输入数据状态声明。显式参数优先，其次取配置 input_state 默认值。
    返回 (已标准化性状集合, 分组字段列表, 方法名)。
    """
    declared = config.get("input_state", {}) or {}
    traits = (pre_standardized_traits
              if pre_standardized_traits is not None
              else declared.get("pre_standardized_traits", []))
    scope = (standardization_scope
             if standardization_scope is not None
             else declared.get("standardization_scope", []))
    method = (standardization_method
              if standardization_method is not None
              else declared.get("standardization_method"))
    return set(traits or []), list(scope or []), method


def _resolve_standardize_contract(mode, traits, rules):
    """PH007：single 恰好 1 性状不标准化；multi 至少 2 性状、默认不标准化。

    2026-10-12 修订（先生指令）：多性状由"强制 z-score"改为"不标准化"，
    各性状保持原始量纲，使方差组分与 EBV 同原始尺度结果可比
    （h² 与遗传相关为尺度无关量，不受影响）。
    multi_force_standardize 保留为恢复开关：量纲差异极大的性状组合
    可经 human-in-the-loop 置 true，恢复强制 z-score（此时返回 True）。
    """
    rule = rules.get("PH007", {})
    if mode == "single":
        if len(traits) != 1:
            raise core.AbgesError("PH007",
                                  "single 模式必须恰好 1 个性状，当前 %d 个" % len(traits),
                                  {"traits": traits})
        return bool(rule.get("single_default_standardize", False))
    if mode == "multi":
        if len(traits) < 2:
            raise core.AbgesError("PH007",
                                  "multi 模式至少需要 2 个性状，当前 %d 个" % len(traits),
                                  {"traits": traits})
        return bool(rule.get("multi_force_standardize", False))
    raise core.AbgesError("PH007", "未知分析模式：%r" % mode,
                          {"allowed": ["single", "multi"]})


def _auto_include_unknown_columns(working, result, report, unknown_cfg, min_levels):
    """把「中文来源」的未知列按分类结果自动纳入建模，绝不静默。

    规则（先生定调 + 防模型污染）：
      - 固定效应（低基数、中文来源）→ 默认纳入 effective_fixed_effects，
        受 auto_include_unknown_fixed_effects 开关控制。
      - 连续型（性状/协变量候选，中文来源）→ 默认只登记为候选并告警，
        不自动入模（auto_register_unknown_traits 默认 false），避免静默注入未知性状。
      - 已知英文名 / 未知英文名（非中文来源）→ 一律不自动纳入，
        交由显式请求逻辑处理（避免改选）。
      - 任何纳入/跳过均写 change_log 并告警，原始中文名全程可追溯。
    """
    if not report:
        return
    include_fixed = bool(unknown_cfg.get("auto_include_unknown_fixed_effects", True))
    register_trait = bool(unknown_cfg.get("auto_register_unknown_traits", False))

    eff_fixed = list(result.effective_fixed_effects)
    eff_cov = list(result.effective_covariates)
    seen_fixed = set(eff_fixed)
    seen_cov = set(eff_cov)

    for entry in report:
        source = entry.get("source")
        # 仅处理「中文来源」的未知列（registry/dictionary/pinyin/fallback 均为中文转换产物）
        if source in ("known", "unknown_english"):
            continue
        code = entry.get("code")
        if code not in working.columns:
            continue
        role = entry.get("role")

        if role == "fixed" and include_fixed:
            level_count = int(working[code].dropna().astype(str).str.strip().nunique())
            if level_count < min_levels:
                result.dropped_fixed_effects.append(
                    {"column": code, "levels": level_count, "code": "E022",
                     "auto": True, "original": entry.get("original")})
                result.warnings.append({
                    "rule_id": "PH_AUTO", "code": "PH_AUTO_FEW_LEVELS",
                    "message": ("未知固定效应 %s(原中文表头:%s) 有效水平=%d(<%d)，"
                                "未纳入" % (code, entry.get("original"),
                                            level_count, min_levels))})
                continue
            if code not in seen_fixed:
                seen_fixed.add(code)
                eff_fixed.append(code)
                result.auto_included_fixed_effects.append(code)
                result.log_change(
                    "PH_AUTO", "AUTO_INCLUDE_FIXED", int(working.shape[0]),
                    "未知列 %s 经中文→英文转换为 %s 且内容判为固定效应，已自动纳入"
                    % (entry.get("original"), code),
                    {"original": entry.get("original"), "code": code,
                     "levels": level_count, "reason": entry.get("reason")})
                result.warnings.append({
                    "rule_id": "PH_AUTO", "code": "PH_AUTO_INCLUDED_FIXED",
                    "message": ("已自动将未知固定效应 %s(原中文表头:%s)纳入模型，请人工复核"
                                % (code, entry.get("original")))})

        elif role == "trait_or_covariate":
            if register_trait:
                if code not in seen_cov:
                    seen_cov.add(code)
                    eff_cov.append(code)
                    result.auto_included_covariates.append(code)
                    result.log_change(
                        "PH_AUTO", "AUTO_REGISTER_COVARIATE", int(working.shape[0]),
                        "未知连续列 %s 经中文→英文转换为 %s，已注册为协变量"
                        % (entry.get("original"), code),
                        {"original": entry.get("original"), "code": code,
                         "reason": entry.get("reason")})
                    result.warnings.append({
                        "rule_id": "PH_AUTO", "code": "PH_AUTO_REGISTERED_COVARIATE",
                        "message": ("已自动将未知连续列 %s(原中文表头:%s)注册为协变量，请人工复核"
                                    % (code, entry.get("original")))})
            else:
                # 默认不静默注入未知性状：登记为候选并告警，交由人工决定
                result.warnings.append({
                    "rule_id": "PH_AUTO", "code": "PH_AUTO_CANDIDATE_TRAIT",
                    "message": ("发现未知连续列 %s(原中文表头:%s)，已转英文代号 %s 但未自动入模"
                                "（auto_register_unknown_traits=false）；如确为性状/协变量请显式登记"
                                % (entry.get("original"), entry.get("original"), code))})

    result.effective_fixed_effects = eff_fixed
    result.effective_covariates = eff_cov


def _remove_group_outliers(frame, traits, fixed_effects, rule, result):
    """
    PH006：分组内 mean ± threshold*SD 剔除极端值，只做一轮。
    分组样本量不足 min_group_size 的组不执行，避免小样本误杀。
    """
    import pandas as pd

    if not rule or rule.get("action") != "EXCLUDE":
        return frame
    threshold = float(rule.get("threshold", 3.0))
    min_group_size = int(rule.get("min_group_size", 5))
    configured_groups = rule.get("group_by", []) or []
    group_columns = [name for name in configured_groups if name in frame.columns]
    if not group_columns:
        group_columns = [name for name in fixed_effects if name in frame.columns]

    working = frame.copy()
    for trait in traits:
        if trait not in working.columns:
            continue
        if not group_columns:
            mask = _outlier_mask(working[trait], threshold, min_group_size)
        else:
            key = working[group_columns[0]].astype(str)
            for extra in group_columns[1:]:
                key = key + "|" + working[extra].astype(str)
            mask = pd.Series(False, index=working.index)
            for _, index in working.groupby(key).groups.items():
                subset = working.loc[index, trait]
                mask.loc[index] = _outlier_mask(subset, threshold, min_group_size)
        removed = int(mask.sum())
        if removed:
            result.log_change(
                "PH006", "EXCLUDE", removed,
                "性状 %s 分组内超出 mean±%.1fSD，置缺失" % (trait, threshold),
                {"group_by": group_columns, "min_group_size": min_group_size})
            working.loc[mask, trait] = pd.NA
            working[trait] = pd.to_numeric(working[trait], errors="coerce")
    return working


def _outlier_mask(series, threshold, min_group_size):
    """返回布尔掩码：超出 mean±threshold*SD 的位置为 True。"""
    import pandas as pd

    numeric = pd.to_numeric(series, errors="coerce")
    valid = numeric.dropna()
    if valid.shape[0] < min_group_size:
        return pd.Series(False, index=series.index)
    mean_value = valid.mean()
    sd_value = valid.std(ddof=1)
    if not sd_value or sd_value <= 0:
        return pd.Series(False, index=series.index)
    low = mean_value - threshold * sd_value
    high = mean_value + threshold * sd_value
    return numeric.notna() & ((numeric < low) | (numeric > high))


# ===============================================================
# 五、引擎输入文件构建
# ===============================================================

def build_engine_phenotype(qc_result, traits, output_path,
                           id_column="ID", extra_columns=None):
    """
    生成 HIBLUP 表型输入文件。
    列序：ID -> 固定效应 -> 协变量 -> 性状 -> 额外列。
    ID 必须在第 1 列，这是 --pheno-pos 位置计算的前提。
    """
    frame = qc_result.frame
    columns = [id_column]
    columns += [name for name in qc_result.effective_fixed_effects
                if name in frame.columns]
    columns += [name for name in qc_result.effective_covariates
                if name in frame.columns]
    columns += [name for name in traits if name in frame.columns]
    for name in (extra_columns or []):
        if name in frame.columns and name not in columns:
            columns.append(name)
    subset = frame[columns].copy()
    dataio.write_engine_table(subset, output_path)
    return {
        "path": output_path,
        "columns": columns,
        "rows": int(subset.shape[0]),
        "column_positions": {name: index + 1 for index, name in enumerate(columns)},
    }


# ---------------------------------------------------------------------------
# PH008 / PH009 / PH010（2026-09-14 新增，源自原版数据质量审计 skill）
# ---------------------------------------------------------------------------
def _ph008_check(df, rules, result):
    """PH008：数值列两两 Pearson + 平均绝对差，识别重复/错位列。"""
    rule = rules.get("PH008", {})
    if not rule:
        return
    corr_thr = float(rule.get("corr_threshold", 0.999))
    diff_thr = float(rule.get("abs_diff_threshold", 1e-6))
    skip = set(rule.get("skip_columns", []))
    numeric_cols = [c for c in df.select_dtypes(include="number").columns
                    if c not in skip]
    pairs = []
    for i in range(len(numeric_cols)):
        for j in range(i + 1, len(numeric_cols)):
            a, b = numeric_cols[i], numeric_cols[j]
            x, y = df[a], df[b]
            mask = x.notna() & y.notna()
            if mask.sum() < 30:
                continue
            try:
                r = float(x[mask].corr(y[mask]))
            except Exception:
                continue
            if r != r:  # NaN
                continue
            mean_diff = float((x[mask] - y[mask]).abs().mean())
            if abs(r) >= corr_thr and mean_diff <= diff_thr:
                pairs.append({"col_a": a, "col_b": b,
                              "pearson_r": round(r, 6),
                              "mean_abs_diff": round(mean_diff, 8)})
    if pairs:
        result.warnings.append({
            "rule_id": "PH008",
            "code": "PH008_DUPLICATE_COLUMNS",
            "message": ("检测到 %d 对数值列疑似重复/错位（|r|>=%.3f 且 mean|diff|<=%.2g），"
                        "请人工确认保留哪一份" % (len(pairs), corr_thr, diff_thr)),
            "action_taken": "仅告警，未删除任何列；建议人工删除重复列后重跑",
            "pairs": pairs[:50],
        })
        result.log_change("PH008", "WARNING", len(pairs),
                          "数值列重复对", {"pairs": len(pairs)})


def _ph009_check(df, rules, result):
    """PH009：比率类列标准差过小，判定为恒等（派生 bug）。"""
    rule = rules.get("PH009", {})
    if not rule:
        return
    std_thr = float(rule.get("std_threshold", 0.01))
    patterns = rule.get("ratio_patterns", ["率", "pct", "ratio", "rate", "%"])
    suspect = []
    for col in df.select_dtypes(include="number").columns:
        col_lower = str(col).lower()
        if not any(p.lower() in col_lower for p in patterns):
            continue
        vals = df[col].dropna()
        if len(vals) < 30:
            continue
        sd = float(vals.std(ddof=1))
        if sd <= std_thr:
            suspect.append({"column": col,
                            "std": round(sd, 6),
                            "mean": round(float(vals.mean()), 4),
                            "n": int(len(vals))})
    if suspect:
        result.warnings.append({
            "rule_id": "PH009",
            "code": "PH009_CONSTANT_RATIO",
            "message": ("检测到 %d 个比率类列标准差<=%.4g（疑似分子分母成比例 bug），"
                        "如屠体重=活体重导致屠宰率恒=100%%" % (len(suspect), std_thr)),
            "action_taken": "仅告警，未修改任何数值；建议核对派生公式",
            "columns": suspect,
        })
        result.log_change("PH009", "WARNING", len(suspect),
                          "比率列恒等", {"columns": [c["column"] for c in suspect]})


def _ph010_check(df, fixed_cols, rules, result):
    """PH010：分类列同一语义多种写法（母/雌/F 混用）。"""
    rule = rules.get("PH010", {})
    if not rule:
        return
    groups = rule.get("synonym_groups", {})
    issues = []
    for col in fixed_cols:
        if col not in df.columns:
            continue
        vals = df[col].dropna().astype(str).str.strip()
        present = set(vals.unique())
        for group_name, variants in groups.items():
            hit = sorted(v for v in variants if v in present)
            if len(hit) >= 2:
                issues.append({"column": col,
                               "semantic": group_name,
                               "levels_found": hit})
    if issues:
        result.warnings.append({
            "rule_id": "PH010",
            "code": "PH010_INCONSISTENT_CODING",
            "message": ("检测到 %d 个分类列存在同义水平多种写法，会导致固定效应被拆成多个 dummy"
                        % len(issues)),
            "action_taken": "仅告警，未自动合并；建议统一为一种写法后重跑",
            "issues": issues,
        })
        result.log_change("PH010", "WARNING", len(issues),
                          "分类编码不一致", {"issues": len(issues)})
