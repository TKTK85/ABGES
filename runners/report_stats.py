# -*- coding: utf-8 -*-
"""报告统计判据与判定文案（P2-3 从 docx_report.py 抽出）。

本模块只回答两类问题，不含任何 docx / 渲染逻辑，可独立单元测试：

  1. "某个统计量落在哪一档"——显著性、遗传力分档、偏度/峰度形态、二次曲率增量；
  2. "这一档该怎么写进报告"——对应的中文判定与建议文案。

【P2-3 阈值外置】此前 `p<0.05` / `|偏度|<0.5` / `|偏度|<1` 等阈值硬编码在
docx_report.py 的渲染主体里（``_p_sig`` 与残差诊断段落），要改口径必须动渲染代码。
现统一收敛到 ``DEFAULTS``，并支持由 ``configs/qc/phenotype-qc.yaml`` 的
``report_thresholds`` 段覆盖；**配置缺失或不可解析时回退默认值，且默认值与历史
口径逐值一致，故报告结论与版式零变更**。

注意：判定**分支**读配置；判定**文案**里的数字也由同一配置格式化生成，
避免"阈值改了、文字没改"的自相矛盾。

依赖：仅标准库 + 可选 PyYAML，无第三方硬依赖。
"""

from __future__ import annotations

import os
import logging
logger = logging.getLogger(__name__)
THRESHOLD_DIAGNOSTICS = {}

from runners.common import DASH, dash

try:  # PyYAML 为软依赖：缺失时静默回退默认阈值
    import yaml
except Exception:  # pragma: no cover - 环境相关
    yaml = None


# ------------------------------------------------------------------
# 一、阈值默认值（与历史实现逐值一致）
# ------------------------------------------------------------------
DEFAULTS = {
    # --- 显著性水平 ---
    "alpha": 0.05,           # P<0.05 视为显著
    "alpha_strong": 0.001,   # P<0.001 视为极显著

    # --- P 值显示口径：极小值给阈值，其余保留三位小数 ---
    "p_display_floor": 1e-4,
    "p_display_floor_2": 1e-3,
    "p_display_nd": 3,

    # --- 遗传力分档 ---
    "h2_low": 0.10,          # h²<0.10 低遗传力
    "h2_moderate": 0.30,     # 0.10≤h²<0.30 中等；≥0.30 中高

    # --- 离群个体占比告警线（超 3SD 个体占该性状有效记录数的比例）---
    "outlier_share_warn": 0.05,

    # --- 残差形态判据（列表展示用分档）---
    "skew_symmetric": 0.5,    # |偏度|<0.5 近似对称
    "skew_mild": 1.0,         # |偏度|<1 轻度，否则明显
    "kurt_near_normal": 0.5,  # |超额峰度|<0.5 视为接近正态

    # --- 近似正态综合判定（严格档 / 宽松档）---
    "shape_strict_skew": 0.5,
    "shape_strict_kurt": 1.0,
    "shape_loose_skew": 1.0,
    "shape_loose_kurt": 2.0,

    # --- 残差–拟合值二次项 R² 增量分档 ---
    "quad_gain_none": 0.01,   # <0.01 无明显系统弯曲
    "quad_gain_mild": 0.05,   # <0.05 轻度；≥0.05 明显且需处理

    # --- 残差均值"判定为零"的相对容差（乘以 sd）---
    "mean_zero_rel_tol": 1e-3,

    # --- 近交水平"极低"判定线（平均近交系数低于此值即表述为极低）---
    "inbreeding_low": 0.01,
}


def _config_path():
    """定位 configs/qc/phenotype-qc.yaml（相对本项目 ABGES 根目录）。"""
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here)          # ABGES/
    return os.path.join(root, "configs", "qc", "phenotype-qc.yaml")


def load_thresholds(path=None):
    """读取 report_thresholds 配置并与默认值合并。

    任一环节失败（文件缺失 / YAML 非法 / 结构不符）都回退到 DEFAULTS，
    不抛异常——报告渲染属软依赖链路，绝不能因配置问题阻断评估交付。
    """
    THRESHOLD_DIAGNOSTICS.clear()
    merged = dict(DEFAULTS)
    target = path or _config_path()
    THRESHOLD_DIAGNOSTICS.update(source=target, degraded=False)
    if yaml is None or not os.path.isfile(target):
        THRESHOLD_DIAGNOSTICS.update(degraded=True, reason="configuration_unavailable")
        logger.warning("Report thresholds unavailable; using defaults")
        return merged
    try:
        with open(target, "r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
    except Exception:
        THRESHOLD_DIAGNOSTICS.update(degraded=True, reason="configuration_invalid")
        logger.exception("Report thresholds invalid; using defaults")
        return merged
    block = data.get("report_thresholds") if isinstance(data, dict) else None
    if not isinstance(block, dict):
        THRESHOLD_DIAGNOSTICS.update(degraded=True, reason="thresholds_missing")
        logger.warning("Report threshold section missing; using defaults")
        return merged
    for key, value in block.items():
        if key in merged and isinstance(value, (int, float)) \
                and not isinstance(value, bool):
            merged[key] = type(merged[key])(value)
    return merged


# 模块加载时取一份快照；渲染期读取，避免每次调用重复解析 YAML
THRESHOLDS = load_thresholds()


def get(name):
    """取阈值；未知名字直接抛错（早失败，便于定位拼写问题）。"""
    if name not in THRESHOLDS:
        raise KeyError("未知的报告阈值名: %s" % name)
    return THRESHOLDS[name]


def reload_thresholds(path=None):
    """重新加载阈值（供测试与运行期热更新使用）。"""
    global THRESHOLDS
    THRESHOLDS = load_thresholds(path)
    return THRESHOLDS


# ------------------------------------------------------------------
# 二、P 值：显著性判定与显示
# ------------------------------------------------------------------
def p_sig(p):
    """依据 P 值给显著性结论文字。"""
    try:
        x = float(p)
    except (TypeError, ValueError):
        return DASH
    if x < get("alpha_strong"):
        return "极显著（P<%g），必须校正" % get("alpha_strong")
    if x < get("alpha"):
        return "显著（P<%g），需校正" % get("alpha")
    return "不显著（P≥%g）" % get("alpha")


def fmt_p(p):
    """统一全报告 P 值显示口径：极小值给阈值、其余保留三位小数。"""
    try:
        x = float(p)
    except (TypeError, ValueError):
        return DASH
    if x < get("p_display_floor"):
        return "<%g" % get("p_display_floor")
    if x < get("p_display_floor_2"):
        return "<%g" % get("p_display_floor_2")
    return "%.*f" % (int(get("p_display_nd")), x)


def p_label(p):
    """规范的 P 值行内标签：阈值型 P<0.001，普通型 P=0.032。"""
    s = fmt_p(p)
    return ("P" + s) if s.startswith("<") else ("P=" + s)


def is_significant(p):
    """P 值是否达显著水平；不可解析者一律判为不显著（不臆造结论）。"""
    try:
        return float(p) < get("alpha")
    except (TypeError, ValueError):
        return False


# ------------------------------------------------------------------
# 三、遗传力分档
# ------------------------------------------------------------------
def h2_band(h2):
    """遗传力分档结论；不可解析返回 None，由调用方决定是否渲染该节。"""
    try:
        x = float(h2)
    except (TypeError, ValueError):
        return None
    if x < get("h2_low"):
        return ("低遗传力（h²<%.2f，表型受环境影响较大，宜结合家系信息选择）"
                % get("h2_low"))
    if x < get("h2_moderate"):
        return "中等遗传力（%.2f≤h²<%.2f，个体与家系选择并重）" % (
            get("h2_low"), get("h2_moderate"))
    return "中高遗传力（h²≥%.2f，个体选择可获得较明显遗传响应）" % get("h2_moderate")


# ------------------------------------------------------------------
# 四、残差形态：偏度 / 峰度 / 二次曲率
# ------------------------------------------------------------------
def fnum(value):
    """安全转 float；不可转返回 None（不用 0 冒充，避免伪造结论）。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def describe_skew(sk):
    """偏度分档描述（含方向）；无数据返回 None。"""
    v = fnum(sk)
    if v is None:
        return None
    av = abs(v)
    if av < get("skew_symmetric"):
        return "近似对称"
    degree = "轻度" if av < get("skew_mild") else "明显"
    return degree + ("左偏" if v < 0 else "右偏")


def describe_kurt(kt):
    """超额峰度分档描述；无数据返回 None。"""
    v = fnum(kt)
    if v is None:
        return None
    if abs(v) < get("kurt_near_normal"):
        return "峰度接近正态"
    return "尾部相对正态偏厚/偏尖"


def mean_is_zero(mean_r, sd_r):
    """残差均值是否可判定为零（相对 sd 的容差内）。"""
    m, s = fnum(mean_r), fnum(sd_r)
    if m is None:
        return False
    return abs(m) < get("mean_zero_rel_tol") * max(1.0, abs(s or 1.0))


def normality_verdict(sk, kt, n=None):
    """综合偏度/峰度给出正态性结论。

    返回 ``(verdict_text, need_action)``；缺形状数据时分别为 ``""`` / ``False``。
      · 严格档：|sk|<0.5 且 |kt|<1.0 → 正态性假设成立
      · 宽松档：|sk|<1.0 且 |kt|<2.0 → 轻度偏离，大样本下可接受
      · 其余     → 明显偏离，需关注
    """
    if sk is None:
        return "", False
    ask = abs(fnum(sk) or 0.0)
    akt = abs(fnum(kt) or 0.0)
    if ask < get("shape_strict_skew") and akt < get("shape_strict_kurt"):
        return "残差近似对称、峰度接近正态，正态性假设成立", False
    if ask < get("shape_loose_skew") and akt < get("shape_loose_kurt"):
        # 轻度偏离：大样本下可接受，**不**触发后续"建议核查"动作
        # （与历史实现一致：need_action 只在 ask≥1.0 或 akt≥2.0 时为真）
        return ("残差轻度偏离正态（偏度 %s、超额峰度 %s），在大样本（n=%s）下可接受"
                % (dash(sk), dash(kt), dash(n)), False)
    return ("残差明显偏离正态（偏度 %s、超额峰度 %s），需关注极端值与性状分布"
            % (dash(sk), dash(kt)), True)


def quadratic_verdict(gain, coef):
    """据残差–拟合值二次项 R² 增量分档给出结论。

    返回 ``(text, need_action)``；gain 为 None 时为 ``("", False)``。
    """
    g, c = fnum(gain), fnum(coef)
    if g is None:
        return "", False
    shape = "U 型（开口向上）" if (c is not None and c > 0) else "倒 U 型（开口向下）"
    if g < get("quad_gain_none"):
        return ("残差–拟合值无明显系统弯曲（二次项 R² 增量 %s），线性与方差齐性假设基本满足"
                % dash(gain), False)
    if g < get("quad_gain_mild"):
        return ("残差–拟合值呈轻度 %s弯曲（二次项 R² 增量 %s），提示协变量可能存在弱非线性"
                % (shape, dash(gain)), False)
    return ("残差–拟合值存在明显 %s弯曲（二次项 R² 增量 %s），提示协变量可能存在非线性"
            "（可尝试加入二次项）或固定效应水平划分不足，建议核查后重估"
            % (shape, dash(gain)), True)


def trend_significance_text(p):
    """遗传进展（EBV~世代回归）的显著性结论。

    与 p_sig 的区别：p_sig 是"某个 P 值"的通用判定，
    此处文案须指明主语是遗传进展，故单列（沿用历史原文）。
    """
    v = fnum(p)
    if v is None:
        return "遗传进展在统计上不显著（P≥%g）" % get("alpha")
    if v < get("alpha_strong"):
        return "遗传进展在统计上极显著（P<%g）" % get("alpha_strong")
    if v < get("alpha"):
        return "遗传进展在统计上显著（P<%g）" % get("alpha")
    return "遗传进展在统计上不显著（P≥%g）" % get("alpha")


# ------------------------------------------------------------------
# 五、枚举值 -> 中文（判定结果的展示层）
# ------------------------------------------------------------------
VERDICT_CN = {
    "pass": "通过", "passed": "通过", "pass_with_warning": "带告警通过",
    "warning": "告警", "block": "阻断", "blocked": "阻断",
    "human_required": "需人工确认", "skip": "跳过", "skipped": "跳过",
}

STATUS_CN = {"completed": "完成（检查全部通过）",
             "partial": "部分完成（存在告警或跳过项）",
             "failed": "失败（存在阻断项）"}

# 实际模型身份 -> （中文名称，关系矩阵描述，是否依赖系谱，是否依赖基因型）
MODEL_INFO = {
    "pblup": ("PBLUP 系谱最佳线性无偏预测",
              "系谱加性遗传关系矩阵 A（由系谱亲缘关系构建）", True, False),
    "gblup": ("GBLUP 基因组最佳线性无偏预测",
              "基因组加性遗传关系矩阵 G（由全基因组分子标记构建）", False, True),
    "ssblup": ("SSGBLUP 单步基因组最佳线性无偏预测",
               "单步混合关系矩阵 H（系谱关系 A 与基因组关系 G 融合）", True, True),
    "ssgblup": ("SSGBLUP 单步基因组最佳线性无偏预测",
                "单步混合关系矩阵 H（系谱关系 A 与基因组关系 G 融合）", True, True),
}

# 正态性判定值 -> 中文（与 abges_explore 的 Verdict 取值对应）
NORMALITY_CN = {"normal": "近似正态", "mild": "轻度偏离正态",
                "skewed": "明显偏态", "insufficient": "样本不足"}


def verdict_cn(v):
    """闸门/校验结论 -> 中文；无值返回占位符。"""
    if v in (None, ""):
        return DASH
    return VERDICT_CN.get(str(v), str(v))


def status_cn(value):
    """交付状态 -> 中文；无值返回占位符。"""
    if value in (None, ""):
        return DASH
    return STATUS_CN.get(str(value), str(value))


def resolve_model(metadata, model_label):
    """解析模型身份：返回 (model_key, (中文名, 关系矩阵描述, 用系谱, 用基因型))。"""
    key = str(metadata.get("model_id") or "").strip().lower()
    if key not in MODEL_INFO:
        m = str(model_label or "").upper()
        key = "ssblup" if "SS" in m else ("gblup" if m.startswith("G") else "pblup")
    return key, MODEL_INFO.get(key, (model_label, "", True, False))
