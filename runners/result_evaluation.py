# -*- coding: utf-8 -*-
"""Shared result-evaluation artifacts for PBLUP, GBLUP and SSGBLUP.

O3（2026-08-24）：GBLUP 多性状分支。plan.mode == "multi" 且 traits >= 2 时
走 _build_artifacts_multi：逐性状产出 EBV 汇总与 Top20，报告含遗传相关；
基线对照暂不支持（口径不同，如实告警而非硬凑数值）。
单性状路径行为与既往完全一致（返回键 report_path / top_path 原样保留）。
"""

import json
import os


def _load_baseline(baseline_path):
    if not baseline_path or not os.path.isfile(baseline_path):
        return None
    with open(baseline_path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    return payload.get("summary") if isinstance(payload.get("summary"), dict) else payload


def build_artifacts(plan, state, parsed, validation, validate_tool,
                    write_json_artifact, reporting_ids=None,
                    reporting_population=None, heritability_by_trait=None,
                    genetic_correlations=None):
    """Create model-independent result summaries and report artifacts."""
    traits = list(plan.get("traits") or [])
    if (plan.get("mode") == "multi" and len(traits) >= 2):
        return _build_artifacts_multi(
            plan, state, parsed, validation, validate_tool, write_json_artifact,
            reporting_ids, reporting_population, traits,
            heritability_by_trait or {}, genetic_correlations or [])

    prediction_individual_count = len(parsed.get("rand") or [])
    report_parsed = _restrict_to_ids(parsed, reporting_ids)
    reporting_population = dict(reporting_population or {})
    reporting_population.setdefault("basis", "all_prediction_ids" if reporting_ids is None else "explicit_ids")
    reporting_population["prediction_individual_count"] = prediction_individual_count
    reporting_population["reporting_individual_count"] = len(report_parsed.get("rand") or [])
    ebv_summary = validate_tool.summarize_breeding_values(report_parsed)
    top_individuals = validate_tool.rank_top_individuals(
        report_parsed, top_n=20,
        genetic_variance=(validation.summary or {}).get("genetic_variance"))
    result_evaluation = _compact_result_evaluation(
        validate_tool.evaluate_result_quality(report_parsed, validation.summary))

    warnings = []
    baseline_comparison = None
    baseline_payload = _load_baseline(plan.get("baseline_path"))
    if baseline_payload:
        tolerance = float(plan.get("baseline_tolerance") or 1e-4)
        consistent, difference_items = validate_tool.compare_with_baseline(
            validation.summary, baseline_payload, tolerance=tolerance)
        baseline_comparison = {
            "baseline_path": plan.get("baseline_path"),
            "tolerance": tolerance,
            "consistent": bool(consistent),
            "items": difference_items,
        }
        if not consistent:
            warnings.append({
                "code": "BASELINE_DIFF",
                "message": "与历史基线存在数值差异：%s" % json.dumps(
                    difference_items, ensure_ascii=False)[:600],
                "affected_count": None,
                "action_taken": "已在报告中列出逐项差异与基线来源；"
                                "本次输入经 ABGES 表型质控（含 PH006 组内离群置缺失），"
                                "与基线输入不完全等同，差异如实保留，未做任何调参对齐",
            })

    report_path = write_json_artifact(
        os.path.join(state.dirs["reports"], "validation_report.json"),
        {"checks": validation.checks,
         "summary": validation.summary,
         "suggested_status": validation.suggest_status(),
         "ebv_summary": ebv_summary,
         "result_evaluation": result_evaluation,
         "reporting_population": reporting_population,
         "baseline_comparison": baseline_comparison})
    top_path = write_json_artifact(
        os.path.join(state.dirs["reports"], "ebv_top20.json"),
        {"trait": plan["trait"], "top_individuals": top_individuals,
         "reporting_population": reporting_population,
         "result_evaluation": result_evaluation})
    return {
        "warnings": warnings,
        "ebv_summary": ebv_summary,
        "result_evaluation": result_evaluation,
        "baseline_comparison": baseline_comparison,
        "reporting_population": reporting_population,
        "top_individuals": top_individuals,
        "report_path": report_path,
        "top_path": top_path,
        "artifact_pairs": [("validation_report", report_path),
                           ("ebv_top20", top_path)],
    }


def _build_artifacts_multi(plan, state, parsed, validation, validate_tool,
                           write_json_artifact, reporting_ids,
                           reporting_population, traits,
                           heritability_by_trait, genetic_correlations):
    """多性状结果评估：逐性状 EBV 汇总 / Top20，报告附遗传相关。"""
    rand_by_trait = parsed.get("rand_by_trait") or {}
    ebv_summary = {}
    result_evaluation = {}
    top_artifacts = []
    top_payloads = {}
    for trait in traits:
        view = dict(parsed)
        view["rand"] = rand_by_trait.get(trait) or []
        view = _restrict_to_ids(view, reporting_ids)
        population = dict(reporting_population or {})
        population.setdefault("basis",
                              "all_prediction_ids" if reporting_ids is None
                              else "explicit_ids")
        population["trait"] = trait
        population["prediction_individual_count"] = len(rand_by_trait.get(trait) or [])
        population["reporting_individual_count"] = len(view.get("rand") or [])
        ebv_summary[trait] = validate_tool.summarize_breeding_values(view)
        top_individuals = validate_tool.rank_top_individuals(
            view, top_n=20,
            genetic_variance=(heritability_by_trait.get(trait) or {}).get("genetic_variance"))
        result_evaluation[trait] = _compact_result_evaluation(
            validate_tool.evaluate_result_quality(
                view, heritability_by_trait.get(trait) or {}))
        top_path = write_json_artifact(
            os.path.join(state.dirs["reports"], "ebv_top20_%s.json" % trait),
            {"trait": trait, "top_individuals": top_individuals,
             "reporting_population": population,
             "result_evaluation": result_evaluation[trait]})
        top_artifacts.append(("ebv_top20_%s" % trait, top_path))
        top_payloads[trait] = top_individuals

    # 基线对照：多性状 h² 为按性状字典，与单性状基线结构不同，
    # 不做数值硬凑，如实告警说明暂不支持。
    warnings = [{
        "code": "MULTI_TRAIT_BASELINE_UNSUPPORTED",
        "message": "多性状实验分支暂不支持基线回归对照，本次未执行 baseline 比对",
        "affected_count": None,
        "action_taken": "多性状 summary 的数值项为按性状字典，与单性状基线"
                        "结构不同；如需基线对照，请逐性状单独运行单性状任务",
    }] if plan.get("baseline_path") else []

    genetic_correlation_records = [
        entry for entry in genetic_correlations if entry.get("is_genetic")]

    report_path = write_json_artifact(
        os.path.join(state.dirs["reports"], "validation_report.json"),
        {"mode": "multi",
         "traits": list(traits),
         "checks": validation.checks,
         "summary": validation.summary,
         "suggested_status": validation.suggest_status(),
         "ebv_summary_by_trait": ebv_summary,
         "result_evaluation_by_trait": result_evaluation,
         "genetic_correlations": genetic_correlation_records,
         "baseline_comparison": None})
    return {
        "warnings": warnings,
        "ebv_summary": ebv_summary,
        "result_evaluation": result_evaluation,
        "baseline_comparison": None,
        "genetic_correlations": genetic_correlation_records,
        "report_path": report_path,
        "artifact_pairs": [("validation_report", report_path)] + top_artifacts,
    }


def _restrict_to_ids(parsed, reporting_ids):
    """Return a shallow parsed-result copy restricted to an explicit ID set."""
    if reporting_ids is None:
        return parsed
    allowed = {str(item) for item in reporting_ids}
    restricted = dict(parsed)
    for key in ("rand", "pev", "reliability", "reliability_records"):
        records = parsed.get(key)
        if isinstance(records, list):
            restricted[key] = [record for record in records
                               if str(record.get("ID", record.get("id", ""))) in allowed]
    # 多性状：逐性状 rand 表同样按报告群体裁剪
    rand_by_trait = parsed.get("rand_by_trait")
    if isinstance(rand_by_trait, dict):
        restricted["rand_by_trait"] = {
            trait: [record for record in (records or [])
                    if str(record.get("ID", record.get("id", ""))) in allowed]
            for trait, records in rand_by_trait.items()}
    return restricted


def _compact_result_evaluation(payload):
    """Keep population metrics in JSON; row-level reliability lives in Excel."""
    compact = dict(payload or {})
    reliability = compact.get("ebv_reliability")
    if isinstance(reliability, dict):
        reliability = dict(reliability)
        reliability.pop("individuals", None)
        reliability["individual_detail_artifact"] = "delivery_ebv_workbook"
        compact["ebv_reliability"] = reliability
    return compact
