# -*- coding: utf-8 -*-
"""面向育种专家的结果洞察提取层（与渲染解耦、可独立测试）。

本模块只做一件事：把运行期已经产出的引擎结果（parsed）、质控快照、
原始/引擎表型，整理成育种专家关心的结构化事实——

  * 规范方差组分（VA/VE/VP、各自 SE、占表型方差比例、h²±SE、95%CI、
    LogL / -2LogL、SE/h²、迭代收敛）；
  * 固定效应/协变量系数（.beta）与方差分析 F 检验（.anova）；
  * 分世代遗传趋势（平均 EBV ±SE、平均表型、有效记录数）；
  * 按信息来源划分的 EBV 群体（有自身记录 / 在群但无记录 / 仅系谱）；
  * 群体近交、数据流转链条、个体可靠性摘要。

铁律：**所有数字均由运行期真实数据现算，不写死、不臆造**；任一子项缺数据
或计算异常都降级为空（由渲染层决定不输出该节），绝不抛异常阻断报告主流程。
"""

import math
import logging
import os

logger = logging.getLogger(__name__)


# 世代列的可能列名（大小写不敏感匹配在前，启发式兜底在后）
_GENERATION_TOKENS = ("gen", "generation", "世代", "generationno", "geng")
_RESIDUAL_TOKENS = ("residuals", "residual", "e")
# EBV 效应列需要排除的后缀（--r2 时 .rand 附带的 SE/PEV/R2 列）
_EBV_SUFFIX = ("_SE", "_PEV", "_R2")


def _safe_float(value):
    try:
        if value is None:
            return None
        if isinstance(value, str) and value.strip() in (".", "", "NA", "NaN", "nan"):
            return None
        out = float(value)
        if math.isnan(out) or math.isinf(out):
            return None
        return out
    except (TypeError, ValueError):
        return None


def _round(value, nd=4):
    value = _safe_float(value)
    return None if value is None else round(value, nd)


def _effect_column(columns):
    """识别 .rand 中的加性 EBV 效应列（PA/GA/HA），排除 ID、残差与 SE/PEV/R2 列。"""
    excluded = {"id", "residuals", "residual"}
    chosen = None
    for name in columns:
        low = str(name).lower()
        if low in excluded:
            continue
        if any(low.endswith(suf.lower()) for suf in _EBV_SUFFIX):
            continue
        if low in _RESIDUAL_TOKENS:
            continue
        chosen = name
        break
    return chosen


def _generation_column(columns):
    for name in columns:
        if str(name).strip().lower() in _GENERATION_TOKENS:
            return name
    return None


def _ols_trend(xs, ys):
    """个体层面 EBV(y) 对世代(x) 的简单线性回归。

    斜率即“每代遗传进展（EBV/代）”，并给斜率 SE、t、双侧 P、决定系数 R²。
    相比仅用首末两代均值差，个体级回归利用全部个体、可做显著性检验。
    P 值优先用 scipy 学生 t 分布；scipy 不可用且样本很大时以正态近似兜底。
    """
    n = len(xs)
    if n < 3:
        return {}
    xm = sum(xs) / n
    ym = sum(ys) / n
    sxx = sum((x - xm) ** 2 for x in xs)
    sxy = sum((x - xm) * (y - ym) for x, y in zip(xs, ys))
    syy = sum((y - ym) ** 2 for y in ys)
    if sxx <= 0:
        return {}
    slope = sxy / sxx
    intercept = ym - slope * xm
    sse = sum((y - (intercept + slope * x)) ** 2 for x, y in zip(xs, ys))
    dfree = n - 2
    se = t = p = None
    if dfree > 0:
        mse = sse / dfree
        var_slope = mse / sxx
        if var_slope > 0:
            se = math.sqrt(var_slope)
            t = slope / se
            try:
                from scipy import stats
                p = float(2.0 * stats.t.sf(abs(t), dfree))
            except Exception:
                p = float(2.0 * (1.0 - 0.5 * (1.0 + math.erf(abs(t) / math.sqrt(2.0)))))
    r2 = (sxy * sxy / (sxx * syy)) if syy > 0 else None
    return {"n": int(n), "slope": _round(slope, 4), "intercept": _round(intercept, 4),
            "se": _round(se, 4), "t": _round(t, 4), "p": p,
            "r2": _round(r2, 4), "df": int(dfree)}


def variance_components(parsed):
    """规范方差组分表数据；复用引擎解析器，不重复实现口径。"""
    out = {"rows": [], "h2": None, "h2_se": None, "p_value": None,
           "ci_lower": None, "ci_upper": None, "vp": None,
           "logl": None, "neg2logl": None, "se_over_h2": None,
           "n_iterations": parsed.get("n_iterations"),
           "converged": parsed.get("converged")}
    try:
        # 与各 runner 一致优先顶层导入（bootstrap 已把 tools/HIBLUP 加入 sys.path，
        # 且 tools/ 与 tools/HIBLUP 均无 __init__.py，包路径导入在运行进程会失败）；
        # 包路径作为离线/其他入口的回退。
        try:
            import hiblup_tool as _ht
        except Exception:
            from tools.HIBLUP import hiblup_tool as _ht
        comps = _ht.extract_variance_components(parsed)
    except Exception:
        comps = []
    va = ve = va_se = ve_se = None
    for entry in comps:
        var = _safe_float(entry.get("variance"))
        se = _safe_float(entry.get("variance_se"))
        if entry.get("is_residual"):
            ve, ve_se = var, se
        elif va is None:  # 首个非残差随机效应即加性遗传（PA/GA/HA）
            va, va_se = var, se
            out["h2"] = _round(entry.get("h2"), 6)
            out["h2_se"] = _round(entry.get("h2_se"), 6)
            out["p_value"] = _safe_float(entry.get("p_value"))
    if va is not None and ve is not None:
        vp = va + ve
        out["vp"] = _round(vp, 6)
        if vp > 0:
            out["rows"] = [
                {"name": "加性遗传方差 VA", "value": _round(va), "se": _round(va_se),
                 "ratio": _round(va / vp, 4)},
                {"name": "残差方差 VE", "value": _round(ve), "se": _round(ve_se),
                 "ratio": _round(ve / vp, 4)},
                {"name": "表型方差 VP=VA+VE", "value": _round(vp), "se": None,
                 "ratio": 1.0},
            ]
    h2, h2se = out["h2"], out["h2_se"]
    if h2 is not None and h2se is not None:
        out["ci_lower"] = _round(h2 - 1.96 * h2se, 4)
        out["ci_upper"] = _round(h2 + 1.96 * h2se, 4)
        if h2 != 0:
            out["se_over_h2"] = _round(h2se / h2, 4)
    trace = parsed.get("iteration_trace") or []
    if trace:
        logl = _safe_float(trace[-1].get("logL"))
        out["logl"] = _round(logl, 4)
        if logl is not None:
            out["neg2logl"] = _round(-2.0 * logl, 4)
    return out


def fixed_effects(parsed):
    """固定效应/协变量系数（.beta：Levels/Estimation/SE）。"""
    rows = []
    for rec in parsed.get("beta") or []:
        level = rec.get("Levels", rec.get("levels"))
        if level is None:
            continue
        rows.append({"level": str(level),
                     "est": _round(_safe_float(rec.get("Estimation", rec.get("est"))), 6),
                     "se": _round(_safe_float(rec.get("SE", rec.get("se"))), 6)})
    return rows


def anova_table(parsed):
    """
    方差分析 F 检验（.anova）；残差行 F/P 为 '.'，按缺失处理。

    多性状（2026-10-13 新增）：HIBLUP v1.7.0 起多性状产出逐性状
    prefix.TRAIT.anova，解析层已合并进 result["anova"] 并为每条记录
    标注 trait 字段。此处透传，使报告能区分性状；单性状时 trait 为空串，
    报告层据此不加"性状"列，既有版式不变。
    """
    rows = []
    for rec in parsed.get("anova") or []:
        factor = rec.get("Factors", rec.get("factors"))
        if factor is None:
            continue
        rows.append({
            "trait": str(rec.get("trait") or ""),
            "factor": str(factor),
            "df": _safe_float(rec.get("Df")),
            "ss": _round(_safe_float(rec.get("SumSq")), 4),
            "ms": _round(_safe_float(rec.get("MeanSq")), 4),
            "f": _round(_safe_float(rec.get("F")), 4),
            "p": _safe_float(rec.get("Pr(>F)")),
        })
    return rows


def residual_diagnostics(parsed, trait, engine_pheno_path):
    """模型残差诊断统计（对应线性模型 plot(model) 的假设检验）。

    残差取 HIBLUP .rand 的 residuals 列（仅实际进入模型、有自身表型的个体非缺失），
    拟合值 fitted = 观测表型 y - 残差 e。据此给出零均值、正态性（偏度/峰度）、
    离散程度等可核验事实，供报告判断模型经典假设是否成立。所有量均现算，缺数据则
    返回 available=False，由渲染层决定不输出该节。
    """
    out = {"available": False}
    try:
        import pandas as pd
        records = parsed.get("rand") or []
        if not records or not engine_pheno_path or not trait:
            return out
        rand = pd.DataFrame(records)
        if "ID" not in rand.columns or "residuals" not in rand.columns:
            return out
        eng = pd.read_csv(engine_pheno_path, sep="\t")
        if "ID" not in eng.columns or trait not in eng.columns:
            return out
        rr = rand[["ID", "residuals"]].copy()
        rr["residuals"] = pd.to_numeric(rr["residuals"], errors="coerce")
        ee = eng[["ID", trait]].copy()
        ee[trait] = pd.to_numeric(ee[trait], errors="coerce")
        merged = rr.merge(ee, on="ID", how="inner").dropna(
            subset=["residuals", trait])
        if len(merged) < 8:
            return out
        merged["fitted"] = merged[trait] - merged["residuals"]
        resid = merged["residuals"]
        out.update({
            "available": True,
            "n": int(len(merged)),
            "mean": _round(resid.mean(), 4),
            "sd": _round(resid.std(ddof=1), 4),
            "median": _round(resid.median(), 4),
            "q1": _round(resid.quantile(0.25), 4),
            "q3": _round(resid.quantile(0.75), 4),
            "min": _round(resid.min(), 4),
            "max": _round(resid.max(), 4),
            # 偏度、超额峰度：正态分布两者均≈0
            "skew": _round(resid.skew(), 4),
            "kurt": _round(resid.kurt(), 4),
            "fitted_min": _round(merged["fitted"].min(), 4),
            "fitted_max": _round(merged["fitted"].max(), 4),
        })
        # 残差-拟合值结构指标：线性相关 + 二次曲率，用于客观判断异方差/非线性（U 型弯曲）
        try:
            import numpy as np
            fx = merged["fitted"].to_numpy(dtype=float)
            rv = merged["residuals"].to_numpy(dtype=float)
            if fx.std() > 0 and rv.std() > 0:
                lin = float(np.corrcoef(fx, rv)[0, 1])
                zx = (fx - fx.mean()) / fx.std()
                c1 = np.polyfit(zx, rv, 1)
                c2 = np.polyfit(zx, rv, 2)
                yhat1 = np.polyval(c1, zx)
                yhat2 = np.polyval(c2, zx)
                sst = float(((rv - rv.mean()) ** 2).sum())
                r2_lin = 1.0 - float(((rv - yhat1) ** 2).sum()) / sst if sst else 0.0
                r2_quad = 1.0 - float(((rv - yhat2) ** 2).sum()) / sst if sst else 0.0
                out["lin_corr"] = _round(lin, 4)
                # 标准化拟合值上的二次项系数（残差原始尺度）：>0 开口向上(U)，<0 倒 U
                out["quad_coef"] = _round(float(c2[0]), 4)
                # 引入二次项后 R² 的增量：越大说明非线性/弯曲越明显
                out["quad_r2_gain"] = _round(max(0.0, r2_quad - r2_lin), 4)
        except Exception:
            pass
    except Exception:
        return {"available": False}
    return out


def population_and_trend(parsed, trait, raw_pheno_path, engine_pheno_path,
                        genetic_reporting=None):
    """分世代遗传趋势 + 按信息来源的 EBV 分群。数字与预测总数严格闭环。"""
    genetic_reporting = genetic_reporting or {}
    result = {"trend_rows": [], "trend_delta_per_gen": None,
              "trend_first": None, "trend_last": None,
              "groups": [], "prediction_n": 0, "generation_available": False,
              "partition_closed": None, "id_generation": {},
              # 【S3】内部诊断字段（不落盘为 json，不影响任何既有 schema）
              "population_source": None, "population_degraded": None,
              "trend_interpretation": "世代分组 EBV 描述统计；未声明遗传世代语义时不解释为遗传趋势",
              "reference_group": {"configured": False, "available": False}}
    try:
        import pandas as pd
    except Exception:
        return result
    rand_records = parsed.get("rand") or []
    if not rand_records:
        return result
    try:
        rand = pd.DataFrame(rand_records)
        eff = _effect_column(rand.columns)
        if eff is None:
            return result
        rand = rand[["ID", eff]].copy()
        rand.columns = ["ID", "EBV"]
        rand["EBV"] = pd.to_numeric(rand["EBV"], errors="coerce")
        rand["ID"] = rand["ID"].astype(str)
        rand = rand.dropna(subset=["EBV"])
        result["prediction_n"] = int(len(rand))
        # 全预测群 EBV 分布（与分群、核心结论同口径，避免被报告群体裁剪后口径不一致）
        es = rand["EBV"]
        result["ebv_distribution"] = {
            "n": int(es.size),
            "mean": _round(es.mean(), 4),
            "sd": _round(es.std(ddof=1), 4) if es.size > 1 else 0.0,
            "median": _round(es.median(), 4),
            "p25": _round(es.quantile(0.25), 4),
            "p75": _round(es.quantile(0.75), 4),
            "min": _round(es.min(), 4),
            "max": _round(es.max(), 4)}

        # ==========================================================
        # 群体信息来源：世代 / 自身表型 / 在群判定
        # 【S3 修复】原先这里用 pd.read_csv(sep="\t") 去读原始 .xlsx
        # （plan["phenotype_path"]），必然抛 UnicodeDecodeError，而异常被
        # `except Exception: pass` 静默吞掉 → generation_available 恒 False、
        # raw_ids 恒空 → 报告趋势表渲染为 "—"、fig3/fig4 不生成，且三路分群
        # 全部落入"仅系谱"（曾把部分在群个体误判为不在表型文件中）。
        # 现改读【引擎表型 TXT】（含 Gen 列、覆盖目标群体），与
        # valid_records / data_funnel 实际入模数 / residual_diagnostics(:222)
        # 完全同源同口径；原始 xlsx 仅保留为降级通道。
        # ==========================================================
        valid_ids, raw_ids, raw_gen, raw_phe = set(), set(), {}, {}
        reference_values = {}
        reference_column = genetic_reporting.get("reference_column")
        reference_value = genetic_reporting.get("reference_value")
        reference_min_n = int(genetic_reporting.get("minimum_n", 30) or 30)
        degrade = None

        # ---- ① 引擎表型 TXT（主来源，模型实际输入）----
        if engine_pheno_path and os.path.isfile(engine_pheno_path):
            try:
                # dtype=str：防止纯数字 ID 被读成 int64/float，
                # astype(str) 后变 "1001.0" 而与 .rand 的 "1001" 比对失败
                eng = pd.read_csv(engine_pheno_path, sep="\t", dtype=str)
                eng.columns = [str(c).strip() for c in eng.columns]
                if reference_column and reference_column in eng.columns:
                    reference_values = dict(zip(eng["ID"].astype(str),
                                                eng[reference_column].astype(str).str.strip()))
                gcol = _generation_column(eng.columns)
                tcol = trait if (trait and trait in eng.columns) else None
                if "ID" not in eng.columns:
                    degrade = "engine_missing_id_column"
                elif gcol is None:
                    degrade = "engine_missing_generation_column"
                else:
                    result["population_source"] = "engine_phenotype"
                    result["generation_available"] = True
                    gnum = pd.to_numeric(eng[gcol], errors="coerce")
                    pnum = (pd.to_numeric(eng[tcol], errors="coerce")
                            if tcol else pd.Series([float("nan")] * len(eng)))
                    for rid, g, pv in zip(eng["ID"].astype(str), gnum, pnum):
                        raw_ids.add(rid)
                        if pd.notna(g):
                            raw_gen[rid] = g
                        if pd.notna(pv):
                            raw_phe[rid] = float(pv)
                            valid_ids.add(rid)
            except Exception as exc:
                degrade = "engine_read_failed:%r" % (exc,)
        else:
            degrade = "engine_phenotype_missing"

        # ---- ② 降级：原始表型（统一读取器，支持 xlsx/txt）----
        # 仅在引擎路径不可用时启用（如阶段 06 被跳过 / 手工重渲染）。
        if result["population_source"] is None and raw_pheno_path \
                and os.path.isfile(raw_pheno_path):
            try:
                import abges_dataio        # tools/Python 已在 sys.path
                raw, _info = abges_dataio.read_table(raw_pheno_path, dtype=str)
                raw.columns = [str(c).strip() for c in raw.columns]
                if reference_column and reference_column in raw.columns:
                    reference_values = dict(zip(raw["ID"].astype(str),
                                                raw[reference_column].astype(str).str.strip()))
                gcol = _generation_column(raw.columns)
                tcol = trait if (trait and trait in raw.columns) else None
                if "ID" in raw.columns and gcol is not None:
                    result["population_source"] = "raw_phenotype"
                    result["generation_available"] = True
                    gnum = pd.to_numeric(raw[gcol], errors="coerce")
                    pnum = (pd.to_numeric(raw[tcol], errors="coerce")
                            if tcol else pd.Series([float("nan")] * len(raw)))
                    for rid, g, pv in zip(raw["ID"].astype(str), gnum, pnum):
                        raw_ids.add(rid)
                        if pd.notna(g):
                            raw_gen[rid] = g
                        if pd.notna(pv):
                            raw_phe[rid] = float(pv)
                            valid_ids.add(rid)
                else:
                    degrade = "raw_missing_id_or_generation_column"
            except Exception as exc:
                degrade = (degrade + " | " if degrade else "") + \
                          "raw_read_failed:%r" % (exc,)

        if result["population_source"] is None:
            # 三段降级全部失败：明确留痕，绝不 pass
            result["population_degraded"] = degrade or "no_phenotype_source"
            logger.error("群体信息来源不可用（%s）；分世代遗传趋势与信息来源分群将为空",
                         result["population_degraded"])

        ebv_by_id = dict(zip(rand["ID"], rand["EBV"]))
        if reference_column and reference_value is not None:
            wanted = str(reference_value).strip()
            base_values = [float(ebv) for iid, ebv in ebv_by_id.items()
                           if reference_values.get(iid) == wanted]
            result["reference_group"] = {
                "configured": True, "column": reference_column,
                "value": wanted, "n": len(base_values),
                "minimum_n": reference_min_n,
                "available": len(base_values) >= reference_min_n,
                "mean_ebv": _round(sum(base_values) / len(base_values), 4)
                if len(base_values) >= reference_min_n else None,
                "reason": None if len(base_values) >= reference_min_n else
                "参考群预测个体数低于 minimum_n，未生成基准化 EBV",
            }
            if result["reference_group"]["available"]:
                result["reference_group"]["centered_mean_ebv"] = 0.0
                result["reference_group"]["interpretation"] = (
                    "基准化 EBV = 原始 EBV - 参考群 EBV 均值；原始 EBV 保留不变")
        # ---- 信息来源分群（三者之和=预测总数）----
        g_with, g_inphe, g_pedonly = [], [], []
        for rid, ebv in ebv_by_id.items():
            if rid in valid_ids:
                g_with.append(ebv)
            elif rid in raw_ids:
                g_inphe.append(ebv)
            else:
                g_pedonly.append(ebv)
        result["partition_closed"] = (
            len(g_with) + len(g_inphe) + len(g_pedonly) == len(ebv_by_id))

        def _stat(values):
            s = pd.Series(values, dtype="float64")
            if s.empty:
                return {"n": 0, "mean": None, "sd": None}
            return {"n": int(s.size),
                    "mean": _round(s.mean(), 4),
                    "sd": _round(s.std(ddof=1), 4) if s.size > 1 else 0.0}

        result["groups"] = [
            ("有自身有效表型记录", _stat(g_with)),
            ("在群但自身无有效记录", _stat(g_inphe)),
            ("仅系谱（无表型记录）", _stat(g_pedonly)),
        ]

        # ---- 分世代遗传趋势（仅有世代信息的个体）----
        if result["generation_available"]:
            rows = {}
            reg_x, reg_y = [], []
            for rid, ebv in ebv_by_id.items():
                g = raw_gen.get(rid)
                if g is None:
                    continue
                g = int(g)
                reg_x.append(float(g)); reg_y.append(float(ebv))
                bucket = rows.setdefault(g, {"ids": [], "ebv": [],
                                             "valid_n": 0, "phe": []})
                bucket["ebv"].append(ebv)
                if rid in valid_ids:
                    bucket["valid_n"] += 1
                    if rid in raw_phe:
                        bucket["phe"].append(raw_phe[rid])
            trend_rows = []
            for g in sorted(rows):
                b = rows[g]
                ebv_s = pd.Series(b["ebv"], dtype="float64")
                phe_s = pd.Series(b["phe"], dtype="float64")
                se = (ebv_s.std(ddof=1) / math.sqrt(ebv_s.size)
                      if ebv_s.size > 1 else 0.0)
                trend_rows.append({
                    "gen": g, "n_total": int(ebv_s.size),
                    "n_valid": int(b["valid_n"]),
                    "mean_phe": _round(phe_s.mean(), 4) if not phe_s.empty else None,
                    "mean_ebv": _round(ebv_s.mean(), 4),
                    "sd_ebv": _round(ebv_s.std(ddof=1), 4) if ebv_s.size > 1 else 0.0,
                    "se_ebv": _round(se, 4),
                })
            result["trend_rows"] = trend_rows
            if len(trend_rows) >= 2:
                result["generation_mean_difference"] = _round(
                    trend_rows[-1]["mean_ebv"] - trend_rows[0]["mean_ebv"], 4)
                result["generation_difference_scope"] = "描述性世代均值差异；未校正世代构成/连通性"
            if genetic_reporting.get("generation_semantics") == "genetic_generation":
                result["trend_interpretation"] = "遗传世代 EBV 趋势（关联结构及世代定义需经业务确认）"
            if (len(trend_rows) >= 2
                    and genetic_reporting.get("generation_semantics") == "genetic_generation"):
                first, last = trend_rows[0], trend_rows[-1]
                result["trend_first"] = first["mean_ebv"]
                result["trend_last"] = last["mean_ebv"]
                span = last["gen"] - first["gen"]
                if span > 0 and first["mean_ebv"] is not None and last["mean_ebv"] is not None:
                    result["trend_delta_per_gen"] = _round(
                        (last["mean_ebv"] - first["mean_ebv"]) / span, 4)
                    result["trend_delta_total"] = _round(
                        last["mean_ebv"] - first["mean_ebv"], 4)
            # 个体层面 EBV 对世代回归：斜率=每代遗传进展，附 SE/t/P/R²
            result["trend_regression"] = _ols_trend(reg_x, reg_y)
            result["id_generation"] = dict(raw_gen)
    except Exception:
        # 趋势/分群为增强信息，任何异常都不影响报告其余部分
        pass
    return result


def inbreeding(ped_snap):
    """从系谱质控快照 after 提取近交与群体结构。"""
    out = {}
    if not ped_snap:
        return out
    after = ped_snap.get("after") or {}
    if not after:
        return out
    out["individuals"] = after.get("Individuals")
    out["founders"] = after.get("FounderCount")
    out["both_parents_known_rate"] = after.get("BothParentsKnownRate")
    out["duplicate_ids"] = after.get("DuplicateIDs")
    out["self_as_parent"] = after.get("SelfAsParent")
    out["max_generation_depth"] = after.get("MaxGenerationDepth")
    out["mean_inbreeding"] = _safe_float(after.get("MeanInbreeding"))
    out["max_inbreeding"] = _safe_float(after.get("MaxInbreeding"))
    return out


def data_funnel(pheno_snap, integration_metrics, prediction_n, valid_records=None):
    """数据流转链条（各环节真实规模，便于专家核对口径）。"""
    steps = []
    b = (pheno_snap or {}).get("before") or {}
    a = (pheno_snap or {}).get("after") or {}
    im = integration_metrics or {}
    if b.get("n_records") is not None:
        steps.append(("原始表型记录数", b.get("n_records")))
    if a.get("n_records") is not None:
        steps.append(("质控后表型记录数（离群置缺失、不删行）", a.get("n_records")))
    if valid_records is not None:
        steps.append(("目标性状有效、实际进入模型记录数", valid_records))
    if im.get("pedigree_individuals") is not None:
        steps.append(("系谱个体数（补奠基者后）", im.get("pedigree_individuals")))
    if im.get("pheno_ped_overlap") is not None:
        steps.append(("表型×系谱交集个体数", im.get("pheno_ped_overlap")))
    if prediction_n:
        steps.append(("输出育种值（EBV）个体数", prediction_n))
    return steps


def reliability_brief(result_eval):
    """个体可靠性摘要（开启 --r2 时有值）。"""
    rel = (result_eval or {}).get("ebv_reliability") or {}
    if not rel.get("available"):
        return {"available": False, "reason": rel.get("reason")}
    rs = rel.get("reliability_summary") or {}
    acs = rel.get("accuracy_summary") or {}
    return {"available": True,
            "rel_mean": rs.get("mean"), "rel_median": rs.get("median"),
            "rel_min": rs.get("min"), "rel_max": rs.get("max"),
            "acc_mean": acs.get("mean"), "acc_median": acs.get("median"),
            "acc_min": acs.get("min"), "acc_max": acs.get("max"),
            "records_total": rel.get("records_total"),
            "records_valid": rel.get("records_valid"),
            "records_out_of_range": rel.get("records_out_of_range"),
            "source": rel.get("canonical_source") or rel.get("source"),
            "r2_vs_pev_va_max_abs_difference": rel.get("r2_vs_pev_va_max_abs_difference")}


def selection_response(parsed, variance, reliability, proportions=(0.05, 0.10, 0.20),
                       generation_interval=None, scenario=None):
    """按声明的候选群构造假设选种情景；绝不输出实际选种名单或决策。

    默认全体预测个体仅用于情景演示。只有提供个体准确率和世代间隔时，
    才估算每代进展；群体平均准确率和 sqrt(h2) 不作个体准确率替代。
    """
    out = {"available": False, "rows": []}
    try:
        records = parsed.get("rand") or []
        if not records:
            return out
        import pandas as pd
        frame = pd.DataFrame(records)
        eff = _effect_column(frame.columns)
        if eff is None:
            return out
        frame["_ebv"] = pd.to_numeric(frame[eff], errors="coerce")
        scope = scenario or {}
        candidate_ids = {str(value).strip() for value in scope.get("candidate_ids", [])}
        if candidate_ids and "ID" in frame.columns:
            frame = frame.loc[frame["ID"].astype(str).str.strip().isin(candidate_ids)]
        ebv = frame["_ebv"].dropna()
        n = int(ebv.size)
        if n < 10:
            return out
        mean = float(ebv.mean())
        sd = float(ebv.std(ddof=1)) if n > 1 else 0.0
        var_rows = (variance or {}).get("rows") or []
        va = _safe_float(var_rows[0]["value"]) if var_rows else None
        sigma_a = math.sqrt(va) if va not in (None, 0) else None
        rel = reliability or {}
        acc = _safe_float(rel.get("acc_mean")) if rel.get("available") else None
        if acc is not None and acc > 0:
            r_use, r_src = acc, "个体平均准确率（已开 --r2）"
        else:
            r_use, r_src = None, "无个体准确率；不计算 ΔG"
        ordered = ebv.sort_values(ascending=False)
        rows = []
        for pct in proportions:
            k = max(1, int(math.ceil(n * pct)))
            sel_mean = float(ordered.iloc[:k].mean())
            diff = sel_mean - mean
            intensity = (diff / sd) if sd else None
            d_g = None
            if (intensity is not None and r_use and sigma_a
                    and generation_interval is not None and generation_interval > 0):
                d_g = intensity * r_use * sigma_a / generation_interval
            rows.append({
                "proportion": "%.0f%%" % (pct * 100),
                "n_select": k,
                "sel_mean": _round(sel_mean, 4),
                "selection_diff": _round(diff, 4),
                "selection_intensity": _round(intensity, 4),
                "delta_g": _round(d_g, 4),
            })
        out.update({
            "available": True, "population_n": n,
            "population_mean": _round(mean, 4),
            "population_sd": _round(sd, 4),
            "sigma_a": _round(sigma_a, 4),
            "accuracy_used": _round(r_use, 4), "accuracy_source": r_src,
            "generation_interval": generation_interval, "rows": rows,
            "scenario_only": True,
            "population_scope": ("任务显式候选 ID" if candidate_ids else
                                 "全体预测个体的假设情景；非实际候选群"),
            "decision_status": "不构成选种名单或实际选种决策",
        })
    except Exception:
        return {"available": False, "rows": []}
    return out


def family_summary(top_individuals, pedigree_path):
    """Top 个体的亲本与父系家系分布（以父亲为家系单位），服务控近交与家系均衡。

    读取引擎系谱（首三列 ID/Sire/Dam），为每个 Top 个体补父亲、母亲与父系家系，
    并统计 Top 群覆盖的父系数与最大家系占比。缺系谱则 available=False。
    """
    out = {"available": False, "parent_of": {}, "family_rows": [],
           "family_count": None, "max_family_share": None}
    try:
        if not top_individuals or not pedigree_path:
            return out
        import pandas as pd
        # 引擎系谱首三列 ID Sire Dam；首行第一列为 id/iid/animal 时视为带表头
        head = pd.read_csv(pedigree_path, sep=r"\s+", engine="python",
                           header=None, dtype=str, nrows=1)
        first = str(head.iloc[0, 0]).strip().lower()
        header = 0 if first in ("id", "iid", "animal") else None
        ped = pd.read_csv(pedigree_path, sep=r"\s+", engine="python",
                          header=header, dtype=str)
        if ped.shape[1] < 3:
            return out
        idc, sic, dc = ped.columns[0], ped.columns[1], ped.columns[2]
        missing = {"0", "nan", "none", "na", ".", ""}

        def _clean(x):
            sx = str(x).strip().lower()
            return None if sx in missing else str(x).strip()

        parent = {str(r[idc]): (_clean(r[sic]), _clean(r[dc]))
                  for _, r in ped.iterrows()}
        fam_count = {}
        for it in top_individuals:
            iid = str(it.get("ID"))
            sire, dam = parent.get(iid, (None, None))
            family = sire if sire else "奠基者/未知"
            out["parent_of"][iid] = {"sire": sire, "dam": dam, "family": family}
            fam_count[family] = fam_count.get(family, 0) + 1
        total = sum(fam_count.values())
        fam_rows = sorted(fam_count.items(), key=lambda kv: (-kv[1], str(kv[0])))
        out.update({
            "available": True,
            "family_rows": [{"family": k, "n": v} for k, v in fam_rows],
            "family_count": len(fam_count),
            "max_family_share": _round(max(fam_count.values()) / total, 4) if total else None,
        })
    except Exception as exc:
        out["error"] = repr(exc)
        return out
    return out


def build_breeding_insights(plan, parsed, pheno_snap=None, ped_snap=None,
                            geno_snap=None, integration_metrics=None,
                            result_eval=None, model_key="pblup",
                            valid_records=None):
    """汇总全部育种洞察；parsed 为 parse_hiblup_output 的结果。"""
    parsed = parsed or {}
    trait = (plan or {}).get("trait")
    raw_pheno_path = (plan or {}).get("phenotype_path")
    # engine_phenotype 路径由 runner 放入 context，这里经 plan 透传
    engine_pheno_path = (plan or {}).get("_engine_phenotype_path")
    variance = variance_components(parsed)
    reporting = (plan or {}).get("genetic_reporting") or {}
    pop = population_and_trend(parsed, trait, raw_pheno_path, engine_pheno_path,
                               genetic_reporting=reporting)
    residual = residual_diagnostics(parsed, trait, engine_pheno_path)
    rel_brief = reliability_brief(result_eval)
    try:
        gen_interval = (plan or {}).get("generation_interval")
        if gen_interval is not None:
            gen_interval = float(gen_interval)
    except (TypeError, ValueError):
        gen_interval = None
    selection = selection_response(parsed, variance, rel_brief,
                                   generation_interval=gen_interval,
                                   scenario=(plan or {}).get("selection_scenario"))
    insights = {
        "variance": variance,
        "fixed_effects": fixed_effects(parsed),
        "anova": anova_table(parsed),
        "residual": residual,
        "inbreeding": inbreeding(ped_snap),
        "reliability": rel_brief,
        "selection_response": selection,
        "funnel": data_funnel(pheno_snap, integration_metrics,
                              pop.get("prediction_n"), valid_records),
        "model_key": model_key,
        "trait": trait,
    }
    insights.update(pop)
    return insights
