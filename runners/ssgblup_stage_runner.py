# -*- coding: utf-8 -*-
"""
SSGBLUP 单性状真实阶段执行器（单步基因组 BLUP）
================================================
实现 orchestrator 的 stage_runner 契约，串联 ABGES 全流程：

    00 任务解析 -> 01 表型质控 -> 02 系谱质控 -> 03 基因型质控 -> 05 数据整合
    -> 06 建模计划 -> 07 引擎执行 -> 08 结果校验 -> 09 交付报告

设计约束（与 configs/policies/system-policies.yaml 一致）：
  1. 不自起子进程。HIBLUP 一律经 context["__tools__"].run_hiblup 调用，
     执行边界与审计由 tool-executor 统一落实；
  2. 不修改任何原始输入文件，中间文件写 run_dir/intermediate，
     结果写 task.output_dir（均为 D 盘）；
  3. 每个闸门检查项都给出明确结论（passed / applicable=False），
     不留空档——缺依据会被 gatekeeper 判为 block；
  4. 告警如实上报。工具已声明处置动作的告警带 action_taken，
     未处置的原样传播并由 orchestrator 降级为 partial，不做粉饰。

标准化 PH007（configs/models/ssblup.yaml#standardization）：
    单/多性状均 -> none（保持原始量纲；2026-10-12 修订，多性状不再强制 z-score）。
本执行器只支持单性状 SSGBLUP，因此 G4-06 的判定依据是"未做标准化"。

与 PBLUP/GBLUP 执行器的差异：
  - 同时承接 02 系谱与 03 基因型（SSGBLUP 需两者构建 H 矩阵）；
  - 06 建模同时传 --pedigree 与 --bfile（H 矩阵由 --alpha 融合 A 与 G，
    HIBLUP 实测口径，见 configs/models/ssblup.yaml#H 矩阵参数）；
  - model_id 用登记表中的 "ssblup"（model-registry 无独立 ssgblup 条目，
    ssblup 即单步基因组 BLUP，requires=[phenotype, pedigree, genotype]）。
"""

import os
import sys
import json
import datetime

# 引导：把 harness 根目录加入 sys.path，使本模块既可被 main.py 加载，
# 也可单独 import 调试（bootstrap 幂等，重复调用无副作用）。
_RUNNER_DIR = os.path.dirname(os.path.abspath(__file__))
_ABGES_ROOT = os.path.dirname(_RUNNER_DIR)
_HARNESS_DIR = os.path.join(_ABGES_ROOT, "harness")
if _HARNESS_DIR not in sys.path:
    sys.path.insert(0, _HARNESS_DIR)

from _bootstrap import bootstrap  # noqa: E402

bootstrap()

import abges_core as core  # noqa: E402
import abges_phenotype as phenotype_tool  # noqa: E402
import abges_pedigree as pedigree_tool  # noqa: E402
import abges_integration as integration_tool  # noqa: E402
import abges_validate as validate_tool  # noqa: E402
import hiblup_tool  # noqa: E402
from runners import result_evaluation  # noqa: E402
from runners import docx_report  # noqa: E402
from runners import delivery_artifacts  # noqa: E402
from runners.common import (
    collect_fixed_effect_levels as _collect_fixed_effect_levels,
    convert_tool_warnings as _convert_tool_warnings,
    empty_result as _empty_result,
    is_pure_ascii as _is_pure_ascii,
    require_context as _require_context,
    run_derived_trait_stage as _run_derived_trait_stage,
    validation_checks_to_gate_facts as _validation_checks_to_gate_facts,
    write_json_artifact as _write_json_artifact,
    run_effect_screening as _run_effect_screening,
    effect_screening_warnings as _effect_screening_warnings,
    resolve_pre_standardized as _resolve_pre_standardized_common,
    phenotype_qc_config_for_run as _phenotype_qc_config_for_run,
    quality_thresholds as _quality_thresholds,
    run_independent_validation as _run_independent_validation,
    apply_declared_fixed_effects as _apply_declared_fixed_effects,
    diagnose_model_design as _diagnose_model_design,
    design_gate_facts as _design_gate_facts,
    delivery_formats as _delivery_formats,
)
import plink_tool  # noqa: E402

MODEL_ID = "ssblup"
ID_COLUMN = "ID"

# 统计登记用的阶段名，G2-04 依此判定处理前后统计是否完整
STAT_STAGE_PHENOTYPE = "01-phenotype"
STAT_STAGE_PEDIGREE = "02-pedigree"
STAT_STAGE_GENOTYPE = "03-genotype"


# ===============================================================
# 一、通用小工具
# ===============================================================

def _stage_supervisor(context, gatekeeper):
    """
    解析任务，落定本次运行的口径：性状、固定效应、协变量、输出目录。
    本执行器只承接单性状 PBLUP；其它组合直接报错，不静默降级。
    """
    task = _require_context(context, "__task__", "00")
    policy = _require_context(context, "__policy__", "00")

    traits = list(task.get("traits") or [])
    if len(traits) != 1:
        raise core.AbgesError(
            "E013", "ssgblup_stage_runner 只支持单性状分析，本次收到 %d 个性状"
                    % len(traits), {"traits": traits})
    if task.get("multi_trait"):
        raise core.AbgesError("E013", "ssgblup_stage_runner 不支持 multi_trait=true")

    model_id = str(context.get("__model__") or task.get("model") or "").lower()
    if model_id != MODEL_ID:
        raise core.AbgesError(
            "E012", "ssgblup_stage_runner 只支持 ssblup，本次选定模型为 %r" % model_id)

    inputs = task.get("inputs") or {}
    if not inputs.get("pedigree"):
        raise core.AbgesError("G1-05", "SSGBLUP 需要系谱，但任务未提供 pedigree")
    if not inputs.get("genotype"):
        raise core.AbgesError("G1-05", "SSGBLUP 需要基因型，但任务未提供 genotype")

    trait = traits[0]
    fixed_effects = task.get("fixed_effects")
    # 协变量未声明时按空处理：不引入 phenotype-qc.yaml 的候选列，
    # 避免对本任务并不存在的列产生"列不存在"告警（PH001）。
    covariates = task.get("covariates")
    covariates = [] if covariates is None else list(covariates)

    output_dir = policy.assert_output_dir_allowed(
        task.get("output_dir") or "workspace/output")

    engine_params = dict(task.get("engine_params") or {})
    model_config = core.load_model_config(MODEL_ID)
    engine_defaults = model_config.get("hiblup", {}) or {}

    # 固定效应水平 -> ASCII 映射（HIBLUP v1.7.0 遇非 ASCII 崩溃，G4-04 强制）
    # 例 {"Sex": {"公": "M", "母": "F"}}；01 表型 QC 后、生成引擎文件前应用。
    fixed_effect_level_map = task.get("fixed_effect_level_map") or {}

    plan = {
        "task_id": task.get("task_id") or "task",
        "task_name": task.get("task_name") or task.get("task_id") or "task",
        "trait": trait,
        "model_id": MODEL_ID,
        "species": task.get("species"),
        "pedigree_generation_semantics": task.get("pedigree_generation_semantics"),
        "late_founder_generation": task.get("late_founder_generation"),
        "genetic_reporting": task.get("genetic_reporting") or {},
        "selection_scenario": task.get("selection_scenario") or {},
        "generation_interval": task.get("generation_interval"),
        "output_format": task.get("output_format") or _delivery_formats(),
        "validation_path": inputs.get("validation"),
        "validation_adjusted_columns": task.get("validation_adjusted_columns") or {},
        "validation_min_n": int(task.get("validation_min_n", 30)),
        "fixed_effects": fixed_effects,
        "covariates": covariates,
        "fixed_effect_level_map": fixed_effect_level_map,
        "covariates_source": "task" if task.get("covariates") is not None
                             else "任务未声明协变量，按空列表处理",
        "output_dir": output_dir,
        "phenotype_path": inputs.get("phenotype"),
        "egg_record_path": inputs.get("egg_record"),
        "pedigree_path": inputs.get("pedigree"),
        "genotype_path": inputs.get("genotype"),
        "baseline_path": task.get("baseline"),
        "baseline_tolerance": task.get("baseline_tolerance"),
        "engine": {
            "vc_method": engine_params.get("vc_method",
                                           engine_defaults.get("vc_method", "AI")),
            "tolerance": float(engine_params.get(
                "tolerance", engine_defaults.get("tolerance", 1e-6))),
            "max_iter": int(engine_params.get(
                "max_iter", engine_defaults.get("max_iter", 30))),
            "em_max_iter": int(engine_defaults.get("em_max_iter", 200)),
            "threads": engine_params.get("threads"),
            # O1（2026-08-24 性能实测）：--r2 耗时 6.7 倍且不改 h²/VA/VE。
            # 【2026-10-19 默认开启】reliability 默认 True，默认附加引擎 --r2 产出可靠性；
            # 如需关闭在 engine_params 显式 reliability: false（引擎不附加 --r2）。
            "reliability": bool(engine_params.get("reliability", True)),
            "timeout": int(engine_defaults.get("timeout", 3600)),
        },
        "standardization_contract": (model_config.get("standardization", {})
                                     .get("single_trait", "none")),
        # 上游标准化声明：必须透传到 01 表型质控；否则 PH006 的
        # skip_when_pre_standardized 不生效，缺失尺度切换留痕且可能在
        # 原始观测尺度上误判已标准化的 z 值（静默失真）。
        "pre_standardized": _resolve_pre_standardized(task, trait),
    }
    return _empty_result(blackboard={"plan": plan})


def _resolve_pre_standardized(task, trait):
    """
    解析 task.pre_standardized（契约见 schemas/task-schema.yaml）。

    未声明时返回 None，表示"按原始观测值处理"，行为与既往一致。
    声明格式错误直接报错，不做猜测式兼容——错误的尺度声明会让
    PH006 按错误标准判定，后果比中止更严重。
    """
    # 【P1-4 基类抽取】统一委托 runners/common.py（原三份各自实现，
    # 且 gblup 版本只认列表，传字符串会按字符拆开并误报「未覆盖」）。
    return _resolve_pre_standardized_common(task, trait)


# ===============================================================
# 三、01 表型质控
# ===============================================================

def _stage_phenotype(context, gatekeeper):
    """读入表型 -> PH001~PH007 质控 -> 生成引擎表型文件 -> 产出 G1/G2 表型侧事实。"""
    plan = _require_context(context, "plan", "01")
    state = _require_context(context, "__state__", "01")
    trait = plan["trait"]

    frame, info, rename_detail, unmapped = phenotype_tool.load_phenotype(
        plan["phenotype_path"])
    derived_frame = context.get("derived_phenotype_frame")
    if derived_frame is not None:
        frame = derived_frame.copy()
        info = dict(info)
        info["derived_trait_input"] = context.get("derived_trait_path")

    pre_std = plan.get("pre_standardized") or {}
    qc = phenotype_tool.run_phenotype_qc(
        frame, traits=[trait], fixed_effects=plan["fixed_effects"],
        covariates=plan["covariates"], mode="single",
        rename_detail=rename_detail,
        pre_standardized_traits=pre_std.get("traits"),
        standardization_scope=pre_std.get("scope"),
        standardization_method=pre_std.get("method"),
        config=_phenotype_qc_config_for_run(state.dirs["intermediate"]))
    _apply_declared_fixed_effects(qc, plan.get("fixed_effects"))

    # ---- 固定效应水平 -> ASCII 转码（在生成引擎文件前应用，防 HIBLUP 崩溃） ----
    level_map = plan.get("fixed_effect_level_map") or {}
    applied_level_map = {}
    if level_map:
        for col, mapping in level_map.items():
            if col in qc.frame.columns and mapping:
                before_vals = sorted(qc.frame[col].dropna().unique().tolist())
                qc.frame[col] = qc.frame[col].map(
                    lambda v, m=mapping: m.get(str(v).strip(), v)
                    if isinstance(v, str) else v)
                after_vals = sorted(qc.frame[col].dropna().unique().tolist())
                applied_level_map[col] = {"before": before_vals, "after": after_vals}

    engine_path = os.path.join(state.dirs["input"], "engine_phenotype.txt")
    engine_phenotype = phenotype_tool.build_engine_phenotype(
        qc, [trait], engine_path, id_column=ID_COLUMN)
    engine_phenotype_path = engine_phenotype["path"]

    # 修复 F-6：HIBLUP 要求 ASCII 分类标签（与 PBLUP:280 对齐），防非 ASCII
    # 固定效应水平致 HIBLUP 崩溃；源数据不变，仅生成 ASCII 映射后的引擎输入文件。
    import abges_ascii as ascii_tool
    engine_input_path, _engine_label_mapping, _engine_ascii_sanitized = ascii_tool.sanitize_phenotype_for_engine(
        engine_phenotype["path"], state.dirs["input"], timestamp=state.run_id)
    if engine_input_path != engine_phenotype_path:
        os.replace(engine_input_path, engine_phenotype_path)
        engine_input_path = engine_phenotype_path
    engine_phenotype["path"] = engine_input_path

    # ---- 处理前后统计（G2-04 的判定依据） ----
    snapshot = core.StatisticsSnapshot(STAT_STAGE_PHENOTYPE)
    snapshot.capture_before(**(qc.statistics.get("before") or {}))
    snapshot.capture_after(**(qc.statistics.get("after") or {}))
    state.record_statistics(STAT_STAGE_PHENOTYPE, snapshot)

    # ---- 关键指标 ----
    valid_records = int(qc.frame[trait].notna().sum())
    duplicate_ids = int(qc.frame[ID_COLUMN].duplicated().sum())
    trait_variance = qc.frame[trait].var()
    trait_variance = float(trait_variance) if trait_variance == trait_variance else 0.0
    drop_rate = float(qc.statistics.get("drop_rate", 0.0) or 0.0)
    standardized = bool(qc.statistics.get("standardized", False))
    fixed_levels = _collect_fixed_effect_levels(qc.frame, qc.effective_fixed_effects)
    non_ascii_columns = [name for name in engine_phenotype["columns"]
                         if not _is_pure_ascii(name)]
    non_ascii_levels = {column: [value for value in values if not _is_pure_ascii(value)]
                        for column, values in fixed_levels.items()}
    non_ascii_levels = {k: v for k, v in non_ascii_levels.items() if v}
    single_level_columns = [column for column, values in fixed_levels.items()
                            if len(values) < 2]

    # ---- G1 表型侧事实 ----
    g1_facts = {
        "G1-01": {
            "passed": qc.frame is not None and qc.frame.shape[0] > 0,
            "actual": "%s 已解析（%s，%s）" % (info.get("kind"), info.get("encoding"),
                                          info.get("delimiter")),
            "threshold": "可解析",
        },
        "G1-02": {
            "passed": info.get("rows", 0) > 0 and info.get("cols", 0) >= 2,
            "actual": {"rows": info.get("rows"), "cols": info.get("cols")},
            "threshold": {"rows": "> 0", "cols": ">= 2"},
        },
        "G1-03": {
            "passed": duplicate_ids == 0,
            "actual": {"duplicate_ids": duplicate_ids},
            "threshold": 0,
            "message": None if duplicate_ids == 0
            else "存在 %d 条重复 ID，已按 phenotype-qc#PH002 策略处理" % duplicate_ids,
        },
        "G1-04": {
            # 本项目的编码判定用候选编码逐个试解码（确定性命中），
            # 不是统计猜测，故置信度按 1.0 记，判定依据一并写明。
            "passed": bool(info.get("encoding")),
            "actual": {"encoding": info.get("encoding"),
                       "method": "候选编码逐个试解码，确定性命中",
                       "confidence": 1.0},
            "threshold": 0.7,
        },
        "G1-05": {
            "passed": bool(plan.get("pedigree_path")),
            "actual": {"pedigree": bool(plan.get("pedigree_path")),
                       "genotype": False},
            "threshold": "系谱或基因型至少其一",
        },
    }

    thresholds = _quality_thresholds()
    min_records = int(thresholds.get("phenotype_min_valid_records", 100))
    max_drop_rate = float(thresholds.get("phenotype_max_drop_rate", 0.30))
    # ---- G2 表型侧事实（系谱/基因型项由 02 阶段给出结论） ----
    g2_facts = {
        "G2-01": {
            "passed": not qc.blocks,
            "actual": {"phenotype_blocks": len(qc.blocks)},
            "threshold": 0,
            "message": None if not qc.blocks
            else "存在未处理的 error 级质控问题：%s" % qc.blocks[:3],
        },
        "G2-02": {
            "passed": valid_records >= min_records,
            "actual": valid_records,
            "threshold": min_records,
        },
        "G2-03": {
            "passed": drop_rate <= max_drop_rate,
            "actual": round(drop_rate, 6),
            "threshold": max_drop_rate,
        },
        "G2-04": {
            "passed": state.statistics_complete(STAT_STAGE_PHENOTYPE),
            "actual": {"phenotype_statistics_complete":
                       state.statistics_complete(STAT_STAGE_PHENOTYPE)},
            "threshold": "before/after 均非空",
        },
        "G2-05": {"applicable": False,
                  "reason": "系谱质控结论由 02-pedigree-agent 给出，本阶段不作判定"},
        "G2-06": {"applicable": False, "reason": "本次任务未提供基因型数据"},
    }

    # ---- 数据驱动效应筛选（阶段01 产出诊断+提示，全程不自动应用） ----
    # 修复 F-1：原调用传未定义的 traits/mode（NameError），
    # 与 SSGBLUP 单性状语义及 ssgblup:716、pblup 阶段01 写法对齐。
    effect_screening = _run_effect_screening(qc, [trait], "single")
    screening_warn = _effect_screening_warnings(effect_screening)

    # ---- 落盘质控报告 ----
    report_path = _write_json_artifact(
        os.path.join(state.dirs["reports"], "phenotype_qc_report.json"),
        {
            "input": {"path": plan["phenotype_path"], **info},
            "rename_detail": qc.rename_detail,
            "unmapped_columns": unmapped,
            "effective_fixed_effects": qc.effective_fixed_effects,
            "effective_covariates": qc.effective_covariates,
            "effect_screening": effect_screening,
            "dropped_fixed_effects": qc.dropped_fixed_effects,
            "fixed_effect_levels": fixed_levels,
            "valid_records": valid_records,
            "drop_rate": drop_rate,
            "standardized": standardized,
            "trait_variance": round(trait_variance, 6),
            "change_log": qc.change_log,
            "warnings": qc.warnings,
            "statistics": qc.statistics,
        })
    change_log_path = _write_json_artifact(
        os.path.join(state.dirs["change_logs"], "phenotype_change_log.json"),
        {"stage": STAT_STAGE_PHENOTYPE, "change_log": qc.change_log})

    metrics = {
        "valid_records": valid_records,
        "duplicate_ids": duplicate_ids,
        "trait_variance": trait_variance,
        "drop_rate": drop_rate,
        "standardized": standardized,
        "fixed_effect_levels": fixed_levels,
        "fixed_effect_level_map_applied": applied_level_map,
        "single_level_columns": single_level_columns,
        "non_ascii_columns": non_ascii_columns,
        "non_ascii_levels": non_ascii_levels,
        "encoding": info.get("encoding"),
        "rows": info.get("rows"),
        "cols": info.get("cols"),
    }

    return _empty_result(
        gate_facts={"G1": g1_facts, "G2": g2_facts},
        artifacts=[("engine_phenotype", engine_phenotype["path"]),
                   ("phenotype_qc_report", report_path),
                   ("phenotype_change_log", change_log_path)],
        warnings=_convert_tool_warnings(qc.warnings, "PH") + screening_warn,
        blackboard={
            "phenotype_qc": qc,
            "phenotype_info": info,
            "effect_screening": effect_screening,
            "engine_phenotype": engine_phenotype,
            "phenotype_metrics": metrics,
            "phenotype_statistics_snapshot": snapshot,
            "phenotype_gate_facts": {"G1": g1_facts, "G2": g2_facts},
        })


# ===============================================================
# 四、02 系谱质控
# ===============================================================

def _stage_pedigree(context, gatekeeper):
    """
    读入系谱 -> PD001~PD013 质控 -> 生成引擎系谱文件。
    本阶段是 G1/G2 的最后一次评估，须把表型侧事实一并复述，
    否则 gatekeeper 的报告会只反映系谱一侧（后评估覆盖前评估）。
    """
    plan = _require_context(context, "plan", "02")
    state = _require_context(context, "__state__", "02")
    upstream_facts = (context.get("phenotype_gate_facts") or {})

    frame, info, rename_detail = pedigree_tool.load_pedigree(plan["pedigree_path"])
    pedigree_config = core.load_yaml_config(pedigree_tool.PEDIGREE_QC_CONFIG)
    pedigree_config["species"] = plan.get("species") or pedigree_config.get("species")
    pedigree_config["generation_column_semantics"] = (
        plan.get("pedigree_generation_semantics")
        or pedigree_config.get("generation_column_semantics", "unspecified"))
    if plan.get("late_founder_generation") is not None:
        pedigree_config["late_founder_generation"] = int(plan["late_founder_generation"])
    qc = pedigree_tool.run_pedigree_qc(frame, config=pedigree_config)
    phenotype_qc = context.get("phenotype_qc")
    phenotype_frame = getattr(phenotype_qc, "frame", None)
    excluded_ids = {str(item.get("ID")) for item in qc.exclusions
                    if item.get("ID") is not None
                    and item.get("record_disposition") != "duplicate_row_removed"}
    if phenotype_frame is not None and ID_COLUMN in phenotype_frame.columns:
        affected = sorted({str(value) for value in phenotype_frame[ID_COLUMN].dropna()
                           if str(value) in excluded_ids})
        qc.exclusion_summary["phenotype_individuals_affected"] = len(affected)
        qc.exclusion_summary["phenotype_ids_affected"] = affected

    engine_path = os.path.join(state.dirs["input"], "engine_pedigree.txt")
    engine_pedigree = pedigree_tool.build_engine_pedigree(qc, engine_path)
    readable_qc_report = delivery_artifacts.render_pedigree_qc_report(
        os.path.join(state.dirs["reports"], "pedigree_qc_report.md"), qc,
        species=pedigree_config.get("species"),
        generation_semantics=pedigree_config.get("generation_column_semantics"))

    snapshot = core.StatisticsSnapshot(STAT_STAGE_PEDIGREE)
    snapshot.capture_before(**(qc.statistics.get("before") or {}))
    snapshot.capture_after(**(qc.statistics.get("after") or {}))
    state.record_statistics(STAT_STAGE_PEDIGREE, snapshot)

    stats_after = qc.statistics.get("after") or {}
    pedigree_individuals = int(stats_after.get("Individuals",
                                               engine_pedigree.get("rows", 0)) or 0)
    cycle_count = len(qc.cycles or [])
    final_graph_valid = bool(getattr(qc, "final_graph_valid", False))
    statistics_complete = (state.statistics_complete(STAT_STAGE_PHENOTYPE)
                           and state.statistics_complete(STAT_STAGE_PEDIGREE))

    # ---- G1：沿用表型侧结论，G1-05 用系谱实际读入结果重述 ----
    g1_facts = dict(upstream_facts.get("G1") or {})
    g1_facts["G1-05"] = {
        "passed": pedigree_individuals > 0,
        "actual": {"pedigree_individuals": pedigree_individuals,
                   "genotype": bool(plan.get("genotype_path"))},
        "threshold": "系谱或基因型至少其一",
    }

    # ---- G2：合并表型侧与系谱侧 ----
    g2_facts = dict(upstream_facts.get("G2") or {})
    g2_facts["G2-04"] = {
        "passed": statistics_complete,
        "actual": {"phenotype": state.statistics_complete(STAT_STAGE_PHENOTYPE),
                   "pedigree": state.statistics_complete(STAT_STAGE_PEDIGREE)},
        "threshold": "各阶段 before/after 均非空",
    }
    g2_facts["G2-06"] = {"applicable": False,
                         "reason": "基因型质控结论由 03-genotype-agent 给出，本阶段不作判定"}
    g2_facts["G2-05"] = {
        "passed": (final_graph_valid and qc.minimum_size_passed
                   and stats_after.get("SelfAsParent", 0) == 0),
        "actual": {"cycles": cycle_count,
                   "self_as_parent": stats_after.get("SelfAsParent", 0),
                   "final_graph_valid": final_graph_valid,
                   "minimum_size_passed": qc.minimum_size_passed,
                   "excluded_records": len(qc.exclusions)},
        "threshold": "最终系谱结构有效且达到任务最小规模",
        "message": None if (final_graph_valid and qc.minimum_size_passed
                            and stats_after.get("SelfAsParent", 0) == 0)
        else "最终系谱结构无效或剔除后规模未达到任务准入下限",
    }
    # G2-01 需把系谱侧一并计入
    phenotype_blocks = (g2_facts.get("G2-01", {}).get("actual") or {}).get(
        "phenotype_blocks", 0)
    g2_facts["G2-01"] = {
        "passed": phenotype_blocks == 0,
        "actual": {"phenotype_blocks": phenotype_blocks, "pedigree_blocks": 0},
        "threshold": 0,
        "message": None,
    }

    report_path = _write_json_artifact(
        os.path.join(state.dirs["reports"], "pedigree_qc_report.json"),
        {
            "input": {"path": plan["pedigree_path"], **info},
            "rename_detail": rename_detail,
            "change_log": qc.change_log,
            "warnings": qc.warnings,
            "cycles": qc.cycles[:20],
            "exclusions": qc.exclusions,
            "exclusion_summary": qc.exclusion_summary,
            "deduplicated_records": qc.deduplicated_records,
            "final_graph_valid": qc.final_graph_valid,
            "statistics": qc.statistics,
        })
    change_log_path = _write_json_artifact(
        os.path.join(state.dirs["change_logs"], "pedigree_change_log.json"),
        {"stage": STAT_STAGE_PEDIGREE, "change_log": qc.change_log,
         "exclusions": qc.exclusions, "exclusion_summary": qc.exclusion_summary})

    return _empty_result(
        gate_facts={"G1": g1_facts, "G2": g2_facts},
        artifacts=[("engine_pedigree", engine_pedigree["path"]),
                   ("pedigree_qc_report", report_path),
                   ("pedigree_qc_report_readable", readable_qc_report),
                   ("pedigree_change_log", change_log_path)],
        warnings=_convert_tool_warnings(qc.warnings, "PD"),
        blackboard={
            "pedigree_qc": qc,
            "pedigree_info": info,
            "engine_pedigree": engine_pedigree,
            "pedigree_individuals": pedigree_individuals,
            "pedigree_ids": sorted({str(value).strip() for value in qc.frame[ID_COLUMN].dropna()}),
            "pedigree_statistics_snapshot": snapshot,
        })


# ===============================================================
# 四点五、03 基因型质控
# ===============================================================

def _stage_genotype(context, gatekeeper):
    """
    读入基因型（PLINK 二进制前缀）-> plink_tool.run_genotype_qc 质控 ->
    产出 G2 基因型侧事实与质控后基因型前缀。
    SSGBLUP 需同时系谱与基因型；本阶段在 02 系谱之后执行，
    基因型质控结论写入 G2-06，其余 G2 事实沿用 02 已合并的结论。
    对已 QC 的输入（mind/geno/maf/hwe 已应用）是幂等复核，前后规模通常不变。
    """
    plan = _require_context(context, "plan", "03")
    state = _require_context(context, "__state__", "03")
    upstream_facts = (context.get("phenotype_gate_facts") or {})

    genotype_path = plan["genotype_path"]
    bfile_prefix = genotype_path
    for ext in (".bed", ".bim", ".fam"):
        if bfile_prefix.lower().endswith(ext):
            bfile_prefix = bfile_prefix[: -len(ext)]
            break

    qc_out_dir = os.path.join(state.dirs["intermediate"], "genotype_qc")
    os.makedirs(qc_out_dir, exist_ok=True)
    # SSGBLUP 典型设计是只基因型化奠基代/部分个体、后代大面积缺失
    # （H 矩阵通过系谱关系连接未分型后代）。此时按"全体样本"算的样本级(--mind)与变异级
    # (--geno/--maf/--hwe)过滤都会把奠基代基因型整批误判为不达标——
    # 任何 SNP 的检出率上限≈基因型化个体占比（如 60/1536=3.9%），必然全灭。
    # 故本 runner 对变异级/样本级缺失与频率阈值全部放宽为不过滤（交给
    # H 矩阵融合处理），仅保留格式校验与规模统计；实际数据质量由 G2-06
    # 的 SNP/样本规模下限把关。覆盖阈值须如实记入审计与报告。
    thresholds = _quality_thresholds()
    qc = plink_tool.run_genotype_qc(
        bfile_prefix, qc_out_dir,
        thresholds={
            "mind": 1.0, "geno": 1.0, "maf": 0.0, "hwe": 0.0,
            "min_snp_after_qc": int(thresholds.get("genotype_min_snps", 1000)),
            "min_sample_after_qc": int(thresholds.get("genotype_min_samples", 50)),
        })

    scale_before = qc.get("before") or {}
    scale_after = qc.get("after") or {}
    n_sample_after = int(scale_after.get("n_sample", 0) or 0)
    n_snp_after = int(scale_after.get("n_snp", 0) or 0)
    qc_status = qc.get("status")
    qc_ok = qc_status == "completed"
    qc_prefix = qc.get("output_prefix") or bfile_prefix

    snapshot = core.StatisticsSnapshot(STAT_STAGE_GENOTYPE)
    snapshot.capture_before(Individuals=int(scale_before.get("n_sample", 0) or 0),
                            Variants=int(scale_before.get("n_snp", 0) or 0))
    snapshot.capture_after(Individuals=n_sample_after, Variants=n_snp_after)
    state.record_statistics(STAT_STAGE_GENOTYPE, snapshot)
    statistics_complete = (state.statistics_complete(STAT_STAGE_PHENOTYPE)
                           and state.statistics_complete(STAT_STAGE_GENOTYPE))

    # ---- G1：本阶段须复述全套 G1（沿用 02 结论），G1-05 用基因型实际读入重述 ----
    g1_facts = dict(upstream_facts.get("G1") or {})
    g1_facts["G1-05"] = {
        "passed": n_sample_after > 0 and n_snp_after > 0,
        "actual": {"genotype_samples": n_sample_after,
                   "genotype_snps": n_snp_after},
        "threshold": "系谱或基因型至少其一",
    }

    # ---- G2：在 02 已合并结论基础上补基因型侧 G2-06，并复算 G2-04/G2-01 ----
    g2_facts = dict(upstream_facts.get("G2") or {})
    g2_facts["G2-04"] = {
        "passed": statistics_complete,
        "actual": {"phenotype": state.statistics_complete(STAT_STAGE_PHENOTYPE),
                   "genotype": state.statistics_complete(STAT_STAGE_GENOTYPE)},
        "threshold": "各阶段 before/after 均非空",
    }
    min_snps = int(thresholds.get("genotype_min_snps", 1000))
    min_samples = int(thresholds.get("genotype_min_samples", 50))
    g2_facts["G2-06"] = {
        "passed": qc_ok and n_snp_after >= min_snps and n_sample_after >= min_samples,
        "actual": {"qc_status": qc_status,
                   "n_sample": n_sample_after, "n_snp": n_snp_after,
                   "removed": qc.get("removed"),
                   "missing_rate": qc.get("missing_rate")},
        "threshold": "status=completed 且 SNP>=%d 且样本>=%d" % (min_snps, min_samples),
        "message": None if (qc_ok and n_snp_after >= min_snps and n_sample_after >= min_samples)
        else "基因型质控未达标：status=%s，SNP=%d，样本=%d"
             % (qc_status, n_snp_after, n_sample_after),
    }
    # O6（2026-08-24）：缺失基因型审计告警（SSGBLUP 口径）。
    # 注意：SSGBLUP 典型设计只基因型化奠基代/部分个体，.imiss/.lmiss
    # 仅统计已分型子集内部的缺失率，未分型个体不在其中（其信息由
    # 系谱经 H 矩阵补全）。故此告警衡量的是"已分型部分的数据质量"，
    # 而非全群覆盖度。HIBLUP 将缺失基因型强制编码为杂合(1)且无填充，
    # 已分型子集若缺失率仍高，偏差风险更直接。阈值 0.05 告警不阻断。
    missing_report = qc.get("missing_rate") or {}
    genotype_warnings = []
    mean_snp_missing = missing_report.get("mean_snp_missing_rate")
    if mean_snp_missing is not None and mean_snp_missing > 0.05:
        genotype_warnings.append({
            "code": "GENO_MISSING_HIGH",
            "message": "已分型子集平均位点缺失率 %.2f%% 超过 5%%："
                       "HIBLUP 会将缺失基因型强制编码为杂合(1)，"
                       "可能引入偏差；建议核查分型质量或先填充"
                       "（beagle 等）后再入 SSGBLUP"
                       % (mean_snp_missing * 100),
        })
    # G2-01 需把基因型侧一并计入
    phenotype_blocks = (g2_facts.get("G2-01", {}).get("actual") or {}).get(
        "phenotype_blocks", 0)
    g2_facts["G2-01"] = {
        "passed": phenotype_blocks == 0 and qc_ok,
        "actual": {"phenotype_blocks": phenotype_blocks,
                   "genotype_ok": qc_ok},
        "threshold": 0,
        "message": None,
    }

    report_path = _write_json_artifact(
        os.path.join(state.dirs["reports"], "genotype_qc_report.json"),
        {
            "input": {"path": genotype_path, "bfile_prefix": bfile_prefix},
            "before": scale_before,
            "after": scale_after,
            "removed": qc.get("removed"),
            "steps": qc.get("steps"),
            "status": qc_status,
            "output_prefix": qc_prefix,
            "missing_rate": qc.get("missing_rate"),
        })
    change_log_path = _write_json_artifact(
        os.path.join(state.dirs["change_logs"], "genotype_change_log.json"),
        {"stage": STAT_STAGE_GENOTYPE, "before": scale_before,
         "after": scale_after, "removed": qc.get("removed")})

    return _empty_result(
        gate_facts={"G1": g1_facts, "G2": g2_facts},
        artifacts=[("genotype_qc_report", report_path),
                   ("genotype_change_log", change_log_path)],
        warnings=genotype_warnings,
        blackboard={
            "genotype_qc": qc,
            "genotype_prefix": qc_prefix,
            "genotype_scale_after": scale_after,
            "genotype_statistics_snapshot": snapshot,
        })


# ===============================================================
# 五、05 数据整合
# ===============================================================

def _stage_integration(context, gatekeeper):
    """表型 × 系谱 × 基因型对齐，产出 G3 闸门事实。"""
    state = _require_context(context, "__state__", "05")
    phenotype_qc = _require_context(context, "phenotype_qc", "05")
    pedigree_qc = _require_context(context, "pedigree_qc", "05")
    genotype_prefix = _require_context(context, "genotype_prefix", "05")

    # 从 QC 后基因型 .fam 提取个体号（第 2 列 IID）
    fam_path = genotype_prefix + ".fam"
    genotype_ids = []
    if os.path.isfile(fam_path):
        with open(fam_path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                parts = line.split()
                if len(parts) >= 2:
                    genotype_ids.append(parts[1])

    integration = integration_tool.integrate_sources(
        phenotype_qc.frame, ped_frame=pedigree_qc.frame,
        genotype_ids=genotype_ids, id_column=ID_COLUMN, model=MODEL_ID,
        thresholds=_quality_thresholds())
    g3_facts = integration_tool.build_gate_g3_facts(
        integration, model=MODEL_ID, thresholds=_quality_thresholds())

    report_path = _write_json_artifact(
        os.path.join(state.dirs["reports"], "integration_report.json"),
        {"metrics": integration.metrics,
         "diagnostics": integration.diagnostics,
         "change_log": integration.change_log,
         "warnings": integration.warnings})

    return _empty_result(
        gate_facts={"G3": g3_facts},
        artifacts=[("integration_report", report_path)],
        warnings=_convert_tool_warnings(integration.warnings, "G3"),
        blackboard={"integration": integration,
                    "genotype_individuals": len(genotype_ids),
                    "analysis_ids": sorted({str(value) for value in
                                            phenotype_qc.frame[ID_COLUMN].dropna()}),
                    "integration_metrics": dict(integration.metrics)})


# ===============================================================
# 六、06 建模计划（组装命令 + G4 事实）
# ===============================================================

def _stage_planner(context, gatekeeper):
    """引擎自检 -> 组装 HIBLUP 命令 -> 产出 G4 建模准入事实。"""
    plan = _require_context(context, "plan", "06")
    state = _require_context(context, "__state__", "06")
    policy = _require_context(context, "__policy__", "06")
    tools = _require_context(context, "__tools__", "06")
    engine_phenotype = _require_context(context, "engine_phenotype", "06")
    engine_pedigree = _require_context(context, "engine_pedigree", "06")
    genotype_prefix = _require_context(context, "genotype_prefix", "06")
    genotype_scale_after = _require_context(context, "genotype_scale_after", "06")
    phenotype_qc = _require_context(context, "phenotype_qc", "06")
    metrics = _require_context(context, "phenotype_metrics", "06")
    integration_metrics = _require_context(context, "integration_metrics", "06")

    trait = plan["trait"]
    engine_available, engine_info = tools.verify_hiblup()
    if not engine_available:
        raise core.AbgesError("E014", "HIBLUP 引擎不可用：%s" % engine_info)

    output_prefix = policy.build_output_prefix(
        plan["output_dir"], plan["task_id"], trait, MODEL_ID)

    engine_config = plan["engine"]
    # 数据驱动效应筛选（阶段06）：仅读取阶段01 的诊断结论，不自动应用
    # 本模块只做诊断+提示：是否入模由用户任务要求决定（先生拍板 2026-10-17）。
    effect_screening = context.get("effect_screening")
    if effect_screening is None:
        # 兜底：阶段01 未产出时现算一次，保证流水线可独立运行
        effect_screening = _run_effect_screening(phenotype_qc, [trait], "single")
    command, hiblup_plan = hiblup_tool.assemble_hiblup_command(
        engine_phenotype["path"], output_prefix, model_id=MODEL_ID,
        trait_columns=[trait],
        fixed_effects={trait: phenotype_qc.effective_fixed_effects},
        covariates={trait: phenotype_qc.effective_covariates}
        if phenotype_qc.effective_covariates else None,
        pedigree_path=engine_pedigree["path"],
        genotype_prefix=genotype_prefix, mode="single",
        vc_method=engine_config["vc_method"],
        tolerance=engine_config["tolerance"],
        max_iter=engine_config["max_iter"],
        em_max_iter=engine_config["em_max_iter"],
        threads=policy.thread_count(engine_config["threads"]),
        reliability=engine_config["reliability"],
        extra_args=engine_config.get("extra_args"),
        step_size=engine_config.get("step_size"),
        keep_file=engine_config.get("keep_file"),
        remove_file=engine_config.get("remove_file"))

    # ---- G4 事实 ----
    declared_position = engine_phenotype["column_positions"].get(trait)
    actual_position = (hiblup_plan.get("trait_positions") or [None])[0]
    dropped_fixed = hiblup_plan.get("fixed_effects_dropped") or []
    single_level = metrics["single_level_columns"]
    non_ascii_columns = metrics["non_ascii_columns"]
    non_ascii_levels = metrics["non_ascii_levels"]
    prefix_conflict = [name for name in os.listdir(plan["output_dir"])
                       if name.startswith(os.path.basename(output_prefix))]
    genotype_snps = int(genotype_scale_after.get("n_snp", 0) or 0)

    g4_facts = {
        "G4-01": {
            "passed": (integration_metrics.get("pedigree_individuals", 0) > 0
                       and genotype_snps > 0
                       and metrics["valid_records"] > 0),
            "actual": {"model": MODEL_ID,
                       "phenotype_records": metrics["valid_records"],
                       "pedigree_individuals":
                           integration_metrics.get("pedigree_individuals"),
                       "genotype_snps": genotype_snps,
                       "engine": engine_info},
            "threshold": "ssblup requires phenotype + pedigree + genotype",
        },
        "G4-02": {
            "passed": (declared_position is not None
                       and declared_position == actual_position),
            "actual": {"declared": declared_position, "engine_arg": actual_position},
            "threshold": "两者一致（1-based）",
        },
        "G4-03": {
            "passed": not single_level and not dropped_fixed,
            "actual": {"levels": {column: len(values) for column, values
                                  in metrics["fixed_effect_levels"].items()},
                       "dropped_by_engine": dropped_fixed},
            "threshold": ">= 2",
            "message": None if (not single_level and not dropped_fixed)
            else "单水平固定效应：%s，已按 E022 剔除" % (single_level or dropped_fixed),
        },
        "G4-04": {
            "passed": not non_ascii_columns and not non_ascii_levels,
            "actual": {"non_ascii_columns": non_ascii_columns,
                       "non_ascii_levels": non_ascii_levels},
            "threshold": "全部 ASCII",
        },
        "G4-05": {
            "passed": metrics["trait_variance"] > 0,
            "actual": round(metrics["trait_variance"], 6),
            "threshold": "> 0",
        },
        "G4-06": {
            # 单性状合同为 none：未做标准化才算合规
            "passed": (not metrics["standardized"]
                       and plan["standardization_contract"] == "none"),
            "actual": {"mode": "single", "standardized": metrics["standardized"],
                       "contract": plan["standardization_contract"]},
            "threshold": "single_trait=none",
        },
        "G4-07": {
            "passed": (os.access(plan["output_dir"], os.W_OK)
                       and not prefix_conflict),
            "actual": {"output_dir": plan["output_dir"],
                       "prefix": os.path.basename(output_prefix),
                       "existing_files_with_prefix": prefix_conflict},
            "threshold": "目录可写且前缀未被占用",
        },
    }
    design_diagnostics = _diagnose_model_design(
        phenotype_qc.frame, traits, phenotype_qc.effective_fixed_effects,
        phenotype_qc.effective_covariates)
    g4_facts.update(_design_gate_facts(design_diagnostics))

    plan_path = _write_json_artifact(
        os.path.join(state.dirs["reports"], "execution_plan.json"),
        {"command_line": hiblup_plan["command_line"],
         "plan": hiblup_plan,
         "output_prefix": output_prefix,
         "engine": engine_info,
         "engine_params": engine_config,
         "effect_screening": effect_screening})

    return _empty_result(
        gate_facts={"G4": g4_facts},
        artifacts=[("execution_plan", plan_path)],
        blackboard={"hiblup_command": command,
                    "hiblup_plan": hiblup_plan,
                    "output_prefix": output_prefix,
                    "engine_info": engine_info,
                    "effect_screening": effect_screening,
                    "model_design_diagnostics": design_diagnostics,
                    "g4_facts": g4_facts})


# ===============================================================
# 七、07 引擎执行
# ===============================================================

def _stage_execution(context, gatekeeper):
    """
    经 tool-executor 调 HIBLUP，解析输出并做首轮 G5 判定。
    本阶段同时复述 G4（registry 中 07 挂了 G4 与 G5 两道闸门）。
    """
    plan = _require_context(context, "plan", "07")
    state = _require_context(context, "__state__", "07")
    policy = _require_context(context, "__policy__", "07")
    tools = _require_context(context, "__tools__", "07")
    command = _require_context(context, "hiblup_command", "07")
    output_prefix = _require_context(context, "output_prefix", "07")
    g4_facts = _require_context(context, "g4_facts", "07")
    pedigree_individuals = _require_context(context, "pedigree_individuals", "07")
    pedigree_ids = _require_context(context, "pedigree_ids", "07")

    trait = plan["trait"]
    exec_result = tools.run_hiblup(
        command, timeout=plan["engine"]["timeout"], workdir=state.dirs["logs"],
        threads=plan["engine"]["threads"],
        note="PBLUP 单性状 %s" % trait)

    log_path = os.path.join(state.dirs["logs"], "hiblup_stdout.log")
    with open(log_path, "w", encoding="utf-8") as handle:
        handle.write("$ %s\n\n" % " ".join(str(item) for item in command))
        handle.write("[exit_code] %s\n[duration_seconds] %.3f\n\n"
                     % (exec_result.exit_code, exec_result.duration))
        handle.write("----- stdout -----\n%s\n" % (exec_result.stdout or ""))
        handle.write("----- stderr -----\n%s\n" % (exec_result.stderr or ""))

    parsed = hiblup_tool.parse_hiblup_output(
        output_prefix, method=MODEL_ID, mode="single", trait_names=[trait])
    heritability = hiblup_tool.extract_heritability(parsed)

    # G5-07：SSGBLUP 为全部系谱个体产出 EBV（H 矩阵融合 A 与 G，
    # 未测基因型个体经系谱获得 EBV），预期记录数取系谱个体数。
    validation = validate_tool.validate_engine_result(
        parsed, exec_result.exit_code, trait=trait,
        expected_individuals=pedigree_individuals, heritability=heritability,
        expected_ids=pedigree_ids, species=plan.get("species"))

    artifacts = [("hiblup_stdout_log", log_path)]
    for suffix in (".vars", ".rand", ".beta", ".log"):
        candidate = output_prefix + suffix
        if os.path.isfile(candidate):
            artifacts.append(("hiblup" + suffix.replace(".", "_"), candidate))

    errors = []
    if exec_result.exit_code != 0:
        errors.append({
            "code": "E014",
            "message": "HIBLUP 非零退出（exit_code=%s）：%s"
                       % (exec_result.exit_code,
                          (exec_result.stderr or "")[-500:]),
            "blocking": True,
        })

    return _empty_result(
        gate_facts={"G4": g4_facts,
                    "G5": _validation_checks_to_gate_facts(validation.checks)},
        artifacts=artifacts,
        errors=errors,
        blackboard={
            "engine_exec": {
                "exit_code": exec_result.exit_code,
                "duration_seconds": round(exec_result.duration, 3),
                "command_line": exec_result.command_line,
                "workdir": state.dirs["logs"],
            },
            "parsed_result": parsed,
            "heritability": heritability,
            "validation": validation,
        })


# ===============================================================
# 八、08 结果校验
# ===============================================================

def _stage_validator(context, gatekeeper):
    """Create shared result-evaluation artifacts and preserve G5 gate facts."""
    plan = _require_context(context, "plan", "08")
    state = _require_context(context, "__state__", "08")
    parsed = _require_context(context, "parsed_result", "08")
    validation = _require_context(context, "validation", "08")
    analysis_ids = _require_context(context, "analysis_ids", "08")
    phenotype_qc = _require_context(context, "phenotype_qc", "08")
    independent_validation, independent_warnings = _run_independent_validation(
        plan, parsed, phenotype_qc)
    result = result_evaluation.build_artifacts(
        plan, state, parsed, validation, validate_tool, _write_json_artifact,
        reporting_ids=analysis_ids,
        reporting_population={"basis": "phenotype_qc_ids",
                              "analysis_individual_count": len(analysis_ids)})
    return _empty_result(
        gate_facts={"G5": _validation_checks_to_gate_facts(validation.checks)},
        artifacts=[("validation_report", result["report_path"]),
                   ("ebv_top20", result["top_path"])],
        warnings=result["warnings"] + independent_warnings,
        blackboard={"ebv_summary": result["ebv_summary"],
                    "result_evaluation": result["result_evaluation"],
                    "baseline_comparison": result["baseline_comparison"],
                    "validation_report_path": result["report_path"],
                    "reporting_population": result.get("reporting_population"),
                    "top_individuals": result.get("top_individuals"),
                    "independent_validation": independent_validation})

# ===============================================================

def _stage_reporter(context, gatekeeper):
    """写运行元数据、统计与 Markdown 报告，产出 G6 交付事实。"""
    plan = _require_context(context, "plan", "09")
    state = _require_context(context, "__state__", "09")
    policy = _require_context(context, "__policy__", "09")
    output_prefix = _require_context(context, "output_prefix", "09")
    hiblup_plan = _require_context(context, "hiblup_plan", "09")
    engine_exec = _require_context(context, "engine_exec", "09")
    validation = _require_context(context, "validation", "09")
    phenotype_qc = _require_context(context, "phenotype_qc", "09")

    snapshots = [snapshot for snapshot in
                 (context.get("phenotype_statistics_snapshot"),
                  context.get("pedigree_statistics_snapshot"),
                  context.get("genotype_statistics_snapshot")) if snapshot]

    gate_warnings = gatekeeper.all_warnings()
    stage_warnings = list(state.state.get("warnings") or [])
    g5_report = gatekeeper.reports.get("G5")
    g5_clean = bool(g5_report and not g5_report.blocked and not g5_report.warnings
                    and not g5_report.human_required and g5_report.passed)
    unhandled = [item for item in stage_warnings
                 if not item.get("action_taken") or item.get("requires_review")]
    expected_status = ("completed" if (g5_clean and not unhandled
                                       and not gate_warnings)
                       else "partial")

    # ---- 运行元数据（G6-01） ----
    metadata = {
        "run_id": state.run_id,
        "timestamp_start": state.state.get("created_at"),
        "timestamp_end": datetime.datetime.now().isoformat(timespec="seconds"),
        "duration_seconds": engine_exec.get("duration_seconds"),
        "abges_version": core.ABGES_VERSION,
        "engine_name": "HIBLUP",
        "engine_version": context.get("engine_info"),
        "full_command_line": hiblup_plan["command_line"],
        "harness_command_line": " ".join(sys.argv),
        "input_files": {
            role: core.describe_input_file(path)
            for role, path in (("phenotype", plan["phenotype_path"]),
                               ("pedigree", plan["pedigree_path"]),
                               ("genotype", plan["genotype_path"]))
            if path and os.path.isfile(path)
        },
        "output_files": sorted(set(state.state.get("artifacts", {}).keys())
                               | {"delivery_ebv_workbook", "delivery_report",
                                  "delivery_docx_report"}),
        "config_snapshot": {
            "model": MODEL_ID,
            "engine_params": plan["engine"],
            "standardization": {"mode": "single",
                                "contract": plan["standardization_contract"],
                                "applied": False},
            "covariates_source": plan["covariates_source"],
        },
        "model_id": MODEL_ID,
        "trait": plan["trait"],
        "fixed_effects": phenotype_qc.effective_fixed_effects,
        "covariates": phenotype_qc.effective_covariates,
        "status": expected_status,
        "status_note": "本字段为报告阶段依据 G1-G6 判定给出的交付状态；"
                       "最终状态以 harness 汇总的 reports/result.json 为准。",
        "warnings": stage_warnings,
        "errors": list(state.state.get("errors") or []),
    }
    # ---- 面向专家的紧凑交付物；全量审计证据保留在运行目录 ----
    gate_reports_dicts = [rep.to_dict() for rep in gatekeeper.reports.values()]
    snapshots_dicts = [snap.to_dict() for snap in snapshots]
    raw_unchanged, raw_violations = policy.raw_guard.verify_unchanged()
    report_prefix = os.path.join(state.dirs["reports"], os.path.basename(output_prefix))
    workbook_path = report_prefix + "_育种值交付.xlsx"
    workbook_ok = False
    try:
        delivery_artifacts.create_workbook(
            workbook_path, plan, context, validation, expected_status,
            gate_warnings, stage_warnings, metadata)
        workbook_ok = os.path.isfile(workbook_path) and os.path.getsize(workbook_path) > 0
    except Exception as exc:
        stage_warnings.append({
            "stage": "09", "code": "EXCEL_DELIVERY_FAILED",
            "message": "Excel 育种值交付表生成失败：%s" % exc,
            "affected_count": None,
            "action_taken": "交付状态降为 partial；需修复工作簿生成后再交付"})
        expected_status = "partial"
        metadata["status"] = expected_status
    docx_path = None
    try:
        docx_path = docx_report.render_run_docx(
            output_prefix, plan, context, validation, gate_warnings,
            stage_warnings, expected_status, metadata, model_label="SSGBLUP",
            snapshots=snapshots_dicts, gate_reports=gate_reports_dicts,
            raw_guard={"unchanged": raw_unchanged, "violations": raw_violations},
            top_individuals=context.get("top_individuals"))
    except Exception as exc:
        stage_warnings.append({
            "stage": "09", "code": "DOCX_REPORT_FAILED",
            "message": "Word 结果报告生成失败：%s" % exc,
            "affected_count": None,
            "action_taken": "已保留 Markdown 报告，Word 报告降级跳过，不影响评估结论"})
    unavailable = set()
    if not workbook_ok:
        unavailable.add("delivery_ebv_workbook")
    if not docx_path:
        unavailable.add("delivery_docx_report")
    metadata["output_files"] = sorted(
        name for name in metadata.get("output_files", []) if name not in unavailable)

    metadata["analysis_policy"] = validate_tool.analysis_policy()
    metadata_path = core.write_run_metadata(output_prefix, metadata)
    statistics_path = core.write_statistics(output_prefix, snapshots)

    # ---- Markdown 报告（G6-03：告警必须全部列出） ----
    report_path = delivery_artifacts.render_markdown(
        report_prefix + "_育种值摘要.md", plan, context, validation, expected_status,
        gate_warnings, stage_warnings, metadata, workbook_path)

    metadata_ok = (os.path.isfile(metadata_path)
                   and bool(hiblup_plan.get("command_line")))
    statistics_ok = (os.path.isfile(statistics_path)
                     and all(snapshot.is_complete() for snapshot in snapshots)
                     and len(snapshots) >= 1)
    warnings_covered = os.path.isfile(report_path)
    violations = raw_violations  # 复用 Word 报告前统一校验的结果（raw_unchanged 同名复用）

    g6_facts = {
        "G6-01": {
            "passed": metadata_ok,
            "actual": {"path": os.path.basename(metadata_path),
                       "command_line": hiblup_plan.get("command_line")},
            "threshold": "存在且含完整命令行",
        },
        "G6-02": {
            "passed": statistics_ok,
            "actual": {"path": os.path.basename(statistics_path),
                       "stages": [snapshot.stage for snapshot in snapshots]},
            "threshold": "各阶段 before/after 均已写入",
        },
        "G6-03": {
            "passed": warnings_covered,
            "actual": {"gate_warnings": len(gate_warnings),
                       "stage_warnings": len(stage_warnings),
                       "report": os.path.basename(report_path),
                       "docx_report": (os.path.basename(docx_path)
                                       if docx_path else None)},
            "threshold": "全部告警进报告",
        },
        "G6-04": {
            "passed": expected_status in ("completed", "partial", "failed"),
            "actual": expected_status,
            "threshold": ["completed", "partial", "failed"],
        },
        "G6-05": {
            # 一致性判定：声称 completed 时 G5 必须全通过。
            # G5 未全通过时状态已降级为 partial，不构成违规。
            "passed": (g5_clean or expected_status != "completed"),
            "actual": {"expected_status": expected_status,
                       "g5_fully_passed": g5_clean},
            "threshold": "completed 需 G5 全 pass",
        },
        "G6-06": {
            "passed": raw_unchanged,
            "actual": {"violations": violations},
            "threshold": "原始输入校验和不变",
        },
        "G6-07": {
            "passed": workbook_ok,
            "actual": {"path": os.path.basename(workbook_path),
                       "bytes": os.path.getsize(workbook_path) if workbook_ok else 0},
            "threshold": "Excel 工作簿存在且非空",
        },
    }

    return _empty_result(
        gate_facts={"G6": g6_facts},
        artifacts=[("run_metadata", metadata_path),
                   ("run_statistics", statistics_path),
                   ("delivery_report", report_path)]
                  + ([("delivery_docx_report", docx_path)] if docx_path else [])
                  + ([("delivery_ebv_workbook", workbook_path)] if workbook_ok else []),
        blackboard={"expected_status": expected_status})


def _render_markdown_report(output_prefix, plan, context, validation,
                            gate_warnings, stage_warnings, expected_status,
                            metadata):
    """生成交付报告，全部告警逐条列出（G6-03）。"""
    summary = validation.summary
    phenotype_metrics = context.get("phenotype_metrics") or {}
    integration_metrics = context.get("integration_metrics") or {}
    ebv_summary = context.get("ebv_summary") or {}
    baseline_comparison = context.get("baseline_comparison")
    lines = []

    lines.append("# %s 遗传评估报告（%s，SSGBLUP）"
                 % (plan["task_id"], plan["trait"]))
    lines.append("")
    lines.append("- 运行标识：%s" % metadata["run_id"])
    lines.append("- 生成时间：%s" % metadata["timestamp_end"])
    lines.append("- 交付状态（报告阶段判定）：**%s**" % expected_status)
    lines.append("- 引擎：%s %s" % (metadata["engine_name"],
                                 metadata.get("engine_version") or ""))
    lines.append("")

    lines.append("## 一、数据规模")
    lines.append("")
    lines.append("| 项目 | 数值 |")
    lines.append("| --- | --- |")
    lines.append("| 表型记录数（读入） | %s |" % phenotype_metrics.get("rows"))
    lines.append("| 目标性状有效记录数 | %s |" % phenotype_metrics.get("valid_records"))
    lines.append("| 质控剔除比例 | %s |" % phenotype_metrics.get("drop_rate"))
    lines.append("| 系谱个体数 | %s |"
                 % integration_metrics.get("pedigree_individuals"))
    lines.append("| 表型×系谱交集 | %s |"
                 % integration_metrics.get("pheno_ped_overlap"))
    lines.append("| 交集覆盖率 | %s |"
                 % integration_metrics.get("pheno_ped_coverage"))
    lines.append("| 基因型个体数 | %s |"
                 % integration_metrics.get("genotype_individuals"))
    lines.append("| 表型×基因型交集 | %s |"
                 % integration_metrics.get("pheno_geno_overlap"))
    lines.append("")

    lines.append("## 二、模型与命令")
    lines.append("")
    lines.append("- 模型：SSGBLUP（单步基因组 BLUP，H 矩阵融合系谱 A 与基因组 G，加性遗传效应）")
    lines.append("- 固定效应：%s" % ", ".join(metadata["fixed_effects"] or ["无"]))
    lines.append("- 协变量：%s" % (", ".join(metadata["covariates"]) or "无"))
    lines.append("- 标准化：单性状合同为 none，本次未做标准化，保持原始量纲")
    lines.append("")
    lines.append("```")
    lines.append(metadata["full_command_line"])
    lines.append("```")
    lines.append("")

    lines.append("## 三、方差组分与遗传力")
    lines.append("")
    lines.append("| 指标 | 估计值 |")
    lines.append("| --- | --- |")
    lines.append("| 遗传力 h² | %s |" % summary.get("heritability"))
    lines.append("| h² 标准误 | %s |" % summary.get("heritability_se"))
    lines.append("| 加性遗传方差 | %s |" % summary.get("genetic_variance"))
    lines.append("| 残差方差 | %s |" % summary.get("residual_variance"))
    lines.append("| 是否收敛 | %s |" % summary.get("converged"))
    lines.append("| 迭代次数 | %s |" % summary.get("n_iterations"))
    lines.append("| EBV 记录数 | %s |" % summary.get("ebv_records"))
    lines.append("")
    if ebv_summary:
        lines.append("育种值分布：均值 %s，标准差 %s，最小 %s，最大 %s"
                     % (ebv_summary.get("mean"), ebv_summary.get("sd"),
                        ebv_summary.get("min"), ebv_summary.get("max")))
        lines.append("")

    lines.append("## 四、闸门判定")
    lines.append("")
    lines.append("| 检查项 | 结论 | 实际值 | 阈值 |")
    lines.append("| --- | --- | --- | --- |")
    for check in validation.checks:
        lines.append("| %s | %s | %s | %s |"
                     % (check["check_id"], check["verdict"],
                        check.get("actual"), check.get("threshold")))
    lines.append("")

    lines.append("## 五、告警清单")
    lines.append("")
    if not gate_warnings and not stage_warnings:
        lines.append("本次运行未产生任何告警。")
    else:
        if gate_warnings:
            lines.append("### 闸门告警（pass_with_warning）")
            lines.append("")
            for item in gate_warnings:
                lines.append("- **%s**：%s"
                             % (item.get("check_id"), item.get("message")))
            lines.append("")
        if stage_warnings:
            lines.append("### 阶段告警")
            lines.append("")
            for item in stage_warnings:
                handled = item.get("action_taken") or "未处置"
                lines.append("- **%s / %s**：%s（处置：%s）"
                             % (item.get("stage"), item.get("code"),
                                item.get("message"), handled))
            lines.append("")

    lines.append("## 六、基线回归对照")
    lines.append("")
    if not baseline_comparison:
        lines.append("本次任务未提供 `baseline`，不做回归对照。")
        lines.append("")
    else:
        lines.append("- 基线来源：`%s`"
                     % (baseline_comparison.get("baseline_path") or "未记录"))
        lines.append("- 对照结论：%s"
                     % ("全部一致" if baseline_comparison.get("consistent")
                        else "存在差异，逐项见下表"))
        lines.append("")
        lines.append("| 指标 | 基线值 | 本次值 | 绝对差 | 容差 | 一致 |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for item in baseline_comparison.get("items") or []:
            lines.append("| %s | %s | %s | %s | %s | %s |"
                         % (item.get("item"), item.get("expected"),
                            item.get("actual"),
                            item.get("diff", item.get("note", "-")),
                            item.get("tolerance", "-"),
                            "是" if item.get("match") else "否"))
        lines.append("")

    lines.append("## 七、产物清单")
    lines.append("")
    for name in metadata["output_files"]:
        lines.append("- %s" % name)
    lines.append("")

    report_path = output_prefix + "_report.md"
    with open(report_path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines))
    return report_path


# ===============================================================
# 十、调度入口
# ===============================================================

def _stage_derived_trait(context, gatekeeper):
    """04: generate the requested registered trait before Agent 01 QC."""
    return _run_derived_trait_stage(context)


_STAGE_HANDLERS = {
    "00": _stage_supervisor,
    "01": _stage_phenotype,
    "04": _stage_derived_trait,
    "02": _stage_pedigree,
    "03": _stage_genotype,
    "05": _stage_integration,
    "06": _stage_planner,
    "07": _stage_execution,
    "08": _stage_validator,
    "09": _stage_reporter,
}


def run(agent_id, context, gatekeeper):
    """
    stage_runner 契约入口。
    未登记的 agent（03 基因型 / 04 衍生性状）在 PBLUP 单性状任务中
    由 router 依 skip_conditions 跳过，正常不会走到这里；
    若真被调用，如实返回"本执行器不承接"而不是假装完成。
    """
    handler = _STAGE_HANDLERS.get(str(agent_id))
    if handler is None:
        return _empty_result(
            warnings=[{
                "code": "STAGE_NOT_IMPLEMENTED",
                "message": "ssgblup_stage_runner 不承接阶段 %s" % agent_id,
                "affected_count": None,
                "action_taken": "该阶段在单性状 SSGBLUP 流程中不产生数据，"
                                "已跳过且不影响结果",
            }])
    return handler(context, gatekeeper)
