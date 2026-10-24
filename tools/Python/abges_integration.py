# -*- coding: utf-8 -*-
"""
ABGES 多源数据整合模块
======================
职责：把表型、系谱、基因型三源数据按个体号对齐，产出对接诊断指标，
供 G3 数据对接闸门判定。

核心风险：个体号体系不一致（前导零、大小写、空白、前缀）会导致交集为空，
本模块必须把不一致暴露出来并给出可操作的诊断，而不是返回一张空表。
"""

import abges_core as core
import abges_dataio as dataio


class IntegrationResult(object):
    """整合结果容器。"""

    def __init__(self):
        self.phenotype = None
        self.pedigree = None
        self.genotype_ids = []
        self.metrics = {}
        self.warnings = []
        self.diagnostics = {}
        self.change_log = []

    def to_dict(self):
        return {
            "metrics": self.metrics,
            "warnings": self.warnings,
            "diagnostics": self.diagnostics,
            "change_log": self.change_log,
        }


# ===============================================================
# 一、ID 集合比对
# ===============================================================

def compare_id_sets(left_ids, right_ids, left_name="left", right_name="right"):
    """
    比对两个 ID 集合，返回交集规模、覆盖率与不匹配样例。
    """
    left_set = set(str(value) for value in left_ids)
    right_set = set(str(value) for value in right_ids)
    overlap = left_set & right_set
    return {
        "%s_count" % left_name: len(left_set),
        "%s_count" % right_name: len(right_set),
        "overlap": len(overlap),
        "%s_coverage" % left_name: round(len(overlap) / len(left_set), 6) if left_set else 0.0,
        "%s_coverage" % right_name: round(len(overlap) / len(right_set), 6) if right_set else 0.0,
        "%s_only_sample" % left_name: sorted(left_set - right_set)[:10],
        "%s_only_sample" % right_name: sorted(right_set - left_set)[:10],
    }


def diagnose_id_mismatch(left_ids, right_ids):
    """
    诊断 ID 不匹配的常见原因，给出可操作建议。
    检查：前导零、大小写、空白、公共前缀差异、纯数字 vs 带前缀。
    """
    left_set = set(str(value).strip() for value in left_ids)
    right_set = set(str(value).strip() for value in right_ids)
    if left_set & right_set:
        return {"issue": None, "hint": "两侧存在直接交集"}

    findings = []
    # 前导零
    if {value.lstrip("0") for value in left_set} & {value.lstrip("0") for value in right_set}:
        findings.append("去掉前导零后可匹配，建议统一 ID 前导零处理")
    # 大小写
    if {value.lower() for value in left_set} & {value.lower() for value in right_set}:
        findings.append("忽略大小写后可匹配，建议统一大小写")
    # 前缀
    left_suffix = {value.split("_")[-1] for value in left_set}
    right_suffix = {value.split("_")[-1] for value in right_set}
    if left_suffix & right_suffix:
        findings.append("去掉下划线前缀后可匹配，两侧使用了不同的编号前缀")
    # 长度差异
    left_lengths = sorted({len(value) for value in left_set})[:5]
    right_lengths = sorted({len(value) for value in right_set})[:5]

    return {
        "issue": "无直接交集",
        "findings": findings or ["未识别出常见可修复模式，需人工核对编号体系"],
        "left_sample": sorted(left_set)[:10],
        "right_sample": sorted(right_set)[:10],
        "left_id_lengths": left_lengths,
        "right_id_lengths": right_lengths,
    }


# ===============================================================
# 二、三源整合
# ===============================================================

def integrate_sources(pheno_frame, ped_frame=None, genotype_ids=None,
                      id_column="ID", model="pblup",
                      restrict_phenotype_to_pedigree=False, thresholds=None):
    """
    对齐三源数据，产出 G3 闸门所需指标。

    参数：
      pheno_frame                 QC 后的表型
      ped_frame                   QC 后的系谱（三列），可为 None
      genotype_ids                基因型个体号列表，可为 None
      restrict_phenotype_to_pedigree
                                  True 时把表型限制到系谱内个体
                                  （默认 False：HIBLUP 会自行忽略不在系谱的记录，
                                    保留原表更利于统计对照）

    返回 IntegrationResult。
    """
    result = IntegrationResult()
    thresholds = thresholds or {}
    min_ped_coverage = float(thresholds.get("phenotype_pedigree_min_coverage", 0.80))
    working = pheno_frame.copy()
    working[id_column] = dataio.normalize_identifier_series(working[id_column])
    pheno_ids = set(working[id_column])

    result.metrics["phenotype_records"] = int(working.shape[0])
    result.metrics["phenotype_individuals"] = len(pheno_ids)

    # ---------- 表型 × 系谱 ----------
    if ped_frame is not None and ped_frame.shape[0] > 0:
        ped = ped_frame.copy()
        ped["ID"] = dataio.normalize_identifier_series(ped["ID"])
        ped_ids = set(ped["ID"])
        comparison = compare_id_sets(pheno_ids, ped_ids, "phenotype", "pedigree")
        result.metrics["pedigree_individuals"] = len(ped_ids)
        result.metrics["pheno_ped_overlap"] = comparison["overlap"]
        result.metrics["pheno_ped_coverage"] = comparison["phenotype_coverage"]
        result.diagnostics["pheno_vs_pedigree"] = comparison
        if comparison["overlap"] == 0:
            result.diagnostics["pheno_ped_mismatch"] = diagnose_id_mismatch(
                pheno_ids, ped_ids)
            raise core.AbgesError(
                "G3-01", "表型与系谱个体号交集为 0，无法对接",
                result.diagnostics["pheno_ped_mismatch"])
        if comparison["phenotype_coverage"] < min_ped_coverage:
            result.warnings.append({
                "check_id": "G3-02",
                "message": "仅 %.2f%% 的表型个体出现在系谱中，未覆盖个体不会获得可靠 EBV"
                           % (comparison["phenotype_coverage"] * 100),
            })
        if restrict_phenotype_to_pedigree:
            before_rows = working.shape[0]
            working = working[working[id_column].isin(ped_ids)].copy()
            result.change_log.append({
                "rule_id": "G3-02",
                "action": "RESTRICT",
                "affected": before_rows - working.shape[0],
                "reason": "表型限制到系谱内个体",
            })
        result.pedigree = ped
    else:
        result.metrics["pedigree_individuals"] = 0
        if model in ("pblup", "ssgblup", "ssblup"):
            raise core.AbgesError("G1-05", "模型 %s 需要系谱，但未提供" % model)

    # ---------- 表型 × 基因型 ----------
    if genotype_ids:
        geno_ids = set(str(value).strip() for value in genotype_ids)
        comparison = compare_id_sets(pheno_ids, geno_ids, "phenotype", "genotype")
        result.metrics["genotype_individuals"] = len(geno_ids)
        result.metrics["pheno_geno_overlap"] = comparison["overlap"]
        result.diagnostics["pheno_vs_genotype"] = comparison
        if comparison["overlap"] == 0:
            result.diagnostics["pheno_geno_mismatch"] = diagnose_id_mismatch(
                pheno_ids, geno_ids)
            raise core.AbgesError(
                "G3-03", "表型与基因型个体号交集为 0，无法对接",
                result.diagnostics["pheno_geno_mismatch"])
        result.genotype_ids = sorted(geno_ids)

        # ---------- SSGBLUP：基因型个体必须在系谱内 ----------
        if model in ("ssgblup", "ssblup") and result.pedigree is not None:
            ped_ids = set(result.pedigree["ID"])
            missing_in_pedigree = sorted(geno_ids - ped_ids)
            if missing_in_pedigree:
                result.diagnostics["genotype_ids_missing_from_pedigree"] = missing_in_pedigree
                result.warnings.append({
                    "check_id": "G3-04",
                    "message": "%d 个基因型个体不在系谱中；不会自动补造基础群，须补齐经核实的系谱后重跑"
                               % len(missing_in_pedigree),
                    "sample": missing_in_pedigree[:20],
                })
    else:
        result.metrics["genotype_individuals"] = 0
        if model in ("gblup", "ssgblup", "ssblup"):
            raise core.AbgesError("G1-05", "模型 %s 需要基因型，但未提供" % model)

    result.phenotype = working
    result.metrics["final_phenotype_records"] = int(working.shape[0])
    return result


# ===============================================================
# 三、G3 闸门指标导出
# ===============================================================

def build_gate_g3_facts(integration_result, model="pblup", thresholds=None):
    """
    把整合指标整理成 G3 闸门可直接判定的事实字典。

    每项必须给出明确结论：
      passed=True/False   已判定
      applicable=False    本次流程不涉及该项（如无基因型时的 G3-03）
    缺少结论的事实会被 gatekeeper 判为 block，因此这里逐项落实，
    不留"未提供判定依据"的空档。
    """
    metrics = integration_result.metrics
    method = str(model).lower()
    has_pedigree = int(metrics.get("pedigree_individuals", 0) or 0) > 0
    has_genotype = int(metrics.get("genotype_individuals", 0) or 0) > 0

    facts = {}
    thresholds = thresholds or {}
    min_ped_overlap = int(thresholds.get("phenotype_pedigree_min_overlap", 100))
    min_ped_coverage = float(thresholds.get("phenotype_pedigree_min_coverage", 0.80))
    min_geno_overlap = int(thresholds.get("phenotype_genotype_min_overlap", 50))

    # ---------- G3-01 表型 × 系谱 交集 ≥ 100 ----------
    if has_pedigree:
        overlap = int(metrics.get("pheno_ped_overlap", 0) or 0)
        facts["G3-01"] = {
            "passed": overlap >= min_ped_overlap,
            "actual": overlap,
            "threshold": min_ped_overlap,
            "message": None if overlap >= min_ped_overlap
            else "表型与系谱交集仅 %d 个个体，不足以支撑方差组分估计" % overlap,
        }
    else:
        facts["G3-01"] = {"applicable": False, "reason": "本次未提供系谱"}

    # ---------- G3-02 交集覆盖率 ≥ 0.80 ----------
    if has_pedigree:
        coverage = float(metrics.get("pheno_ped_coverage", 0.0) or 0.0)
        facts["G3-02"] = {
            "passed": coverage >= min_ped_coverage,
            "actual": round(coverage, 6),
            "threshold": min_ped_coverage,
            "message": None if coverage >= min_ped_coverage
            else "仅 %.2f%% 表型个体在系谱中，未覆盖个体不会获得可靠 EBV"
                 % (coverage * 100),
        }
    else:
        facts["G3-02"] = {"applicable": False, "reason": "本次未提供系谱"}

    # ---------- G3-03 表型 × 基因型 交集 ≥ 50 ----------
    if has_genotype:
        overlap = int(metrics.get("pheno_geno_overlap", 0) or 0)
        facts["G3-03"] = {
            "passed": overlap >= min_geno_overlap,
            "actual": overlap,
            "threshold": min_geno_overlap,
            "message": None if overlap >= min_geno_overlap
            else "表型与基因型交集仅 %d 个个体，基因组信息不足" % overlap,
        }
    else:
        facts["G3-03"] = {"applicable": False, "reason": "本次未提供基因型"}

    # ---------- G3-04 SSGBLUP 场景基因型个体全在系谱内 ----------
    if method in ("ssblup", "ssgblup") and has_genotype and has_pedigree:
        missing_ids = integration_result.diagnostics.get(
            "genotype_ids_missing_from_pedigree", [])
        facts["G3-04"] = {
            "passed": not missing_ids,
            "actual": len(missing_ids),
            "threshold": 0,
            "message": None if not missing_ids else (
                "%d 个基因型个体不在系谱中；不会自动补造基础群，须补齐经核实的系谱后重跑"
                % len(missing_ids)),
        }
    else:
        facts["G3-04"] = {"applicable": False,
                          "reason": "非 SSGBLUP 场景，不要求系谱覆盖全部基因型个体"}

    return facts
