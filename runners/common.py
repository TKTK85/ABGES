"""Shared stage-runner helpers with no model-specific calculation logic."""
from __future__ import annotations

import os
import sys

_RUNNER_DIR = os.path.dirname(os.path.abspath(__file__))
_HARNESS_DIR = os.path.join(os.path.dirname(_RUNNER_DIR), "harness")
if _HARNESS_DIR not in sys.path:
    sys.path.insert(0, _HARNESS_DIR)

from _bootstrap import bootstrap
bootstrap()

import abges_core as core


# 报告中"无值"的统一占位符（U+2014 EM DASH），公文与学术表习惯写法。
# 【P2-3】原先散落在 docx_report / report_stats / report_layout 三处各写一遍，
# 现集中于此，保证改一处即全局生效、且三模块口径不会漂移。
DASH = "\u2014"


def dash(value):
    """把任意运行期取值格式化为报告可展示文本；无值一律返回 DASH。"""
    if value is None or value == "":
        return DASH
    if isinstance(value, bool):
        return "\u662f" if value else "\u5426"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        av = abs(value)
        if av != 0 and (av < 1e-3 or av >= 1e6):
            return "%.2e" % value
        s = "%.4f" % value
        if "." in s:
            s = s.rstrip("0")
            if s.endswith("."):
                s += "0"
        return s
    return str(value)


def require_context(context, key, stage_id):
    """Return a required prior-stage value or raise the standard ABGES error."""
    if key not in context or context[key] is None:
        raise core.AbgesError(
            "E019", "?? %s ???????%s" % (stage_id, key),
            {"available": sorted(name for name in context if not name.startswith("__"))},
        )
    return context[key]


def is_pure_ascii(value):
    try:
        str(value).encode("ascii")
    except UnicodeEncodeError:
        return False
    return True


def collect_fixed_effect_levels(frame, columns):
    levels = {}
    for column in columns:
        if column in frame.columns:
            levels[column] = sorted(frame[column].dropna().astype(str).unique().tolist())
    return levels


def convert_tool_warnings(raw_warnings, default_code="QC"):
    converted = []
    for item in raw_warnings or []:
        if isinstance(item, dict):
            converted.append({
                "code": item.get("rule_id") or item.get("code") or default_code,
                "message": item.get("message", ""),
                "affected_count": item.get("affected_count"),
                "action_taken": item.get("action_taken"),
            })
        else:
            converted.append({"code": default_code, "message": str(item),
                              "affected_count": None, "action_taken": None})
    return converted


def resolve_pre_standardized(task, analysis_traits):
    """解析 task.pre_standardized（契约见 schemas/task-schema.yaml）。

    analysis_traits 既可以是单个性状代码，也可以是性状列表：
      · ssgblup 强制单性状 → 传字符串
      · pblup / gblup 支持多性状 → 传列表
    历史实现里三份各写一遍，且 gblup 版本只认列表——传字符串会被
    `for item in traits` 按字符拆开，进而误报「声明未覆盖本次分析性状」。
    此处统一为「两种形态都兼容」，消除该漂移。

    未声明时返回 None，表示「按原始观测值处理」，行为与既往一致。
    声明格式错误直接报错，不做猜测式兼容——错误的尺度声明会让
    PH006 按错误标准判定，后果比中止更严重。
    """
    declared = (task or {}).get("pre_standardized")
    if declared in (None, {}, []):
        return None
    if not isinstance(declared, dict):
        raise core.AbgesError(
            "E013", "task.pre_standardized 必须是对象，实得 %r"
                    % type(declared).__name__)

    declared_traits = declared.get("traits") or []
    if isinstance(declared_traits, str):
        declared_traits = [declared_traits]
    declared_traits = [str(item) for item in declared_traits]
    if not declared_traits:
        raise core.AbgesError(
            "E013", "task.pre_standardized 已声明但 traits 为空，"
                    "无法判断应对哪些性状改用标准化尺度")

    if isinstance(analysis_traits, str):
        target_traits = [analysis_traits]
    else:
        target_traits = [str(item) for item in (analysis_traits or [])]
    uncovered = [name for name in target_traits if name not in declared_traits]
    if uncovered:
        raise core.AbgesError(
            "E013", "task.pre_standardized.traits=%s 未覆盖本次分析性状 %s"
                    % (declared_traits, uncovered))

    scope = declared.get("scope") or []
    if isinstance(scope, str):
        scope = [scope]
    return {
        "traits": declared_traits,
        "scope": [str(item) for item in scope],
        "method": declared.get("method"),
    }


def validation_checks_to_gate_facts(checks):
    return {
        check["check_id"]: {
            "passed": check.get("verdict") == "pass",
            "actual": check.get("actual"),
            "threshold": check.get("threshold"),
            "message": check.get("message"),
        }
        for check in (checks or [])
    }


def validate_multi_trait_result(validate_tool, parsed, exit_code, traits,
                                expected_individuals, heritability_by_trait,
                                expected_ids=None, species=None):
    """Apply one G5 implementation to every trait and merge the strictest verdict."""
    import json
    per_trait = {}
    for trait in traits:
        view = dict(parsed)
        view["rand"] = (parsed.get("rand_by_trait") or {}).get(trait) or []
        view["rand_columns"] = (parsed.get("rand_columns_by_trait") or {}).get(trait) or []
        per_trait[trait] = validate_tool.validate_engine_result(
            view, exit_code, trait=trait,
            expected_individuals=expected_individuals,
            heritability=heritability_by_trait.get(trait),
            expected_ids=expected_ids, species=species)

    merged = validate_tool.ValidationResult()
    checks_by_id = {}
    severity = {"pass": 0, "pass_with_warning": 1, "block": 2}
    for trait in traits:
        for check in per_trait[trait].checks:
            checks_by_id.setdefault(check["check_id"], {})[trait] = check
    for check_id in sorted(checks_by_id):
        trait_checks = checks_by_id[check_id]
        first = next(iter(trait_checks.values()))
        worst = max(trait_checks.values(),
                    key=lambda entry: severity.get(entry["verdict"], 0))
        actual_values = {trait: entry.get("actual")
                         for trait, entry in trait_checks.items()}
        distinct = {json.dumps(value, default=str, ensure_ascii=False)
                    for value in actual_values.values()}
        actual = (next(iter(actual_values.values())) if len(distinct) == 1
                  else actual_values)
        messages = [entry["message"] for entry in trait_checks.values()
                    if entry.get("message")]
        merged.add_check(check_id, first["desc"], worst["verdict"],
                         actual=actual, threshold=worst.get("threshold"),
                         message="；".join(dict.fromkeys(messages)) or None)

    first_summary = dict(per_trait[traits[0]].summary)
    first_summary.update({
        "traits": list(traits), "mode": "multi", "trait": "+".join(traits),
        "heritability": {trait: per_trait[trait].summary.get("heritability") for trait in traits},
        "heritability_se": {trait: per_trait[trait].summary.get("heritability_se") for trait in traits},
        "genetic_variance": {trait: per_trait[trait].summary.get("genetic_variance") for trait in traits},
        "residual_variance": {trait: per_trait[trait].summary.get("residual_variance") for trait in traits},
        "ebv_records": {trait: per_trait[trait].summary.get("ebv_records") for trait in traits},
    })
    merged.summary = first_summary
    return merged


def write_json_artifact(path, payload):
    return core.write_json(path, payload)


def phenotype_qc_config_for_run(intermediate_dir):
    """Load phenotype QC configuration with run-scoped transient output."""
    config = core.load_yaml_config("configs/qc/phenotype-qc.yaml", use_cache=False)
    config.setdefault("unknown_columns", {})["map_output"] = intermediate_dir
    return config


def quality_thresholds():
    """Read versioned, project-wide thresholds used by data and result gates."""
    import yaml
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / "configs" / "validation" / "analysis-policy.yaml"
    with path.open("r", encoding="utf-8") as stream:
        return dict((yaml.safe_load(stream) or {}).get("quality_gates", {}) or {})


def empty_result(**overrides):
    result = {"placeholder": False, "gate_facts": {}, "artifacts": [],
              "warnings": [], "errors": [], "blackboard": {}}
    result.update(overrides)
    return result


def run_derived_trait_stage(context):
    """Execute Agent 04 and publish an in-memory enriched phenotype for Agent 01.

    Raw inputs remain read-only.  The enriched table is written only below the
    current run directory and is passed through the Harness blackboard.
    """
    import pandas as pd
    import abges_phenotype as phenotype_tool
    import abges_traits as traits_tool

    plan = require_context(context, "plan", "04")
    state = require_context(context, "__state__", "04")
    trait = plan["trait"]
    frame, info, _, _ = phenotype_tool.load_phenotype(plan["phenotype_path"])
    definition = traits_tool.get_derived_definition(trait)
    kind = definition.get("compute_kind")
    if kind == "formula":
        derived = traits_tool.compute_formula_trait(frame, trait)
        enriched = derived.frame
    elif kind == "record":
        egg_record = plan.get("egg_record_path")
        if not egg_record:
            raise core.AbgesError("DT005", "?? %s ?? inputs.egg_record" % trait)
        if trait in ("ACL", "MCL"):
            derived = traits_tool.compute_clutch_traits(egg_record)
        elif trait == "CEN":
            derived = traits_tool.compute_cumulative_egg_number(egg_record)
        else:
            raise core.AbgesError("DT002", "????????????%s" % trait)
        enriched, merge = traits_tool.merge_derived_into_phenotype(
            frame, derived.frame, trait_columns=[trait])
        derived.statistics["merge"] = merge
    else:
        raise core.AbgesError("DT002", "???? %s ??? compute_kind" % trait)
    if derived.status == core.STATUS_FAILED:
        raise core.AbgesError("DT006", "???? %s ???????" % trait, derived.to_dict())
    output = os.path.join(state.dirs["intermediate"], "phenotype_with_%s.tsv" % trait)
    enriched.to_csv(output, sep="\t", index=False, encoding="utf-8")
    report = os.path.join(state.dirs["reports"], "derived_trait_%s.json" % trait)
    write_json_artifact(report, {"input": info, **derived.to_dict(), "output": output})
    snapshot = core.StatisticsSnapshot("04-derived-trait")
    snapshot.capture_before(records=int(frame.shape[0]), trait_present=False)
    snapshot.capture_after(records=int(enriched.shape[0]), valid=int(enriched[trait].notna().sum()))
    state.record_statistics("04-derived-trait", snapshot)
    return empty_result(
        artifacts=[("derived_phenotype", output), ("derived_trait_report", report)],
        warnings=convert_tool_warnings(derived.warnings, "DT"),
        blackboard={"derived_phenotype_frame": enriched, "derived_trait_path": output,
                    "derived_trait_metadata": derived.to_dict()},
        gate_facts={"G2": {"G2-04": {"passed": state.statistics_complete("04-derived-trait"),
                    "actual": {"derived_statistics_complete": state.statistics_complete("04-derived-trait")},
                    "threshold": "????????????"}}})


# ===============================================================
# 六、数据驱动效应筛选（阶段01 产出诊断+提示，全程不自动应用）
# ===============================================================

def run_effect_screening(qc, traits, mode):
    """阶段01（表型质控）末尾调用：在 PH005 之后产出数据驱动效应筛选建议。

    输入 qc 为 PhenotypeQCResult / 多性状 PhenotypeQCBatchResult，
    取 qc.frame、qc.effective_fixed_effects（PH005 之后的有效固定效应）、
    qc.effective_covariates。异常时返回最小可用的降级 dict，**绝不阻断主流程**。

    返回 abges_effects.build_effect_screening 的标准结果 dict。
    """
    import abges_effects as effects_tool
    frame = getattr(qc, "frame", None)
    fixed = getattr(qc, "effective_fixed_effects", []) or []
    covs = getattr(qc, "effective_covariates", []) or []
    try:
        return effects_tool.build_effect_screening(
            frame, traits, fixed_columns=fixed, covariate_columns=covs,
            exploration=None, config=None, mode=mode)
    except Exception as exc:  # 兜底：任何意外都不应让评估主流程崩
        return {
            "Enabled": False, "Mode": mode, "Candidates": [],
            "Recommendations": {"keep": [], "drop": [], "choose_one": [], "review": []},
            "SuggestedFixedEffects": [], "SuggestedCovariates": [],
            "Warnings": [],
            "Degraded": ["效应筛选异常：%s（已跳过，不阻断主流程）" % exc],
        }


def effect_screening_warnings(screening):
    """把效应筛选的 Warnings 转成 _convert_tool_warnings 可识别的格式
    （rule_id / message / affected_count / action_taken）。

    本模块只做数据驱动的诊断 + 提示，不修改任何入模清单——
    是否采纳建议、是否入模始终由用户按任务要求决定（先生拍板 2026-10-17）。
    """
    out = []
    for w in (screening or {}).get("Warnings", []) or []:
        detail = w.get("detail") or ""
        suggestion = w.get("suggestion") or ""
        action = "仅提示，是否入模/剔除由用户按任务要求裁定"
        msg = "效应筛选建议：%s" % detail
        if suggestion:
            msg += "；处置建议：%s" % suggestion
        out.append({
            "rule_id": "PH_SCREEN",
            "message": msg,
            "affected_count": None,
            "action_taken": action,
        })
    return out


def run_independent_validation(plan, parsed, phenotype_qc):
    """Score optional held-out adjusted phenotypes against this run's EBVs."""
    validation_path = (plan or {}).get("validation_path")
    if not validation_path:
        return None, []
    import pandas as pd
    import abges_dataio
    import abges_independent_validation as independent
    try:
        observed, _ = abges_dataio.read_table(validation_path, dtype=str)
    except Exception as exc:
        return {"__validation__": {"status": "rejected", "reason": str(exc)}}, [{
            "stage": "08", "code": "INDEPENDENT_VALIDATION_REJECTED",
            "message": "独立验证文件无法读取：%s" % exc,
            "action_taken": "未使用该验证文件；主遗传评估按其自身 G5 结果验收"}]
    observed.columns = [str(column).strip() for column in observed.columns]
    if "ID" not in observed.columns:
        return {"__validation__": {"status": "rejected", "reason": "独立验证表缺少 ID 列"}}, [{
            "stage": "08", "code": "INDEPENDENT_VALIDATION_REJECTED",
            "message": "独立验证表缺少 ID 列，未生成预测验证指标。",
            "action_taken": "未使用该验证文件；主遗传评估按其自身 G5 结果验收"}]
    frame = getattr(phenotype_qc, "frame", None)
    if frame is None or "ID" not in frame.columns:
        return {"__validation__": {"status": "rejected", "reason": "训练 ID 不可用"}}, []
    traits = list((plan or {}).get("traits") or [(plan or {}).get("trait")])
    mapping = (plan or {}).get("validation_adjusted_columns") or {}
    parsed = parsed or {}
    results = {}
    for trait in traits:
        column = mapping.get(trait) or ("adjusted_phenotype" if len(traits) == 1
                                        else "adjusted_" + str(trait))
        records = ((parsed.get("rand_by_trait") or {}).get(trait)
                   if (plan or {}).get("mode") == "multi" else parsed.get("rand")) or []
        predictions = pd.DataFrame(records)
        if predictions.empty or "ID" not in predictions.columns:
            results[trait] = {"status": "unavailable", "reason": "missing EBV records"}
            continue
        if "EBV" not in predictions.columns:
            effect = next((name for name in predictions.columns
                           if str(name).upper() in {"PA", "GA", "HA"}), None)
            if effect is None:
                results[trait] = {"status": "unavailable", "reason": "EBV column not recognized"}
                continue
            predictions = predictions.rename(columns={effect: "EBV"})
        training = frame.loc[frame[trait].notna(), "ID"].astype(str).str.strip()
        try:
            results[trait] = independent.evaluate_frames(
                predictions[["ID", "EBV"]], observed, set(training), column,
                int((plan or {}).get("validation_min_n", 30)))
        except Exception as exc:
            results[trait] = {"status": "rejected", "reason": str(exc)}
    warnings = []
    review = [trait for trait, item in results.items()
              if item.get("status") in {"rejected", "insufficient_sample", "unavailable"}]
    if review:
        warnings.append({"stage": "08", "code": "INDEPENDENT_VALIDATION_REVIEW",
                         "message": "独立验证未形成可用结论（%s）；详情见报告，未用于准确度结论。"
                                    % ", ".join(review),
                         "action_taken": "未将该验证结果用于准确度结论",
                         "requires_review": True})
    return results, warnings


def diagnose_model_design(frame, traits, fixed_effects, covariates):
    from pathlib import Path
    import yaml
    import abges_effects
    config_path = Path(__file__).resolve().parents[1] / "configs" / "qc" / "model-design.yaml"
    with config_path.open("r", encoding="utf-8") as stream:
        config = yaml.safe_load(stream) or {}
    fixed_config = config.get("fixed_effects") or {}
    return {trait: abges_effects.diagnose_model_design(
        frame, trait, fixed_effects, covariates,
        sparse_min_n=int(fixed_config.get("sparse_group_minimum_n", 5)),
        rank_max_columns=int(fixed_config.get("rank_diagnostic_max_columns", 500)))
        for trait in traits}


def apply_declared_fixed_effects(qc, declared):
    """Keep the fitted fixed-effect set equal to the explicit task contract."""
    if declared is None:
        return qc
    declared = set(declared)
    auto_candidates = set(getattr(qc, "auto_included_fixed_effects", []) or [])
    excluded = auto_candidates - declared
    qc.effective_fixed_effects = [column for column in qc.effective_fixed_effects
                                  if column in declared]
    qc.auto_included_fixed_effects = [column for column in
                                      (getattr(qc, "auto_included_fixed_effects", []) or [])
                                      if column in declared]
    if auto_candidates:
        rewritten = []
        for warning in getattr(qc, "warnings", []) or []:
            if (warning.get("rule_id") == "PH_AUTO"
                    and warning.get("code") == "PH_AUTO_INCLUDED_FIXED"
                    and any(str(column) in str(warning.get("message", ""))
                            for column in auto_candidates)):
                matched = [column for column in auto_candidates
                           if str(column) in str(warning.get("message", ""))]
                warning = dict(warning)
                if set(matched).intersection(declared):
                    warning.update({
                        "code": "PH_TASK_CONTRACT_INCLUDED",
                        "message": "自动筛查候选 %s 同时由任务显式声明，已按任务合同纳入本次模型。"
                                   % ", ".join(sorted(set(matched).intersection(declared))),
                        "action_taken": "按任务固定效应清单纳入，并传入 HIBLUP 模型",
                    })
                else:
                    warning.update({
                        "code": "PH_TASK_CONTRACT_EXCLUDED",
                        "message": ("自动筛查发现候选固定效应 %s；任务显式固定效应为 %s，"
                                    "该候选未进入本次模型。若需纳入，请修改任务声明并重新评估。"
                                    % (", ".join(sorted(set(matched))), ", ".join(sorted(declared)))),
                        "action_taken": "按任务固定效应清单过滤；未传入 HIBLUP 模型",
                    })
            rewritten.append(warning)
        qc.warnings = rewritten
    return qc


def design_gate_facts(diagnostics):
    diagnostics = diagnostics or {}
    sparse = {trait: item.get("sparse_groups") for trait, item in diagnostics.items()
              if item.get("sparse_groups")}
    ranks_ok = bool(diagnostics) and all(
        item.get("rank_evaluated") and not item.get("rank_deficient")
        for item in diagnostics.values())
    disconnected = {trait: item.get("gen_batch") for trait, item in diagnostics.items()
                    if item.get("gen_batch", {}).get("applicable")
                    and not item.get("gen_batch", {}).get("connected")}
    return {
        "G4-08": {"passed": not sparse, "actual": sparse or {},
                  "threshold": "固定效应水平样本量≥5；稀疏水平警告并人工复核",
                  "message": "存在稀疏固定效应水平：%s" % sparse if sparse else None},
        "G4-09": {"passed": ranks_ok,
                  "actual": {trait: {"rank": item.get("design_rank"),
                                     "columns": item.get("design_columns"),
                                     "evaluated": item.get("rank_evaluated"),
                                     "reason": item.get("reason")}
                             for trait, item in diagnostics.items()},
                  "threshold": "各性状拟合设计矩阵可诊断且满列秩",
                  "message": "设计矩阵秩亏或无法诊断"
                  if not ranks_ok else None},
        "G4-10": {"passed": not disconnected, "actual": disconnected or
                  {trait: item.get("gen_batch") for trait, item in diagnostics.items()},
                  "threshold": "同时拟合世代与批次时二部图连通",
                  "message": "世代×批次组间不连通" if disconnected else None},
    }


def delivery_formats():
    from pathlib import Path
    import yaml
    path = Path(__file__).resolve().parents[1] / "configs" / "delivery" / "output-contract.yaml"
    with path.open("r", encoding="utf-8") as stream:
        contract = yaml.safe_load(stream) or {}
    deliverables = contract.get("deliverables") or {}
    return list(deliverables.get("required") or []) + list(deliverables.get("optional") or [])
