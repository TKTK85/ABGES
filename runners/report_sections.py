# -*- coding: utf-8 -*-
"""报告章节文案与前置数据构造（P2-3 从 docx_report.py 抽出）。

职责：
  * 文案常量表（导读页缩写表、闸门中文名、数据整合指标标签）；
  * 表型数据探索块的构造（描述统计 / 分布检验 / 共线性）——
    这是第二章的前置数据，与 docx 渲染无关，故可独立单测。

本模块不含任何 docx 依赖；pandas / abges_explore / report_figures
均在函数内部按需导入，维持"软依赖、缺失即降级"的既有契约。
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


# 探索分组字段允许的最大水平数：超出者不参与分组统计与 one-hot，
# 防止孵化日期这类高基数列把设计矩阵撑爆（如 3000 个体 3000 个日期）。
EXPLORE_MAX_LEVELS = 50


# 主要符号与缩写（导读页，供跨学科专家快速查阅）
ABBREVIATIONS = [
    ("EBV", "估计育种值", "PEV", "预测误差方差"),
    ("BLUP", "最佳线性无偏预测", "AI-REML", "平均信息约束最大似然"),
    ("PBLUP", "系谱 BLUP", "GBLUP", "基因组 BLUP"),
    ("SSGBLUP", "单步基因组 BLUP", "SNP", "单核苷酸多态性标记"),
    ("h²", "遗传力", "VA/VE/VP", "加性遗传/残差/表型方差"),
    ("SE", "标准误", "CI", "置信区间"),
    ("ANOVA", "方差分析（F 检验）", "σA", "加性遗传标准差"),
    ("S", "选择差", "i", "标准化选择强度"),
    ("r", "育种值准确性", "ΔG", "每代预期遗传进展"),
    ("L", "世代间隔", "F", "近交系数"),
]

GATE_NAME = {
    "G1": "表型质量控制闸门", "G2": "系谱/数据质量控制闸门", "G3": "数据整合闸门",
    "G4": "引擎执行闸门", "G5": "结果校验闸门", "G6": "交付完整性闸门",
}

INTEG_LABELS = [
    ("phenotype_records", "表型记录数"),
    ("final_phenotype_records", "建模最终表型记录数"),
    ("phenotype_individuals", "表型个体数"),
    ("pedigree_individuals", "系谱个体数"),
    ("genotype_individuals", "基因型（已分型）个体数"),
    ("pheno_ped_overlap", "表型×系谱交集"),
    ("pheno_ped_coverage", "表型对系谱覆盖率"),
    ("pheno_geno_overlap", "表型×基因型交集"),
]


def build_exploration(output_prefix, plan, context, engine_pheno_path):
    """构建"表型数据探索"结果块并出图。

    读取引擎表型文件（已质控、已标准化列名的 Tab 分隔表），依次产出
    描述性统计、分布（正态性）检验、共线性诊断三块结果及配套图。

    返回 ``(block, figures)``；任何环节不可用则返回 ``(None, {})``，
    由调用方跳过该章节（软依赖，绝不阻断报告生成）。
    """
    import pandas as pd
    import abges_explore
    from runners import report_figures

    if not engine_pheno_path or not os.path.isfile(engine_pheno_path):
        return None, {}
    try:
        frame = pd.read_csv(engine_pheno_path, sep="\t", na_values=["NA", ""],
                            encoding="utf-8")
    except Exception:
        logger.exception("Exploration input could not be read; section omitted")
        return {"Enabled": False, "Warnings": ["EXPLORATION_READ_FAILED"], "Degraded": ["input_read"]}, {}
    if frame is None or frame.empty:
        return None, {}

    plan = plan or {}
    traits = [t for t in (plan.get("traits") or
                          ([plan.get("trait")] if plan.get("trait") else []))
              if t and t in frame.columns]
    if not traits:
        return None, {}

    engine_cols = ((context or {}).get("engine_phenotype") or {}).get("columns")
    if not engine_cols:
        engine_cols = list(frame.columns)
    group_columns = []
    for col in engine_cols:
        if col not in frame.columns or col == "ID" or col in traits:
            continue
        try:
            if int(frame[col].nunique(dropna=True)) <= EXPLORE_MAX_LEVELS:
                group_columns.append(col)
        except Exception:
            continue

    cfg = abges_explore.load_exploration_config()
    if not (cfg or {}).get("enabled", True):
        return {"Enabled": False, "Warnings": [], "Degraded": []}, {}

    block = abges_explore.build_exploration(frame, traits, cfg,
                                            group_columns=group_columns or None)
    # 趋势图优先用世代字段，其次批次，再次第一个分组字段
    trend_col = None
    for name in ("Gen", "Generation", "Batch"):
        if name in group_columns:
            trend_col = name
            break
    if trend_col is None and group_columns:
        trend_col = group_columns[0]

    figures = {}
    try:
        figures = report_figures.render_exploration_figures(
            frame, traits, block, output_prefix + "_figures",
            group_column=trend_col)
    except Exception:
        logger.exception("Exploration figures could not be generated")
        block.setdefault("Warnings", []).append("EXPLORATION_FIGURES_FAILED")
        block.setdefault("Degraded", []).append("figures")
        figures = {}
    block["Figures"] = figures
    block["_GroupColumns"] = group_columns
    return block, figures
