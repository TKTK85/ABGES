# -*- coding: utf-8 -*-
"""结果报告学术图生成（Nature 风格黑白灰阶、matplotlib 软依赖）。

输入育种洞察 insights 与引擎结果 parsed，输出 300dpi PNG 到运行目录，
返回 {图键: 路径}。matplotlib 未安装或任一图绘制异常都不阻断报告主流程：
哪张图失败就缺哪张，由渲染层据实决定是否插图。图内一律英文标注，避免
运行环境缺中文字体导致方块乱码；中文图题由 Word 报告承载。
"""

import os
import logging
import traceback

logger = logging.getLogger(__name__)


def _matplotlib():
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib import rcParams
        # 外文（英文/数字）统一 Times New Roman 衬线，与 Word 正文保持一致；
        # 中文不进图（由 Word 图题承载），故无需中文字体
        rcParams["font.family"] = "serif"
        rcParams["font.serif"] = ["Times New Roman", "DejaVu Serif"]
        rcParams["mathtext.fontset"] = "stix"
        rcParams["axes.unicode_minus"] = False
        rcParams["font.size"] = 9
        return plt
    except Exception:
        return None


# NPG 学术彩色调色板（色盲友好、印刷友好）：主序列深蓝、强调朱红、浅色填充浅蓝；
# 零参考线用灰蓝、数据标签/误差棒用近黑，保证彩色图在打印与灰度备份下仍清晰。
C_PRIMARY = "#3C5488"   # 深蓝：主序列（EBV、主柱、主散点）
C_ACCENT = "#E64B35"    # 朱红：对比/强调序列、参考曲线
C_TEAL = "#00A087"      # 青绿：备用次序列
C_SKY = "#4DBBD5"       # 浅蓝：浅色填充柱/直方图
C_BAND = "#DCE6F2"      # 浅蓝带：置信区间背景
C_MUTE = "#7F8FA6"      # 灰蓝：零值参考线
_INK = "#222222"        # 近黑：误差棒与数据标签
_CE = "#FFFFFF"         # 柱体白色分隔边


def _log_fig_diag(prefix, name, exc_tb):
    """图表生成异常落盘留痕（对标 docx_report._log_report_diag）。

    图表是软依赖、缺图不阻断报告，但**异常情况必须可复查**，
    否则会出现「报告判完成、图一张没有」而无法追溯的局面。
    """
    try:
        with open(str(prefix) + "_报告诊断.log", "a", encoding="utf-8") as handle:
            handle.write("==== [%s] 图表生成异常（已降级，不影响评估结论）====\n%s\n"
                         % (name, exc_tb))
    except Exception:
        pass
    logger.warning("图表 %s 生成失败，已降级（详见报告诊断日志）", name)


def missing_figures(expected, paths):
    """返回「应有但实有缺失」的图键列表，供报告显式标注。"""
    return [key for key in expected if key not in (paths or {})]


def _ebv_series(parsed):
    try:
        import pandas as pd
        records = parsed.get("rand") or []
        if not records:
            return None, None
        frame = pd.DataFrame(records)
        excluded = {"ID", "id", "residuals", "residual"}
        eff = None
        for name in frame.columns:
            low = str(name).lower()
            if low in excluded or low in ("e",) or low.endswith(("_se", "_pev", "_r2")):
                continue
            eff = name
            break
        if eff is None:
            return None, None
        series = pd.to_numeric(frame[eff], errors="coerce").dropna()
        return eff, series
    except Exception:
        return None, None


def _norm_ppf(prob):
    """标准正态分位函数（Acklam 有理近似，纯 numpy，精度足够绘图）。"""
    import numpy as np
    p = np.asarray(prob, dtype="float64")
    p = np.clip(p, 1e-9, 1 - 1e-9)
    a = [-39.69683028665376, 220.9460984245205, -275.9285104469687,
         138.3577518672690, -30.66479806614716, 2.506628277459239]
    b = [-54.47609879822406, 161.5858368580409, -155.6989798598866,
         66.80131188771972, -13.28068155288572]
    c = [-0.007784894002430293, -0.3223964580411365, -2.400758277161838,
         -2.549732539343734, 4.374664141464968, 2.938163982698783]
    d = [0.007784695709041462, 0.3224671290700398, 2.445134137142996,
         3.754408661907416]
    plow, phigh = 0.02425, 1 - 0.02425
    x = np.zeros_like(p)
    m = (p > plow) & (p < phigh)
    q = p[m] - 0.5
    r = q * q
    x[m] = (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / \
        (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)
    ml = p <= plow
    q = np.sqrt(-2 * np.log(p[ml]))
    x[ml] = (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
        ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    mh = p >= phigh
    q = np.sqrt(-2 * np.log(1 - p[mh]))
    x[mh] = -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
        ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    return x


def _residual_frame(parsed, trait, engine_pheno_path):
    """返回 (residual, fitted) numpy 数组；残差来自 .rand，fitted=y-残差。"""
    try:
        import pandas as pd
        import numpy as np
        records = parsed.get("rand") or []
        if not records or not engine_pheno_path or not trait:
            return None, None
        rand = pd.DataFrame(records)
        if "ID" not in rand or "residuals" not in rand:
            return None, None
        eng = pd.read_csv(engine_pheno_path, sep="\t")
        if "ID" not in eng or trait not in eng:
            return None, None
        rr = rand[["ID", "residuals"]].copy()
        rr["residuals"] = pd.to_numeric(rr["residuals"], errors="coerce")
        ee = eng[["ID", trait]].copy()
        ee[trait] = pd.to_numeric(ee[trait], errors="coerce")
        m = rr.merge(ee, on="ID", how="inner").dropna(subset=["residuals", trait])
        if len(m) < 8:
            return None, None
        resid = m["residuals"].to_numpy(dtype="float64")
        fitted = (m[trait] - m["residuals"]).to_numpy(dtype="float64")
        return resid, fitted
    except Exception:
        return None, None


def _fmt_comp(v):
    """方差组分等数值的自适应格式：大值两位小数，小值增加位数，避免非零值显示成 0.00。"""
    try:
        a = abs(float(v))
    except (TypeError, ValueError):
        return str(v)
    if a == 0:
        return "0"
    if a >= 1:
        return "%.2f" % v
    if a >= 0.01:
        return "%.3f" % v
    if a >= 1e-4:
        return "%.4f" % v
    return "%.2e" % v


def render_figures(parsed, insights, out_dir, dpi=300, engine_pheno_path=None,
                   trait=None):
    """生成学术图，返回 {key: path}；全部软失败。图1-4 依赖 parsed/insights，
    图5 残差诊断四联图另需引擎表型路径与目标性状名以还原拟合值。"""
    plt = _matplotlib()
    if plt is None:
        return {}
    os.makedirs(out_dir, exist_ok=True)
    paths = {}

    # ---- 图1：方差组分(±SE) + 遗传力点估计与95%CI ----
    try:
        var = insights.get("variance") or {}
        rows = [r for r in var.get("rows") if not r["name"].startswith("表型")]
        if rows:
            fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(7.2, 2.9),
                                           gridspec_kw={"width_ratios": [1.1, 1.0]})
            labels = ["VA (additive)", "VE (residual)"][:len(rows)]
            vals = [r["value"] for r in rows]
            errs = [r["se"] for r in rows]
            bars = ax1.bar(labels, vals, yerr=errs, capsize=4,
                           color=[C_PRIMARY, C_SKY], edgecolor=_CE, linewidth=0.8,
                           error_kw={"ecolor": _INK, "elinewidth": 1.0})
            for b, v, e in zip(bars, vals, errs):
                ymax = max((x or 0) + (y or 0) for x, y in zip(vals, errs))
                ax1.text(b.get_x() + b.get_width() / 2,
                         (v or 0) + (e or 0) + ymax * 0.03, _fmt_comp(v),
                         ha="center", va="bottom", fontsize=8, color=_INK)
            ax1.set_ylabel("Variance component")
            ax1.spines[["top", "right"]].set_visible(False)
            h2, lo, hi = var.get("h2"), var.get("ci_lower"), var.get("ci_upper")
            if h2 is not None and lo is not None and hi is not None:
                ax2.errorbar([1], [h2], yerr=[[h2 - lo], [hi - h2]],
                             fmt="o", color=C_ACCENT, ecolor=_INK,
                             capsize=6, markersize=6, elinewidth=1.2)
                ax2.axvspan(0.6, 1.4, color=C_BAND, zorder=0)
                ax2.set_xlim(0.4, 1.6); ax2.set_xticks([])
                # 数值标注统一为带白底的文本块，脱离数据点与误差棒，杜绝数字压住图形：
                # 优先置于误差棒正上方并留清晰间隙；顶部不足（高遗传力）时转下方；
                # 上下都不足时置于点左侧空白处兜底。
                _top = min(1.0, max(hi + 0.22, 0.55))
                ax2.set_ylim(0, _top)
                _txt = "h² = %.3f\n95%% CI [%.3f, %.3f]" % (h2, lo, hi)
                _bbox = dict(boxstyle="round,pad=0.28", fc="white",
                             ec="#BFBFBF", lw=0.6, alpha=0.96)
                if _top - hi >= 0.16:
                    ax2.text(1, hi + _top * 0.05, _txt, ha="center", va="bottom",
                             fontsize=8, color=_INK, linespacing=1.35, bbox=_bbox)
                elif lo >= 0.16:
                    ax2.text(1, lo - _top * 0.05, _txt, ha="center", va="top",
                             fontsize=8, color=_INK, linespacing=1.35, bbox=_bbox)
                else:
                    ax2.text(0.43, h2, _txt, ha="left", va="center",
                             fontsize=8, color=_INK, linespacing=1.35, bbox=_bbox)
                ax2.set_ylabel("Heritability h²")
            ax2.spines[["top", "right"]].set_visible(False)
            fig.tight_layout()
            p = os.path.join(out_dir, "fig1_variance_h2.png")
            fig.savefig(p, dpi=dpi, bbox_inches="tight"); plt.close(fig)
            paths["variance"] = p
    except Exception as _fig_exc:
        plt.close("all")
        _log_fig_diag(output_prefix, "fig1_variance_h2", traceback.format_exc())

    # ---- 图2：全体 EBV 分布直方图 ----
    try:
        eff, series = _ebv_series(parsed)
        if series is not None and len(series) > 1:
            import numpy as np
            fig, ax = plt.subplots(figsize=(6.4, 2.9))
            n_arr, bins, _ = ax.hist(series, bins=60, color=C_SKY,
                                     edgecolor=_CE, linewidth=0.4,
                                     label="Observed")
            mean_v = series.mean()
            sd_v = series.std(ddof=1)
            # 同均值、同标准差的正态拟合曲线；密度按 n×组距 缩放到频数尺度，方可与直方图同图比较
            if sd_v and sd_v > 0:
                binw = bins[1] - bins[0]
                xs = np.linspace(series.min(), series.max(), 400)
                pdf = np.exp(-0.5 * ((xs - mean_v) / sd_v) ** 2) / (
                    sd_v * np.sqrt(2 * np.pi))
                ax.plot(xs, pdf * len(series) * binw, color=C_PRIMARY,
                        linewidth=1.8,
                        label="Normal fit N(%.2f, %.2f²)" % (mean_v, sd_v))
            ax.axvline(0, color=C_MUTE, linestyle="--", linewidth=1.0)
            ax.axvline(mean_v, color=C_ACCENT, linestyle="-", linewidth=1.4)
            ax.text(mean_v, ax.get_ylim()[1] * 0.95,
                    "mean=%.2f, SD=%.2f" % (mean_v, sd_v),
                    ha="center", va="top", fontsize=8, color=_INK)
            ax.set_xlabel("Estimated breeding value")
            ax.set_ylabel("Number of individuals")
            ax.legend(fontsize=7, frameon=False, loc="upper right")
            ax.spines[["top", "right"]].set_visible(False)
            fig.tight_layout()
            p = os.path.join(out_dir, "fig2_ebv_dist.png")
            fig.savefig(p, dpi=dpi, bbox_inches="tight"); plt.close(fig)
            paths["ebv_dist"] = p
    except Exception as _fig_exc:
        plt.close("all")
        _log_fig_diag(output_prefix, "fig2_ebv_dist", traceback.format_exc())

    # ---- 图3：分世代遗传趋势（EBV±SE 主轴，表型均值次轴）----
    try:
        trend = insights.get("trend_rows") or []
        if len(trend) >= 2:
            gens = [r["gen"] for r in trend]
            means = [r["mean_ebv"] for r in trend]
            ses = [r.get("se_ebv") or 0 for r in trend]
            fig, ax = plt.subplots(figsize=(6.6, 3.0))
            ax.errorbar(gens, means, yerr=ses, fmt="-o", color=C_PRIMARY,
                        ecolor=C_PRIMARY, capsize=3, markersize=5, linewidth=1.6,
                        elinewidth=1.0, label="Mean EBV (±SE)")
            for g, m, r in zip(gens, means, trend):
                ax.text(g, m + 0.18, "%.2f" % m, ha="center", va="bottom",
                        fontsize=7.5, color=C_PRIMARY)
                # 样本量标注放在坐标轴外下方，避免与低世代数据点重叠
                ax.annotate("n=%d\n(valid %d)" % (r["n_total"], r["n_valid"]),
                            xy=(g, 0), xycoords=ax.get_xaxis_transform(),
                            xytext=(0, -15), textcoords="offset points",
                            ha="center", va="top", fontsize=6.8, color=C_MUTE)
            phes = [(r["gen"], r["mean_phe"]) for r in trend if r.get("mean_phe") is not None]
            if phes:
                ax2 = ax.twinx()
                pg = [x[0] for x in phes]; pv = [x[1] for x in phes]
                ax2.plot(pg, pv, "--s", color=C_ACCENT, markersize=5,
                         label="Mean phenotype")
                ax2.set_ylabel("Mean phenotype")
                ax2.spines[["top"]].set_visible(False)
            ax.set_xlabel("Generation")
            ax.set_ylabel("Mean EBV")
            ax.set_xticks(gens)
            ax.spines[["top"]].set_visible(False)
            fig.subplots_adjust(bottom=0.24, left=0.1, right=0.9, top=0.95)
            p = os.path.join(out_dir, "fig3_genetic_trend.png")
            fig.savefig(p, dpi=dpi, bbox_inches="tight"); plt.close(fig)
            paths["trend"] = p
    except Exception as _fig_exc:
        plt.close("all")
        _log_fig_diag(output_prefix, "fig3_genetic_trend", traceback.format_exc())

    # ---- 图4：分世代个体数与有效记录数（数据结构）----
    try:
        trend = insights.get("trend_rows") or []
        if len(trend) >= 2:
            import numpy as np
            gens = [str(r["gen"]) for r in trend]
            totals = [r["n_total"] for r in trend]
            valids = [r["n_valid"] for r in trend]
            x = np.arange(len(gens))
            fig, ax = plt.subplots(figsize=(6.6, 2.9))
            ax.bar(x - 0.2, totals, width=0.4, color=C_SKY,
                   edgecolor=_CE, linewidth=0.6, label="All individuals")
            ax.bar(x + 0.2, valids, width=0.4, color=C_PRIMARY,
                   edgecolor=_CE, linewidth=0.6, label="With valid record")
            for xi, t in zip(x, totals):
                ax.text(xi - 0.2, t, str(t), ha="center", va="bottom", fontsize=7)
            for xi, v in zip(x, valids):
                ax.text(xi + 0.2, v, str(v), ha="center", va="bottom", fontsize=7)
            ax.set_xticks(x); ax.set_xticklabels(gens)
            ax.set_xlabel("Generation")
            ax.set_ylabel("Number of individuals")
            ax.legend(frameon=False, fontsize=8, ncol=2,
                      loc="lower center", bbox_to_anchor=(0.5, 1.0))
            ax.spines[["top", "right"]].set_visible(False)
            fig.subplots_adjust(bottom=0.13, left=0.11, right=0.97, top=0.86)
            p = os.path.join(out_dir, "fig4_data_structure.png")
            fig.savefig(p, dpi=dpi, bbox_inches="tight"); plt.close(fig)
            paths["structure"] = p
    except Exception as _fig_exc:
        plt.close("all")
        _log_fig_diag(output_prefix, "fig4_data_structure", traceback.format_exc())

    # ---- 图5：模型残差诊断四联图（对应线性模型 plot(model) 的经典四视图）----
    try:
        resid, fitted = _residual_frame(parsed, trait, engine_pheno_path)
        if resid is not None and len(resid) > 8:
            import numpy as np
            n = len(resid)
            fig, axs = plt.subplots(2, 2, figsize=(7.2, 6.0))
            # (a) 残差直方图 + N(mu,sd^2) 正态密度参考
            ax = axs[0, 0]
            mu, sd = resid.mean(), resid.std(ddof=1)
            ax.hist(resid, bins=40, density=True, color=C_SKY,
                    edgecolor=_CE, linewidth=0.4)
            xs = np.linspace(resid.min(), resid.max(), 200)
            dens = np.exp(-0.5 * ((xs - mu) / sd) ** 2) / (sd * np.sqrt(2 * np.pi))
            ax.plot(xs, dens, color=C_ACCENT, linewidth=1.6)
            ax.axvline(0, color=C_MUTE, linestyle="--", linewidth=0.9)
            ax.set_xlabel("Residual"); ax.set_ylabel("Density")
            ax.spines[["top", "right"]].set_visible(False)
            # (b) 残差 vs 拟合值（分箱均值平滑，检视异方差/系统趋势）
            ax = axs[0, 1]
            ax.scatter(fitted, resid, s=4, color=C_PRIMARY, alpha=0.32,
                       edgecolors="none", rasterized=True)
            ax.axhline(0, color=C_MUTE, linestyle="--", linewidth=0.9)
            order = np.argsort(fitted)
            fo, ro = fitted[order], resid[order]
            nb = min(20, max(8, n // 120))
            edges = np.linspace(fo.min(), fo.max(), nb + 1)
            bin_id = np.clip(np.digitize(fo, edges) - 1, 0, nb - 1)
            bx, by = [], []
            for k in range(nb):
                mk = bin_id == k
                if mk.sum() >= 3:
                    bx.append(fo[mk].mean()); by.append(ro[mk].mean())
            if bx:
                ax.plot(bx, by, color=C_ACCENT, linewidth=1.8)
            ax.set_xlabel("Fitted value"); ax.set_ylabel("Residual")
            ax.spines[["top", "right"]].set_visible(False)
            # (c) 标准化残差正态 Q-Q（虚线为 y=x 正态参考）
            ax = axs[1, 0]
            sr = np.sort((resid - mu) / sd)
            prob = (np.arange(1, n + 1) - 0.5) / n
            tq = _norm_ppf(prob)
            ax.scatter(tq, sr, s=4, color=C_PRIMARY, alpha=0.38,
                       edgecolors="none", rasterized=True)
            lo = min(tq.min(), sr.min()); hi = max(tq.max(), sr.max())
            ax.plot([lo, hi], [lo, hi], color=C_ACCENT, linestyle="--", linewidth=1.2)
            ax.set_xlabel("Expected normal quantile")
            ax.set_ylabel("Standardized residual")
            ax.spines[["top", "right"]].set_visible(False)
            # (d) 残差 vs 观测序号（检视顺序相关/漂移）
            ax = axs[1, 1]
            ax.scatter(np.arange(1, n + 1), resid, s=4, color=C_PRIMARY, alpha=0.32,
                       edgecolors="none", rasterized=True)
            ax.axhline(0, color=C_MUTE, linestyle="--", linewidth=0.9)
            ax.set_xlabel("Index"); ax.set_ylabel("Residual")
            ax.spines[["top", "right"]].set_visible(False)
            fig.tight_layout()
            p = os.path.join(out_dir, "fig5_residual_diagnostics.png")
            fig.savefig(p, dpi=dpi, bbox_inches="tight"); plt.close(fig)
            paths["residual"] = p
    except Exception as _fig_exc:
        plt.close("all")
        _log_fig_diag(output_prefix, "fig5_residual_diagnostics", traceback.format_exc())

    return paths


def _fig_label(name):
    """图内文字一律 ASCII：matplotlib 默认字体无中文字形，中文会渲染成方框。

    引擎表型列名在流水线中已做 ASCII 消毒，此处只是兜底——万一有中文
    列名漏到图上，宁可显示为可辨认的替代字符，也不要出现乱码方块。
    """
    text = str(name)
    try:
        text.encode("ascii")
        return text
    except UnicodeEncodeError:
        return text.encode("ascii", "replace").decode("ascii")


def render_exploration_figures(frame, traits, exploration, out_dir, dpi=300,
                               group_column=None):
    """生成表型数据探索图，返回 {key: path}；全部软失败（matplotlib 缺失返回 {}）。

    与 render_figures 同风格：Nature 配色、图内一律英文标注（中文图题由 Word
    报告承载）、每张图独立 try/except、失败即缺该图而不阻断报告主流程。

    图键：
      qq     —— 各性状正态 Q-Q 图（多子图，最多 6 个性状）
      corr   —— 变量相关矩阵热力图
      vif    —— VIF 横向条形图（含 warn=5 / severe=10 阈值线）
      trend  —— 性状按分组字段的均值 ± SD 趋势（多子图）
    """
    plt = _matplotlib()
    if plt is None:
        return {}
    os.makedirs(out_dir, exist_ok=True)
    paths = {}
    coll = (exploration or {}).get("Collinearity", {}) or {}

    # ---- 探索图1：正态 Q-Q 图（偏离直线即非正态）----
    try:
        import numpy as np
        import pandas as pd
        cols = [t for t in traits if t in frame.columns][:6]
        if cols:
            ncol = min(3, len(cols))
            nrow = (len(cols) + ncol - 1) // ncol
            fig, axs = plt.subplots(nrow, ncol, figsize=(3.1 * ncol, 2.7 * nrow),
                                    squeeze=False)
            for i, col in enumerate(cols):
                ax = axs[i // ncol][i % ncol]
                x = pd.to_numeric(frame[col], errors="coerce").dropna().to_numpy()
                if x.size < 3 or x.std(ddof=1) == 0:
                    ax.axis("off")
                    continue
                sr = np.sort((x - x.mean()) / x.std(ddof=1))
                prob = (np.arange(1, x.size + 1) - 0.5) / x.size
                tq = _norm_ppf(prob)
                ax.scatter(tq, sr, s=3, color=C_PRIMARY, alpha=0.35,
                           edgecolors="none", rasterized=True)
                lo = min(tq.min(), sr.min())
                hi = max(tq.max(), sr.max())
                ax.plot([lo, hi], [lo, hi], color=C_ACCENT, linestyle="--",
                        linewidth=1.1)
                ax.set_title(str(col), fontsize=8.5)
                ax.set_xlabel("Normal quantile", fontsize=7.5)
                ax.set_ylabel("Standardized", fontsize=7.5)
                ax.tick_params(labelsize=6.5)
                ax.spines[["top", "right"]].set_visible(False)
            for j in range(len(cols), nrow * ncol):
                axs[j // ncol][j % ncol].axis("off")
            fig.tight_layout()
            p = os.path.join(out_dir, "fig_explore_qq.png")
            fig.savefig(p, dpi=dpi, bbox_inches="tight")
            plt.close(fig)
            paths["qq"] = p
    except Exception as _fig_exc:
        plt.close("all")
        _log_fig_diag(output_prefix, "fig_explore_qq", traceback.format_exc())

    # ---- 探索图2：相关矩阵热力图 ----
    try:
        import numpy as np
        corr = coll.get("Correlation") or {}
        names = list(corr.keys())
        if len(names) >= 2:
            matrix = np.array(
                [[(corr[a].get(b) if corr[a].get(b) is not None else np.nan)
                  for b in names] for a in names], dtype="float64")
            # 分类变量（世代/批次/性别等）两两之间 Pearson 相关无意义，
            # 会被算成全 NaN；若留在图里就是整行整列空白，既难看也易误读。
            # 故只保留至少存在一个有效相关的变量（即数值变量）。
            keep = [i for i, a in enumerate(names)
                    if any(corr[a].get(b) is not None for b in names if b != a)]
            if len(keep) >= 2:
                names = [names[i] for i in keep]
                matrix = matrix[np.ix_(keep, keep)]
            else:
                names = []
            w = max(4.0, 0.62 * len(names) + 2.0)
            h = max(3.2, 0.55 * len(names) + 1.6)
            fig, ax = plt.subplots(figsize=(w, h))
            im = ax.imshow(matrix, cmap="RdBu_r", vmin=-1, vmax=1)
            ax.set_xticks(range(len(names)))
            ax.set_xticklabels([_fig_label(n) for n in names],
                               rotation=45, ha="right", fontsize=7)
            ax.set_yticks(range(len(names)))
            ax.set_yticklabels([_fig_label(n) for n in names], fontsize=7)
            for i in range(len(names)):
                for j in range(len(names)):
                    v = matrix[i, j]
                    if not np.isnan(v):
                        ax.text(j, i, "%.2f" % v, ha="center", va="center",
                                fontsize=5.6,
                                color="white" if abs(v) > 0.6 else _INK)
            fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04)
            ax.set_title("Pearson correlation", fontsize=9)
            fig.tight_layout()
            p = os.path.join(out_dir, "fig_explore_corr.png")
            fig.savefig(p, dpi=dpi, bbox_inches="tight")
            plt.close(fig)
            paths["corr"] = p
    except Exception as _fig_exc:
        plt.close("all")
        _log_fig_diag(output_prefix, "fig_explore_corr", traceback.format_exc())

    # ---- 探索图3：VIF 条形图（含阈值线）----
    try:
        import numpy as np
        vif = coll.get("VIF") or {}
        if vif:
            # 排序：完全共线（VIF=∞）置顶，其后按 VIF 由大到小——与文中 VIF 表口径一致
            items = sorted(
                vif.items(),
                key=lambda kv: (0, 0.0) if kv[1] == float("inf") else (1, -float(kv[1])))
            labels = [k for k, _ in items]
            # 极大值截断到 50，避免完全共线时柱形撑爆坐标
            vals = [50.0 if v == float("inf") else min(float(v), 50.0)
                    for _, v in items]
            fig, ax = plt.subplots(figsize=(6.2, max(2.4, 0.36 * len(labels) + 1.4)))
            ypos = np.arange(len(labels))
            ax.barh(ypos, vals, color=C_PRIMARY, edgecolor=_CE, linewidth=0.7)
            ax.axvline(5, color=C_ACCENT, linestyle="--", linewidth=1.0)
            ax.axvline(10, color="#8B0000", linestyle="-", linewidth=1.1)
            ax.set_yticks(ypos)
            ax.set_yticklabels([_fig_label(l) for l in labels], fontsize=7)
            ax.invert_yaxis()      # ypos=0（VIF 最大/∞）置于图顶
            ax.set_xlabel("VIF (capped at 50)", fontsize=8)
            ax.set_title("Variance inflation factor", fontsize=9)
            ax.text(5, -0.75, "warn=5", fontsize=6.5, color=C_ACCENT)
            ax.text(10, -0.75, "severe=10", fontsize=6.5, color="#8B0000")
            ax.spines[["top", "right"]].set_visible(False)
            fig.tight_layout()
            p = os.path.join(out_dir, "fig_explore_vif.png")
            fig.savefig(p, dpi=dpi, bbox_inches="tight")
            plt.close(fig)
            paths["vif"] = p
    except Exception as _fig_exc:
        plt.close("all")
        _log_fig_diag(output_prefix, "fig_explore_vif", traceback.format_exc())

    # ---- 探索图4：性状按分组字段的均值 ± SD 趋势 ----
    try:
        import numpy as np
        import pandas as pd
        if group_column and group_column in frame.columns:
            cols = [t for t in traits if t in frame.columns][:6]
            if cols:
                ncol = min(3, len(cols))
                nrow = (len(cols) + ncol - 1) // ncol
                fig, axs = plt.subplots(nrow, ncol, figsize=(3.1 * ncol, 2.6 * nrow),
                                        squeeze=False)
                for i, col in enumerate(cols):
                    ax = axs[i // ncol][i % ncol]
                    sub = frame[[group_column, col]].copy()
                    sub[col] = pd.to_numeric(sub[col], errors="coerce")
                    grouped = sub.groupby(sub[group_column].astype(str))[col]
                    mean_ = grouped.mean()
                    sd_ = grouped.std(ddof=1).fillna(0.0)
                    order = list(mean_.index)
                    xs = np.arange(len(order))
                    ax.plot(xs, mean_.to_numpy(), marker="o", color=C_PRIMARY,
                            linewidth=1.1, markersize=3.5)
                    ax.fill_between(xs, (mean_ - sd_).to_numpy(),
                                    (mean_ + sd_).to_numpy(),
                                    color=C_BAND, alpha=0.85)
                    ax.set_xticks(xs)
                    ax.set_xticklabels(order, fontsize=6.5)
                    ax.set_title(str(col), fontsize=8.5)
                    ax.set_ylabel("Mean ± SD", fontsize=7.5)
                    ax.tick_params(labelsize=6.5)
                    ax.spines[["top", "right"]].set_visible(False)
                for j in range(len(cols), nrow * ncol):
                    axs[j // ncol][j % ncol].axis("off")
                fig.tight_layout()
                p = os.path.join(out_dir, "fig_explore_trend.png")
                fig.savefig(p, dpi=dpi, bbox_inches="tight")
                plt.close(fig)
                paths["trend"] = p
    except Exception as _fig_exc:
        plt.close("all")
        _log_fig_diag(output_prefix, "fig_explore_trend", traceback.format_exc())

    return paths


def render_effect_figure(screening, out_dir, dpi=300, config=None):
    """生成效应筛选 η² 横向条形图：每个候选效应一列，按判定着色，含
    eta_squared_min（浅红虚线）与 eta_squared_strong（深红实线）阈值线。

    返回 path 或 None（matplotlib 缺失 / 无可用数据 / 异常均返回 None，
    由报告渲染层据实决定是否插图，绝不阻断主流程）。图内一律英文标注。

    着色规则：
      keep        —— 深蓝（核心/建议保留）
      drop        —— 朱红（建议不入模）
      review      —— 浅蓝（需人工确认）
      choose_one  —— 青绿（二选一）
    """
    plt = _matplotlib()
    if plt is None:
        return None
    try:
        import numpy as np
        import abges_effects as effects_tool
        # 阈值一律读自 phenotype-qc.yaml 的 effect_screening 段（与判定口径一致）
        cfg = effects_tool.load_effect_screening_config()
        eta_min = float(cfg.get("eta_squared_min", 0.01))
        eta_strong = float(cfg.get("eta_squared_strong", 0.05))
        candidates = (screening or {}).get("Candidates") or []
        items = []
        for c in candidates:
            ev = c.get("evidence") or {}
            eta = ev.get("eta_squared")
            if eta is None:
                continue
            items.append((str(c.get("column")), float(eta), c.get("verdict", "review")))
        if not items:
            return None
        items.sort(key=lambda x: -x[1])
        labels = [k for k, _, _ in items]
        vals = [v for _, v, _ in items]
        verdicts = [v for _, _, v in items]
        color_map = {"keep": C_PRIMARY, "drop": C_ACCENT,
                     "review": C_SKY, "choose_one": C_TEAL}
        colors = [color_map.get(v, C_MUTE) for v in verdicts]
        from matplotlib.patches import Patch
        fig, ax = plt.subplots(
            figsize=(6.2, max(2.4, 0.42 * len(labels) + 1.6)))
        ypos = np.arange(len(labels))
        ax.barh(ypos, vals, color=colors, edgecolor=_CE, linewidth=0.7)
        if eta_min > 0:
            ax.axvline(eta_min, color=C_ACCENT, linestyle="--", linewidth=1.0)
        if eta_strong > 0:
            ax.axvline(eta_strong, color="#8B0000", linestyle="-", linewidth=1.1)
        ax.set_yticks(ypos)
        ax.set_yticklabels([_fig_label(l) for l in labels], fontsize=7)
        ax.invert_yaxis()
        ax.set_xlabel("eta squared (variance explained)", fontsize=8)
        ax.set_title("Effect screening: variance explained by each candidate",
                     fontsize=9)
        ax.text(eta_min, -0.75, "keep>=%.2f" % eta_min,
                fontsize=6.5, color=C_ACCENT, ha="center")
        ax.text(eta_strong, -0.75, "strong>=%.2f" % eta_strong,
                fontsize=6.5, color="#8B0000", ha="center")
        handles = [Patch(color=color_map[k], label=k)
                   for k in ("keep", "drop", "review", "choose_one")]
        ax.legend(handles=handles, fontsize=6.5, loc="lower right",
                  frameon=False)
        ax.spines[["top", "right"]].set_visible(False)
        fig.tight_layout()
        os.makedirs(out_dir, exist_ok=True)
        p = os.path.join(out_dir, "fig_effect_eta2.png")
        fig.savefig(p, dpi=dpi, bbox_inches="tight")
        plt.close(fig)
        return p
    except Exception as _fig_exc:
        plt.close("all")
        _log_fig_diag(output_prefix, "fig_effect_eta2", traceback.format_exc())
        return None
