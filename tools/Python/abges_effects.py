# -*- coding: utf-8 -*-
"""
ABGES 数据驱动的效应筛选模块
=============================
职责：在正式建模（阶段 06）之前，依据**当前这批数据本身的统计证据**，
对每个候选固定效应／协变量给出"是否建议入模"的量化结论与可复核理由。

解决的问题：入模效应此前只来自配置默认、任务显式指定、PH005 可估性三处——
除"能不能算"外从未判断"该不该入模"，换一批数据、换一个品系仍是同一套模板。

六条判据（阈值全部读自 phenotype-qc.yaml 的 effect_screening 段）：

  1. 可估性        有效水平数 < min_levels             → drop（设计矩阵不可估）
  2. 水平样本量    存在水平 N < min_level_n            → review（该水平估计不稳）
  3. 分布均衡性    最小水平占比 / 失衡比超阈值          → review
  4. 解释力        单因素 ANOVA 的 η²                 → η²<eta_squared_min → drop（唯一门槛；P 仅透明展示、不参与判定）【核心】
  5. 共线性        复用 abges_explore 的 VIF / 一一映射 → choose_one（二选一）
  6. 相关性        协变量与性状 |r| < covariate_corr_min → drop

设计原则（与项目铁律一致）：
  - 阈值一律外置：全部来自 effect_screening 配置段，本模块不硬编码。
  - 软依赖降级：scipy 缺失时 P 值不可用，仅按 η² 判定，记入 degraded，
    **绝不阻断评估主流程**。
  - **本模块只做"数据驱动的诊断 + 提示"，不修改任何入模清单**（先生拍板 2026-10-17）：
    逐列给出 verdict 与理由作为给用户的提示；"drop" 仅表示解释力低、入模型价值不大，
    是否剔除由用户按任务要求决定。建模规格始终来自用户任务要求，本模块不代劳。
  - 输入必须是 PH005 之后的 effective_fixed_effects，避免对 PH005 已剔除的
    列重复建议（否则告警噪音翻倍）。

与事后检验的分工：本模块是**事前筛选**（建模前定规格）；
HIBLUP 产出的事后 .anova（hiblup_tool 解析、breeding_insights.anova_table 呈现）
用于验证建议是否被印证，二者互补而非替代。

方案文档：docs/ABGES_数据驱动效应筛选方案_v1.0.md
"""

import abges_core as core


PHENOTYPE_QC_CONFIG = "configs/qc/phenotype-qc.yaml"

# verdict 优先级：多性状汇总时取"最该保留"的那个（保守）
_VERDICT_ORDER = {"drop": 0, "review": 1, "choose_one": 2, "keep": 3}

# 配置缺失时的兜底默认（正常情况下阈值来自 yaml）
_DEFAULTS = {
    "enabled": True,
    "min_levels": 2,
    "min_level_n": 5,
    "min_level_share": 0.01,
    "imbalance_ratio": 50,
    "eta_squared_min": 0.01,
    "eta_squared_strong": 0.05,
    "covariate_corr_min": 0.05,
    "reuse_collinearity": True,
}


def _config_section(config, name=None):
    """取配置子段，逐键用默认值兜底。"""
    merged = dict(_DEFAULTS)
    merged.update(config or {})
    return merged


def load_effect_screening_config(config_path=PHENOTYPE_QC_CONFIG):
    """从 phenotype-qc.yaml 读取 effect_screening 段。"""
    try:
        cfg = core.load_yaml_config(config_path)
    except Exception:
        return dict(_DEFAULTS)
    return _config_section(cfg.get("effect_screening", {}) or {})


def _scipy():
    """scipy 软依赖（复用 abges_explore 的实现，避免重复兜底逻辑）。"""
    try:
        import abges_explore
        return abges_explore.scipy_stats()
    except Exception:
        try:
            from scipy import stats
            return stats
        except Exception:
            return None


# ===============================================================
# 一、证据采集
# ===============================================================

def _level_profile(frame, column):
    """分组列的水平画像：水平数、每水平样本量、最小占比、失衡比。"""
    import pandas as pd

    out = {"levels": 0, "level_counts": {}, "min_level_n": None,
           "min_level_share": None, "imbalance_ratio": None}
    if column not in frame.columns:
        return out
    try:
        values = frame[column].dropna().astype(str).str.strip()
        values = values[values != ""]
        if values.empty:
            return out
        counts = values.value_counts()
        total = int(counts.sum())
        min_n = int(counts.min())
        max_n = int(counts.max())
        out["levels"] = int(counts.shape[0])
        out["level_counts"] = {str(k): int(v) for k, v in counts.items()}
        out["min_level_n"] = min_n
        out["min_level_share"] = round(min_n / float(total), 6) if total else None
        out["imbalance_ratio"] = (round(max_n / float(min_n), 4)
                                  if min_n > 0 else None)
    except Exception:
        pass
    return out


def _eta_squared(frame, trait, column, stats_mod=None):
    """单因素方差分析：该分组列单独解释了目标性状多少变异。

    返回 (eta_squared, F, df_between, df_within, P)；不可算时对应项返回 None。
      eta_squared = SS组间 / SStotal
      F           = (SS组间/df组间) / (SS组内/df组内)
      P           = F 分布上尾概率（需 scipy；缺失返回 None 并记 degraded）

    方法学说明：η² 是**未校正其他效应**的单因素解释力，多效应相关时会偏高或
    偏低，故本模块在有共线时优先给 choose_one 而非 drop，报告中亦须明示该口径。
    """
    import pandas as pd

    if trait not in frame.columns or column not in frame.columns:
        return None, None, None, None, None
    values = pd.to_numeric(frame[trait], errors="coerce")
    groups = frame[column].astype(str).str.strip()
    mask = values.notna() & groups.notna() & (groups != "")
    values = values[mask]
    groups = groups[mask]
    if values.empty:
        return None, None, None, None, None

    grand = float(values.mean())
    ss_total = float(((values - grand) ** 2).sum())
    if ss_total <= 0:
        return 0.0, None, 0, 0, None

    ss_between = 0.0
    level_count = 0
    for _, sub in values.groupby(groups):
        if sub.empty:
            continue
        level_count += 1
        ss_between += len(sub) * (float(sub.mean()) - grand) ** 2
    ss_within = ss_total - ss_between

    df_between = level_count - 1
    df_within = int(values.size) - level_count
    eta2 = round(ss_between / ss_total, 6)

    if df_between <= 0 or df_within <= 0:
        return eta2, None, df_between, df_within, None
    if ss_within <= 0:
        return eta2, None, df_between, df_within, None

    f_stat = round((ss_between / df_between) / (ss_within / df_within), 6)
    p_value = None
    if stats_mod is not None:
        try:
            # 直接取 F 分布上尾概率，避免依赖 f_oneway 的版本行为
            p_value = float(stats_mod.f.sf(f_stat, df_between, df_within))
        except Exception:
            p_value = None
    return eta2, f_stat, df_between, df_within, p_value


def _covariate_corr(frame, trait, column):
    """连续协变量与目标性状的 Pearson 相关系数；不可算返回 None。"""
    import numpy as np
    import pandas as pd

    if trait not in frame.columns or column not in frame.columns:
        return None
    try:
        x = pd.to_numeric(frame[column], errors="coerce")
        y = pd.to_numeric(frame[trait], errors="coerce")
        mask = x.notna() & y.notna()
        if int(mask.sum()) < 3:
            return None
        xa = x[mask].to_numpy(dtype="float64")
        ya = y[mask].to_numpy(dtype="float64")
        if float(xa.std(ddof=1)) == 0.0 or float(ya.std(ddof=1)) == 0.0:
            return None
        return round(float(np.corrcoef(xa, ya)[0, 1]), 6)
    except Exception:
        return None


def _token_in_text(column, text):
    """按"整词"判定列名是否出现在告警文本中，避免 Gen 误命中 Gen_1/Gen_2 等哑变量。"""
    import re
    try:
        pat = r"(?<![A-Za-z0-9_])" + re.escape(str(column)) + r"(?![A-Za-z0-9_])"
        return re.search(pat, str(text)) is not None
    except Exception:
        return str(column) in str(text)


def _collinearity_evidence(column, collinearity_block, config):
    """从共线性诊断结果中提取该列的证据（VIF 值 / 是否涉入严重告警）。"""
    evidence = {"vif": None, "collinearity": None}
    block = collinearity_block or {}
    vif_map = block.get("VIF") or {}
    # 列名可能是原列名，也可能是 one-hot 后的 "列名_水平"
    if column in vif_map:
        evidence["vif"] = vif_map[column]
    else:
        related = {k: v for k, v in vif_map.items()
                   if str(k).startswith(str(column) + "_")}
        if related:
            evidence["vif"] = max(related.values())

    severe = float(config.get("vif_severe", 10))
    for warning in (block.get("Warnings") or []):
        detail = str(warning.get("detail") or "")
        kind = str(warning.get("kind") or "")
        level = str(warning.get("level") or "")
        if level != "severe":
            continue
        # 整词匹配：避免 Gen 误命中 Gen_1/Gen_2 的共线告警而误判 choose_one
        if (kind in ("CategoryDependency", "VIF", "Corr")
                and _token_in_text(column, detail)):
            evidence["collinearity"] = detail
            break
    return evidence


# ===============================================================
# 二、单列判定
# ===============================================================

def screen_candidate(frame, trait, column, role="fixed", config=None,
                     collinearity_block=None, stats_mod=None):
    """对单个候选列做数据驱动判定。

    返回 dict：
      column / role / trait / verdict / strong / reasons / evidence
    verdict ∈ {keep, drop, choose_one, review}
    """
    cfg = _config_section(config)
    result = {"column": str(column), "role": role,
              "trait": ("" if trait is None else str(trait)),
              "verdict": "review", "strong": False,
              "reasons": [], "evidence": {}}

    if frame is None or column not in getattr(frame, "columns", []):
        result["verdict"] = "review"
        result["reasons"].append("该候选列在当前数据中不存在，无法评估")
        return result

    min_levels = int(cfg.get("min_levels", 2))
    min_level_n = int(cfg.get("min_level_n", 5))
    min_share = float(cfg.get("min_level_share", 0.01))
    imb_ratio = float(cfg.get("imbalance_ratio", 50))
    eta_min = float(cfg.get("eta_squared_min", 0.01))
    eta_strong = float(cfg.get("eta_squared_strong", 0.05))
    corr_min = float(cfg.get("covariate_corr_min", 0.05))

    profile = _level_profile(frame, column)
    result["evidence"].update(profile)

    # ---- 判据 5：共线性（先判，优先级高于解释力）----
    coll_evidence = {}
    if cfg.get("reuse_collinearity", True):
        coll_evidence = _collinearity_evidence(column, collinearity_block, cfg)
        result["evidence"].update(coll_evidence)
        if coll_evidence.get("collinearity"):
            result["verdict"] = "choose_one"
            result["reasons"].append("共线性：%s；两列不可同时入模，须二选一"
                                     % coll_evidence["collinearity"])
            return result

    # ---- 判据 1：可估性 ----
    if profile["levels"] < min_levels:
        result["verdict"] = "drop"
        result["reasons"].append(
            "有效水平数 %d < %d，设计矩阵中不可估（与 PH005 同判据）"
            % (profile["levels"], min_levels))
        return result

    # ---- 协变量走相关性判据；固定效应走 ANOVA ----
    if role == "covariate":
        r_value = _covariate_corr(frame, trait, column)
        result["evidence"]["corr"] = r_value
        if r_value is None:
            result["verdict"] = "review"
            result["reasons"].append("与性状的相关系数不可算（非常量或样本不足），"
                                     "建议人工确认")
            return result
        if abs(r_value) < corr_min:
            result["verdict"] = "drop"
            result["reasons"].append(
                "与 %s 的 |r|=%.4f < %.2f，对性状几乎无解释力，入模只消耗自由度"
                % (trait, abs(r_value), corr_min))
            return result
        result["verdict"] = "keep"
        result["strong"] = abs(r_value) >= 0.3
        result["reasons"].append("与 %s 的 |r|=%.4f，达到入模相关性阈值 %.2f"
                                 % (trait, abs(r_value), corr_min))
        return result

    # ---- 判据 2 / 3：水平样本量与分布均衡性（须先于解释力判定）----
    # 理由：水平样本量过少或分布严重失衡时，η² 与 P 本身已不可靠
    #（稀有水平会把组间方差带偏，既可能假显著也可能假不显著）。
    # 此时应提示"数据分布有问题，需人工确认"，而不是据一个失真的 η² 直接建议剔除。
    review_reasons = []
    if profile["min_level_n"] is not None and profile["min_level_n"] < min_level_n:
        review_reasons.append("存在水平样本量 %d < %d，该水平效应估计不稳"
                              % (profile["min_level_n"], min_level_n))
    if (profile["min_level_share"] is not None
            and profile["min_level_share"] < min_share):
        review_reasons.append("最小水平占比 %.4f < %.2f，分布失衡"
                              % (profile["min_level_share"], min_share))
    if (profile["imbalance_ratio"] is not None
            and profile["imbalance_ratio"] > imb_ratio):
        review_reasons.append("最大/最小水平样本量之比 %.1f > %.0f，分布失衡"
                              % (profile["imbalance_ratio"], imb_ratio))
    if review_reasons:
        eta2_preview, _, _, _, p_preview = _eta_squared(frame, trait, column,
                                                        stats_mod)
        result["evidence"].update({"eta_squared": eta2_preview, "p": p_preview})
        result["verdict"] = "review"
        result["reasons"].extend(review_reasons)
        result["reasons"].append(
            "此时 η²%s 已不可靠，须人工确认后再定取舍（不自动建议剔除）"
            % ("=%.4f" % eta2_preview if eta2_preview is not None else "不可算"))
        return result

    # ---- 判据 4：解释力（单因素 ANOVA）----
    eta2, f_stat, df_b, df_w, p_value = _eta_squared(frame, trait, column,
                                                     stats_mod)
    result["evidence"].update({"eta_squared": eta2, "f": f_stat,
                               "df_between": df_b, "df_within": df_w,
                               "p": p_value})
    if eta2 is None:
        result["verdict"] = "review"
        result["reasons"].append("该列对 %s 的组间方差不可算（性状缺失或组内无变异）"
                                 % trait)
        return result

    if eta2 > eta_strong:
        result["strong"] = True

    # 无解释力判定（数据驱动的分水岭 = η²，而非 P 值）：
    # 单因素 ANOVA 解释力低于入模阈值即建议剔除。大样本（N 很大）下 P 值几乎
    # 必然显著，不能用 P 挽救一个实际几乎无解释力的效应——否则本应剔除的噪声列
    # 会被"显著"留下，违背"该不该入模"的筛选初衷。P 值仅作透明展示，不再作为
    # 保留噪声列的救命稻草。
    # 注：含稀有水平的列已在上一层判 review（不自动剔除），不会在此被误杀。
    if eta2 < eta_min:
        if p_value is not None:
            basis = ("η²=%.4f < %.2f（P=%.3g；大样本下 P 显著不代表有实际解释力）"
                     % (eta2, eta_min, p_value))
        else:
            basis = "η²=%.4f < %.2f（scipy 缺失，P 不可用）" % (eta2, eta_min)
        result["verdict"] = "drop"
        result["reasons"].append(
            "解释力不足：%s，对 %s 的变异几乎无贡献，建议不入模" % (basis, trait))
        return result

    if result["strong"]:
        result["reasons"].append(
            "η²=%.4f 高于强效应阈值 %.2f，对 %s 变异解释力显著，为核心效应"
            % (eta2, eta_strong, trait))
    else:
        result["reasons"].append(
            "η²=%.4f 达到入模阈值 %.2f，建议保留" % (eta2, eta_min))
    result["verdict"] = "keep"
    return result


# ===============================================================
# 三、汇总入口
# ===============================================================

def build_effect_screening(frame, traits, fixed_columns=None,
                           covariate_columns=None, exploration=None,
                           config=None, mode="single"):
    """对全部候选效应/协变量做数据驱动筛选，产出建议清单。

    参数：
      frame             质控后的表型（列名已标准化）
      traits            目标性状（str 或 list）
      fixed_columns     PH005 之后的 effective_fixed_effects
      covariate_columns effective_covariates
      exploration       已算好的探索结果块（含 Collinearity），可复用避免重算
      config            effect_screening 配置段（None 时自动从 phenotype-qc.yaml 加载 effect_screening 段）
      mode              single | multi；multi 时逐性状计算并保守汇总

    返回 dict：
      Enabled / Mode / Candidates / Recommendations /
      SuggestedFixedEffects / SuggestedCovariates / Warnings / Degraded
    """
    # 修复 F-A：config=None 时按 docstring 承诺真正加载 phenotype-qc.yaml 的
    # effect_screening 段，消除"配置即文档、代码不读"的静默旁路陷阱。
    if config is None:
        config = load_effect_screening_config()
    cfg = _config_section(config)
    empty = {"Enabled": False, "Mode": mode, "Candidates": [],
             "Recommendations": {"keep": [], "drop": [], "choose_one": [],
                                 "review": []},
             "SuggestedFixedEffects": [], "SuggestedCovariates": [],
             "Warnings": [],
             "Degraded": []}
    if not cfg.get("enabled", True):
        empty["Degraded"] = ["效应筛选环节已关闭（effect_screening.enabled=false）"]
        return empty

    if frame is None or getattr(frame, "shape", (0, 0))[0] == 0:
        empty["Enabled"] = True
        empty["Degraded"] = ["表型数据为空，跳过效应筛选"]
        return empty

    traits_list = [traits] if isinstance(traits, str) else list(traits or [])
    if not traits_list:
        empty["Enabled"] = True
        empty["Degraded"] = ["未指定目标性状，跳过效应筛选"]
        return empty

    stats_mod = _scipy()
    degraded = []
    if stats_mod is None:
        degraded.append("scipy 缺失：P 值不可用，仅按 η² 判定（不阻断主流程）")

    fixed_columns = list(fixed_columns or [])
    covariate_columns = list(covariate_columns or [])

    # ---- 共线性：优先复用探索结果，否则现算一次 ----
    # 注意：仅对"预测变量之间"做共线性诊断，必须排除目标性状（响应变量）。
    # 把性状并入设计矩阵会制造"固定效应 ↔ 性状"的高相关（本就是该效应的解释力，
    # 由 η² 单独刻画），从而误判 choose_one，故此处只取固定效应+协变量。
    coll_block = None
    if cfg.get("reuse_collinearity", True):
        coll_block = (exploration or {}).get("Collinearity")
        if coll_block is None:
            try:
                import abges_explore
                candidates = [c for c in (fixed_columns + covariate_columns)
                              if c in frame.columns]
                coll_block = abges_explore.collinearity(
                    frame, list(dict.fromkeys(candidates)),
                    abges_explore.load_exploration_config())
            except Exception as exc:
                coll_block = None
                degraded.append("共线性诊断不可用，跳过判据 5：%s" % exc)

    candidates = []
    for column, role in ([(c, "fixed") for c in fixed_columns]
                         + [(c, "covariate") for c in covariate_columns]):
        per_trait = {}
        for trait_name in traits_list:
            item = screen_candidate(frame, trait_name, column, role=role,
                                    config=cfg, collinearity_block=coll_block,
                                    stats_mod=stats_mod)
            per_trait[str(trait_name)] = item

        # 多性状保守汇总：取"最该保留"的结论（keep > choose_one > review > drop）
        best = max(per_trait.values(),
                   key=lambda x: _VERDICT_ORDER.get(x.get("verdict"), 1))
        merged = dict(best)
        merged["trait"] = "" if len(traits_list) == 1 else "多性状汇总"
        merged["per_trait"] = per_trait
        if len(traits_list) > 1:
            spread = sorted({v["verdict"] for v in per_trait.values()},
                            key=lambda x: _VERDICT_ORDER.get(x, 1))
            if len(spread) > 1:
                merged["reasons"] = list(merged["reasons"]) + [
                    "各性状结论不一致（%s），按最保守口径取「%s」"
                    % (" / ".join(spread), merged["verdict"])]
        candidates.append(merged)

    recommendations = {"keep": [], "drop": [], "choose_one": [], "review": []}
    for item in candidates:
        recommendations.setdefault(item["verdict"], []).append(item["column"])

    keep_set = set(recommendations["keep"])
    review_set = set(recommendations["review"])
    choose_set = set(recommendations["choose_one"])
    drop_set = set(recommendations["drop"])
    suggested_fixed = [c for c in fixed_columns
                       if c in (keep_set | review_set | choose_set)]
    suggested_cov = [c for c in covariate_columns
                     if c in (keep_set | review_set | choose_set)]

    warnings = []
    for item in candidates:
        if item["verdict"] in ("drop", "choose_one", "review"):
            warnings.append({"level": ("severe" if item["verdict"] == "choose_one"
                                       else "warn"),
                             "kind": "EffectScreening",
                             "detail": "%s（%s）：%s" % (
                                 item["column"], item["verdict"],
                                 "；".join(item["reasons"])),
                             "suggestion": item.get("advice") or _advice(item)})

    return {
        "Enabled": True,
        "Mode": mode,
        "Candidates": candidates,
        "Recommendations": recommendations,
        "SuggestedFixedEffects": suggested_fixed,
        "SuggestedCovariates": suggested_cov,
        "Warnings": warnings,
        "Degraded": degraded,
    }


def diagnose_model_design(frame, trait, fixed_columns=None, covariate_columns=None,
                          sparse_min_n=5, rank_max_columns=500):
    """Diagnose fitted-model sparsity, Gen×Batch connectedness and estimability.

    Only columns passed by the model planner are inspected. The diagnostic never
    changes the requested model. Rank is evaluated on the actual complete-case
    fixed-effect design matrix; oversized matrices are reported as unevaluated.
    """
    import pandas as pd
    result = {"available": False, "sparse_groups": [], "gen_batch": None,
              "design_rank": None, "design_columns": None, "rank_deficient": None,
              "rank_evaluated": False, "sparse_group_minimum_n": int(sparse_min_n),
              "rank_diagnostic_max_columns": int(rank_max_columns)}
    if frame is None or trait not in getattr(frame, "columns", []):
        result["reason"] = "trait/frame unavailable"
        return result
    fixed = [c for c in (fixed_columns or []) if c in frame.columns]
    covars = [c for c in (covariate_columns or []) if c in frame.columns]
    selected = list(dict.fromkeys([trait] + fixed + covars))
    work = frame[selected].copy().dropna(subset=[trait])
    if work.empty:
        result["reason"] = "no valid trait records"
        return result
    result["available"] = True
    for column in fixed:
        counts = work[column].astype(str).value_counts(dropna=False)
        sparse = counts[counts < int(sparse_min_n)]
        if len(sparse):
            result["sparse_groups"].append({
                "factor": column,
                "threshold_n": int(sparse_min_n),
                "levels": [{"level": str(level), "n": int(count)}
                           for level, count in sparse.items()],
            })

    def token(name):
        low = str(name).strip().lower()
        if low in {"gen", "generation", "世代"} or "generation" in low:
            return "generation"
        if low in {"batch", "批次", "批号"} or "batch" in low:
            return "batch"
        return None

    gen_col = next((c for c in fixed if token(c) == "generation"), None)
    batch_col = next((c for c in fixed if token(c) == "batch"), None)
    if gen_col and batch_col:
        pairs = work[[gen_col, batch_col]].dropna().astype(str)
        adjacency = {}
        for gen, batch in pairs.itertuples(index=False, name=None):
            left, right = "G:" + gen, "B:" + batch
            adjacency.setdefault(left, set()).add(right)
            adjacency.setdefault(right, set()).add(left)
        components = 0
        unseen = set(adjacency)
        while unseen:
            components += 1
            stack = [unseen.pop()]
            while stack:
                node = stack.pop()
                for neighbor in adjacency[node] & unseen:
                    unseen.remove(neighbor)
                    stack.append(neighbor)
        result["gen_batch"] = {
            "applicable": True,
            "generation_column": gen_col, "batch_column": batch_col,
            "observed_cells": int(len(pairs.drop_duplicates())),
            "components": components,
            "connected": bool(adjacency) and components == 1,
        }
    else:
        result["gen_batch"] = {"applicable": False,
                                "reason": "模型未同时纳入可识别的世代与批次固定效应"}

    try:
        import numpy as np
        pieces = [np.ones((len(work), 1), dtype=float)]
        for column in fixed:
            pieces.append(pd.get_dummies(work[column].astype(str), drop_first=True,
                                         dtype=float).to_numpy())
        for column in covars:
            values = pd.to_numeric(work[column], errors="coerce")
            if values.isna().any():
                result["reason"] = "covariate has missing/non-numeric values after trait filter"
                return result
            pieces.append(values.to_numpy(dtype=float).reshape(-1, 1))
        columns = sum(piece.shape[1] for piece in pieces)
        result["design_columns"] = int(columns)
        if columns <= int(rank_max_columns):
            design = np.column_stack(pieces)
            rank = int(np.linalg.matrix_rank(design))
            result.update({"rank_evaluated": True, "design_rank": rank,
                           "rank_deficient": rank < columns,
                           "complete_case_records": int(len(work))})
        else:
            result["reason"] = "design exceeds configured rank diagnostic column limit"
    except Exception as exc:
        result["reason"] = "design rank diagnostic failed: %s" % exc
    return result


def _advice(item):
    """按判定给出处置建议文字。"""
    verdict = item.get("verdict")
    if verdict == "drop":
        return "建议不入模（保留于报告中备查，须人工确认后方可剔除）"
    if verdict == "choose_one":
        return "与另一变量信息重叠，须二选一，保留生物学层级更合理的一个"
    if verdict == "review":
        return "可保留但需人工确认数据分布是否可接受"
    return "建议入模"


# ===============================================================
# 四、输出结构说明
# ===============================================================
# 本模块只做"数据驱动的诊断 + 提示"，不修改任何入模清单：
#   - 逐列计算出 verdict（keep / drop / choose_one / review）与理由，作为给用户的提示；
#   - "drop" 仅表示该列解释力低、入模型价值不大，是否剔除由用户按任务要求决定；
#   - 建模规格（哪些固定效应 / 协变量）始终来自用户任务要求，本模块不代劳。
