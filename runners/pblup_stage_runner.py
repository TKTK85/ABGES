# -*- coding: utf-8 -*-
"""
PBLUP 单性状真实阶段执行器
==========================
实现 orchestrator 的 stage_runner 契约，串联 ABGES 全流程：

    00 任务解析 -> 01 表型质控 -> 02 系谱质控 -> 05 数据整合
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

标准化 PH007（configs/models/pblup.yaml#standardization）：
    单/多性状均 -> none（保持原始量纲；2026-10-12 修订，多性状不再强制 z-score）。
本执行器只支持单性状 PBLUP，因此 G4-06 的判定依据是"未做标准化"。
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
    validate_multi_trait_result as _validate_multi_trait_result,
    run_independent_validation as _run_independent_validation,
    apply_declared_fixed_effects as _apply_declared_fixed_effects,
    diagnose_model_design as _diagnose_model_design,
    delivery_formats as _delivery_formats,
)

MODEL_ID = "pblup"

ID_COLUMN = "ID"

# 统计登记用的阶段名，G2-04 依此判定处理前后统计是否完整
STAT_STAGE_PHENOTYPE = "01-phenotype"
STAT_STAGE_PEDIGREE = "02-pedigree"


# ===============================================================
# 一、通用小工具
# ===============================================================

def _stage_supervisor(context, gatekeeper):
    """
    解析任务，落定本次运行的口径：性状、固定效应、协变量、输出目录。

    单/多性状口径（2026-10-12 起支持多性状 PBLUP）：
      - traits 恰好 1 个                      -> mode=single（行为与既往完全一致）
      - traits >= 2 且声明 multi_trait: true  -> mode=multi（HIBLUP --multi-trait）
      - 口径冲突（声明与实际性状数不符）如实报错，不静默降级也不静默改选。
    """
    task = _require_context(context, "__task__", "00")
    policy = _require_context(context, "__policy__", "00")

    traits = list(task.get("traits") or [])
    if not traits:
        raise core.AbgesError("E013", "任务未声明任何性状（traits 为空）")
    if len(traits) > 5:
        raise core.AbgesError("E013", "traits 数量超过 5（多性状上限）",
                              {"n": len(traits)})
    mode = "multi" if len(traits) >= 2 else "single"
    declared_multi = task.get("multi_trait")
    if declared_multi is True and mode == "single":
        raise core.AbgesError(
            "E013", "multi_trait=true 但 traits 只有 1 个，口径冲突",
            {"traits": traits})
    if declared_multi is False and mode == "multi":
        raise core.AbgesError(
            "E013", "multi_trait=false 但 traits 有 %d 个，口径冲突；"
                    "多性状分析请在任务中声明 multi_trait: true" % len(traits),
            {"traits": traits})

    model_id = str(context.get("__model__") or task.get("model") or "").lower()
    if model_id != MODEL_ID:
        raise core.AbgesError(
            "E012", "pblup_stage_runner 只支持 pblup，本次选定模型为 %r" % model_id)

    inputs = task.get("inputs") or {}
    if not inputs.get("pedigree"):
        raise core.AbgesError("G1-05", "PBLUP 需要系谱，但任务未提供 pedigree")

    # 输出前缀与元数据用的性状标签：多性状用 "+" 连接（如 EN+ER）
    trait_label = "+".join(traits) if mode == "multi" else traits[0]
    fixed_effects = task.get("fixed_effects")
    # 协变量未声明时按空处理：不引入 phenotype-qc.yaml 的候选列，
    # 避免对本任务并不存在的列产生"列不存在"告警（PH001）。
    covariates = task.get("covariates")
    covariates = [] if covariates is None else list(covariates)

    output_dir = policy.assert_output_dir_allowed(
        task.get("output_dir") or "workspace/output")

    # 【新结构 2026-10-16】任务根目录（由 main.py 传入 context）
    task_root = context.get("task_root") or output_dir

    engine_params = dict(task.get("engine_params") or {})
    model_config = core.load_model_config(MODEL_ID)
    engine_defaults = model_config.get("hiblup", {}) or {}

    plan = {
        "task_id": task.get("task_id") or "task",
        "task_name": task.get("task_name") or task.get("task_id") or "task",
        "trait": trait_label,
        "traits": traits,
        "mode": mode,
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
        "covariates_source": "task" if task.get("covariates") is not None
                             else "任务未声明协变量，按空列表处理",
        "output_dir": output_dir,
        "task_root": task_root,  # 【新结构 2026-10-16】任务根目录
        "phenotype_path": inputs.get("phenotype"),
        "egg_record_path": inputs.get("egg_record"),
        "pedigree_path": inputs.get("pedigree"),
        "baseline_path": task.get("baseline"),
        "baseline_tolerance": task.get("baseline_tolerance"),
        "permanent_environment": bool(task.get("permanent_environment", False)),
        "engine": {
            "vc_method": engine_params.get("vc_method",
                                           engine_defaults.get("vc_method", "AI")),
            "tolerance": float(engine_params.get(
                "tolerance", engine_defaults.get("tolerance", 1e-6))),
            "max_iter": int(engine_params.get(
                "max_iter", engine_defaults.get("max_iter", 30))),
            "em_max_iter": int(engine_defaults.get("em_max_iter", 200)),
            "threads": engine_params.get("threads"),
            # O1（2026-08-24 性能实测）：--r2 使单任务耗时约 6.7 倍且不改变 h²/VA/VE，
            # 但会在 .rand 逐个体输出 PEV/SE/R2，是 EBV 可靠性/准确率的唯一来源。
            # 【2026-10-19 默认开启】reliability 默认 True，默认附加引擎 --r2 产出可靠性；
            # 如确需关闭以省耗时，在 engine_params 显式写 reliability: false 即可（引擎不附加 --r2）。
            "reliability": bool(engine_params.get("reliability", True)),
            "timeout": int(engine_defaults.get("timeout", 3600)),
            "extra_args": list(engine_params.get(
                "extra_args", engine_defaults.get("extra_args", [])) or []),
        },
        # PH007 标准化合同（2026-10-12 修订）：单/多性状均为 none，保持原始量纲
        "standardization_contract": (
            (model_config.get("standardization", {}).get("multi_trait", "none"))
            if mode == "multi"
            else (model_config.get("standardization", {})
                  .get("single_trait", "none"))),
        # 上游标准化声明：必须透传到 01 表型质控；否则 PH006 的
        # skip_when_pre_standardized 不生效，缺失尺度切换留痕且可能在
        # 原始观测尺度上误判已标准化的 z 值（静默失真）。
        "pre_standardized": _resolve_pre_standardized(task, traits),
    }
    return _empty_result(blackboard={"plan": plan})


def _resolve_pre_standardized(task, analysis_traits):
    """
    解析 task.pre_standardized（契约见 schemas/task-schema.yaml）。

    analysis_traits 可为单个性状代码或性状列表；多性状时要求
    本次分析的每个性状都在声明范围内（2026-10-12）。

    未声明时返回 None，表示"按原始观测值处理"，行为与既往一致。
    声明格式错误直接报错，不做猜测式兼容——错误的尺度声明会让
    PH006 按错误标准判定，后果比中止更严重。
    """
    return _resolve_pre_standardized_common(task, analysis_traits)


# ===============================================================
# 三、01 表型质控
# ===============================================================

def _stage_phenotype(context, gatekeeper):
    """读入表型 -> PH001~PH007 质控 -> 生成引擎表型文件 -> 产出 G1/G2 表型侧事实。"""
    plan = _require_context(context, "plan", "01")
    state = _require_context(context, "__state__", "01")
    traits = plan["traits"]
    mode = plan.get("mode") or "single"

    frame, info, rename_detail, unmapped = phenotype_tool.load_phenotype(
        plan["phenotype_path"])
    derived_frame = context.get("derived_phenotype_frame")
    if derived_frame is not None:
        frame = derived_frame.copy()
        info = dict(info)
        info["derived_trait_input"] = context.get("derived_trait_path")

    pre_std = plan.get("pre_standardized") or {}
    if mode == "multi":
        # 2026-10-12（先生指令）：多性状先逐性状各自跑一遍完整 PH001–PH007，
        # 再按 ID 并集组装成一张宽表交由引擎。好处是单性状脏数据不连坐全任务，
        # 且 change_log / before-after 统计按性状独立留痕。
        qc = phenotype_tool.run_phenotype_qc_by_trait(
            frame, traits=traits,
            fixed_effects=plan["fixed_effects"],
            covariates=plan["covariates"],
            rename_detail=rename_detail,
            pre_standardized_traits=pre_std.get("traits"),
            standardization_scope=pre_std.get("scope"),
            standardization_method=pre_std.get("method"),
            config=_phenotype_qc_config_for_run(state.dirs["intermediate"]))
    else:
        qc = phenotype_tool.run_phenotype_qc(
            frame, traits=traits, fixed_effects=plan["fixed_effects"],
            covariates=plan["covariates"], mode="single",
            rename_detail=rename_detail,
            pre_standardized_traits=pre_std.get("traits"),
            standardization_scope=pre_std.get("scope"),
            standardization_method=pre_std.get("method"),
            config=_phenotype_qc_config_for_run(state.dirs["intermediate"]))

    # 明确任务固定效应清单是模型唯一入模合同，且告警同步准确说明过滤结果。
    _apply_declared_fixed_effects(qc, plan.get("fixed_effects"))

    engine_path = os.path.join(state.dirs["input"], "engine_phenotype.txt")
    engine_phenotype = phenotype_tool.build_engine_phenotype(
        qc, traits, engine_path, id_column=ID_COLUMN)
    engine_phenotype_path = engine_phenotype["path"]

    # HIBLUP requires ASCII categorical labels; source data stays unchanged.
    import abges_ascii as ascii_tool
    engine_input_path, engine_label_mapping, engine_ascii_sanitized = ascii_tool.sanitize_phenotype_for_engine(
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

    # ---- 关键指标（多性状：逐性状统计，闸门汇总口径取最严值） ----
    trait_metrics = {}
    for trait_name in traits:
        valid = int(qc.frame[trait_name].notna().sum())
        variance = qc.frame[trait_name].var()
        variance = float(variance) if variance == variance else 0.0
        trait_metrics[trait_name] = {"valid_records": valid,
                                     "trait_variance": variance}
    valid_records = min(item["valid_records"] for item in trait_metrics.values())
    trait_variance = min(item["trait_variance"] for item in trait_metrics.values())
    duplicate_ids = int(qc.frame[ID_COLUMN].duplicated().sum())
    drop_rate = float(qc.statistics.get("drop_rate", 0.0) or 0.0)
    standardized = bool(qc.statistics.get("standardized", False))
    fixed_levels = _collect_fixed_effect_levels(qc.frame, qc.effective_fixed_effects)
    non_ascii_columns = [name for name in engine_phenotype["columns"]
                         if not _is_pure_ascii(name)]
    non_ascii_levels = {column: [value for value in values if not _is_pure_ascii(value)]
                        for column, values in fixed_levels.items()}
    non_ascii_levels = {k: v for k, v in non_ascii_levels.items() if v}
    if engine_ascii_sanitized:
        non_ascii_levels = {}  # G4 validates the sanitized engine input.
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
    # 必须在 PH005 之后、组装之前运行：输入是 PH005 已确认可估的有效固定效应。
    effect_screening = _run_effect_screening(qc, traits, mode)
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
            "traits": traits,
            "mode": mode,
            "trait_metrics": trait_metrics,
            "phenotype_assembly": {
                "mode": mode,
                "cleaning": (("逐性状独立清洗后按 %s 并集组装" % ID_COLUMN)
                             if mode == "multi" else "单性状清洗")},
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
        "has_duplicate_ids": duplicate_ids > 0,
        "trait_variance": trait_variance,
        "trait_metrics": trait_metrics,
        "drop_rate": drop_rate,
        "standardized": standardized,
        "fixed_effect_levels": fixed_levels,
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
        "actual": {"pedigree_individuals": pedigree_individuals, "genotype": False},
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
# 五、05 数据整合
# ===============================================================

def _stage_integration(context, gatekeeper):
    """表型 × 系谱对齐，产出 G3 闸门事实。"""
    state = _require_context(context, "__state__", "05")
    phenotype_qc = _require_context(context, "phenotype_qc", "05")
    pedigree_qc = _require_context(context, "pedigree_qc", "05")

    integration = integration_tool.integrate_sources(
        phenotype_qc.frame, ped_frame=pedigree_qc.frame,
        id_column=ID_COLUMN, model=MODEL_ID,
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
    phenotype_qc = _require_context(context, "phenotype_qc", "06")
    metrics = _require_context(context, "phenotype_metrics", "06")
    integration_metrics = _require_context(context, "integration_metrics", "06")

    traits = plan["traits"]
    mode = plan.get("mode") or "single"
    trait_label = plan["trait"]
    engine_available, engine_info = tools.verify_hiblup()
    if not engine_available:
        raise core.AbgesError("E014", "HIBLUP 引擎不可用：%s" % engine_info)

    # HIBLUP 原始输出 → output/ 目录
    output_prefix = policy.build_output_prefix(
        state.dirs["output"], plan["task_id"], trait_label, MODEL_ID)

    engine_config = plan["engine"]
    # O4（2026-08-24）：任务声明 permanent_environment: true 时，
    # 把个体 ID 列（首列，列号 1）加入 --rand，拟合永久环境效应（PE）。
    # 依据 HIBLUP 官方教程"重复记录模型"：--rand 1 即以 ID 列作环境随机。
    # 前提：表型存在重复记录（产蛋数等）；单条记录性状声明该项无意义，
    # 将以 E013 阻断防止误用。
    random_effects_map = None
    if plan.get("permanent_environment"):
        if not metrics.get("has_duplicate_ids", False):
            raise core.AbgesError(
                "E013",
                "permanent_environment=true 但表型个体 ID 无重复记录，"
                "永久环境效应不可估；请确认这是重复测定性状（如产蛋数），"
                "或去掉该声明")
        random_effects_map = {trait_name: [ID_COLUMN] for trait_name in traits}

    # ---- 数据驱动效应筛选（阶段06）：仅读取阶段01 的诊断结论，不自动应用 ----
    # 本模块只做诊断+提示：是否采纳建议、是否入模始终由用户任务要求决定
    # （先生拍板 2026-10-17），程序不代劳剔除，effective_* 保持 PH005 之后原样。
    effect_screening = context.get("effect_screening")
    if effect_screening is None:
        # 兜底：阶段01 未产出时现算一次，保证流水线可独立运行
        effect_screening = _run_effect_screening(phenotype_qc, traits, mode)
    design_diagnostics_by_trait = _diagnose_model_design(
        phenotype_qc.frame, [traits[0]], phenotype_qc.effective_fixed_effects,
        phenotype_qc.effective_covariates)
    design_diagnostics = design_diagnostics_by_trait[traits[0]]

    command, hiblup_plan = hiblup_tool.assemble_hiblup_command(
        engine_phenotype["path"], output_prefix, model_id=MODEL_ID,
        trait_columns=list(traits),
        fixed_effects={trait_name: phenotype_qc.effective_fixed_effects
                       for trait_name in traits},
        covariates={trait_name: phenotype_qc.effective_covariates
                    for trait_name in traits}
        if phenotype_qc.effective_covariates else None,
        random_effects=random_effects_map,
        pedigree_path=engine_pedigree["path"], mode=mode,
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
    # 多性状：逐个核对各性状列序号（综合 G4-02 的要求：列序号不得靠人工推算）
    column_positions = engine_phenotype["column_positions"]
    engine_positions = hiblup_plan.get("trait_positions") or []
    declared_positions = {name: column_positions.get(name) for name in traits}
    actual_positions = {
        trait_name: (engine_positions[index]
                     if index < len(engine_positions) else None)
        for index, trait_name in enumerate(traits)}
    positions_matched = all(declared_positions[name] is not None
                            and declared_positions[name] == actual_positions[name]
                            for name in traits)
    dropped_fixed = hiblup_plan.get("fixed_effects_dropped") or []
    # 声明存在但源数据缺失、被静默丢弃的固定效应（强制约束：缺失即建模退化）
    missing_fixed = list(getattr(phenotype_qc, "missing_fixed_effects", []) or [])
    single_level = metrics["single_level_columns"]
    non_ascii_columns = metrics["non_ascii_columns"]
    non_ascii_levels = metrics["non_ascii_levels"]
    prefix_conflict = [name for name in os.listdir(plan["output_dir"])
                       if name.startswith(os.path.basename(output_prefix))]

    g4_facts = {
        "G4-01": {
            "passed": (integration_metrics.get("pedigree_individuals", 0) > 0
                       and metrics["valid_records"] > 0),
            "actual": {"model": MODEL_ID,
                       "phenotype_records": metrics["valid_records"],
                       "pedigree_individuals":
                           integration_metrics.get("pedigree_individuals"),
                       "engine": engine_info},
            "threshold": "pblup requires phenotype + pedigree",
        },
        "G4-02": {
            "passed": positions_matched,
            "actual": {"declared": declared_positions,
                       "engine_arg": actual_positions},
            "threshold": "两者一致（1-based，多性状逐个核对）",
        },
        "G4-03": {
            "passed": (not single_level and not dropped_fixed and not missing_fixed),
            "actual": {"levels": {column: len(values) for column, values
                                  in metrics["fixed_effect_levels"].items()},
                       "dropped_by_engine": dropped_fixed,
                       "missing_declared": missing_fixed},
            "threshold": ">= 2 且声明固定效应均已就位",
            "message": None if (not single_level and not dropped_fixed
                                and not missing_fixed)
            else "固定效应建模退化：%s"
                 % (single_level or dropped_fixed or ("缺失:%s" % missing_fixed)),
        },
        "G4-04": {
            "passed": not non_ascii_columns and not non_ascii_levels,
            "actual": {"non_ascii_columns": non_ascii_columns,
                       "non_ascii_levels": non_ascii_levels},
            "threshold": "全部 ASCII",
        },
        "G4-05": {
            "passed": metrics["trait_variance"] > 0,
            "actual": (metrics.get("trait_metrics")
                       if mode == "multi"
                       else round(metrics["trait_variance"], 6)),
            "threshold": "各性状 > 0（汇总取最小）",
        },
        "G4-06": {
            # PH007（2026-10-12 修订）：single/multi 合同均为 none，
            # 两种模式都要求"未做标准化"才算合规（多性状不再强制 z-score）
            "passed": (not metrics["standardized"]
                       and plan["standardization_contract"] == "none"),
            "actual": {"mode": mode, "standardized": metrics["standardized"],
                       "contract": plan["standardization_contract"]},
            "threshold": "single_trait=none / multi_trait=none",
        },
        "G4-07": {
            "passed": (os.access(plan["output_dir"], os.W_OK)
                       and not prefix_conflict),
            "actual": {"output_dir": plan["output_dir"],
                       "prefix": os.path.basename(output_prefix),
                       "existing_files_with_prefix": prefix_conflict},
            "threshold": "目录可写且前缀未被占用",
        },
        "G4-08": {
            "passed": not design_diagnostics.get("sparse_groups"),
            "actual": design_diagnostics.get("sparse_groups"),
            "threshold": "固定效应水平样本量至少 %d；稀疏水平告警并人工复核"
                        % design_diagnostics.get("sparse_group_minimum_n", 5),
            "message": ("存在低于稀疏组阈值 %d 的固定效应水平：%s"
                        % (design_diagnostics.get("sparse_group_minimum_n", 5),
                           design_diagnostics.get("sparse_groups")))
            if design_diagnostics.get("sparse_groups") else None,
        },
        "G4-09": {
            "passed": (design_diagnostics.get("rank_evaluated")
                       and not design_diagnostics.get("rank_deficient")),
            "actual": {"rank": design_diagnostics.get("design_rank"),
                       "columns": design_diagnostics.get("design_columns"),
                       "evaluated": design_diagnostics.get("rank_evaluated"),
                       "reason": design_diagnostics.get("reason")},
            "threshold": "实际固定效应设计矩阵满列秩且诊断成功",
            "message": ("固定效应设计矩阵秩亏或无法完成秩诊断：%s"
                        % design_diagnostics.get("reason"))
            if not design_diagnostics.get("rank_evaluated")
            or design_diagnostics.get("rank_deficient") else None,
        },
        "G4-10": {
            "passed": (not design_diagnostics.get("gen_batch", {}).get("applicable")
                       or design_diagnostics.get("gen_batch", {}).get("connected")),
            "actual": design_diagnostics.get("gen_batch"),
            "threshold": "同时拟合世代和批次时，其二部图连通",
            "message": ("世代与批次组间不连通，相关固定效应差异不可直接比较"
                        if (design_diagnostics.get("gen_batch", {}).get("applicable")
                            and not design_diagnostics.get("gen_batch", {}).get("connected"))
                        else None),
        },
    }

    plan_path = _write_json_artifact(
        os.path.join(state.dirs["reports"], "execution_plan.json"),
        {"command_line": hiblup_plan["command_line"],
         "plan": hiblup_plan,
         "output_prefix": output_prefix,
         "engine": engine_info,
         "engine_params": engine_config,
         # 【新增2026-10-16】表头标准化映射表：
         # 用户在 task yaml 中声明的列名 → ABGES 标准化后的实际列名
         # 用于告知用户批次→Batch 等映射，避免对列名产生困惑
         "header_rename_map": dict(phenotype_qc.rename_detail) if hasattr(phenotype_qc, "rename_detail") else {},
         "declared_fixed_effects": plan.get("fixed_effects"),
         "effective_fixed_effects": phenotype_qc.effective_fixed_effects if hasattr(phenotype_qc, "effective_fixed_effects") else [],
         "declared_covariates": plan.get("covariates"),
         "effective_covariates": phenotype_qc.effective_covariates if hasattr(phenotype_qc, "effective_covariates") else [],
         "effect_screening": effect_screening,
         "model_design_diagnostics": design_diagnostics,
         })

    return _empty_result(
        gate_facts={"G4": g4_facts},
        artifacts=[("execution_plan", plan_path)],
        blackboard={"hiblup_command": command,
                    "hiblup_plan": hiblup_plan,
                    "output_prefix": output_prefix,
                    "engine_info": engine_info,
                    "effect_screening": effect_screening,
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

    traits = plan["traits"]
    mode = plan.get("mode") or "single"
    exec_result = tools.run_hiblup(
        command, timeout=plan["engine"]["timeout"], workdir=state.dirs["logs"],
        threads=plan["engine"]["threads"],
        note="PBLUP %s性状 %s" % (mode, plan["trait"]))

    log_path = os.path.join(state.dirs["logs"], "hiblup_stdout.log")
    with open(log_path, "w", encoding="utf-8") as handle:
        handle.write("$ %s\n\n" % " ".join(str(item) for item in command))
        handle.write("[exit_code] %s\n[duration_seconds] %.3f\n\n"
                     % (exec_result.exit_code, exec_result.duration))
        handle.write("----- stdout -----\n%s\n" % (exec_result.stdout or ""))
        handle.write("----- stderr -----\n%s\n" % (exec_result.stderr or ""))

    parsed = hiblup_tool.parse_hiblup_output(
        output_prefix, method=MODEL_ID, mode=mode, trait_names=traits)

    # G5-07：PBLUP 为全部系谱个体产出 EBV（不剔除无表型个体），
    # 因此预期记录数取系谱个体数，而非有效表型记录数。
    if mode == "multi":
        # 逐性状提取遗传力，再合并为 G5-01~G5-08 的最严结论
        heritability_by_trait = {
            trait_name: hiblup_tool.extract_heritability(parsed, trait=trait_name)
            for trait_name in traits}
        heritability = heritability_by_trait[traits[0]]
        validation = _validate_multi_trait(
            parsed, exec_result.exit_code, traits, pedigree_individuals,
            heritability_by_trait, pedigree_ids, plan.get("species"))
        genetic_correlations = hiblup_tool.extract_genetic_correlations(parsed)
    else:
        heritability = hiblup_tool.extract_heritability(parsed)
        heritability_by_trait = {traits[0]: heritability}
        genetic_correlations = []
        validation = validate_tool.validate_engine_result(
            parsed, exec_result.exit_code, trait=traits[0],
            expected_individuals=pedigree_individuals,
            heritability=heritability, expected_ids=pedigree_ids,
            species=plan.get("species"))

    errors = []

    # 强制约束：HIBLUP 必需输出必须齐全且非空（verify_required_outputs 已查非空）
    _ok, _missing = hiblup_tool.verify_required_outputs(
        output_prefix, method=MODEL_ID, mode=mode, trait_names=traits)
    if not _ok:
        errors.append({
            "code": "E015",
            "message": "HIBLUP 必需输出缺失或为空：%s" % _missing,
            "blocking": True,
        })

    artifacts = [("hiblup_stdout_log", log_path)]
    for suffix in (".vars", ".rand", ".beta", ".log", ".covars"):
        candidate = output_prefix + suffix
        if os.path.isfile(candidate):
            artifacts.append(("hiblup" + suffix.replace(".", "_"), candidate))
    if mode == "multi":
        # 逐性状 .rand / .beta 产物
        for trait_name in traits:
            for suffix in (".rand", ".beta"):
                candidate = "%s.%s%s" % (output_prefix, trait_name, suffix)
                if os.path.isfile(candidate):
                    artifacts.append(("hiblup_%s%s" % (trait_name,
                                                       suffix.replace(".", "_")),
                                      candidate))

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
            "heritability_by_trait": heritability_by_trait,
            "genetic_correlations": genetic_correlations,
            "validation": validation,
        })


def _validate_multi_trait(parsed, exit_code, traits, expected_individuals,
                          heritability_by_trait, expected_ids=None, species=None):
    return _validate_multi_trait_result(
        validate_tool, parsed, exit_code, traits, expected_individuals,
        heritability_by_trait, expected_ids, species)


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
                              "analysis_individual_count": len(analysis_ids)},
        heritability_by_trait=context.get("heritability_by_trait"),
        genetic_correlations=context.get("genetic_correlations"))
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

    # 报告输出到 reports/ 目录（与 HIBLUP 输出分离）
    report_prefix = policy.build_output_prefix(
        state.dirs["reports"], plan["task_id"], plan["trait"], MODEL_ID)

    snapshots = [snapshot for snapshot in
                 (context.get("phenotype_statistics_snapshot"),
                  context.get("pedigree_statistics_snapshot")) if snapshot]

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
                               ("pedigree", plan["pedigree_path"]))
            if path and os.path.isfile(path)
        },
        "output_files": sorted(set(state.state.get("artifacts", {}).keys())
                               | {"delivery_ebv_workbook", "delivery_report",
                                  "delivery_docx_report"}),
        "config_snapshot": {
            "model": MODEL_ID,
            "output_contract": plan.get("output_format"),
            "genetic_reporting": plan.get("genetic_reporting"),
            "selection_scenario": plan.get("selection_scenario"),
            "engine_params": plan["engine"],
            "standardization": {"mode": plan.get("mode") or "single",
                                "contract": plan["standardization_contract"],
                                "applied": bool(phenotype_qc.statistics.get(
                                    "standardized", False))},
            "covariates_source": plan["covariates_source"],
        },
        "model_id": MODEL_ID,
        "trait": plan["trait"],
        "traits": plan["traits"],
        "mode": plan.get("mode") or "single",
        "fixed_effects": phenotype_qc.effective_fixed_effects,
        "covariates": phenotype_qc.effective_covariates,
        "status": expected_status,
        "status_note": "本字段为报告阶段依据 G1-G6 判定给出的交付状态；"
                       "最终状态以 harness 汇总的 reports/result.json 为准。",
        "warnings": stage_warnings,
        "errors": list(state.state.get("errors") or []),
    }
    # ---- 面向育种专家的紧凑交付物：Excel 明细 + 摘要报告 ----
    gate_reports_dicts = [rep.to_dict() for rep in gatekeeper.reports.values()]
    snapshots_dicts = [snap.to_dict() for snap in snapshots]
    raw_unchanged, raw_violations = policy.raw_guard.verify_unchanged()
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

    # Word 与 Markdown 共用同一组核心估计，避免重复堆叠日志与诊断图。
    docx_path = None
    try:
        docx_path = docx_report.render_run_docx(
            report_prefix, plan, context, validation, gate_warnings,
            stage_warnings, expected_status, metadata, model_label="PBLUP",
            snapshots=snapshots_dicts, gate_reports=gate_reports_dicts,
            raw_guard={"unchanged": raw_unchanged, "violations": raw_violations},
            top_individuals=context.get("top_individuals"))
    except Exception as exc:
        # 【2026-10-21 统一口径｜先生拍板】原此处抛 E_DOCX 强制中断，但上一行注释
        # 写的却是「软依赖：缺失/异常则降级，不阻断主流程」——注释与代码自相矛盾，
        # 且与 gblup / ssgblup 的同名分支（仅记 warning）不一致，判定为漂移。
        # 现统一为软依赖：Word 失败降级跳过，Markdown 交付报告兜底，评估结论不受影响。
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

    # ---- 文件整理【精简结构 2026-10-16】----
    # 只保留三个交付目录：input/  output/  reports/
    # 其他中间文件（state.json / checkpoint / metadata / statistics 等）保留在磁盘，不对外暴露

    reports_dir = state.dirs["reports"]
    output_dir = state.dirs["output"]

    # 引擎结果与报告分别生成，时间戳可能不同，分别按各自产物前缀登记。
    engine_prefix = os.path.basename(output_prefix)
    file_prefix = os.path.basename(report_prefix)

    # ---- G6 闸门仍需 metadata / statistics 完整性检查（内部文件，不对外暴露）----
    metadata_dir = core.ensure_dir(os.path.join(state.run_dir, "metadata"))
    statistics_dir = core.ensure_dir(os.path.join(state.run_dir, "statistics"))
    metadata["analysis_policy"] = validate_tool.analysis_policy()
    metadata_path = os.path.join(metadata_dir, file_prefix + "_metadata.json")
    with open(metadata_path, "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2, ensure_ascii=False)
    statistics_prefix = os.path.join(statistics_dir, file_prefix)
    statistics_path = core.write_statistics(statistics_prefix, snapshots)

    # ---- Markdown 交付摘要（G6-03：告警必须全部列出） ----
    report_path = delivery_artifacts.render_markdown(
        report_prefix + "_育种值摘要.md", plan, context, validation, expected_status,
        gate_warnings, stage_warnings, metadata, workbook_path)

    metadata_ok = (os.path.isfile(metadata_path)
                   and bool(hiblup_plan.get("command_line")))
    statistics_ok = (os.path.isfile(statistics_path)
                     and all(snapshot.is_complete() for snapshot in snapshots)
                     and len(snapshots) >= 1)
    # 与 gblup/ssgblup 对齐：以 Markdown 报告是否落盘判定「告警是否逐条覆盖」，
    # 不再与 Word 报告成败绑定（Word 为软依赖，不应反向拖累 G6-03 判定）。
    warnings_covered = os.path.isfile(report_path)
    violations = raw_violations

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

    # ---- 用户可见产物：只报告 input/ output/ reports/ 三个目录下的文件 ----
    # 中间工程文件（state.json / checkpoint / metadata / statistics 等）保留在磁盘，但不列入 artifacts
    _run_abs = os.path.abspath(state.run_dir)
    _user_dirs = {"input", "output", "reports"}
    _found = {}
    for _root, _dirs, _files in os.walk(_run_abs):
        # 只收录 user-facing 三个目录的文件
        _rel = os.path.relpath(_root, _run_abs)
        if _rel.split(os.sep)[0] in _user_dirs:
            for _fn in _files:
                _found[_fn] = os.path.join(_root, _fn)

    _key_map = {
        engine_prefix + ".vars": "hiblup_vars",
        engine_prefix + ".rand": "hiblup_rand",
        engine_prefix + ".beta": "hiblup_beta",
        engine_prefix + ".anova": "hiblup_anova",
        engine_prefix + ".log": "hiblup_log",
        os.path.basename(workbook_path): "delivery_ebv_workbook",
        # 【2026-10-21 补齐】Markdown 交付报告此前因函数漏接从未落盘，
        # 亦未登记进 artifacts；现与 gblup/ssgblup 对齐后一并登记，保证可追溯。
        os.path.basename(report_path): "delivery_report",
    }
    if docx_path:
        _key_map[os.path.basename(docx_path)] = "delivery_docx_report"
    _artifacts = []
    for _fn, _key in _key_map.items():
        if _fn in _found:
            _artifacts.append((_key, _found[_fn]))

    # state.artifacts 只保留用户可见文件
    state.state["artifacts"] = {
        _k: _v for _k, _v in state.state.get("artifacts", {}).items()
        if isinstance(_v, dict) and os.path.abspath(_v.get("path", "")).startswith(_run_abs)
    }
    state.save()

    return _empty_result(
        gate_facts={"G6": g6_facts},
        artifacts=_artifacts,
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

    lines.append("# %s 遗传评估报告（%s，PBLUP）"
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
    lines.append("")

    lines.append("## 二、模型与命令")
    lines.append("")
    lines.append("- 模型：PBLUP（系谱 A 矩阵，加性遗传效应）")
    lines.append("- 固定效应：%s" % ", ".join(metadata["fixed_effects"] or ["无"]))
    lines.append("- 协变量：%s" % (", ".join(metadata["covariates"]) or "无"))
    if plan.get("mode") == "multi":
        lines.append("- 标准化：多性状合同为 none，各性状未做标准化，保持原始量纲")
        lines.append("- 表型清洗：逐性状独立运行 PH001–PH007 后按个体号并集组装")
    else:
        lines.append("- 标准化：单性状合同为 none，本次未做标准化，保持原始量纲")
    lines.append("")
    lines.append("```")
    lines.append(metadata["full_command_line"])
    lines.append("```")
    lines.append("")

    lines.append("## 三、方差组分与遗传力")
    lines.append("")
    is_multi = (plan.get("mode") == "multi"
                and isinstance(summary.get("heritability"), dict))
    if is_multi:
        # ---- 多性状：逐性状表 + 遗传相关表 ----
        lines.append("多性状联合分析（%s），逐性状方差组分与遗传力："
                     % "+".join(summary.get("traits") or []))
        lines.append("")
        lines.append("| 性状 | 遗传力 h² | h² 标准误 | 加性遗传方差 | 残差方差 |")
        lines.append("| --- | --- | --- | --- | --- |")
        for trait_name in (summary.get("traits") or []):
            lines.append("| %s | %s | %s | %s | %s |"
                         % (trait_name,
                            (summary.get("heritability") or {}).get(trait_name),
                            (summary.get("heritability_se") or {}).get(trait_name),
                            (summary.get("genetic_variance") or {}).get(trait_name),
                            (summary.get("residual_variance") or {}).get(trait_name)))
        lines.append("")
        lines.append("- 是否收敛：%s" % summary.get("converged"))
        lines.append("- 迭代次数：%s" % summary.get("n_iterations"))
        ebv_records = summary.get("ebv_records")
        if isinstance(ebv_records, dict):
            lines.append("- EBV 记录数（逐性状）：%s"
                         % "，".join("%s=%s" % (name, count)
                                     for name, count in ebv_records.items()))
        lines.append("")
        genetic_correlations = context.get("genetic_correlations") or []
        genetic_records = [entry for entry in genetic_correlations
                           if entry.get("is_genetic")]
        if genetic_records:
            lines.append("性状间加性遗传相关（rg）：")
            lines.append("")
            lines.append("| 性状对 | rg | rg 标准误 | 协方差 |")
            lines.append("| --- | --- | --- | --- |")
            for entry in genetic_records:
                lines.append("| %s:%s | %s | %s | %s |"
                             % (entry.get("trait_a"), entry.get("trait_b"),
                                round(entry["correlation"], 6)
                                if entry.get("correlation") is not None else None,
                                round(entry["se"], 6)
                                if entry.get("se") is not None else None,
                                round(entry["covariance"], 6)
                                if entry.get("covariance") is not None else None))
            lines.append("")
        if isinstance(ebv_summary, dict):
            for trait_name, item in ebv_summary.items():
                if item:
                    lines.append("%s 育种值分布：均值 %s，标准差 %s，最小 %s，最大 %s"
                                 % (trait_name, item.get("mean"), item.get("sd"),
                                    item.get("min"), item.get("max")))
            lines.append("")
    else:
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
                "message": "pblup_stage_runner 不承接阶段 %s" % agent_id,
                "affected_count": None,
                "action_taken": "该阶段在单性状 PBLUP 流程中不产生数据，"
                                "已跳过且不影响结果",
            }])
    return handler(context, gatekeeper)
