# -*- coding: utf-8 -*-
"""
ABGES ASCII 安全化模块
======================
HIBLUP v1.7.0 在 Windows 上写 .rand 结果文件时，若列名或固定效应水平值
含非 ASCII 字符会触发 0xC0000409 崩溃。本模块在调用引擎前把这些标签
安全转换为 ASCII，同时保留完整映射以便报告还原为中文。

三级降级策略（永不卡死）：
  1. 常见词映射   ——  栋舍 -> House、公 -> M，语义最清晰；
  2. 拼音转换     ——  育雏舍 -> YuChuShe，新词也有语义且可复现；
  3. 位置占位符   ——  FE1 / GenL1，兜底保证一定能产出 ASCII。

数据结构与列顺序完全不变，仅替换标签本身。
"""

import os
import logging

logger = logging.getLogger(__name__)

from abges_core import AbgesError, make_timestamp, ensure_dir

try:
    from pypinyin import pinyin as _pinyin, Style as _PinyinStyle
    _PYPINYIN_AVAILABLE = True
except ImportError:
    _PYPINYIN_AVAILABLE = False


# ---------------------------------------------------------------
# 常见词映射：优先使用语义化短名，便于人工阅读引擎输出
# ---------------------------------------------------------------
COMMON_COLUMN_MAP = {
    "个体号": "ID", "翅号": "ID", "编号": "ID", "个体编号": "ID",
    "世代": "Gen", "代次": "Gen",
    "批次": "Batch", "孵化批次": "Batch",
    "性别": "Sex",
    "栋舍": "House", "鸡舍": "House", "舍别": "House",
    "笼位": "Cage", "笼号": "Cage",
    "父号": "Sire", "父亲": "Sire", "公号": "Sire",
    "母号": "Dam", "母亲": "Dam", "母号编号": "Dam",
    "日龄": "Age", "周龄": "WeekAge",
    "体重": "BW", "初生重": "BirthWt",
    "开产日龄": "AFE", "产蛋数": "EN", "产蛋率": "ER",
    "采食量": "FI", "增重": "WG", "料重比": "FCR",
    "品系": "Line", "家系": "Family", "群体": "Population",
}

COMMON_LEVEL_MAP = {
    "公": "M", "雄": "M", "男": "M",
    "母": "F", "雌": "F", "女": "F",
    "未知": "U", "不详": "U",
    "是": "Y", "否": "N",
    "正常": "Normal", "异常": "Abnormal",
}


def is_pure_ascii(text):
    """判断文本是否全部为 ASCII 字符。"""
    return all(ord(char) < 128 for char in str(text))


def convert_to_pinyin(text):
    """
    把中文转为 PascalCase 拼音，例如 育雏舍 -> YuChuShe。
    仅保留字母数字以保证引擎标签安全。未装 pypinyin 时返回 None。
    """
    if not _PYPINYIN_AVAILABLE:
        return None
    try:
        syllable_groups = _pinyin(str(text), style=_PinyinStyle.NORMAL)
        syllables = [item for group in syllable_groups for item in group]
        joined = "".join(part.capitalize() for part in syllables if part.strip())
        cleaned = "".join(char for char in joined if char.isalnum())
        return cleaned or None
    except Exception:
        return None


def ensure_unique_label(candidate, used_labels):
    """在已用标签集合内保证唯一，冲突时追加 _1/_2 后缀。"""
    unique = candidate
    serial = 1
    while unique in used_labels:
        unique = "%s_%d" % (candidate, serial)
        serial += 1
    used_labels.add(unique)
    return unique


def convert_any_to_ascii(text, fallback_prefix, used_labels, preferred_map=None):
    """
    任意文本转 ASCII 的统一入口，三级降级，永不抛错。
    返回 (ascii文本, 是否发生转换)。
    """
    text = str(text)
    if is_pure_ascii(text):
        return text, False
    if preferred_map and text in preferred_map:
        return ensure_unique_label(preferred_map[text], used_labels), True
    pinyin_form = convert_to_pinyin(text)
    if pinyin_form:
        return ensure_unique_label(pinyin_form, used_labels), True
    placeholder = "%s%d" % (fallback_prefix, len(used_labels) + 1)
    return ensure_unique_label(placeholder, used_labels), True


def build_column_rename_map(columns):
    """
    为一组列名构建 ASCII 重命名映射。
    返回 {原列名: 新列名}，仅包含实际发生变化的列。
    """
    used_labels = set()
    rename_map = {}
    for column in columns:
        new_name, changed = convert_any_to_ascii(
            column, "FE", used_labels, COMMON_COLUMN_MAP)
        used_labels.add(new_name)
        if changed:
            rename_map[column] = new_name
    return rename_map


def build_level_value_map(values, column_label):
    """
    为某一列的水平值构建 ASCII 映射。
    返回 {原水平值: 新水平值}，仅包含实际发生变化的取值。
    """
    used_labels = set()
    value_map = {}
    for value in values:
        new_value, changed = convert_any_to_ascii(
            value, "%sL" % column_label, used_labels, COMMON_LEVEL_MAP)
        if changed:
            value_map[value] = new_value
    return value_map


def sanitize_phenotype_for_engine(phenotype_path, output_dir, timestamp=None):
    """
    检查表型文件的列名与水平值，必要时生成一份仅标签 ASCII 化、
    数据结构与列顺序完全不变的临时文件供引擎使用。

    参数
    ----
    phenotype_path : str   引擎输入表型文件（Tab 分隔）
    output_dir     : str   临时文件输出目录
    timestamp      : str   时间戳，缺省自动生成

    返回
    ----
    (供引擎使用的路径, 映射字典, 是否生成了临时文件)

    映射字典结构：
        {"__columns__": {原列名: 新列名},
         "原列名": {原水平值: 新水平值, ...}}
    """
    import pandas as pd

    if not os.path.exists(phenotype_path):
        raise AbgesError("E023", "表型文件不存在：%s" % phenotype_path)

    frame = pd.read_csv(phenotype_path, sep="\t")
    original_columns = [str(column).strip() for column in frame.columns]
    frame.columns = original_columns

    column_rename_map = build_column_rename_map(original_columns)

    level_maps = {}
    for column in original_columns:
        series = frame[column]
        # 数值列的取值天然是 ASCII，跳过以节省开销
        if pd.api.types.is_numeric_dtype(series):
            continue
        unique_values = [value for value in series.dropna().unique()]
        if all(is_pure_ascii(value) for value in unique_values):
            continue
        target_label = column_rename_map.get(column, column)
        value_map = build_level_value_map(unique_values, target_label)
        if value_map:
            level_maps[column] = value_map

    if not column_rename_map and not level_maps:
        return phenotype_path, {}, False

    sanitized = frame.rename(columns=column_rename_map)
    for original_column, value_map in level_maps.items():
        target_column = column_rename_map.get(original_column, original_column)
        sanitized[target_column] = sanitized[target_column].map(
            lambda cell: value_map.get(cell, cell) if pd.notna(cell) else cell)

    ensure_dir(output_dir)
    stamp = timestamp or make_timestamp()
    safe_path = os.path.join(output_dir, "_engine_ascii_pheno_%s.txt" % stamp)
    # 缺失值必须写成 "NA"（非空串），否则 HIBLUP 按空白符坍缩列、报
    # "line N does not have M elements"。na_rep 与 write_engine_table 保持一致。
    sanitized.to_csv(safe_path, sep="\t", index=False, na_rep="NA",
                     encoding="utf-8", lineterminator="\n")

    mapping = {}
    if column_rename_map:
        mapping["__columns__"] = column_rename_map
    mapping.update(level_maps)

    logger.warning(
        "表型文件含非 ASCII 标签，已自动转换供引擎使用（避免 HIBLUP 写 .rand 崩溃）。"
        "映射：%s；临时文件：%s", mapping, safe_path)
    return safe_path, mapping, True


def restore_labels_for_report(text, mapping):
    """
    报告阶段把 ASCII 标签还原为原始中文，便于人工阅读。
    mapping 为 sanitize_phenotype_for_engine 返回的映射字典。
    """
    if not mapping:
        return text
    reverse = {}
    for original, converted in (mapping.get("__columns__") or {}).items():
        reverse[converted] = original
    for column, value_map in mapping.items():
        if column == "__columns__":
            continue
        for original, converted in value_map.items():
            reverse.setdefault(converted, original)
    return reverse.get(text, text)
