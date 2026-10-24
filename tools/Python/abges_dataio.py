# -*- coding: utf-8 -*-
"""
ABGES 通用数据读取与标准化模块
==============================
职责：把任意来源的表格文件（csv/txt/tsv/xlsx/xls）读成 DataFrame，
并按配置中的 header_map 完成列名标准化。

设计原则：
  - 零重复逻辑：全系统只有这一处读表入口，编码/分隔符/表头映射规则集中在此。
  - 主动防御：文件不存在、工作表不存在、必需列缺失，一律明确抛错，
    绝不静默跳过或用空表继续。
  - 只读原始：本模块永不写回原始文件。
"""

import os
import re

import abges_core as core


# 统一视为"未知/缺失"的取值（小写比较）
MISSING_TOKENS = {
    "", "na", "n/a", "nan", "none", "null", "-", "--",
    "未知", "无", "缺失", "空",
}

# 按扩展名判定读取方式
EXCEL_EXTENSIONS = (".xlsx", ".xlsm", ".xls")
TEXT_EXTENSIONS = (".csv", ".txt", ".tsv", ".dat")


# ===============================================================
# 一、基础读取
# ===============================================================

def sniff_delimiter(file_path, encoding):
    """
    根据首行字符出现频次推断分隔符。
    顺序：制表符 > 逗号 > 分号 > 连续空白。
    """
    with open(file_path, "rb") as handle:
        head = handle.read(65536)
    try:
        text = head.decode(encoding, errors="replace")
    except LookupError:
        text = head.decode("utf-8", errors="replace")
    first_line = text.splitlines()[0] if text.splitlines() else ""
    for candidate in ("\t", ",", ";", "|"):
        if first_line.count(candidate) >= 1:
            return candidate
    return r"\s+"


def read_table(file_path, sheet=0, delimiter=None, dtype=None):
    """
    读取任意表格文件，返回 (DataFrame, 读取信息 dict)。

    参数：
      sheet      Excel 工作表索引或名称，文本文件忽略
      delimiter  显式分隔符；None 表示自动推断
      dtype      传给 pandas 的 dtype，默认全部按 str 读入以免丢前导零
    """
    import pandas as pd

    core.require_readable_file(file_path, "E011")
    extension = os.path.splitext(file_path)[1].lower()
    info = {"path": os.path.abspath(file_path), "kind": None,
            "encoding": None, "delimiter": None, "sheet": None}

    if extension in EXCEL_EXTENSIONS:
        info["kind"] = "excel"
        excel = pd.ExcelFile(file_path)
        sheet_names = list(excel.sheet_names)
        target = _resolve_sheet(sheet, sheet_names, file_path)
        info["sheet"] = target
        frame = excel.parse(target, dtype=dtype if dtype is not None else str)
    elif extension in TEXT_EXTENSIONS or extension == "":
        info["kind"] = "text"
        encoding = core.detect_encoding(file_path)
        info["encoding"] = encoding
        sep = delimiter or sniff_delimiter(file_path, encoding)
        info["delimiter"] = sep
        frame = pd.read_csv(file_path, sep=sep, encoding=encoding,
                            dtype=dtype if dtype is not None else str,
                            engine="python", keep_default_na=True)
    else:
        raise core.AbgesError("E011", "不支持的文件类型：%s" % extension,
                              {"path": file_path,
                               "supported": list(EXCEL_EXTENSIONS + TEXT_EXTENSIONS)})

    frame.columns = [str(name).strip() for name in frame.columns]
    info["rows"] = int(frame.shape[0])
    info["cols"] = int(frame.shape[1])
    if frame.shape[0] == 0:
        raise core.AbgesError("E011", "文件无数据行：%s" % file_path, info)
    return frame, info


def _resolve_sheet(sheet, sheet_names, file_path):
    """解析 Excel 工作表标识，不存在时报错并列出可选项。"""
    if sheet is None:
        return sheet_names[0]
    if isinstance(sheet, int):
        if sheet < 0 or sheet >= len(sheet_names):
            raise core.AbgesError("E012", "工作表索引越界：%d" % sheet,
                                  {"path": file_path, "sheets": sheet_names})
        return sheet_names[sheet]
    if sheet not in sheet_names:
        raise core.AbgesError("E012", "工作表不存在：%s" % sheet,
                              {"path": file_path, "sheets": sheet_names})
    return sheet


def list_excel_sheets(file_path):
    """列出 Excel 全部工作表名，供 Agent 决策使用。"""
    import pandas as pd
    core.require_readable_file(file_path, "E011")
    return list(pd.ExcelFile(file_path).sheet_names)


# ===============================================================
# 二、表头发现与标准化
# ===============================================================

def auto_detect_columns(frame, alias_config):
    """
    根据 column-aliases.yaml 配置，从 DataFrame 实际列名中，
    自动发现并建立 {标准代码: 实际列名} 的映射。

    返回：
        dict {standard_code: actual_column_name}
        匹配失败的列不计在内（交 normalize_header 兜底）。

    匹配策略（按优先级）：
        1. 精确匹配 aliases（含大小写折叠、空格归一）
        2. 大小写无关匹配（原始列名 vs 别名，不区分大小写）
        3. 歧义别名直接跳过（交 unknown_columns 处理）

    歧义处理：
        同一别名命中多个标准代码 → 保留 role 优先级最高（id > pedigree > fixed > covariate > trait）
        同一标准代码被多个别名命中 → 以第一个成功匹配为准
    """
    import os, yaml

    if isinstance(alias_config, str) and os.path.isfile(alias_config):
        with open(alias_config, "r", encoding="utf-8") as f:
            alias_config = yaml.safe_load(f)

    # role 优先级（数字越小优先级越高）
    role_priority = {"id": 0, "pedigree": 1, "fixed": 2, "covariate": 3, "trait": 4}

    # 建立扁平别名 → 条目列表 映射（支持歧义别名检测）
    alias_to_entries = {}
    all_entries = (
        alias_config.get("identifiers", []) +
        alias_config.get("pedigree", []) +
        alias_config.get("fixed_effects", []) +
        alias_config.get("covariates", []) +
        alias_config.get("traits", [])
    )
    for entry in all_entries:
        for als in entry.get("aliases", []):
            key = str(als).strip()
            if key not in alias_to_entries:
                alias_to_entries[key] = []
            alias_to_entries[key].append(entry)

    detected = {}          # standard_code → actual_column
    ambiguous_warnings = []  # 歧义别名记录

    for col in frame.columns:
        col_str = str(col).strip()
        col_lower = col_str.lower()

        # 直接查 alias 表
        entry_list = alias_to_entries.get(col_str, []) or alias_to_entries.get(col_lower, [])

        if len(entry_list) == 1:
            sc = entry_list[0]["standard_code"]
            if sc not in detected:
                detected[sc] = col
            continue

        if len(entry_list) > 1:
            # 歧义：多个候选，按 role 优先级选最优
            entry_list.sort(key=lambda e: role_priority.get(e.get("role", "trait"), 5))
            sc = entry_list[0]["standard_code"]
            if sc not in detected:
                detected[sc] = col
            others = [e["standard_code"] for e in entry_list[1:]]
            ambiguous_warnings.append({"column": col, "chosen": sc, "rejected": others})
            continue

        # 精确匹配失败，尝试大小写折叠匹配（扫描所有别名）
        found = False
        for als, entries in alias_to_entries.items():
            if str(als).lower() == col_lower:
                if len(entries) == 1:
                    sc = entries[0]["standard_code"]
                    if sc not in detected:
                        detected[sc] = col
                    found = True
                    break
                elif len(entries) > 1:
                    entries.sort(key=lambda e: role_priority.get(e.get("role", "trait"), 5))
                    sc = entries[0]["standard_code"]
                    if sc not in detected:
                        detected[sc] = col
                    others = [e["standard_code"] for e in entries[1:]]
                    ambiguous_warnings.append({"column": col, "chosen": sc, "rejected": others, "note": "case-insensitive match"})
                    found = True
                    break
        # 没找到 → 交 normalize_header 的 unknown_columns 兜底

    return {
        "detected": detected,
        "ambiguous_warnings": ambiguous_warnings,
        "coverage": len(detected),
        "total_columns": len(frame.columns),
    }


def normalize_header(frame, header_map):
    """
    按 header_map（原始列名小写 -> 标准列名）重命名列。
    返回 (新 DataFrame, 重命名明细 dict, 未识别列 list)。

    冲突处理：同一标准列名被多个原始列命中时，保留第一个，
    其余列保持原名并计入 unmapped，绝不静默覆盖数据。
    """
    lowered_map = {str(key).strip().lower(): value
                   for key, value in (header_map or {}).items()}
    rename_detail = {}
    taken_targets = set(str(name) for name in frame.columns)
    unmapped = []

    for column in frame.columns:
        key = str(column).strip().lower()
        target = lowered_map.get(key)
        if target is None:
            unmapped.append(column)
            continue
        if target == column:
            taken_targets.add(target)
            continue
        if target in rename_detail.values() or target in taken_targets:
            unmapped.append(column)
            continue
        rename_detail[column] = target
        taken_targets.add(target)

    renamed = frame.rename(columns=rename_detail)
    return renamed, rename_detail, unmapped


def require_columns(frame, columns, error_code, context=""):
    """
    主动防御：必需列缺失时明确告知缺了什么、当前有什么。
    """
    missing = [name for name in columns if name not in frame.columns]
    if missing:
        raise core.AbgesError(
            error_code,
            "缺少必需列 %s%s" % (missing, ("（%s）" % context) if context else ""),
            {"missing": missing, "available": list(frame.columns)})
    return True


# ===============================================================
# 三、取值标准化
# ===============================================================

def normalize_identifier(value, missing_token="NA"):
    """
    个体号标准化：去空白、去浮点尾巴 .0、缺失归一。
    个体号是全系统主键，任何一处不一致都会导致跨表关联失败。
    """
    if value is None:
        return missing_token
    if isinstance(value, float):
        if value != value:            # NaN
            return missing_token
        if value == int(value):
            value = int(value)
    text = str(value).strip()
    if text.lower() in MISSING_TOKENS:
        return missing_token
    if re.match(r"^-?\d+\.0$", text):
        text = text[:-2]
    return text


def normalize_identifier_series(series, missing_token="NA"):
    """对整列个体号做标准化，返回新 Series。"""
    return series.map(lambda value: normalize_identifier(value, missing_token))


def to_numeric_column(series, column_name=None):
    """
    强制转数值。返回 (数值 Series, 无法转换的原值列表)。
    无法转换的取值置 NaN 并原样记录，供 change_log 追溯。
    """
    import pandas as pd

    converted = pd.to_numeric(series, errors="coerce")
    became_na = converted.isna() & series.notna()
    bad_values = []
    if became_na.any():
        raw = series[became_na].astype(str).str.strip()
        bad_values = [value for value in raw.unique().tolist()
                      if value.lower() not in MISSING_TOKENS]
    return converted, bad_values


def parse_flexible_date(series):
    """
    解析多种中文/西文日期写法，返回 datetime Series。
    支持：2025年5月13日 / 2025-05-13 / 2025/5/13 / 20250513。
    无法解析的置 NaT，不抛错（由上层按需判定）。
    """
    import pandas as pd

    text = series.astype(str).str.strip()
    text = (text.str.replace("年", "-", regex=False)
                .str.replace("月", "-", regex=False)
                .str.replace("日", "", regex=False)
                .str.replace(".", "-", regex=False)
                .str.replace("/", "-", regex=False))
    parsed = pd.to_datetime(text, errors="coerce", format="mixed")
    still_bad = parsed.isna() & text.str.match(r"^\d{8}$").fillna(False)
    if still_bad.any():
        fixed = pd.to_datetime(text[still_bad], format="%Y%m%d", errors="coerce")
        parsed.loc[still_bad] = fixed
    return parsed


# ===============================================================
# 四、引擎输入文件写出
# ===============================================================

def write_engine_table(frame, output_path, sep="\t", na_rep="NA"):
    """
    写出供引擎读取的文本表。
    HIBLUP 要求：Tab 分隔、UTF-8 无 BOM、缺失统一 NA、首列为 id。
    写完立即回读校验行列数，杜绝"声称写出但内容为空"。

    B1 修复：na_rep 只兜住 NaN/pd.NA，兜不住空字符串 "" 或纯空白单元格；
    后者会被写成空字段，HIBLUP 按空白符解析时中间空列坍缩导致整行错位。
    故写出前把所有"空白/空串/仅空格"的单元格统一归为缺失，再由 na_rep 兜底。
    注意 pandas 3.x 字符串列默认是 StringDtype（非 object），缺失为 nan，
    须用 pd.isna() 而非 `is None` 判断。
    """
    import pandas as _pd
    core.ensure_dir(os.path.dirname(os.path.abspath(output_path)))
    clean = frame.copy()
    for column in clean.columns:
        dtype_str = str(clean[column].dtype)
        if clean[column].dtype == object or "str" in dtype_str.lower():
            clean[column] = clean[column].map(
                lambda v: na_rep if (_pd.isna(v) or (isinstance(v, str) and v.strip() == ""))
                else v)
    clean.to_csv(output_path, sep=sep, index=False, na_rep=na_rep,
                 encoding="utf-8", lineterminator="\n")
    if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
        raise core.AbgesError("E013", "引擎输入文件写出失败：%s" % output_path)
    with open(output_path, "r", encoding="utf-8") as handle:
        line_count = sum(1 for _ in handle)
    expected = frame.shape[0] + 1
    if line_count != expected:
        raise core.AbgesError(
            "E013", "引擎输入文件行数不符：期望 %d 实际 %d" % (expected, line_count),
            {"path": output_path})
    return output_path


def summarize_frame(frame, name="table"):
    """生成表的基础规模描述，用于处理前后统计。"""
    return {
        "name": name,
        "rows": int(frame.shape[0]),
        "cols": int(frame.shape[1]),
        "columns": [str(column) for column in frame.columns],
    }
