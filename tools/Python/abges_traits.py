# -*- coding: utf-8 -*-
"""
ABGES 衍生性状计算模块
======================
公式与算法一律取自 configs/traits/derived-traits.yaml，本模块不发明公式。

两类计算：
  formula 型  —— 同一行基础性状按算术公式计算（如 FCR = FI / WG）
  record 型   —— 聚合明细捡蛋记录后计算（ACL / MCL / NCC / CEN）

捡蛋记录纠错规则 v2（先生指定，2026-10-16）：
  同一个体同一天出现 >=3 条记录 → 整天丢弃
  同一个体同一天出现 2 条记录   → 前一天有记录则保留第一条；前一天无则前向搜索平移
  同一个体同一天出现 1 条记录   → 直接保留
"""

import os
import glob
import ast

import abges_core as core
import abges_dataio as dataio


DERIVED_TRAITS_CONFIG = "configs/traits/derived-traits.yaml"

# 公式型性状允许出现的字符，防止把任意代码塞进 formula 执行
FORMULA_ALLOWED = set("0123456789.+-*/() _abcdefghijklmnopqrstuvwxyz"
                      "ABCDEFGHIJKLMNOPQRSTUVWXYZ")



def _safe_expression(text, namespace):
    if len(text) > 512:
        raise core.AbgesError("DT004", "Expression too long")
    tree = ast.parse(text, mode="eval")
    allowed = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Compare, ast.Name, ast.Load,
               ast.Constant, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod, ast.USub, ast.UAdd,
               ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Eq, ast.NotEq)
    nodes = list(ast.walk(tree))
    if len(nodes) > 128 or any(not isinstance(n, allowed) for n in nodes):
        raise core.AbgesError("DT004", "Only arithmetic and comparison expressions are allowed")
    for node in nodes:
        if isinstance(node, ast.Name) and node.id not in namespace:
            raise core.AbgesError("DT004", "Unregistered expression variable")
        if isinstance(node, ast.Constant) and (isinstance(node.value, bool) or not isinstance(node.value, (int, float))):
            raise core.AbgesError("DT004", "Only numeric literals are allowed")
    return eval(compile(tree, "<trait-expression>", "eval"), {"__builtins__": {}}, namespace)


class DerivedTraitResult(object):
    """衍生性状计算结果容器。"""

    def __init__(self, trait_id):
        self.trait_id = trait_id
        self.frame = None
        self.statistics = {}
        self.warnings = []
        self.failures = []
        self.status = core.STATUS_FAILED

    def to_dict(self):
        return {
            "trait_id": self.trait_id,
            "status": self.status,
            "statistics": self.statistics,
            "warnings": self.warnings,
            "failures": self.failures,
        }


# ===============================================================
# 一、配置查询
# ===============================================================

def load_derived_config():
    """加载衍生性状配置。"""
    return core.load_yaml_config(DERIVED_TRAITS_CONFIG)


def get_derived_definition(trait_id, config=None):
    """按 trait_id 取衍生性状定义；未登记直接报错，禁止临时发明。"""
    config = config or load_derived_config()
    for entry in config.get("derived_traits", []) or []:
        if entry.get("trait_id") == trait_id:
            return entry
    known = [entry.get("trait_id")
             for entry in config.get("derived_traits", []) or []]
    raise core.AbgesError("DT001", "未登记的衍生性状：%s" % trait_id,
                          {"known": known})


def list_computable_traits(available_columns, config=None):
    """
    列出当前数据可计算的 formula 型衍生性状。
    record 型需要外部明细文件，不在此判定。
    """
    config = config or load_derived_config()
    columns = set(available_columns)
    computable = []
    for entry in config.get("derived_traits", []) or []:
        if entry.get("compute_kind") != "formula":
            continue
        if entry.get("enabled") is False:
            continue
        required = set(entry.get("required_traits", []))
        if required and required.issubset(columns):
            computable.append(entry["trait_id"])
    return computable


# ===============================================================
# 二、formula 型
# ===============================================================

def compute_formula_trait(frame, trait_id, config=None):
    """
    按登记公式计算 formula 型衍生性状。
    required_traits 缺失时明确报错，不静默跳过。
    guards 不满足的行不计算（记为无法计算），不做任何猜测填补。
    """
    import pandas as pd
    import numpy as np

    config = config or load_derived_config()
    definition = get_derived_definition(trait_id, config)
    if definition.get("compute_kind") != "formula":
        raise core.AbgesError("DT002", "%s 不是 formula 型衍生性状" % trait_id)

    required = definition.get("required_traits", [])
    missing = [name for name in required if name not in frame.columns]
    if missing:
        raise core.AbgesError(
            "DT003", "计算 %s 缺少基础性状 %s" % (trait_id, missing),
            {"required": required, "available": list(frame.columns)})

    formula = str(definition.get("formula", "")).strip()
    if not formula:
        raise core.AbgesError("DT004", "%s 未定义 formula" % trait_id)
    illegal = set(formula) - FORMULA_ALLOWED
    if illegal:
        raise core.AbgesError("DT004", "%s 的 formula 含非法字符 %s"
                              % (trait_id, sorted(illegal)))

    result = DerivedTraitResult(trait_id)
    namespace = {}
    for name in required:
        namespace[name] = pd.to_numeric(frame[name], errors="coerce")

    guard_mask = pd.Series(True, index=frame.index)
    for guard in definition.get("guards", []) or []:
        guard_mask &= _evaluate_condition(guard, namespace, frame.index)

    try:
        raw_values = _safe_expression(formula, namespace)  # noqa: S307
    except ZeroDivisionError:
        raw_values = pd.Series(np.nan, index=frame.index)
    values = pd.Series(raw_values, index=frame.index).astype(float)
    values = values.replace([np.inf, -np.inf], np.nan)
    values[~guard_mask] = np.nan

    # validation：越界值置缺失并计入失败明细，不静默保留
    for expression in definition.get("validation", []) or []:
        check_namespace = dict(namespace)
        check_namespace[trait_id] = values
        ok_mask = _evaluate_condition(expression, check_namespace, frame.index)
        bad_mask = values.notna() & ~ok_mask
        if bad_mask.any():
            result.failures.append({
                "check": expression,
                "count": int(bad_mask.sum()),
                "examples": values[bad_mask].head(10).round(6).tolist(),
            })
            values[bad_mask] = np.nan

    output = frame.copy()
    output[trait_id] = values
    result.frame = output
    valid_n = int(values.notna().sum())
    result.statistics = {
        "trait_id": trait_id,
        "formula": formula,
        "n_input": int(frame.shape[0]),
        "n_valid": valid_n,
        "n_uncomputable": int(frame.shape[0]) - valid_n,
        "mean": round(float(values.mean()), 6) if valid_n else None,
        "sd": round(float(values.std(ddof=1)), 6) if valid_n > 1 else None,
        "min": round(float(values.min()), 6) if valid_n else None,
        "max": round(float(values.max()), 6) if valid_n else None,
    }
    if valid_n == 0:
        result.status = core.STATUS_FAILED
        result.warnings.append("%s 无任何可计算记录" % trait_id)
    elif valid_n < frame.shape[0]:
        result.status = core.STATUS_PARTIAL
        result.warnings.append(
            "%s 有 %d 条记录无法计算（guards 不满足或基础性状缺失）"
            % (trait_id, frame.shape[0] - valid_n))
    else:
        result.status = core.STATUS_COMPLETED
    return result


def _evaluate_condition(expression, namespace, index):
    """
    安全求值布尔条件（如 "WG > 0"、"FCR <= 10"）。
    仅允许比较与算术，禁止函数调用。
    """
    import pandas as pd

    text = str(expression).strip()
    illegal = set(text) - FORMULA_ALLOWED - set("<>=! ")
    if illegal:
        raise core.AbgesError("DT004", "条件表达式含非法字符 %s" % sorted(illegal),
                              {"expression": text})
    try:
        outcome = _safe_expression(text, namespace)  # noqa: S307
    except Exception as exc:
        raise core.AbgesError("DT004", "条件表达式求值失败：%s" % text,
                              {"error": str(exc)})
    if isinstance(outcome, bool):
        return pd.Series(outcome, index=index)
    return pd.Series(outcome, index=index).fillna(False).astype(bool)


# ===============================================================
# 三、record 型：捡蛋记录读取
# ===============================================================

def read_egg_records(egg_source, config=None):
    """
    读取捡蛋明细记录，返回 DataFrame[ID, date]。
    egg_source 可为目录（读取其中全部 csv/xlsx）或单个文件。
    无法识别 ID/日期列时明确报错，不猜。
    """
    import pandas as pd

    config = config or load_derived_config()
    schema = config.get("egg_record_schema", {})
    # 保留配置中的声明顺序——顺序即优先级，不可退化为 set，
    # 否则「翅号」与「ID」并存时会按列出现顺序误选场内短号。
    id_aliases = _ordered_aliases(schema.get("id_aliases", []))
    date_aliases = _ordered_aliases(schema.get("date_aliases", []))

    files = _collect_egg_files(egg_source)
    if not files:
        raise core.AbgesError("DT005", "未找到捡蛋记录文件",
                              {"source": str(egg_source)})

    collected = []
    per_file = []
    for path in files:
        frame, _ = dataio.read_table(path)
        id_column, date_column, selection = _detect_egg_columns(
            frame, id_aliases, date_aliases, path)
        subset = frame[[id_column, date_column]].copy()
        subset.columns = ["ID", "date"]
        subset["ID"] = dataio.normalize_identifier_series(subset["ID"])
        subset["date"] = dataio.parse_flexible_date(subset["date"])
        bad_dates = int(subset["date"].isna().sum())
        subset = subset.dropna(subset=["date"])
        subset = subset[subset["ID"] != "NA"]
        per_file.append({
            "file": os.path.basename(path),
            "rows_read": int(frame.shape[0]),
            "rows_valid": int(subset.shape[0]),
            "unparsable_dates": bad_dates,
            "id_column": str(id_column),
            "date_column": str(date_column),
            # 同名候选列的取舍留痕，便于复核「为什么选了这一列」
            "id_column_candidates": selection["id_candidates"],
            "id_column_rejected": selection["id_rejected"],
            "date_column_candidates": selection["date_candidates"],
            "date_column_rejected": selection["date_rejected"],
            "id_column_source": selection["id_source"],
            "date_column_source": selection["date_source"],
        })
        collected.append(subset)

    combined = pd.concat(collected, ignore_index=True)
    if combined.empty:
        raise core.AbgesError("DT005", "捡蛋记录解析后为空",
                              {"files": per_file})
    return combined, per_file


def _collect_egg_files(egg_source):
    """收集捡蛋记录文件列表。"""
    if os.path.isfile(egg_source):
        return [egg_source]
    if not os.path.isdir(egg_source):
        return []
    files = []
    for pattern in ("*.csv", "*.CSV", "*.xlsx", "*.xls", "*.txt"):
        files += glob.glob(os.path.join(egg_source, pattern))
    return sorted(set(files))


def _ordered_aliases(raw_aliases):
    """把配置中的别名列表规范化为小写、去重且保序的列表（顺序即优先级）。"""
    ordered = []
    seen = set()
    for name in raw_aliases or []:
        key = str(name).strip().lower()
        if key and key not in seen:
            seen.add(key)
            ordered.append(key)
    return ordered


def _match_by_alias_priority(frame, aliases):
    """
    按别名声明顺序（而非列出现顺序）挑选列。
    返回 (选中列, 全部命中的候选列列表)。

    这里必须按别名优先级匹配：捡蛋记录常同时含「翅号」与「ID」两列，
    若按列出现顺序遍历，先遇到哪个就选哪个，会误选场内短号导致 ID 对接失败。
    """
    by_key = {}
    for column in frame.columns:
        key = str(column).strip().lower()
        by_key.setdefault(key, column)
    candidates = [by_key[alias] for alias in aliases if alias in by_key]
    return (candidates[0] if candidates else None), candidates


def _detect_egg_columns(frame, id_aliases, date_aliases, path):
    """
    识别 ID 列与日期列。优先按别名优先级，其次按内容特征。
    两者都识别不出时报错，绝不"用最后一列凑合"。

    返回 (id_column, date_column, selection)，selection 记录候选与取舍依据，
    供上层写入统计以满足可追溯要求。
    """
    id_column, id_candidates = _match_by_alias_priority(frame, id_aliases)
    date_column, date_candidates = _match_by_alias_priority(frame, date_aliases)
    id_source = "alias" if id_column is not None else None
    date_source = "alias" if date_column is not None else None

    # ID 与日期撞同一列时，让日期让位（ID 优先级更高，日期还有内容特征兜底）
    if id_column is not None and date_column is not None and id_column == date_column:
        date_column = next((c for c in date_candidates if c != id_column), None)
        date_source = "alias" if date_column is not None else None

    if date_column is None:
        for column in frame.columns:
            if column == id_column:
                continue
            sample = frame[column].dropna().astype(str).head(20)
            if sample.empty:
                continue
            hit = sample.str.match(r"\d{4}[-/年.]\d{1,2}[-/月.]\d{1,2}").mean()
            if hit >= 0.8:
                date_column = column
                date_source = "content_pattern"
                break
    if id_column is None:
        for column in frame.columns:
            if column == date_column:
                continue
            sample = frame[column].dropna().astype(str).head(20)
            if not sample.empty and sample.str.match(r"^[A-Za-z0-9_\-]+$").all():
                id_column = column
                id_source = "content_pattern"
                break

    if id_column is None or date_column is None:
        raise core.AbgesError(
            "DT006", "捡蛋记录无法识别 ID/日期列：%s" % os.path.basename(path),
            {"columns": [str(name) for name in frame.columns],
             "id_column": str(id_column), "date_column": str(date_column)})

    selection = {
        "id_candidates": [str(c) for c in id_candidates],
        "id_rejected": [str(c) for c in id_candidates if c != id_column],
        "id_source": id_source,
        "date_candidates": [str(c) for c in date_candidates],
        "date_rejected": [str(c) for c in date_candidates if c != date_column],
        "date_source": date_source,
    }
    return id_column, date_column, selection


def correct_daily_records(egg_frame):
    """
    捡蛋记录纠错 v2，返回 (纠正后 DataFrame[ID,date], 纠错统计, 双产记录)。

    规则（与 skills/04-derived-trait-calculation/references/clutch_algorithm.md 一致）：
      同日 >=3 条                     → 整天丢弃
      同日 =2 条且前一天有该鸡记录     → 保留第一条，删除第二条
      同日 =2 条且前一天无该鸡记录     → 保留第一条在原日，第二条前向搜索最近空档平移；
                                        T+1~T+7 均无空档时记为双产日（连产按 1 枚计入）
      同日 =1 条                      → 直接保留

    2026-10-17 重写，修正四项缺陷：
      1. 改为按“天”遍历（原按“记录”遍历，同一天的两条会各判一次，导致两条都被
         平移、原日整日丢失——真实产蛋日被抹掉，连产长度虚高）。
      2. 每个个体独立维护已占用日期集合 used（原用全局 rows[-10:] 判断，
         会跨个体读到别的鸡的日期，多鸡混表时结果错乱）。
      3. >=3 条整天丢弃只记一次（原逐条累加，3 条记成 9）。
      4. 双产日真正落地（原 double_egg_days / double_records 恒为空）。
    """
    import pandas as pd
    from datetime import timedelta

    working = egg_frame.copy()
    working["date"] = pd.to_datetime(working["date"]).dt.normalize()
    # {(ID, date): 条数}，仅用于统计口径
    date_count = working.groupby(["ID", "date"]).size().to_dict()
    # 前向搜索窗口：T+1 ~ T+7
    max_forward = 7

    discarded = 0
    discarded_days = 0
    shifted = 0
    double_egg_days = 0
    rows = []
    double_records = []

    for identifier, group in working.groupby("ID"):
        # 该个体逐日条数（升序遍历，保证同一天只判定一次）
        day_counts = group.groupby("date").size().to_dict()
        own_dates = set(day_counts)
        used = set()          # 该个体最终占用的日期，含平移新产生的日期

        for day in sorted(own_dates):
            cnt = day_counts[day]
            if cnt >= 3:
                # 整天丢弃：一次记清，不再逐条累加
                discarded += cnt
                discarded_days += 1
                continue
            # 无论 1 条还是 2 条，当天都保留 1 条（2 条时即“保留第一条”）
            rows.append({"ID": identifier, "date": day})
            used.add(day)
            if cnt == 1:
                continue

            prev_has = (day - timedelta(days=1)) in own_dates
            if prev_has:
                # 前一天有该鸡记录 → 判定为重复登记，删除第二条
                discarded += 1
                continue
            # 前一天无记录 → 疑似漏记，第二条前向搜索最近空档平移
            placed = False
            for offset in range(1, max_forward + 1):
                candidate = day + timedelta(days=offset)
                if candidate in own_dates or candidate in used:
                    continue
                rows.append({"ID": identifier, "date": candidate})
                used.add(candidate)
                shifted += 1
                placed = True
                break
            if not placed:
                # T+1~T+7 全被占用（长连产）→ 记为双产日：第二条不再另占一日，
                # 连产按 1 枚计入（生理上限），该条等同丢弃。
                double_egg_days += 1
                discarded += 1
                double_records.append({"ID": identifier, "date": day,
                                       "records": int(cnt)})

    corrected = pd.DataFrame(rows)
    if corrected.empty:
        corrected = pd.DataFrame(columns=["ID", "date"])
    else:
        corrected = (corrected.drop_duplicates(subset=["ID", "date"])
                     .sort_values(["ID", "date"]).reset_index(drop=True))
    statistics = {
        "raw_records": int(egg_frame.shape[0]),
        "individual_days_raw": int(len(date_count)),
        "discarded": discarded,
        "discarded_days": discarded_days,
        "shifted": shifted,
        "double_egg_days": double_egg_days,
        "individual_days_final": int(corrected.shape[0]),
        "individuals": int(corrected["ID"].nunique()),
    }
    return corrected, statistics, double_records


# ===============================================================
# 四、record 型：ACL / MCL / CEN
# ===============================================================

def compute_clutch_traits(egg_source, config=None):
    """
    计算 ACL（平均连产长度）、MCL（最大连产长度）、NCC（连产周期数）。
    连产定义：连续产蛋天数序列，遇停产日中断。
    统计窗口取个体自身首末产蛋日之间的连续日历日。
    """
    import pandas as pd
    import numpy as np

    config = config or load_derived_config()
    acl_definition = get_derived_definition("ACL", config)
    mcl_definition = get_derived_definition("MCL", config)
    ncc_definition = get_derived_definition("NCC", config)

    egg_frame, file_detail = read_egg_records(egg_source, config)
    corrected, correction_stats, _ = correct_daily_records(egg_frame)

    records = []
    for identifier, group in corrected.groupby("ID"):
        laid_dates = set(group["date"].dt.date)
        span = pd.date_range(group["date"].min(), group["date"].max())
        sequence = [1 if day.date() in laid_dates else 0 for day in span]
        clutches = []
        current = 0
        for flag in sequence:
            if flag == 1:
                current += 1
            else:
                if current > 0:
                    clutches.append(current)
                current = 0
        if current > 0:
            clutches.append(current)
        if clutches:
            records.append({
                "ID": str(identifier),
                "ACL": round(float(np.mean(clutches)), 4),
                "MCL": int(max(clutches)),
                "NCC": int(len(clutches)),
                "total_eggs": int(len(laid_dates)),
            })

    frame = pd.DataFrame(records)
    result = DerivedTraitResult("ACL+MCL+NCC")
    if frame.empty:
        result.status = core.STATUS_FAILED
        result.warnings.append("无任何个体可计算连产性状")
        result.frame = frame
        return result

    # validation
    for trait_id, definition in (("ACL", acl_definition), ("MCL", mcl_definition), ("NCC", ncc_definition)):
        for expression in definition.get("validation", []) or []:
            namespace = {"ACL": frame["ACL"], "MCL": frame["MCL"], "NCC": frame["NCC"]}
            ok_mask = _evaluate_condition(expression, namespace, frame.index)
            bad_mask = ~ok_mask
            if bad_mask.any():
                result.failures.append({
                    "trait": trait_id,
                    "check": expression,
                    "count": int(bad_mask.sum()),
                    "examples": frame.loc[bad_mask, ["ID", trait_id]]
                                     .head(10).to_dict("records"),
                })

    result.frame = frame
    result.statistics = {
        "algorithm": "clutch",
        "files": file_detail,
        "correction": correction_stats,
        "individuals": int(frame.shape[0]),
        "ACL": {
            "mean": round(float(frame["ACL"].mean()), 6),
            "sd": round(float(frame["ACL"].std(ddof=1)), 6) if frame.shape[0] > 1 else None,
            "min": round(float(frame["ACL"].min()), 6),
            "max": round(float(frame["ACL"].max()), 6),
        },
        "MCL": {
            "mean": round(float(frame["MCL"].mean()), 6),
            "min": int(frame["MCL"].min()),
            "max": int(frame["MCL"].max()),
        },
        "NCC": {
            "mean": round(float(frame["NCC"].mean()), 6),
            "min": int(frame["NCC"].min()),
            "max": int(frame["NCC"].max()),
        },
    }
    result.status = core.STATUS_PARTIAL if result.failures else core.STATUS_COMPLETED
    return result


def compute_cumulative_egg_number(egg_source, config=None):
    """
    计算 CEN（累计产蛋数）= 统计区间内去重日期计数。
    区间由捡蛋记录文件本身界定（文件已按目标日龄裁剪）。
    """
    import pandas as pd

    config = config or load_derived_config()
    get_derived_definition("CEN", config)

    egg_frame, file_detail = read_egg_records(egg_source, config)
    corrected, correction_stats, _ = correct_daily_records(egg_frame)
    counts = (corrected.groupby("ID")["date"].nunique()
              .reset_index(name="CEN"))
    counts["ID"] = counts["ID"].astype(str)

    result = DerivedTraitResult("CEN")
    result.frame = counts
    result.statistics = {
        "algorithm": "cumulative_egg_number",
        "files": file_detail,
        "correction": correction_stats,
        "individuals": int(counts.shape[0]),
        "mean": round(float(counts["CEN"].mean()), 6),
        "min": int(counts["CEN"].min()),
        "max": int(counts["CEN"].max()),
    }
    result.status = core.STATUS_COMPLETED if counts.shape[0] else core.STATUS_FAILED
    return result


# ===============================================================
# 五、合并回表型
# ===============================================================

def merge_derived_into_phenotype(pheno_frame, derived_frame, id_column="ID",
                                 trait_columns=None):
    """
    把衍生性状按 ID 左连接回表型表，并给出匹配率。
    匹配率过低意味着编号体系不一致，必须显式暴露而不是静默产生一堆缺失。
    """
    import pandas as pd

    if derived_frame is None or derived_frame.empty:
        raise core.AbgesError("DT008", "衍生性状表为空，无法合并")
    trait_columns = trait_columns or [name for name in derived_frame.columns
                                      if name != "ID"]
    left = pheno_frame.copy()
    left[id_column] = dataio.normalize_identifier_series(left[id_column])
    right = derived_frame.copy()
    right["ID"] = dataio.normalize_identifier_series(right["ID"])
    right = right[["ID"] + trait_columns]

    pheno_ids = set(left[id_column])
    derived_ids = set(right["ID"])
    overlap = pheno_ids & derived_ids
    match_rate = len(overlap) / len(derived_ids) if derived_ids else 0.0

    merged = left.merge(right, left_on=id_column, right_on="ID", how="left",
                        suffixes=("", "_derived"))
    if "ID_derived" in merged.columns:
        merged = merged.drop(columns=["ID_derived"])
    for column in trait_columns:
        if column in merged.columns:
            merged[column] = pd.to_numeric(merged[column], errors="coerce")

    report = {
        "phenotype_ids": len(pheno_ids),
        "derived_ids": len(derived_ids),
        "matched_ids": len(overlap),
        "match_rate": round(match_rate, 6),
        "traits_merged": trait_columns,
        "merged_valid_counts": {column: int(merged[column].notna().sum())
                                for column in trait_columns
                                if column in merged.columns},
    }
    return merged, report
