# -*- coding: utf-8 -*-
"""
ABGES 结果验证模块
==================
把 HIBLUP 解析结果转换为 G5 结果验收闸门可直接判定的事实，
并给出遗传力、EBV 分布、收敛状态等专业层面的核验结论。

铁律：本模块只陈述事实，不美化结果。
遗传力越界、未收敛、SE 过大，一律如实标出并影响交付状态。
"""

import re
from collections import Counter
from pathlib import Path
import yaml

import abges_core as core


# 家禽常见性状遗传力文献参考区间（用于 G5-08 提示，不作阻断依据）
def _validation_policy():
    """Load versioned thresholds; conservative defaults preserve old behavior."""
    path = Path(__file__).resolve().parents[2] / "configs" / "validation" / "analysis-policy.yaml"
    try:
        with path.open("r", encoding="utf-8") as stream:
            return yaml.safe_load(stream) or {}
    except (OSError, yaml.YAMLError):
        return {}


def analysis_policy():
    """Public read-only access to the project's versioned validation policy."""
    return _validation_policy()


class ValidationResult(object):
    """结果验证容器，承载检查明细与交付状态建议。"""

    def __init__(self):
        self.checks = []
        self.summary = {}
        self.warnings = []
        self.blocks = []

    def add_check(self, check_id, description, verdict, actual=None,
                  threshold=None, message=None):
        entry = {
            "check_id": check_id,
            "desc": description,
            "verdict": verdict,
            "actual": actual,
            "threshold": threshold,
            "message": message,
        }
        self.checks.append(entry)
        if verdict == "block":
            self.blocks.append(entry)
        elif verdict == "pass_with_warning":
            self.warnings.append(entry)
        return entry

    def suggest_status(self):
        """
        依据检查结论建议交付状态：
          有 block            -> failed
          有 pass_with_warning -> partial
          全 pass             -> completed
        对应 G6-05：存在告警却报 completed 属违规。
        """
        if self.blocks:
            return core.STATUS_FAILED
        if self.warnings:
            return core.STATUS_PARTIAL
        return core.STATUS_COMPLETED

    def to_dict(self):
        return {
            "checks": self.checks,
            "summary": self.summary,
            "warnings": self.warnings,
            "blocks": self.blocks,
            "suggested_status": self.suggest_status(),
        }


# ===============================================================
# 一、G5 结果验收
# ===============================================================

def validate_engine_result(parsed, exit_code, trait=None,
                           expected_individuals=None, heritability=None,
                           expected_ids=None, species=None):
    """
    执行 G5-01 至 G5-08 全部检查。

    参数：
      parsed                 hiblup_tool.parse_hiblup_output 的返回
      exit_code              引擎退出码
      trait                  性状代码，用于 G5-08 文献区间提示
      expected_individuals   预期 EBV 记录数（通常为系谱个体数）
      heritability           hiblup_tool.extract_heritability 的返回，可为 None

    返回 ValidationResult。
    """
    result = ValidationResult()

    # ---------- G5-01 退出码 ----------
    result.add_check(
        "G5-01", "引擎退出码为 0",
        "pass" if exit_code == 0 else "block",
        actual=exit_code, threshold=0,
        message=None if exit_code == 0 else "引擎异常退出，结果不可信")

    # ---------- G5-02 四件输出齐全 ----------
    missing = parsed.get("missing_files") or []
    result.add_check(
        "G5-02", ".vars/.rand/.beta/.log 齐全且非空",
        "pass" if not missing else "block",
        actual=len(missing), threshold=0,
        message=None if not missing else "缺少输出文件：%s" % missing[:4])

    # ---------- G5-03 收敛 ----------
    # O15（2026-08-24 实测修订）：未收敛时 HIBLUP 仍会产出全套 .vars/.rand，
    # 数值不可信，必须 BLOCK（原为 pass_with_warning，属放行漏洞）。
    converged = parsed.get("converged")
    iterations = parsed.get("n_iterations")
    if converged is True:
        result.add_check("G5-03", "算法已收敛", "pass",
                         actual="converged", threshold="converged")
    elif converged is False:
        result.add_check(
            "G5-03", "算法已收敛", "block",
            actual="not_converged", threshold="converged",
            message="模型未收敛（迭代 %s 次），方差组分与 EBV 不可信，"
                    "结果判定为 block；建议提高 --ai-maxit、更换 vc-method 初值"
                    "（如 HI）或检查数据结构" % iterations)
    else:
        # 强制约束：收敛状态未确认（日志缺失/损坏）时不得放行，必须 BLOCK
        result.add_check(
            "G5-03", "算法已收敛", "block",
            actual="unconfirmed", threshold="converged",
            message="日志中未找到收敛标记，无法确认收敛状态，方差组分与 EBV 不可信")

    # ---------- G5-04 / G5-05 / G5-06 遗传力与方差 ----------
    heritability = heritability or {}
    estimate = heritability.get("estimate")
    standard_error = heritability.get("se")
    residual_variance = heritability.get("residual_variance")

    validation_policy = _validation_policy()
    quality_gates = validation_policy.get("quality_gates", {}) or {}
    h2_ratio_limit = float(quality_gates.get("se_to_h2_warning", 0.5))
    h2_bounds = validation_policy.get("heritability_bounds", [0.0, 1.0])
    if estimate is None:
        result.add_check("G5-04", "遗传力落在配置范围内", "block",
                         actual=None, threshold=list(h2_bounds),
                         message="未能从 .vars 解析出遗传力")
    else:
        in_range = float(h2_bounds[0]) <= estimate <= float(h2_bounds[1])
        result.add_check(
            "G5-04", "遗传力落在配置范围内",
            "pass" if in_range else "block",
            actual=round(estimate, 6), threshold=list(h2_bounds),
            message=None if in_range else "遗传力超出配置范围，需核查模型或数据")

    if residual_variance is None:
        result.add_check("G5-05", "残差方差 > 0", "block",
                         actual=None, threshold="> 0",
                         message="未能解析残差方差")
    else:
        result.add_check(
            "G5-05", "残差方差 > 0",
            "pass" if residual_variance > 0 else "block",
            actual=round(residual_variance, 6), threshold="> 0")

    se_ratio_limit = h2_ratio_limit
    if estimate and standard_error is not None and estimate > 0:
        se_ratio = standard_error / estimate
        result.add_check(
            "G5-06", "SE/h² 在配置的预警阈值内",
            "pass" if se_ratio <= se_ratio_limit else "pass_with_warning",
            actual=round(se_ratio, 6), threshold=se_ratio_limit,
            message=None if se_ratio <= se_ratio_limit
            else "估计不稳定（SE/h²=%.3f），样本量或数据结构可能不足" % se_ratio)
    else:
        result.add_check("G5-06", "SE/h² 在配置的预警阈值内", "pass_with_warning",
                         actual=None, threshold=se_ratio_limit,
                         message="缺少 SE，无法评估估计稳定性")

    # ---------- G5-07 EBV 记录数 ----------
    ebv_records = len(parsed.get("rand") or [])
    id_field = next((key for key in ("ID", "id", "IID", "iid")
                     if any(key in record for record in (parsed.get("rand") or []))), None)
    observed_ids = [str(record.get(id_field)).strip() for record in (parsed.get("rand") or [])
                    if id_field and record.get(id_field) not in (None, "")]
    duplicate_ids = sorted(iid for iid, count in Counter(observed_ids).items() if count > 1)
    if expected_ids is not None:
        expected_set = {str(iid).strip() for iid in expected_ids if iid not in (None, "")}
        observed_set = set(observed_ids)
        missing_ids = sorted(expected_set - observed_set)
        extra_ids = sorted(observed_set - expected_set)
        consistent = (not missing_ids and not extra_ids and not duplicate_ids
                      and len(observed_ids) == len(expected_set)
                      and (expected_individuals is None
                           or ebv_records == expected_individuals))
        actual_ids = {"observed": len(observed_ids), "expected": len(expected_set),
                      "missing_count": len(missing_ids), "extra_count": len(extra_ids),
                      "duplicate_count": len(duplicate_ids),
                      "missing_examples": missing_ids[:10], "extra_examples": extra_ids[:10],
                      "duplicate_examples": duplicate_ids[:10]}
        threshold_ids = "实际 EBV ID 集合与预期分析个体集合完全一致且无重复"
    elif expected_individuals:
        consistent = ebv_records == expected_individuals and not duplicate_ids
        actual_ids = {"observed": ebv_records, "expected": expected_individuals,
                      "duplicate_count": len(duplicate_ids),
                      "duplicate_examples": duplicate_ids[:10]}
        threshold_ids = expected_individuals
    else:
        consistent = ebv_records > 0 and not duplicate_ids
        actual_ids = {"observed": ebv_records, "duplicate_count": len(duplicate_ids),
                      "duplicate_examples": duplicate_ids[:10]}
        threshold_ids = "> 0 且 ID 不重复"
    if expected_individuals or expected_ids is not None:
        result.add_check(
            "G5-07", "EBV 个体 ID 集合与预期范围一致",
            "pass" if consistent else "block",
            actual=actual_ids, threshold=threshold_ids,
            message=None if consistent
            else "EBV ID 集合不匹配：缺失 %d、额外 %d、重复 %d"
                 % (actual_ids.get("missing_count", 0), actual_ids.get("extra_count", 0),
                    actual_ids.get("duplicate_count", 0)))
    else:
        result.add_check(
            "G5-07", "EBV 记录有效且 ID 不重复",
            "pass" if consistent else "block",
            actual=actual_ids, threshold=threshold_ids,
            message=None if consistent else "未产出有效 EBV 记录或存在重复 ID")

    # ---------- G5-08 文献参考区间 ----------
    species_ranges = (validation_policy.get("heritability_reference_ranges_by_species", {}) or {}).get(
        str(species or "").lower(), {})
    reference = species_ranges.get(str(trait).upper()) if trait else None
    if reference and estimate is not None:
        low, high = reference
        within = low <= estimate <= high
        result.add_check(
            "G5-08", "遗传力落在该性状文献参考区间内",
            "pass" if within else "pass_with_warning",
            actual=round(estimate, 6), threshold=list(reference),
            message=None if within
            else "遗传力 %.4f 偏离 %s 常见范围 [%.2f, %.2f]，请在报告中提示核查"
                 % (estimate, trait, low, high))
    else:
        result.add_check("G5-08", "遗传力落在该性状文献参考区间内", "pass",
                         actual=round(estimate, 6) if estimate is not None else None,
                         threshold=None,
                         message="该性状无文献参考区间，跳过判定")

    result.summary = {
        "trait": trait,
        "heritability": round(estimate, 6) if estimate is not None else None,
        "heritability_se": round(standard_error, 6) if standard_error is not None else None,
        "genetic_variance": heritability.get("genetic_variance"),
        "residual_variance": residual_variance,
        "converged": converged,
        "n_iterations": iterations,
        "ebv_records": ebv_records,
        "output_suffix": parsed.get("output_suffix"),
        "model_formula": parsed.get("model_formula"),
    }
    return result


# ===============================================================
# 二、EBV 分布诊断
# ===============================================================

def summarize_breeding_values(parsed, effect_column=None):
    """
    汇总 EBV 分布。effect_column 为 .rand 中的随机效应列名
    （PBLUP 单性状通常为 PA）；未指定时自动取第一个非 ID/residuals 列。
    """
    import pandas as pd

    records = parsed.get("rand") or []
    if not records:
        return {"n": 0, "note": "无 EBV 记录"}
    frame = pd.DataFrame(records)
    if effect_column is None:
        excluded = {"ID", "id", "residuals", "residual"}
        candidates = [name for name in frame.columns if name not in excluded]
        if not candidates:
            return {"n": int(frame.shape[0]), "note": "未识别出随机效应列",
                    "columns": list(frame.columns)}
        effect_column = candidates[0]
    if effect_column not in frame.columns:
        raise core.AbgesError("E030", "EBV 列不存在：%s" % effect_column,
                              {"columns": list(frame.columns)})
    values = pd.to_numeric(frame[effect_column], errors="coerce").dropna()
    if values.empty:
        return {"n": 0, "note": "EBV 列全为缺失", "column": effect_column}
    return {
        "column": effect_column,
        "n": int(values.shape[0]),
        "mean": round(float(values.mean()), 6),
        "sd": round(float(values.std(ddof=1)), 6) if values.shape[0] > 1 else None,
        "min": round(float(values.min()), 6),
        "p25": round(float(values.quantile(0.25)), 6),
        "median": round(float(values.median()), 6),
        "p75": round(float(values.quantile(0.75)), 6),
        "max": round(float(values.max()), 6),
        "positive_count": int((values > 0).sum()),
        "negative_count": int((values < 0).sum()),
    }


def rank_top_individuals(parsed, effect_column=None, top_n=20,
                         ascending=False, genetic_variance=None):
    """
    按 EBV 排名取前 top_n 个体，用于选种建议。
    ascending=True 用于 FCR 这类越小越好的性状。

    开启 HIBLUP --r2 后，.rand 在效应列（如 PA）后同前缀附带
    <效应>_SE / <效应>_PEV / <效应>_R2 列，本函数据实把 EBV 标准误、
    预测误差方差、可靠性与准确率（√可靠性）一并带出；未开 --r2 时这些
    列不存在，对应字段不输出，行为与既往完全一致。可靠性优先采用引擎
    直接给出的 R2，缺失时退回 1 - PEV / 加性遗传方差。
    """
    import pandas as pd

    records = parsed.get("rand") or []
    if not records:
        return []
    frame = pd.DataFrame(records)
    id_column = "ID" if "ID" in frame.columns else frame.columns[0]
    if effect_column is None:
        excluded = {id_column, "residuals", "residual"}
        candidates = [name for name in frame.columns if name not in excluded]
        if not candidates:
            return []
        effect_column = candidates[0]
    frame[effect_column] = pd.to_numeric(frame[effect_column], errors="coerce")
    frame = frame.dropna(subset=[effect_column])

    # 定位与效应列同前缀的 SE / PEV / R2 配套列（列名大小写不敏感）
    prefix = str(effect_column).upper()
    se_col = pev_col = r2_col = None
    for name in frame.columns:
        upper = str(name).upper()
        if upper == prefix + "_SE":
            se_col = name
        elif upper == prefix + "_PEV":
            pev_col = name
        elif upper == prefix + "_R2":
            r2_col = name

    def _num(value):
        try:
            if value is None or (isinstance(value, str) and value.strip() in (".", "", "NA")):
                return None
            return float(value)
        except (TypeError, ValueError):
            return None

    ordered = frame.sort_values(effect_column, ascending=ascending).head(top_n)
    results = []
    for index, (_, row) in enumerate(ordered.iterrows()):
        item = {"rank": index + 1,
                "ID": str(row[id_column]),
                "EBV": round(float(row[effect_column]), 6)}
        if se_col is not None:
            se_value = _num(row[se_col])
            if se_value is not None:
                item["EBV_SE"] = round(se_value, 6)
        if pev_col is not None:
            pev_value = _num(row[pev_col])
            if pev_value is not None:
                item["PEV"] = round(pev_value, 6)
        reliability = _num(row[r2_col]) if r2_col is not None else None
        reliability_source = "HIBLUP_PA_R2" if reliability is not None else None
        if reliability is None and pev_col is not None:
            pev_value = _num(row[pev_col])
            try:
                va = float(genetic_variance) if genetic_variance is not None else None
                if pev_value is not None and va and va > 0:
                    reliability = 1.0 - pev_value / va
                    reliability_source = "derived_1_minus_PEV_over_VA"
            except (TypeError, ValueError):
                reliability = None
        if reliability is not None and 0.0 <= reliability <= 1.0:
            item["reliability"] = round(reliability, 6)
            item["reliability_source"] = reliability_source
            item["accuracy"] = round(math.sqrt(reliability), 6)
        results.append(item)
    return results


# ===============================================================
# 三、回归基线比对
# ===============================================================

def compare_with_baseline(summary, baseline, tolerance=1e-4):
    """
    与既有回归基线比对，用于验证新架构与已跑通结果一致。

    baseline 形如：
      {"heritability": 0.42, "heritability_se": 0.03,
       "genetic_variance": 0.42, "residual_variance": 0.58}
    返回 (是否全部一致, 明细列表)。
    """
    details = []
    consistent = True
    for key, expected in baseline.items():
        actual = summary.get(key)
        if actual is None or expected is None:
            details.append({"item": key, "expected": expected,
                            "actual": actual, "match": False,
                            "note": "缺少可比对数值"})
            consistent = False
            continue
        difference = abs(float(actual) - float(expected))
        matched = difference <= tolerance
        details.append({"item": key, "expected": expected, "actual": actual,
                        "diff": difference, "tolerance": tolerance,
                        "match": matched})
        if not matched:
            consistent = False
    return consistent, details

# ===============================================================
# 四、结果精度与 EBV 可靠性评价
# ===============================================================

import math

_PEV_COLUMN_TOKENS = ("pev", "predictionerrorvariance", "prediction_error_variance")


def evaluate_result_quality(parsed, heritability=None, confidence_level=0.95):
    """Evaluate output precision without changing engine estimates.

    Reliability and accuracy are calculated only when individual PEV is
    available from HIBLUP output. Missing PEV is reported as unavailable.
    """
    heritability = heritability or {}
    h2 = _as_float(heritability.get("estimate", heritability.get("heritability")))
    h2_se = _as_float(heritability.get("se", heritability.get("heritability_se")))
    genetic_variance = _as_float(heritability.get("genetic_variance"))
    z_score = 1.96 if confidence_level == 0.95 else None
    precision_available = h2 is not None and h2_se is not None and z_score is not None
    h2_precision = {
        "available": precision_available,
        "confidence_level": confidence_level,
        "estimate": _round_or_none(h2),
        "se": _round_or_none(h2_se),
        "ci_lower": _round_or_none(h2 - z_score * h2_se) if precision_available else None,
        "ci_upper": _round_or_none(h2 + z_score * h2_se) if precision_available else None,
    }
    if not precision_available:
        h2_precision["reason"] = "HIBLUP output does not contain both heritability and its standard error"
    return {
        "method": {
            "heritability_ci": "estimate +/- 1.96 * SE" if z_score else None,
            "reliability": "1 - PEV / additive_genetic_variance",
            "accuracy": "sqrt(reliability)",
        },
        "heritability_precision": h2_precision,
        "ebv_reliability": _evaluate_ebv_reliability(parsed, genetic_variance),
    }


def _evaluate_ebv_reliability(parsed, genetic_variance):
    if genetic_variance is None or genetic_variance <= 0:
        return {"available": False, "reason": "additive genetic variance is unavailable or non-positive"}
    records, source = _reliability_records(parsed)
    # 【2026-10-20】展示用来源名去目录与运行时间戳（详见 _display_source 说明）
    source = _display_source(source)
    if not records:
        return {"available": False, "source": source,
                "reason": "no individual PEV output was found; reliability and accuracy were not calculated"}
    import pandas as pd
    frame = pd.DataFrame(records)
    if frame.empty:
        return {"available": False, "source": source, "reason": "PEV output is empty"}
    id_column = "ID" if "ID" in frame.columns else frame.columns[0]
    pev_column = _find_pev_column(frame.columns)
    r2_column = _find_r2_column(frame.columns)
    if not pev_column and not r2_column:
        return {"available": False, "source": source,
                "reason": "reliability output has neither recognized PA_R2 nor PEV column",
                "columns": [str(item) for item in frame.columns]}
    frame["_pev"] = (pd.to_numeric(frame[pev_column], errors="coerce")
                     if pev_column else float("nan"))
    derived = 1.0 - frame["_pev"] / genetic_variance
    if r2_column:
        direct = pd.to_numeric(frame[r2_column], errors="coerce")
        frame["_reliability"] = direct.where(direct.notna(), derived)
        frame["_reliability_source"] = direct.map(
            lambda value: "HIBLUP_PA_R2" if pd.notna(value)
            else "derived_1_minus_PEV_over_VA")
        comparable = frame["_reliability"].notna() & derived.notna()
        comparable &= direct.notna()
        disagreement = (direct.loc[comparable] - derived.loc[comparable]).abs()
        if direct.notna().all():
            canonical_source = "HIBLUP_PA_R2"
        elif direct.notna().any():
            canonical_source = "mixed_PA_R2_and_derived_PEV_VA"
        else:
            canonical_source = "derived_1_minus_PEV_over_VA"
    else:
        frame["_reliability"] = derived
        frame["_reliability_source"] = "derived_1_minus_PEV_over_VA"
        disagreement = pd.Series(dtype="float64")
        canonical_source = "derived_1_minus_PEV_over_VA"
    valid = frame.loc[frame["_reliability"].notna()].copy()
    usable = valid.loc[valid["_reliability"].between(0.0, 1.0, inclusive="both")].copy()
    payload = {
        "available": bool(len(usable)), "source": source,
        "id_column": str(id_column), "pev_column": str(pev_column) if pev_column else None,
        "r2_column": str(r2_column) if r2_column else None,
        "canonical_source": canonical_source,
        "r2_vs_pev_va_max_abs_difference": (_round_or_none(disagreement.max())
                                              if len(disagreement) else None),
        "genetic_variance": _round_or_none(genetic_variance),
        "records_total": int(len(frame)),
        "records_with_pev": int(frame["_pev"].notna().sum()),
        "records_with_reliability": int(frame["_reliability"].notna().sum()),
        "records_valid": int(len(usable)), "records_out_of_range": int(len(valid) - len(usable)),
    }
    if usable.empty:
        payload["reason"] = "all reliability values were missing or outside [0, 1]"
        return payload
    usable["_accuracy"] = usable["_reliability"].map(math.sqrt)
    payload["reliability_summary"] = _distribution_summary(usable["_reliability"])
    payload["accuracy_summary"] = _distribution_summary(usable["_accuracy"])
    payload["individuals"] = [
        {"ID": str(row[id_column]), "PEV": _round_or_none(row["_pev"]),
         "reliability": _round_or_none(row["_reliability"]),
         "reliability_source": row["_reliability_source"],
         "accuracy": _round_or_none(row["_accuracy"])}
        for _, row in usable.iterrows()]
    return payload


# 运行时间戳形态：_<8位日期>_<6位时间>，见于引擎产物名 EN_20261020_223151.rand
_RUN_STAMP = re.compile(r"_\d{8}_\d{6}")


def _display_source(source):
    """
    结果展示用的「来源名」：去掉目录与运行时间戳。

    【2026-10-20】必要性（先生要求「输出固定」+「展示中排除 _runs」）：
    真实来源为绝对路径，形如
        …\\workspace\\SAMPLE_POPULATION_EN_PBLUP_GenBatch\\_runs\\run_001_20261020_223148_…\\output\\EN_20261020_223151.rand
    该串既含内部 `_runs` 目录，又含每次变化的 run 编号与运行时间戳，
    直接写进报告会导致「每次运行的报告内容都不一样」，也让 _runs 出现在展示中。
    故统一规约为稳定的产物名（如 EN.rand），使每次运行展示一致。
    """
    text = str(source or "").strip()
    if not text:
        return text
    if "\\" in text or "/" in text:
        text = text.replace("\\", "/").rsplit("/", 1)[-1]
    return _RUN_STAMP.sub("", text)


def _reliability_records(parsed):
    if parsed.get("reliability_records"):
        return parsed["reliability_records"], parsed.get("reliability_path") or "optional_reliability_output"
    return parsed.get("rand") or [], "rand"


def _find_pev_column(columns):
    for column in columns:
        normalized = "".join(char for char in str(column).lower() if char.isalnum() or char == "_")
        if normalized in _PEV_COLUMN_TOKENS or normalized.startswith("pev") or normalized.endswith("_pev"):
            return column
    return None


def _find_r2_column(columns):
    for column in columns:
        normalized = "".join(char for char in str(column).lower()
                             if char.isalnum() or char == "_")
        if normalized in {"pa_r2", "r2", "reliability", "pa_reliability"}:
            return column
    return None


def _as_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _round_or_none(value):
    return round(float(value), 6) if value is not None else None


def _distribution_summary(values):
    return {"mean": _round_or_none(values.mean()), "min": _round_or_none(values.min()),
            "max": _round_or_none(values.max()), "median": _round_or_none(values.median())}
