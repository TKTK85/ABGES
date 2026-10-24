# -*- coding: utf-8 -*-
"""
ABGES 未知 / 中文表头 → 英文代号 转换层
==========================================

解决痛点：数据里出现「既未被任务请求、也未被 trait-registry.yaml 注册」的列
（尤其是中文表头），原 run_phenotype_qc 会整列忽略。本模块在表型 QC 早期
把这类表头自动转成英文代号，使下游（HIBLUP 命令、ANOVA、衍生计算）能统一用
英文引用，原始中文名仅存于映射表，可追溯、不静默。

代号生成三级优先级（先生定调：用英文缩写，如产蛋→CD）：
  1. column-aliases.yaml（统一别名注册表，2026-10-16 新增，优先级最高）
  2. trait-registry.yaml 的 name_cn 命中（产蛋数→EN、体重→BW，保口径一致）
  3. pypinyin 拼音首字母兜底（产蛋→CD）
  三级均失败 → UX_ + base26 字母保底（UX_A / UX_B / …）

治理铁律：不静默、可追溯、不发明公式。本模块只为未知列命名 / 分类，绝不编造
衍生公式；分类为启发式，结果须在报告中显著标注并建议人工复核。

触发规则（先生明确）：中文表头 → 一律转换为英文（未知固定效应亦同）；
英文表头（无论是否已知）保持不变，交由显式请求逻辑处理。
"""

import logging
import os

logger = logging.getLogger(__name__)

import abges_core as core

HEADER_TRANSLATION_CONFIG = "configs/traits/header_translation.yaml"
TRAIT_REGISTRY_CONFIG = "configs/traits/trait-registry.yaml"
COLUMN_ALIAS_CONFIG = "configs/column-aliases.yaml"

# 配置降级原因登记册：{配置相对路径: 降级原因}。
# 治理铁律「不静默」——凡配置加载失败一律登记，供告警与审计取用。
_CONFIG_DEGRADE = {}


def column_alias_degradations():
    """返回本次进程中「配置降级」的原因清单，供报告告警与审计使用。

    返回 {配置路径: 降级原因}；空 dict 表示全部配置正常加载。
    """
    return dict(_CONFIG_DEGRADE)


# ===============================================================
# 一、基础工具
# ===============================================================

def _is_chinese(text):
    """判断字符串是否含中文字符。"""
    return any('\u4e00' <= ch <= '\u9fff' for ch in str(text))


def _base26(n):
    """正整数 → base26 字母序列：1→A, 26→Z, 27→AA。"""
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _pinyin_initials(text):
    """取字符串中全部中文字符的拼音首字母（大写），非中文忽略。

    仅对中文部分取首字母，避免数字 / 英文字母噪声（如「产蛋数300日龄」→「CDS」）。
    依赖轻量纯 Python 库 pypinyin（已装入 D 盘 venv）。
    """
    from pypinyin import pinyin, Style
    chinese = [ch for ch in str(text) if '\u4e00' <= ch <= '\u9fff']
    if not chinese:
        return ""
    result = []
    for ch in chinese:
        initial = pinyin(ch, style=Style.FIRST_LETTER, heteronym=False)[0][0]
        result.append(initial.upper())
    return "".join(result)


# ===============================================================
# 二、代号解析（三级优先级）
# ===============================================================

def _build_cn_map(registry):
    """trait-registry 的 name_cn → trait_id 映射。"""
    cn_map = {}
    for section in ("identifiers", "fixed_effect_candidates",
                   "covariate_candidates", "traits"):
        for entry in registry.get(section, []) or []:
            cn = entry.get("name_cn")
            if cn:
                cn_map[str(cn).strip()] = entry["trait_id"]
    return cn_map


def _load_header_translation():
    """加载人工中文→英文缩写词典（降级兼容用，主数据源已迁移至 column-aliases.yaml）。"""
    try:
        cfg = core.load_yaml_config(HEADER_TRANSLATION_CONFIG)
    except core.AbgesError:
        return {}
    return {str(k).strip(): str(v).strip()
            for k, v in (cfg.get("chinese_to_code", {}) or {}).items()}


def _load_column_alias_registry():
    """加载 column-aliases.yaml 并展平为 {别名: standard_code} 字典。

    整合 identifiers / pedigree / fixed_effects / covariates / traits 五个分组，
    对 ambiguous（歧义别名）直接跳过，交歧义处理逻辑。

    降级语义（治理铁律：不静默）：
      - 文件不存在 → 静默返回空 dict（该配置本就可选，非异常）；
      - 文件存在但解析失败 / 为空 → 记 logging.error 并登记降级原因，
        **绝不静默吞掉**。原因可由 column_alias_degradations() 取用。
    """
    abs_path = core.resolve_project_path(
        *COLUMN_ALIAS_CONFIG.replace("\\", "/").split("/"))
    if not os.path.isfile(abs_path):
        return {}          # 可选配置，缺失属正常
    try:
        cfg = core.load_yaml_config(COLUMN_ALIAS_CONFIG)
    except Exception as exc:
        reason = "列别名配置加载失败：%s" % (exc,)
        _CONFIG_DEGRADE[COLUMN_ALIAS_CONFIG] = reason
        logger.error("%s → 已降级至旧三级优先级（registry/dictionary/pinyin），"
                      "请修正 %s 后重跑", reason, COLUMN_ALIAS_CONFIG)
        return {}
    alias_map = {}
    sections = (
        cfg.get("identifiers", []) +
        cfg.get("pedigree", []) +
        cfg.get("fixed_effects", []) +
        cfg.get("covariates", []) +
        cfg.get("traits", [])
    )
    ambiguous_aliases = set()
    for entry in cfg.get("ambiguous", []) or []:
        ambiguous_aliases.add(str(entry.get("alias", "")).strip())

    for entry in sections:
        code = entry.get("standard_code", "")
        if not code:
            continue
        for als in entry.get("aliases", []) or []:
            key = str(als).strip()
            if key in ambiguous_aliases:
                continue  # 歧义别名跳过，交歧义处理逻辑
            if key.lower() not in alias_map:  # 不覆盖已有（保留首个匹配）
                alias_map[key.lower()] = code
            # 大小写敏感键也存一份（精确匹配优先）
            if key not in alias_map:
                alias_map[key] = code
    return alias_map


def resolve_code(header, alias_map, cn_map, dict_map, used_codes, cfg):
    """按四级优先级为单个表头解析英文代号。

    返回 (code, source)。code == header 表示无需改名（已是已知英文代号）。

    注意：已知英文标准代号集合不含 frame 列名本身，避免「列名恒等于自身」
    导致永远判为已知、永不转换。used_codes 用于碰撞规避（已占用的代号不再生成）。
    """
    h = str(header).strip()

    # P1：column-aliases.yaml 统一别名注册表（优先级最高）
    if alias_map:
        key_lower = h.lower()
        if key_lower in alias_map:
            code = alias_map[key_lower]
            if code not in used_codes:
                return code, "alias_registry"
        if h in alias_map:
            code = alias_map[h]
            if code not in used_codes:
                return code, "alias_registry"

    # 1. 注册表 name_cn 命中（P2）
    if cfg.get("use_registry_name_cn", True) and h in cn_map:
        code = cn_map[h]
        if code not in used_codes:
            return code, "registry"

    # 2. 人工词典（P3）
    if cfg.get("use_dictionary", True) and h in dict_map:
        code = dict_map[h]
        if code not in used_codes:
            return code, "dictionary"

    # 3. 拼音首字母兜底（仅含中文时）（P4）
    if cfg.get("use_pinyin_fallback", True) and _is_chinese(h):
        try:
            code = _pinyin_initials(h)
            if code and code not in used_codes:
                return code, "pinyin"
        except Exception:
            pass

    # 保底：UX_ + base26
    prefix = cfg.get("fallback_prefix", "UX_")
    i = 1
    while True:
        cand = prefix + _base26(i)
        if cand not in used_codes:
            return cand, "fallback"
        i += 1


# ===============================================================
# 三、内容分类（trait / 固定效应 / 排除）
# ===============================================================

def classify_column(series, cfg):
    """按列内容判定角色。

    返回 (role, reason)。role ∈ {fixed, trait_or_covariate, excluded}。

    修正（2026-10-13）：固定效应多为「非数值的分类列」（Gen/Batch/Sex/Family 等），
    不能因 numeric_ratio 低而整列排除。正确启发式：
      - id_ratio 过高（≈唯一）→ 疑似 ID，排除；
      - 唯一值数 ≤ fixed_max_levels → 判为固定效应（无论是否数值）；
      - 唯一值数高且数值占比足 → 连续型（性状/协变量）；
      - 高基数且非数值 → 排除（自由文本等噪声）。
    """
    import pandas as pd

    non_null = series.dropna()
    if len(non_null) == 0:
        return "excluded", "empty_column"
    s = non_null.astype(str).str.strip()
    n_rows = len(non_null)
    n_unique = s.nunique()
    numeric = pd.to_numeric(series, errors="coerce")
    numeric_ratio = float(numeric.notna().mean())

    min_num = float(cfg.get("numeric_ratio_min", 0.5))
    max_levels = int(cfg.get("fixed_max_levels", 30))
    id_ratio_max = float(cfg.get("id_uniqueness_ratio", 0.9))

    id_ratio = n_unique / n_rows if n_rows else 0.0
    if id_ratio >= id_ratio_max:
        return "excluded", "likely_id(%.2f)" % id_ratio
    if n_unique <= max_levels:
        return "fixed", "low_cardinality(%d)" % n_unique
    # 高基数
    if numeric_ratio >= min_num:
        return "trait_or_covariate", "continuous(%d)" % n_unique
    return "excluded", "non_numeric_high_cardinality(%.2f)" % numeric_ratio


# ===============================================================
# 四、整帧归一
# ===============================================================

def normalize_headers(frame, cfg, registry=None, dict_map=None):
    """对帧内未知 / 中文表头做英文代号转换。

    参数：
      frame      pandas DataFrame（表头可能为中文 / 英文 / 未知）
      cfg        unknown_columns 配置段
      registry   trait-registry 原始解析结果（含 name_cn），None 时自动加载
      dict_map   人工词典，None 时自动加载

    返回 (new_frame, rename_map, report)：
      rename_map   {原表头: 英文代号}（仅含实际改名的列）
      report       每列 [{original, code, source, role, reason, action}]
                   - 已知英文标准代号（registered_trait_id）：action=kept
                   - 未知英文表头：action=kept（保留原名）
                   - 中文表头经转换：action=renamed
    """
    import pandas as pd

    cfg = cfg or {}
    if registry is None:
        registry = core.load_yaml_config(TRAIT_REGISTRY_CONFIG)
    if dict_map is None:
        dict_map = _load_header_translation()

    # P1：column-aliases.yaml 统一别名注册表（2026-10-16 新增）
    alias_map = _load_column_alias_registry()

    cn_map = _build_cn_map(registry)
    # 已知英文标准代号 = 注册表所有 trait_id + 结构性字段（ID/Sire/Dam）
    structural = {"ID", "Sire", "Dam"}
    known_codes = set(cn_map.values()) | structural
    # 已占用代号（防碰撞）：仅以「现有列名」为初值。
    # 注意：不能并入 known_codes —— 否则注册表 name_cn→trait_id 的映射会被自身
    # 占用的 trait_id 挡掉（如 体重→BW 因 BW 已是已知代号而被拒，退化成拼音兜底）。
    # 真正的碰撞只在「同帧内已存在该英文列」时发生，由 frame.columns 覆盖即可。
    used_codes = set(frame.columns)

    rename_map = {}
    report = []

    for col in list(frame.columns):
        is_cn = _is_chinese(str(col))

        if not is_cn:
            # 英文表头：已知标准代号 → kept(known)；未知英文 → kept(unknown_english)
            known = col in known_codes
            report.append({
                "original": col,
                "code": col,
                "source": "known" if known else "unknown_english",
                "role": None,
                "reason": "registered_trait_id" if known else "english_header_kept",
                "action": "kept",
            })
            continue

        # 中文表头 → 四级优先级转换（P1: column-aliases / P2: registry / P3: dictionary / P4: pinyin）
        code, source = resolve_code(col, alias_map, cn_map, dict_map, used_codes, cfg)
        renamed = (code != col)
        if renamed:
            rename_map[col] = code
            used_codes.add(code)

        # 内容分类（仅对未知列有意义；known 已跳过）
        role, reason = classify_column(frame[col], cfg)
        action = "renamed" if renamed else "kept_unknown_english"

        report.append({
            "original": col,
            "code": code,
            "source": source,
            "role": role,
            "reason": reason,
            "action": action,
        })

    new_frame = frame.rename(columns=rename_map)
    return new_frame, rename_map, report


# ===============================================================
# 五、映射表落盘
# ===============================================================

def write_unknown_map(report, out_dir, run_id=None):
    """把未知列映射表落盘为 YAML，返回文件路径或 None。"""
    if not out_dir:
        return None
    try:
        os.makedirs(out_dir, exist_ok=True)
    except OSError:
        return None
    stamp = run_id or "unknown"
    path = os.path.join(out_dir, "unknown_columns_map_%s.yaml" % stamp)
    payload = {
        "generated_by": "abges_unknown.normalize_headers",
        "entries": report,
    }
    try:
        import yaml
        with open(path, "w", encoding="utf-8") as fh:
            yaml.safe_dump(payload, fh, allow_unicode=True, sort_keys=False)
    except Exception:
        return None
    return path
