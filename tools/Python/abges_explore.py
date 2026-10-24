# -*- coding: utf-8 -*-
"""
ABGES 表型数据探索模块
======================
职责：在正式建模（阶段 06）之前，对表型数据做一次"体检"，输出三类结论：

  1. 描述统计增强 —— 在既有 17 项指标之上补 IQR / 超3SD / 超3Q 离群计数，
     并把分组统计从"仅 N+Mean"扩展为 N/Mean/SD/CV；
  2. 分布（正态性）检验 —— 偏度 / 峰度 + Shapiro-Wilk + Kolmogorov-Smirnov，
     按样本量分层判定（大样本不迷信 P 值）；
  3. 共线性诊断 —— 相关矩阵 / VIF / 条件数，识别会污染方差组分估计的变量对。

设计原则（与项目铁律一致）：
  - 阈值一律外置：全部读自 phenotype-qc.yaml 的 exploration 段，本模块不硬编码。
  - 软依赖降级：scipy 缺失时跳过 SW/KS 检验（偏度/峰度仍用 numpy 计算），
    记入 degraded 列表并告警，**绝不阻断评估主流程**。
  - 只诊断不擅自改数据：本模块不修改 frame，只产出结论与建议。
  - 共线性默认 action=warn（先生拍板 2026-10-17），不阻断建模。

方案文档：docs/ABGES_表型数据探索方案_v1.0.md
"""

import abges_core as core
import abges_phenotype as phenotype


PHENOTYPE_QC_CONFIG = "configs/qc/phenotype-qc.yaml"


# 配置缺失时的兜底默认（仅当 yaml 未配置该键时启用，正常情况下阈值来自配置）
_DEFAULTS = {
    "enabled": True,
    "descriptors": {"iqr": True, "outlier_3sd": True, "outlier_3q": True,
                    "group_extended": True},
    "distribution": {"small_sample_n": 200, "sw_max_n": 5000,
                     "skew_normal_max": 0.5, "skew_warn_max": 1.0,
                     "kurtosis_normal_max": 1.0, "kurtosis_warn_max": 3.0,
                     "ks_enabled": True},
    "collinearity": {"action": "warn", "corr_warn": 0.8, "corr_severe": 0.95,
                     "vif_warn": 5, "vif_severe": 10,
                     "condition_number_severe": 30, "identical_corr": 0.999},
}


def _section(config, name):
    """取配置子段，逐键用默认值兜底。"""
    merged = dict(_DEFAULTS.get(name, {}))
    merged.update((config or {}).get(name, {}) or {})
    return merged


def load_exploration_config(config_path=PHENOTYPE_QC_CONFIG):
    """从 phenotype-qc.yaml 读取 exploration 段。"""
    try:
        cfg = core.load_yaml_config(config_path)
    except Exception:
        return dict(_DEFAULTS)
    return (cfg.get("exploration", {}) or {}) or dict(_DEFAULTS)


def scipy_stats():
    """scipy.stats 软依赖；缺失返回 None。

    公开函数：abges_effects 等同层模块复用，避免各模块各写一份导入兜底。
    """
    try:
        from scipy import stats
        return stats
    except Exception:
        return None


def _scipy_stats():
    """向后兼容别名（既有调用点仍在用）。"""
    return scipy_stats()


# ===============================================================
# 一、描述统计增强
# ===============================================================

def describe_extended(series, config=None):
    """在 describe_continuous 的 17 项之上追加 IQR / 超3SD / 超3Q 计数。

    追加项含义：
      IQR            Q3 − Q1
      Outlier3SD     落在 mean ± 3SD 之外的个体数
      Outlier3Q      落在 [Q1−3·IQR, Q3+3·IQR] 之外的个体数（极端离群）
    """
    import pandas as pd

    desc = dict(phenotype.describe_continuous(series))
    opt = _section(config, "descriptors")

    numeric = pd.to_numeric(series, errors="coerce").dropna()
    if numeric.empty:
        desc.update({"IQR": None, "Outlier3SD": None, "Outlier3Q": None})
        return desc

    q1 = float(numeric.quantile(0.25))
    q3 = float(numeric.quantile(0.75))
    iqr = q3 - q1
    mean_value = float(numeric.mean())
    sd_value = float(numeric.std(ddof=1)) if len(numeric) > 1 else 0.0

    out = {}
    out["IQR"] = round(iqr, 6) if opt.get("iqr", True) else None
    if opt.get("outlier_3sd", True) and sd_value > 0:
        out["Outlier3SD"] = int(((numeric < mean_value - 3 * sd_value) |
                                 (numeric > mean_value + 3 * sd_value)).sum())
    else:
        out["Outlier3SD"] = None
    if opt.get("outlier_3q", True) and iqr > 0:
        out["Outlier3Q"] = int(((numeric < q1 - 3 * iqr) |
                                (numeric > q3 + 3 * iqr)).sum())
    else:
        out["Outlier3Q"] = None
    desc.update(out)
    return desc


def describe_grouped_extended(frame, trait, group_columns):
    """分组统计扩展：每组给出 N / Mean / SD / CV（原实现仅 N + Mean）。"""
    import pandas as pd

    result = {}
    for column in group_columns:
        if column not in frame.columns:
            continue
        numeric = pd.to_numeric(frame[trait], errors="coerce")
        grouped = numeric.groupby(frame[column].astype(str))
        levels = {}
        for level, sub in grouped:
            valid = sub.dropna()
            if valid.empty:
                levels[str(level)] = {"N": 0, "Mean": None, "SD": None, "CV": None}
                continue
            mean_value = float(valid.mean())
            sd_value = float(valid.std(ddof=1)) if len(valid) > 1 else 0.0
            levels[str(level)] = {
                "N": int(valid.shape[0]),
                "Mean": round(mean_value, 6),
                "SD": round(sd_value, 6),
                "CV": phenotype._coefficient_of_variation(mean_value, sd_value),
            }
        result[column] = levels
    return result


# ===============================================================
# 二、分布（正态性）检验
# ===============================================================

def _moments(values):
    """样本偏度与（超额）峰度的纯 numpy 矩估计，与 scipy bias=True 口径一致。"""
    import numpy as np

    x = np.asarray(values, dtype="float64")
    n = x.size
    if n < 3:
        return None, None
    center = x - x.mean()
    m2 = float((center ** 2).mean())
    if m2 <= 0:
        return 0.0, 0.0
    m3 = float((center ** 3).mean())
    m4 = float((center ** 4).mean())
    skew = m3 / (m2 ** 1.5)
    # 超额峰度（正态分布为 0）
    kurt = m4 / (m2 ** 2) - 3.0
    return round(float(skew), 6), round(float(kurt), 6)


def _norm_cdf(values):
    """标准正态 CDF：优先 scipy 向量化实现，不可用时退回 math.erf。"""
    import math

    import numpy as _np
    try:
        from scipy import special as _special
        return _special.ndtr(_np.asarray(values, dtype="float64"))
    except Exception:
        arr = _np.asarray(values, dtype="float64")
        return _np.array([0.5 * (1.0 + math.erf(float(v) / math.sqrt(2.0)))
                          for v in arr])


def _ks_normal(values):
    """样本对正态分布的 K-S 检验（位置与尺度由样本均值/标准差估计）。

    返回 (D, P)：
      D  经验累积分布与正态 CDF 的最大垂直距离（越接近 0 越贴合正态）
      P  Kolmogorov 渐近分布尾概率（Marsaglia-Tsang-Wang 级数，取前 100 项）

    方法学说明：以样本均值与标准差替代总体参数时属 Lilliefors 情形，
    K-S 检验偏保守（P 值偏大、不易拒绝），故本报告以 Shapiro-Wilk 为主、
    K-S 仅作辅助佐证，两者结论不一致时以偏度/峰度的矩判定为准。
    """
    import math

    import numpy as np

    x = np.sort(np.asarray(values, dtype="float64"))
    x = x[np.isfinite(x)]
    n = int(x.size)
    if n < 3:
        return None, None
    mean_value = float(x.mean())
    sd_value = float(x.std(ddof=1))
    if not (sd_value > 0):
        return None, None
    cdf = _norm_cdf((x - mean_value) / sd_value)
    idx = np.arange(1, n + 1, dtype="float64")
    d_stat = max(float(np.max(idx / n - cdf)),
                 float(np.max(cdf - (idx - 1.0) / n)))

    lam = (math.sqrt(n) + 0.12 + 0.11 / math.sqrt(n)) * d_stat
    if lam <= 0:
        return d_stat, 1.0
    total = 0.0
    for k in range(1, 101):
        term = math.exp(-2.0 * k * k * lam * lam)
        total += term if k % 2 == 1 else -term
        if term < 1e-16:      # 后续项对结果已无可观贡献
            break
    return d_stat, min(1.0, max(0.0, 2.0 * total))


def normality(series, config=None):
    """分布（正态性）检验 + 分层判定。

    返回 dict：
      N / Skew / Kurtosis / SWStat / SWTest.P / KSStat / KsTest.P
      Verdict   : normal | mild | skewed | insufficient
      Basis     : 判定依据说明（写明走了哪条规则）
      Advice    : 处置建议
      Degraded  : 降级原因（如 scipy 缺失），正常为 None

    判定规则（阈值全部来自配置）：
      n ≤ small_sample_n   → 以 SWTest.P 为主（>0.05 且 |偏度|<warn_max 判正态）
      small_sample_n < n ≤ sw_max_n → P 值与矩判定取"或"（宽松）
      n > sw_max_n         → 不看 P 值，只看 |偏度| 与 |峰度|
    """
    import pandas as pd

    opt = _section(config, "distribution")
    small_n = int(opt.get("small_sample_n", 200))
    sw_max_n = int(opt.get("sw_max_n", 5000))
    skew_norm = float(opt.get("skew_normal_max", 0.5))
    skew_warn = float(opt.get("skew_warn_max", 1.0))
    kurt_norm = float(opt.get("kurtosis_normal_max", 1.0))
    kurt_warn = float(opt.get("kurtosis_warn_max", 3.0))

    numeric = pd.to_numeric(series, errors="coerce").dropna()
    n = int(numeric.size)
    base = {"N": n, "Skew": None, "Kurtosis": None, "SWStat": None,
            "SWTest.P": None, "KSStat": None, "KsTest.P": None,
            "Verdict": "insufficient", "Basis": "有效样本不足（<3）",
            "Advice": "样本量过小，不做正态性判定", "Degraded": None}
    if n < 3:
        return base

    skew, kurt = _moments(numeric.to_numpy())
    base["Skew"] = skew
    base["Kurtosis"] = kurt

    stats_mod = _scipy_stats()
    degraded = None
    sw_p = ks_p = None
    if stats_mod is not None:
        try:
            if n <= sw_max_n:
                sw_stat, sw_p = stats_mod.shapiro(numeric.to_numpy())
                base["SWStat"] = round(float(sw_stat), 6)
                base["SWTest.P"] = round(float(sw_p), 10)
        except Exception as exc:  # 样本量过大或计算异常
            degraded = "shapiro 计算失败：%s" % exc
        # K-S 检验自行实现（不调 scipy.stats.kstest）：
        # scipy 1.18 的 ks_1samp 内部以 3 个位置参数调用 ndtr()，在本环境下
        # 恒抛 TypeError，导致 K-S 列全空。统计量 D 与 P 值均为标准算法，
        # 自实现反而更稳，且不依赖 scipy 的具体版本行为。
        if opt.get("ks_enabled", True):
            try:
                ks_stat, ks_p = _ks_normal(numeric.to_numpy())
                if ks_stat is not None:
                    base["KSStat"] = round(float(ks_stat), 6)
                    base["KsTest.P"] = round(float(ks_p), 10)
            except Exception as exc:
                degraded = (degraded + "; " if degraded else "") + "kstest 失败：%s" % exc
    else:
        degraded = "scipy 缺失：跳过 Shapiro-Wilk 与 K-S 检验，仅按偏度/峰度判定"

    base["Degraded"] = degraded

    abs_skew = abs(skew) if skew is not None else 0.0
    abs_kurt = abs(kurt) if kurt is not None else 0.0
    moments_ok = (abs_skew < skew_norm and abs_kurt < kurt_norm)

    # ---- 分层判定 ----
    if n <= small_n:
        if sw_p is not None and sw_p > 0.05 and abs_skew < skew_warn:
            verdict, basis = "normal", "小样本：SWTest.P=%.4g>0.05 且 |偏度|<%.1f" % (sw_p, skew_warn)
        elif abs_skew > skew_warn or abs_kurt > kurt_warn:
            verdict, basis = "skewed", "小样本：|偏度| 或 |峰度| 超 warn 阈值"
        elif moments_ok:
            verdict, basis = "normal", "小样本：矩判定通过（无 SW 结果或 P≤0.05 但形态良好）"
        else:
            verdict, basis = "mild", "小样本：轻微偏离正态"
    elif n <= sw_max_n:
        # 中等样本：P 值与矩判定取宽松（满足其一即正常），避免大样本过度拒绝
        if (sw_p is not None and sw_p > 0.05) or moments_ok:
            verdict, basis = "normal", "中等样本：SWTest.P>0.05 或 矩判定通过"
        elif abs_skew > skew_warn or abs_kurt > kurt_warn:
            verdict, basis = "skewed", "中等样本：|偏度| 或 |峰度| 超 warn 阈值"
        else:
            verdict, basis = "mild", "中等样本：SW 拒绝但形态偏离轻微"
    else:
        # 大样本：明确不看 P 值（SW 在 n 大时功效过强，必然拒绝）
        if moments_ok:
            verdict, basis = "normal", ("大样本(n=%d>%d)：不看 P 值，"
                                        "|偏度|<%.1f 且 |峰度|<%.1f" % (n, sw_max_n, skew_norm, kurt_norm))
        elif abs_skew > skew_warn or abs_kurt > kurt_warn:
            verdict, basis = "skewed", "大样本：|偏度| 或 |峰度| 超 warn 阈值"
        else:
            verdict, basis = "mild", "大样本：矩轻微偏离正态"

    advice = {
        "normal": "近似正态，无需变换，可直接入模",
        "mild": "轻微软偏离；BLUP 混合模型对此稳健，无需变换，建议结合 Q-Q 图确认",
        "skewed": "明显偏态/重尾；建议核查极端值是否为记录错误，"
                  "必要时对数或 Box-Cox 变换并注明还原口径；"
                  "若为双峰应回查群体结构（批次/品系未分层）而非变换",
        "insufficient": "样本量过小，不做正态性判定",
    }[verdict]
    base["Verdict"] = verdict
    base["Basis"] = basis
    base["Advice"] = advice
    return base


# ===============================================================
# 三、共线性诊断
# ===============================================================

def _is_numeric_like(series):
    """判断一列是否为数值型（bool 视作数值）。

    直接用 pandas 官方类型判定，避免依赖 `dtype == object` 这种在新版
    pandas（字符串列已改为 StringDtype）下会误判的写法。
    """
    import pandas as pd

    try:
        return bool(pd.api.types.is_numeric_dtype(series)
                    or pd.api.types.is_bool_dtype(series))
    except Exception:
        return True


def _is_functionally_dependent(frame, col_a, col_b):
    """判断两个分类列是否存在函数依赖（一一映射 / 常量映射）。

    典型育种场景：批次与世代一一对应（一个世代只有一个批次），
    此时两列携带完全相同的信息，同时入模必然导致设计矩阵秩亏。
    """
    import pandas as pd

    try:
        pair = pd.DataFrame({"a": frame[col_a].astype(str),
                             "b": frame[col_b].astype(str)}).dropna()
        if pair.empty or pair["a"].nunique() <= 1 or pair["b"].nunique() <= 1:
            return False
        a_decides_b = bool((pair.groupby("a")["b"].nunique() <= 1).all())
        b_decides_a = bool((pair.groupby("b")["a"].nunique() <= 1).all())
        return a_decides_b or b_decides_a
    except Exception:
        return False


def _design_matrix(frame, columns):
    """构造设计矩阵：数值列直接用；分类列 one-hot（drop_first 避免完全共线）。

    返回 (matrix, names, dropped)；dropped 为因常量/全缺失被剔除的列名。
    """
    import numpy as np
    import pandas as pd

    parts, names, dropped = [], [], []
    for col in columns:
        if col not in frame.columns:
            dropped.append(col)
            continue
        raw = frame[col]
        # 分类判定优先看 dtype 语义，不能只认 object：
        # pandas >=2.0/3.0 字符串列 dtype 为 StringDtype（str(dtype)=='str'），
        # 既 != object 也不 == 'string'，若漏判会被当数值列转 NaN 后整列丢弃，
        # 导致世代/批次/性别等分类固定效应完全不参与共线性诊断。
        is_categorical = not _is_numeric_like(raw)
        if not is_categorical:
            numeric = pd.to_numeric(raw, errors="coerce")
            n_unique = int(numeric.dropna().nunique())
            # 数值型但取值极少（如 Gen 编码为 1/2/3）也按分类处理
            if 0 < n_unique <= 10 and n_unique < max(3, len(raw) // 20):
                is_categorical = True
        if is_categorical:
            dummies = pd.get_dummies(raw.astype(str), prefix=str(col),
                                     drop_first=True, dtype=float)
            if dummies.shape[1] == 0:
                dropped.append(col)
                continue
            for dcol in dummies.columns:
                vec = dummies[dcol].to_numpy(dtype="float64")
                if not np.all(np.isnan(vec)) and np.nanstd(vec) > 0:
                    parts.append(vec)
                    names.append(str(dcol))
                else:
                    dropped.append(str(dcol))
        else:
            vec = numeric.to_numpy(dtype="float64")
            if np.all(np.isnan(vec)):
                dropped.append(col)
                continue
            # 数值列缺失用列均值填补（VIF 需要完整矩阵）
            col_mean = np.nanmean(vec)
            vec = np.where(np.isnan(vec), col_mean, vec)
            if np.nanstd(vec) <= 0:      # 常量列无法参与 VIF
                dropped.append(col)
                continue
            parts.append(vec)
            names.append(str(col))
    if not parts:
        return None, [], dropped
    matrix = np.column_stack(parts)
    return matrix, names, dropped


def collinearity(frame, columns, config=None):
    """共线性诊断：相关矩阵 + VIF + 条件数。

    返回 dict：
      Correlation  {col: {col: r}}        仅数值列两两 Pearson
      VIF          {变量: 值}
      ConditionNumber  条件数 κ
      Warnings     [{level, kind, detail, suggestion}]
      Action       配置动作（warn / block）
      Degraded     降级原因（如 numpy 计算失败），正常 None
    """
    import numpy as np
    import pandas as pd

    opt = _section(config, "collinearity")
    corr_warn = float(opt.get("corr_warn", 0.8))
    corr_severe = float(opt.get("corr_severe", 0.95))
    vif_warn = float(opt.get("vif_warn", 5))
    vif_severe = float(opt.get("vif_severe", 10))
    cond_severe = float(opt.get("condition_number_severe", 30))
    identical = float(opt.get("identical_corr", 0.999))
    action = str(opt.get("action", "warn"))

    result = {"Correlation": {}, "VIF": {}, "ConditionNumber": None,
              "Warnings": [], "Action": action, "Degraded": None}

    usable = [c for c in columns if c in frame.columns]
    if len(usable) < 2:
        result["Degraded"] = "可用变量不足 2 个，跳过共线性诊断"
        return result

    # ---- 相关矩阵（数值列两两 Pearson）----
    try:
        numeric_frame = frame[usable].apply(pd.to_numeric, errors="coerce")
        corr = numeric_frame.corr(min_periods=3)
        result["Correlation"] = {
            str(a): {str(b): (None if pd.isna(v) else round(float(v), 6))
                     for b, v in row.items()}
            for a, row in corr.iterrows()
        }
    except Exception as exc:
        result["Degraded"] = "相关矩阵计算失败：%s" % exc

    # ---- VIF 与条件数（基于 one-hot 后的设计矩阵）----
    matrix, names, dropped = _design_matrix(frame, usable)
    if matrix is None or matrix.shape[1] < 2:
        result["Degraded"] = (result["Degraded"] or "") + " 设计矩阵列不足，跳过 VIF/条件数"
        return result

    try:
        # 中心化后逐列 OLS 求 R² → VIF
        centered = matrix - matrix.mean(axis=0)
        vif_out = {}
        for j, name in enumerate(names):
            y = centered[:, j]
            others = np.delete(centered, j, axis=1)
            if others.shape[1] == 0:
                vif_out[name] = 1.0
                continue
            beta, *_ = np.linalg.lstsq(others, y, rcond=None)
            pred = others @ beta
            ss_res = float(((y - pred) ** 2).sum())
            ss_tot = float((y ** 2).sum())
            if ss_tot <= 0:
                vif_out[name] = 1.0
                continue
            r2 = 1.0 - ss_res / ss_tot
            vif_out[name] = round(float("inf"), 4) if r2 >= 1.0 else round(1.0 / (1.0 - r2), 4)
        result["VIF"] = vif_out

        # 条件数：标准化后矩阵的 cond
        std = matrix.std(axis=0)
        std[std <= 0] = 1.0
        z = (matrix - matrix.mean(axis=0)) / std
        result["ConditionNumber"] = round(float(np.linalg.cond(z)), 4)
    except Exception as exc:
        result["Degraded"] = (result["Degraded"] or "") + " VIF/条件数计算失败：%s" % exc

    # ---- 告警汇总（默认仅告警，不阻断）----
    def _fmt_vif(value):
        # 完全共线时 R²=1，VIF 为无穷大；报告里写成 ∞ 更直观
        if value == float("inf"):
            return "∞"
        return "%.4f" % float(value)

    warnings = []
    for name, vif in result["VIF"].items():
        if vif == float("inf") or vif >= vif_severe:
            warnings.append({"level": "severe", "kind": "VIF",
                             "detail": "%s VIF=%s" % (name, _fmt_vif(vif)),
                             "suggestion": "该变量与其余变量严重共线，不可同时入模（删列或合并）"})
        elif vif >= vif_warn:
            warnings.append({"level": "warn", "kind": "VIF",
                             "detail": "%s VIF=%s" % (name, _fmt_vif(vif)),
                             "suggestion": "共线性偏高，建议确认是否保留"})

    seen = set()
    for a, row in result["Correlation"].items():
        for b, r in row.items():
            if a == b or r is None:
                continue
            key = tuple(sorted((a, b)))
            if key in seen:
                continue
            seen.add(key)
            ar = abs(r)
            if ar >= identical:
                warnings.append({"level": "severe", "kind": "Corr",
                                 "detail": "%s 与 %s |r|=%.4f（近乎恒等）" % (a, b, ar),
                                 "suggestion": "两列数值近乎相同，须删去其一"})
            elif ar >= corr_severe:
                warnings.append({"level": "severe", "kind": "Corr",
                                 "detail": "%s 与 %s |r|=%.4f" % (a, b, ar),
                                 "suggestion": "严重相关，须删去其一或合并"})
            elif ar >= corr_warn:
                warnings.append({"level": "warn", "kind": "Corr",
                                 "detail": "%s 与 %s |r|=%.4f" % (a, b, ar),
                                 "suggestion": "相关偏高，入模前请确认"})

    cond = result["ConditionNumber"]
    if cond is not None and cond >= cond_severe:
        warnings.append({"level": "severe", "kind": "ConditionNumber",
                         "detail": "条件数 κ=%.2f" % cond,
                         "suggestion": "设计矩阵病态，方差组分估计不可靠，须剔除共线变量"})

    # ---- 分类列冗余检测（相关系数测不到，须单独判）----
    # 场景：批次与世代一一对应、性别与某编码列重复等，Pearson r 无意义，
    # 但两列信息完全重叠，同时入模必致秩亏。
    cat_cols = [c for c in usable if not _is_numeric_like(frame[c])]
    for i in range(len(cat_cols)):
        for j in range(i + 1, len(cat_cols)):
            a, b = cat_cols[i], cat_cols[j]
            if not _is_functionally_dependent(frame, a, b):
                continue
            warnings.append({
                "level": "severe", "kind": "CategoryDependency",
                "detail": "分类变量 %s 与 %s 存在一一映射（信息完全重叠）" % (a, b),
                "suggestion": "两者不可同时入模，保留生物学层级更合理的一个（通常保留批次/世代其一）"})

    result["Warnings"] = warnings
    return result


# ===============================================================
# 四、汇总入口
# ===============================================================

def build_exploration(frame, traits, config=None, group_columns=None):
    """一次性产出表型数据探索结果块。

    参数：
      frame          表型 DataFrame（已标准化列名）
      traits         目标性状列名列表
      config         exploration 配置段（None 时自动加载 phenotype-qc.yaml）
      group_columns  分组字段（None 时用 statistics.group_by_candidates）

    返回 dict：
      Enabled / Descriptors / Groups / Normality / Collinearity
      Warnings / Degraded
    """
    if config is None:
        config = load_exploration_config()
    if not (config or {}).get("enabled", True):
        return {"Enabled": False, "Warnings": [], "Degraded": ["探索环节已关闭（enabled=false）"]}

    degraded = []
    if _scipy_stats() is None:
        degraded.append("scipy 缺失：分布检验降级为仅偏度/峰度判定（已按软依赖策略跳过 SW/KS）")

    descriptors, groups, normality_out = {}, {}, {}
    for trait in traits:
        if trait not in frame.columns:
            continue
        descriptors[trait] = describe_extended(frame[trait], config)
        if group_columns:
            groups[trait] = describe_grouped_extended(frame, trait, group_columns)
        normality_out[trait] = normality(frame[trait], config)

    # 共线性：固定效应 + 协变量 + 性状（凡可能入模者）
    candidate_cols = list(dict.fromkeys(
        [c for c in (group_columns or []) if c in frame.columns] +
        [t for t in traits if t in frame.columns]))
    collinearity_out = collinearity(frame, candidate_cols, config)

    warnings = list(collinearity_out.get("Warnings", []))
    for trait, item in normality_out.items():
        if item.get("Verdict") == "skewed":
            warnings.append({"level": "warn", "kind": "Distribution",
                             "detail": "%s：%s" % (trait, item.get("Basis", "")),
                             "suggestion": item.get("Advice", "")})
    for trait, desc in descriptors.items():
        total = desc.get("N") or 0
        out3 = desc.get("Outlier3SD")
        if out3 and total and out3 / total > 0.05:
            warnings.append({"level": "warn", "kind": "Outlier",
                             "detail": "%s：超3SD个体 %d/%d (%.1f%%)" % (
                                 trait, out3, total, out3 / total * 100),
                             "suggestion": "离群占比 >5%，疑测量系统性问题，建议核查"})

    return {
        "Enabled": True,
        "Descriptors": descriptors,
        "Groups": groups,
        "Normality": normality_out,
        "Collinearity": collinearity_out,
        "Warnings": warnings,
        "Degraded": degraded,
    }
