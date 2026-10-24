# -*- coding: utf-8 -*-
"""
ABGES 系谱质量控制模块
======================
严格实现 configs/qc/pedigree-qc.yaml 中 PD001–PD015 规则。

系谱错误会直接污染 A 矩阵与全部 EBV，因此本模块多数规则为强门控：
  - PD007 循环系谱：BLOCK，必须输出完整环路径
  - PD013 最小规模：BLOCK
  - PD006 性别冲突：HUMAN_CONFIRMATION_REQUIRED，绝不自动改
近交系数采用 Meuwissen–Luo 共祖系数递推，可选 Henderson 递推交叉验证。
"""

import sys
import re

import abges_core as core
import abges_dataio as dataio


PEDIGREE_QC_CONFIG = "configs/qc/pedigree-qc.yaml"

# 系谱三列的同义表头（小写比较）
PEDIGREE_HEADER_MAP = {
    "id": "ID", "个体号": "ID", "个体id": "ID", "个体编号": "ID",
    "翅号": "ID", "鸡号": "ID", "animal": "ID", "individual": "ID",
    "sire": "Sire", "父": "Sire", "父本": "Sire", "父号": "Sire",
    "父id": "Sire", "father": "Sire", "公": "Sire",
    "dam": "Dam", "母": "Dam", "母本": "Dam", "母号": "Dam",
    "母id": "Dam", "mother": "Dam", "母鸡": "Dam",
    # 可选列（WT 核查维度）：世代 / 出生日期 / 独立翅号列
    "gen": "Gen", "世代": "Gen", "代次": "Gen", "generation": "Gen",
    "birth": "BirthDate", "出生日期": "BirthDate", "生日": "BirthDate",
    "birthdate": "BirthDate", "出壳日期": "BirthDate", "hatch": "BirthDate",
    "wing": "Wing", "翼号": "Wing", "翅号2": "Wing",
}


class PedigreeQCResult(object):
    """系谱 QC 结果容器。"""

    def __init__(self):
        self.frame = None
        self.change_log = []
        self.warnings = []
        self.notes = []
        self.human_confirmations = []
        self.statistics = {}
        self.inbreeding = {}
        self.cycles = []
        self.exclusions = []
        self.exclusion_summary = {}
        self.final_graph_valid = False
        self.minimum_size_passed = False
        self.deduplicated_records = 0
        self.generation = {}        # PD014：世代判定结果与置信度
        self.lineage = {}           # PD015：血缘链追溯摘要

    def log_change(self, rule_id, action, affected, reason, detail=None):
        self.change_log.append({
            "rule_id": rule_id,
            "action": action,
            "affected": int(affected),
            "reason": reason,
            "detail": detail or {},
        })

    def to_dict(self):
        return {
            "change_log": self.change_log,
            "warnings": self.warnings,
            "notes": self.notes,
            "human_confirmations": self.human_confirmations,
            "statistics": self.statistics,
            "cycles": self.cycles[:20],
            "exclusions": self.exclusions,
            "exclusion_summary": self.exclusion_summary,
            "final_graph_valid": self.final_graph_valid,
            "minimum_size_passed": self.minimum_size_passed,
            "deduplicated_records": self.deduplicated_records,
            "generation": self.generation,
            "lineage": self.lineage,
        }


# ===============================================================
# 一、读取与标准化
# ===============================================================

def load_pedigree(file_path, sheet=None, config=None):
    """读取系谱文件并标准化为 ID/Sire/Dam 三列。"""
    config = config or core.load_yaml_config(PEDIGREE_QC_CONFIG)
    frame, info = dataio.read_table(file_path, sheet=sheet if sheet is not None else 0)
    frame, rename_detail, _ = dataio.normalize_header(frame, PEDIGREE_HEADER_MAP)
    dataio.require_columns(frame, ["ID", "Sire", "Dam"], "PD001", "系谱三列结构")
    return frame, info, rename_detail


def standardize_pedigree(frame, config=None):
    """
    把任意来源的三列系谱标准化：ID/Sire/Dam 去空白、去 .0，
    未知亲本统一为 missing_value（默认 NA）。
    保留原始行号便于回溯。
    """
    import pandas as pd

    config = config or core.load_yaml_config(PEDIGREE_QC_CONFIG)
    input_config = config.get("input", {})
    missing_token = input_config.get("missing_value", "NA")
    unknown_tokens = set(str(token).strip().lower()
                         for token in input_config.get("unknown_tokens", []))

    dataio.require_columns(frame, ["ID", "Sire", "Dam"], "PD001", "系谱三列结构")

    def normalize(value):
        text = dataio.normalize_identifier(value, missing_token)
        if text.strip().lower() in unknown_tokens:
            return missing_token
        return text

    output = pd.DataFrame()
    output["SourceRow"] = list(range(2, frame.shape[0] + 2))
    for column in ("ID", "Sire", "Dam"):
        output[column] = [normalize(value) for value in frame[column]]

    # 携带可选列（WT 核查维度所需：Gen / BirthDate / Wing）。若原始文件未提供，
    # 这些列不存在，下游 PD014/PD003/PD008/PD010 自动跳过。
    optional_cols = ((config.get("input", {}) or {}).get("optional_columns", {}) or {})
    for _role, col in optional_cols.items():
        if col in frame.columns and col not in output.columns:
            output[col] = list(frame[col].values)
    return output, missing_token


# ===============================================================
# 二、结构检查
# ===============================================================

def find_cycles(ped_frame, missing_token="NA", max_cycles=100):
    """
    DFS 检测祖先链闭环（A→B→C→A）。返回最多 max_cycles 条环路径。
    PD007 要求输出完整路径供人工修复，因此保留完整节点序列。
    """
    parent_map = _build_parent_map(ped_frame)
    sys.setrecursionlimit(max(10000, len(parent_map) + 2000))

    WHITE, GRAY, BLACK = 0, 1, 2
    color = dict((key, WHITE) for key in parent_map)
    path = []
    cycles = []

    def visit(node):
        color[node] = GRAY
        path.append(node)
        sire, dam = parent_map.get(node, (missing_token, missing_token))
        for parent in (sire, dam):
            if parent in (None, missing_token) or parent not in parent_map:
                continue
            if color[parent] == GRAY:
                start = path.index(parent)
                cycles.append(path[start:] + [parent])
                if len(cycles) >= max_cycles:
                    return
            elif color[parent] == WHITE:
                visit(parent)
                if len(cycles) >= max_cycles:
                    return
        path.pop()
        color[node] = BLACK

    for node in list(parent_map.keys()):
        if color[node] == WHITE:
            visit(node)
            if len(cycles) >= max_cycles:
                break
    return cycles


def _build_parent_map(ped_frame):
    """构造 {ID: (Sire, Dam)}。重复 ID 以首次出现为准。"""
    parent_map = {}
    for identifier, sire, dam in zip(ped_frame["ID"], ped_frame["Sire"],
                                     ped_frame["Dam"]):
        if identifier not in parent_map:
            parent_map[identifier] = (sire, dam)
    return parent_map


def topological_order(ped_frame, missing_token="NA"):
    """
    返回拓扑序（亲本必先于子代）。HIBLUP 对系谱顺序有硬要求。
    """
    parent_map = _build_parent_map(ped_frame)
    sys.setrecursionlimit(max(10000, len(parent_map) + 2000))
    visited = set()
    order = []

    def visit(node):
        if node in visited:
            return
        visited.add(node)
        sire, dam = parent_map.get(node, (missing_token, missing_token))
        for parent in (sire, dam):
            if parent not in (None, missing_token) and parent in parent_map:
                visit(parent)
        order.append(node)

    for node in ped_frame["ID"]:
        visit(node)
    return order


def compute_generation_depth(ped_frame, missing_token="NA"):
    """计算每个个体的系谱深度（奠基者为 0），返回 {ID: depth}。"""
    parent_map = _build_parent_map(ped_frame)
    depth = {}
    for node in topological_order(ped_frame, missing_token):
        sire, dam = parent_map.get(node, (missing_token, missing_token))
        parent_depths = [depth.get(parent, -1)
                         for parent in (sire, dam)
                         if parent not in (None, missing_token) and parent in parent_map]
        depth[node] = (max(parent_depths) + 1) if parent_depths else 0
    return depth


# ===============================================================
# 三、近交系数
# ===============================================================

def compute_inbreeding(ped_frame, missing_token="NA"):
    """
    Meuwissen–Luo 共祖系数递推计算近交系数。
    返回 {ID: {"F": 值, "note": 说明}}。
    单亲缺失按 F=0 处理（可能低估，note 中说明）。
    """
    parent_map = _build_parent_map(ped_frame)
    sys.setrecursionlimit(max(10000, len(parent_map) + 2000))
    phi_memo = {}
    ancestor_memo = {}

    def is_ancestor(candidate, target):
        if candidate == target:
            return False
        key = (candidate, target)
        if key in ancestor_memo:
            return ancestor_memo[key]
        if target not in parent_map:
            ancestor_memo[key] = False
            return False
        sire, dam = parent_map[target]
        answer = (sire == candidate or dam == candidate
                  or (sire not in (None, missing_token) and is_ancestor(candidate, sire))
                  or (dam not in (None, missing_token) and is_ancestor(candidate, dam)))
        ancestor_memo[key] = answer
        return answer

    def kinship(left, right):
        if left == missing_token or right == missing_token:
            return 0.0
        if left == right:
            key = (left, right)
            if key in phi_memo:
                return phi_memo[key]
            sire, dam = parent_map.get(left, (missing_token, missing_token))
            if sire == missing_token and dam == missing_token:
                value = 0.5
            else:
                value = 0.5 * (1.0 + kinship(sire, dam))
            phi_memo[key] = value
            return value
        ordered = (left, right) if left <= right else (right, left)
        if ordered in phi_memo:
            return phi_memo[ordered]
        if is_ancestor(left, right):
            sire, dam = parent_map.get(right, (missing_token, missing_token))
            value = 0.5 * (kinship(left, sire) + kinship(left, dam))
        else:
            sire, dam = parent_map.get(left, (missing_token, missing_token))
            value = 0.5 * (kinship(sire, right) + kinship(dam, right))
        phi_memo[ordered] = value
        return value

    results = {}
    for identifier in ped_frame["ID"]:
        sire, dam = parent_map.get(identifier, (missing_token, missing_token))
        if sire == missing_token and dam == missing_token:
            results[identifier] = {"F": 0.0, "note": "双亲未知，按基础群 F=0"}
        elif sire == missing_token or dam == missing_token:
            results[identifier] = {"F": 0.0, "note": "单亲缺失，F 按 0 处理（可能低估）"}
        else:
            results[identifier] = {"F": float(kinship(sire, dam)), "note": ""}
    return results


def compute_inbreeding_henderson(ped_frame, missing_token="NA",
                                 memory_limit_gb=4.0):
    """
    Henderson 关系矩阵递推，作为与 Meuwissen–Luo 独立的交叉验证算法。
    N×N 稠密矩阵超过内存上限时明确报错，不静默降级。
    """
    import numpy as np

    parent_map = _build_parent_map(ped_frame)
    order = topological_order(ped_frame, missing_token)
    size = len(order)
    estimated_bytes = size * size * 8
    if estimated_bytes > memory_limit_gb * 1024 ** 3:
        raise core.AbgesError(
            "PD012",
            "Henderson 需 %d×%d 关系矩阵(约 %.1fGB)，超过上限 %.1fGB"
            % (size, size, estimated_bytes / 1024 ** 3, memory_limit_gb),
            {"n": size})

    index_of = dict((identifier, position)
                    for position, identifier in enumerate(order))
    matrix = np.zeros((size, size), dtype=float)
    results = {}
    for position, identifier in enumerate(order):
        sire, dam = parent_map.get(identifier, (missing_token, missing_token))
        sire_index = index_of.get(sire, -1) if sire != missing_token else -1
        dam_index = index_of.get(dam, -1) if dam != missing_token else -1
        if sire_index >= 0 and dam_index >= 0:
            coefficient = 0.5 * matrix[sire_index, dam_index]
        else:
            coefficient = 0.0
        matrix[position, position] = 1.0 + coefficient
        row = np.zeros(position, dtype=float)
        if sire_index >= 0:
            row += matrix[sire_index, :position]
        if dam_index >= 0:
            row += matrix[dam_index, :position]
        row *= 0.5
        matrix[position, :position] = row
        matrix[:position, position] = row
        results[identifier] = {"F": float(coefficient), "note": ""}
    return results, matrix


def cross_validate_inbreeding(ped_frame, missing_token="NA", tolerance=1e-9):
    """
    两套独立算法交叉验证近交系数。返回 (是否一致, 最大差值, 不一致明细)。
    """
    primary = compute_inbreeding(ped_frame, missing_token)
    secondary, _ = compute_inbreeding_henderson(ped_frame, missing_token)
    max_diff = 0.0
    mismatches = []
    for identifier, entry in primary.items():
        other = secondary.get(identifier)
        if other is None:
            continue
        diff = abs(entry["F"] - other["F"])
        max_diff = max(max_diff, diff)
        if diff > tolerance:
            mismatches.append({"ID": identifier,
                               "meuwissen_luo": entry["F"],
                               "henderson": other["F"],
                               "diff": diff})
    return (not mismatches), max_diff, mismatches[:50]


# ===============================================================
# 三（补）、WT 核查维度补齐：PD014–PD010
# ===============================================================

def _pattern_signature(value):
    """把 ID/翅号 归并为模式签名，用于编码格式自洽判定（如 A001 与 B001 同签名）。"""
    s = "" if value is None else str(value).strip()
    s = re.sub(r"\d", "#", s)
    s = re.sub(r"[A-Za-z]", "A", s)
    return s


def clean_dirty_identifiers(working, missing_token="NA"):
    """
    PD003 脏数据归一：对 ID/Sire/Dam 去前后空白、全角转半角、压缩内部多余空白；
    并检测"仅大小写差异"的潜在重复 ID（如 A1 与 a1），返回告警而不自动合并。
    """
    import unicodedata
    changes = 0
    for column in ("ID", "Sire", "Dam"):
        new_vals = []
        for value in working[column]:
            if value == missing_token:
                new_vals.append(value)
                continue
            cleaned = unicodedata.normalize("NFKC", str(value)).strip()
            cleaned = re.sub(r"\s+", "", cleaned)
            if cleaned == "":
                cleaned = missing_token
            if cleaned != str(value):
                changes += 1
            new_vals.append(cleaned)
        working[column] = new_vals

    # 仅大小写差异的潜在重复
    case_collisions = []
    seen = {}
    for value in working["ID"]:
        if value == missing_token:
            continue
        key = str(value).lower()
        if key in seen and seen[key] != value:
            case_collisions.append({"a": seen[key], "b": value})
        else:
            seen.setdefault(key, value)
    return working, changes, case_collisions


def detect_exact_duplicate_rows(working, keep="first"):
    """
    PD002 系谱关系重复检测：仅按 ID/Sire/Dam 三列去重，可选字段差异不改变关系身份。
    返回 (frame, 被剔除样本列表)。
    """
    cols = [column for column in ("ID", "Sire", "Dam") if column in working.columns]
    samples = []
    if working.duplicated(subset=cols, keep=False).any():
        for _, row in working[working.duplicated(subset=cols, keep=False)].iterrows():
            samples.append({c: row[c] for c in cols})
        working = working.drop_duplicates(subset=cols, keep=keep).reset_index(drop=True)
    return working, samples


def determine_generation(working, missing_token="NA", gen_col="Gen"):
    """
    PD014 世代判定与置信声明。
    优先级：① 显式世代列（gen_col）存在 → 直接采用（置信度 高）；
            ② 否则按拓扑推断遗传世代（genetic generation，置信度 中）。
    返回 (gen_of: {ID:int}, method, conflicts)。
    """
    topo_depth = compute_generation_depth(working, missing_token)
    gen_of = {}
    method = "topological"
    conflicts = []

    if gen_col in working.columns:
        import pandas as pd
        raw = working.set_index("ID")[gen_col]
        present = [str(v).strip() for v in raw.values
                   if str(v).strip() not in ("", "nan", "None", missing_token)]

        def _to_int(v):
            d = re.sub(r"\D", "", str(v).strip())
            return int(d) if d else None

        parsed = {}
        if present:
            if all(_to_int(v) is not None for v in present):
                parsed = {k: _to_int(v) for k, v in raw.items()}
            else:
                uniq = sorted(set(present))
                rank = {u: i for i, u in enumerate(uniq)}
                parsed = {k: rank.get(str(v).strip()) for k, v in raw.items()}
        if parsed:
            gen_of = {k: v for k, v in parsed.items() if v is not None}
            method = "explicit"
            diffs = [int(gen_of[k]) - int(topo_depth[k])
                     for k in gen_of if k in topo_depth and topo_depth[k] is not None]
            if diffs:
                offset = max(set(diffs), key=diffs.count)
                for k in gen_of:
                    t = topo_depth.get(k)
                    if t is not None and (int(gen_of[k]) - int(t) - offset) != 0:
                        conflicts.append({"ID": k, "explicit": gen_of[k], "topological": t})
    if not gen_of:
        gen_of = {k: int(v) for k, v in topo_depth.items()}
        method = "topological"
    return gen_of, method, conflicts


def check_wing_encoding(working, gen_of, wing_col="Wing"):
    """
    PD003 翅号/编码唯一性与格式自洽。同世代内个体ID（或独立翅号列）应唯一且编码格式一致。
    无世代列时退化为全局检查。返回告警 dict 列表。
    """
    import pandas as pd
    warns = []
    target = wing_col if (wing_col and wing_col in working.columns) else "ID"
    if gen_of:
        group_key = working["ID"].map(lambda i: gen_of.get(i))
    else:
        group_key = pd.Series([0] * len(working))
    scoped = working.assign(_grp=group_key)
    for grp, sub in scoped.groupby("_grp"):
        if grp is None:
            continue
        if wing_col and wing_col in working.columns:
            vals = sub[wing_col].replace({"": None}).dropna()
            dup = vals[vals.duplicated(keep=False)]
            if not dup.empty:
                warns.append({"message": "世代 %s 内翅号列重复 %d 例" % (grp, int(dup.shape[0])),
                              "sample": sorted(set(dup.astype(str)))[:10]})
        sigs = sub[target].map(_pattern_signature)
        if sigs.nunique() > 1:
            warns.append({"message": "世代 %s 内 %s 编码格式不一致（%d 种模式）：%s"
                                   % (grp, target, sigs.nunique(), sorted(set(sigs)))})
    return warns


def validate_birth_dates(working, birth_col="BirthDate", missing_token="NA"):
    """
    PD008 出生日期格式与越界校验。拒绝无法解析、年份异常（<1900 或 > 明年）。
    月日越界（如 02/30）由解析器直接判为无法解析。返回 {"invalid": [...]}。
    """
    import datetime as _dt
    import pandas as pd
    invalid = []
    series = working[birth_col].astype(str).str.strip()
    parsed = dataio.parse_flexible_date(working[birth_col])
    low_tokens = ("", "nan", "none", missing_token.lower())
    current_year = _dt.date.today().year
    for idx in range(len(working)):
        raw = str(series.iloc[idx]).strip()
        if raw.lower() in low_tokens:
            continue
        dt = parsed.iloc[idx]
        if pd.isna(dt):
            invalid.append({"ID": working.iloc[idx]["ID"], "value": raw, "reason": "无法解析"})
        elif dt.year < 1900 or dt.year > current_year + 1:
            invalid.append({"ID": working.iloc[idx]["ID"], "value": raw,
                            "reason": "年份异常 %d" % dt.year})
    return {"invalid": invalid}


def trace_lineages(working, missing_token="NA", recent_generations=2):
    """
    PD015 血缘中断 / 链追溯（可查询性）。对最近 recent_generations 世代个体沿父系/母系链
    向上追溯，分类终点（合法基础群 / 记录缺失 / 断链），汇总链完整率与平均链长。
    须在 PD005 补建奠基者之前调用，以免真实断链被掩盖。
    """
    parent_map = _build_parent_map(working)
    depth = compute_generation_depth(working, missing_token)
    max_depth = max(depth.values()) if depth else 0
    threshold = max_depth - recent_generations + 1
    targets = [i for i, d in depth.items() if d >= threshold] or list(parent_map.keys())
    max_hops = len(parent_map) + 2

    def trace(start_id, side):
        node = parent_map.get(start_id, (missing_token, missing_token))
        nxt = node[0] if side == "Sire" else node[1]
        hops = 0
        chain = [start_id]
        while nxt not in (None, missing_token):
            chain.append(nxt)
            hops += 1
            if hops > max_hops:
                return hops, "loop_guard", chain
            if nxt not in parent_map:
                return hops, "chain_break", chain      # 祖先不在系谱集合 → 断链
            p_sire, p_dam = parent_map[nxt]
            if p_sire == missing_token and p_dam == missing_token:
                return hops, "legal_founder", chain
            nxt = p_sire if side == "Sire" else p_dam
        return hops, "record_missing_or_unknown", chain

    pat_end, mat_end, all_hops = [], [], []
    break_chains = []
    for tid in targets:
        ps, pe, pc = trace(tid, "Sire")
        pat_end.append(pe); all_hops.append(ps)
        if pe == "chain_break":
            break_chains.append({"ID": tid, "side": "Sire", "chain": pc})
        ms, me, mc = trace(tid, "Dam")
        mat_end.append(me); all_hops.append(ms)
        if me == "chain_break":
            break_chains.append({"ID": tid, "side": "Dam", "chain": mc})

    n = len(targets)
    pat_ok = sum(1 for e in pat_end if e != "chain_break")
    mat_ok = sum(1 for e in mat_end if e != "chain_break")
    return {
        "targets": n,
        "paternal_chain_completeness_rate": round(pat_ok / n, 6) if n else 0.0,
        "maternal_chain_completeness_rate": round(mat_ok / n, 6) if n else 0.0,
        "mean_chain_length": round(sum(all_hops) / len(all_hops), 4) if all_hops else 0.0,
        "break_chains": break_chains,
    }


def check_base_group_distribution(working, gen_col="Gen", missing_token="NA"):
    """
    PD010 基础群分布合理性。双亲缺失个体（基础群）应聚集于最早引入世代；
    散布后续世代即异常。需显式世代列方可判定。返回告警 dict 列表。
    """
    warns = []
    if gen_col not in working.columns:
        return warns
    founders = working[(working["Sire"] == missing_token)
                       & (working["Dam"] == missing_token)]
    if founders.empty:
        return warns

    def _g(v):
        d = re.sub(r"\D", "", str(v).strip())
        return int(d) if d else None

    f_gens = founders[gen_col].map(_g).dropna()
    if f_gens.empty:
        return warns
    min_gen = int(min(f_gens))
    offending = [r["ID"] for _, r in founders.iterrows()
                if _g(r[gen_col]) is not None and _g(r[gen_col]) > min_gen]
    if offending:
        warns.append({"message": "基础群（双亲缺失）散布于晚于最早世代(%d)的 %d 例，异常"
                               % (min_gen, len(offending)),
                      "sample_ids": offending[:20]})
    return warns


# ===============================================================
# 四、主流程
# ===============================================================

def run_pedigree_qc(frame, config=None, phenotype_sex=None):
    """按系谱 QC 规则剔除错误个体及其依赖后代，保留无关有效分支。"""
    import pandas as pd

    config = config or core.load_yaml_config(PEDIGREE_QC_CONFIG)
    rules = config.get("rules", {})
    result = PedigreeQCResult()
    working, missing_token = standardize_pedigree(frame, config)
    result.statistics["before"] = _pedigree_statistics(working, missing_token)
    exclusion_reason = {}

    def mark(ids, rule_id, reason):
        for identifier in ids:
            if identifier not in (None, missing_token):
                exclusion_reason.setdefault(identifier, {"rule_id": rule_id,
                                                         "reason": reason,
                                                         "cascade": False})

    working, dirty_changes, collisions = clean_dirty_identifiers(working, missing_token)
    if dirty_changes:
        result.log_change("PD003", "MODIFY", dirty_changes,
                          "标识符无歧义规范化（空白清理、全角转半角）")
    for pair in collisions:
        mark((pair["a"], pair["b"]), "PD003", "个体号仅大小写不同，身份不明确")
    source_records = working.copy()

    # 空 ID 行无法向后代关系定位，按行直接排除并留痕。
    missing_id = working["ID"] == missing_token
    for _, row in working.loc[missing_id].iterrows():
        missing_detail = {"ID": None, "source_row": row.get("SourceRow"),
                          "Sire": row.get("Sire"), "Dam": row.get("Dam"),
                          "rule_id": "PD002", "reason": "个体号缺失",
                          "cascade": False}
        optional_cols = (config.get("input", {}).get("optional_columns", {}) or {})
        for label, role in (("Gen", "generation_column"), ("Batch", "batch_column"),
                            ("Sex", "sex_column")):
            column = optional_cols.get(role, label)
            if column in working.columns:
                missing_detail[label] = row.get(column)
        result.exclusions.append(missing_detail)
    working = working.loc[~missing_id].copy()

    # 完全相同重复行只保留一行；冲突 ID 的所有记录均剔除。
    duplicate_mask = working["ID"].duplicated(keep=False)
    for identifier, group in working.loc[duplicate_mask].groupby("ID"):
        if len(set(zip(group["Sire"], group["Dam"]))) > 1:
            mark((identifier,), "PD002", "同一 ID 对应多个不同父母组合")
    working = working.loc[~working["ID"].isin(exclusion_reason)].copy()
    # 去重键只由个体号、父本号、母本号构成；其他可选字段不改变系谱身份。
    duplicate_key = ["ID", "Sire", "Dam"]
    keep_duplicate = rules.get("PD002", {}).get("keep", "first")
    duplicate_triples = working.duplicated(subset=duplicate_key, keep=False)
    duplicate_samples = working.loc[duplicate_triples, ["SourceRow", "ID", "Sire", "Dam"]].to_dict("records")
    dropped_duplicate_rows = working.loc[
        working.duplicated(subset=duplicate_key, keep=keep_duplicate)
    ].copy()
    before_dedup = len(working)
    working = working.drop_duplicates(subset=["ID", "Sire", "Dam"],
                                      keep=keep_duplicate).copy()
    deduplicated = before_dedup - len(working)
    result.deduplicated_records = deduplicated
    duplicate_optional = (config.get("input", {}).get("optional_columns", {}) or {})
    for _, row in dropped_duplicate_rows.iterrows():
        detail = {"ID": row["ID"], "source_row": row.get("SourceRow"),
                  "Sire": row["Sire"], "Dam": row["Dam"],
                  "rule_id": "PD002", "reason": "ID/Sire/Dam 完全重复，保留首条记录",
                  "cascade": False, "record_disposition": "duplicate_row_removed"}
        detail.update({label: row.get(column) for label, column in {
            "Gen": duplicate_optional.get("generation_column", "Gen"),
            "Batch": duplicate_optional.get("batch_column", "Batch"),
            "Sex": duplicate_optional.get("sex_column", "Sex"),
        }.items() if column in dropped_duplicate_rows.columns})
        result.exclusions.append(detail)
    if deduplicated:
        result.log_change("PD002", "DEDUPLICATE", deduplicated,
                          "ID、父本号和母本号完全相同的记录保留一条",
                          {"samples": duplicate_samples[:20]})

    self_ids = set(working.loc[((working["ID"] == working["Sire"])
                                | (working["ID"] == working["Dam"]))
                               & (working["ID"] != missing_token), "ID"])
    mark(self_ids, "PD004", "个体自身登记为父本或母本")
    double_parent = set(working.loc[(working["Sire"] == working["Dam"])
                                    & (working["Sire"] != missing_token), "ID"])
    mark(double_parent, "PD006", "父本号与母本号相同")

    ids = set(working["ID"])
    for _, row in working.iterrows():
        if row["ID"] not in exclusion_reason and any(
                p != missing_token and p not in ids for p in (row["Sire"], row["Dam"])):
            mark((row["ID"],), "PD005", "引用的非缺失亲本在系谱中无对应记录")

    sire_ids = set(working.loc[working["Sire"] != missing_token, "Sire"])
    dam_ids = set(working.loc[working["Dam"] != missing_token, "Dam"])
    # 系谱规则按有性生殖的 sire/dam 亲本角色定义，不将规则准入限制在
    # species 白名单。species 仅用于输入编码适配与报告描述。
    mark(sire_ids & dam_ids, "PD006", "同一亲本 ID 同时处于父本和母本角色")
    sex_column = (config.get("input", {}).get("optional_columns", {}) or {}).get("sex_column", "Sex")
    if phenotype_sex is None and sex_column in working.columns:
        phenotype_sex = dict(zip(working["ID"], working[sex_column]))
    if phenotype_sex:
        bad_sex = []
        sire_codes = set(str(value).upper() for value in
                         (config.get("species_sex_codes", {}).get("sire_male") or ["M", "MALE", "1", "公", "雄"]))
        dam_codes = set(str(value).upper() for value in
                        (config.get("species_sex_codes", {}).get("dam_female") or ["F", "FEMALE", "2", "母", "雌"]))
        bad_sex.extend(i for i in sire_ids if str(phenotype_sex.get(i, "")).strip().upper()
                       in dam_codes)
        bad_sex.extend(i for i in dam_ids if str(phenotype_sex.get(i, "")).strip().upper()
                       in sire_codes)
        mark(bad_sex, "PD006", "可用性别信息与亲本角色冲突")

    working = working.loc[~working["ID"].isin(exclusion_reason)].copy().reset_index(drop=True)
    while True:
        cycles = find_cycles(working, missing_token)
        if not cycles:
            break
        result.cycles.extend(["->".join(cycle) for cycle in cycles])
        cycle_ids = {identifier for cycle in cycles for identifier in cycle[:-1]}
        before_cycle_size = len(working)
        mark(cycle_ids, "PD007", "个体处于循环系谱环内")
        working = working.loc[~working["ID"].isin(exclusion_reason)].copy().reset_index(drop=True)
        if len(working) == before_cycle_size:
            raise core.AbgesError("PD007", "检测到循环但无法定位可剔除个体")

    opt = (config.get("input", {}) or {}).get("optional_columns", {}) or {}
    optional = opt
    gen_col = opt.get("generation_column", "Gen")
    birth_col = opt.get("birth_date_column", "BirthDate")
    gen_of, gen_method, gen_conflicts = determine_generation(working, missing_token, gen_col)
    generation_semantics = str(config.get("generation_column_semantics", "unspecified")).lower()
    has_explicit_genetic_generation = (
        generation_semantics in ("genetic", "genetic_generation")
        and gen_col in working.columns)
    if has_explicit_genetic_generation:
        for item in gen_conflicts:
            mark((item.get("ID"),), "PD014", "声明为遗传世代的字段与拓扑世代冲突")
        for identifier in list(working["ID"]):
            row = working.loc[working["ID"] == identifier].iloc[0]
            child_generation = gen_of.get(identifier)
            for parent in (row["Sire"], row["Dam"]):
                parent_generation = gen_of.get(parent)
                if (parent != missing_token and child_generation is not None
                        and parent_generation is not None and parent_generation >= child_generation):
                    mark((identifier,), "PD008", "遗传世代字段中亲本世代不早于子代")
    else:
        result.notes.append({"rule_id": "PD008",
                             "message": "世代字段未声明为遗传世代，不据此执行世代关系硬剔除"})
    if birth_col in working.columns:
        for item in validate_birth_dates(working, birth_col, missing_token)["invalid"]:
            mark((item.get("ID"),), "PD008", "出生日期无效：%s" % item.get("reason"))
        dates = dataio.parse_flexible_date(working[birth_col])
        date_by_id = dict(zip(working["ID"], dates))
        for _, row in working.iterrows():
            child_date = date_by_id.get(row["ID"])
            if child_date is None or pd.isna(child_date):
                continue
            for parent in (row["Sire"], row["Dam"]):
                parent_date = date_by_id.get(parent)
                if (parent != missing_token and parent_date is not None and not pd.isna(parent_date)
                        and child_date <= parent_date):
                    mark((row["ID"],), "PD008", "出生日期不晚于已知亲本")
    late_founder_generation = int(config.get("late_founder_generation", 0))
    topology_depth = compute_generation_depth(working, missing_token)
    for _, row in working.iterrows():
        generation = topology_depth.get(row["ID"])
        has_unknown_parent = any(parent == missing_token for parent in (row["Sire"], row["Dam"]))
        if (generation is not None and generation > late_founder_generation
                and has_unknown_parent):
            mark((row["ID"],), "PD010",
                 "系谱拓扑深度晚于基础群允许值仍有未知亲本（深度=%s，阈值=%s）"
                 % (generation, late_founder_generation))

    # 独立翅号列若在同一世代重复，冲突个体身份无法区分，整组剔除。
    wing_col = opt.get("wing_column", "Wing")
    if wing_col in working.columns:
        scoped = working.assign(_generation=working["ID"].map(lambda value: gen_of.get(value)))
        for _, group in scoped.groupby("_generation", dropna=False):
            wing_values = group[wing_col].astype(str).str.strip()
            valid_wings = wing_values[~wing_values.str.lower().isin(
                ("", "nan", "none", missing_token.lower()))]
            duplicate_wings = set(valid_wings[valid_wings.duplicated(keep=False)])
            if duplicate_wings:
                mark(group.loc[wing_values.isin(duplicate_wings), "ID"],
                     "PD003", "同世代翅号重复，个体身份无法区分")

    # 级联过滤，未知的 NA/0 父母按合法基础群保留。
    while True:
        present = set(working["ID"]) - set(exclusion_reason)
        newly = []
        for _, row in working.iterrows():
            if row["ID"] in exclusion_reason:
                continue
            parent = next((p for p in (row["Sire"], row["Dam"])
                           if p != missing_token and p not in present), None)
            if parent is not None:
                newly.append((row["ID"], parent))
        if not newly:
            break
        for child, parent in newly:
            if child not in exclusion_reason:
                exclusion_reason[child] = {"rule_id": "PD005",
                                           "reason": "亲本 %s 缺档或已剔除，级联剔除后代" % parent,
                                           "cascade": True}

    detail_fields = {
        "Gen": optional.get("generation_column", "Gen"),
        "Batch": optional.get("batch_column", "Batch"),
        "Sex": optional.get("sex_column", "Sex"),
    }
    for _, row in source_records.iterrows():
        info = exclusion_reason.get(row["ID"])
        if info and not any(e.get("source_row") == row.get("SourceRow")
                            and e.get("ID") == row["ID"] for e in result.exclusions):
            detail = {"ID": row["ID"], "source_row": row.get("SourceRow"),
                      "Sire": row.get("Sire"), "Dam": row.get("Dam"),
                      "rule_id": info["rule_id"], "reason": info["reason"],
                      "cascade": info["cascade"]}
            detail.update({label: row.get(column) for label, column in detail_fields.items()
                           if column in source_records.columns})
            result.exclusions.append(detail)
    rule_counts = {}
    for item in result.exclusions:
        key = item.get("rule_id") or "unknown"
        rule_counts[key] = rule_counts.get(key, 0) + 1
    grouped_exclusions = {}
    group_fields = {
        "世代": opt.get("generation_column", "Gen"),
        "批次": opt.get("batch_column", "Batch"),
        "性别": opt.get("sex_column", "Sex"),
        "父系家系": "Sire",
    }
    excluded_rows = {str(item.get("source_row")) for item in result.exclusions}
    for label, column in group_fields.items():
        if column not in source_records.columns:
            continue
        levels = {}
        for _, row in source_records.iterrows():
            value = str(row.get(column))
            if value.lower() in ("", "nan", "none"):
                value = "未知"
            cell = levels.setdefault(value, {"input_records": 0, "excluded_records": 0})
            cell["input_records"] += 1
            if str(row.get("SourceRow")) in excluded_rows:
                cell["excluded_records"] += 1
        for cell in levels.values():
            cell["excluded_rate"] = (cell["excluded_records"] / float(cell["input_records"])
                                     if cell["input_records"] else 0.0)
        grouped_exclusions[label] = levels
    result.exclusion_summary = {
        "total_excluded_records": len(result.exclusions),
        "duplicate_rows_removed": result.deduplicated_records,
        "cascade_excluded_records": sum(1 for item in result.exclusions if item.get("cascade")),
        "direct_excluded_records": sum(1 for item in result.exclusions
                                        if not item.get("cascade")
                                        and item.get("record_disposition") != "duplicate_row_removed"),
        "by_rule": rule_counts,
        "by_group": grouped_exclusions,
        "retained_records": None,
    }
    working = working.loc[~working["ID"].isin(exclusion_reason)].copy().reset_index(drop=True)
    if result.exclusions:
        result.log_change("PD001-PD008", "EXCLUDE", len(result.exclusions),
                          "剔除问题记录及依赖其亲本关系的后代")
        result.warnings.append({"rule_id": "PD001-PD008",
                                "message": "系谱 QC 剔除 %d 条记录，详见剔除清单" % len(result.exclusions),
                                "excluded_records": len(result.exclusions)})

    gen_of, gen_method, final_gen_conflicts = determine_generation(working, missing_token, gen_col)
    result.generation = {"method": gen_method, "conflicts": final_gen_conflicts,
                         "map": {k: int(v) for k, v in gen_of.items()}}
    result.lineage = (trace_lineages(working, missing_token,
                                    recent_generations=rules.get("PD015", {}).get("recent_generations", 5))
                      if not working.empty else {})
    if birth_col not in working.columns:
        result.notes.append({"rule_id": "PD008", "message": "无出生日期列，跳过出生日期检查"})
    for item in check_wing_encoding(working, gen_of, opt.get("wing_column", "Wing")):
        result.warnings.append({"rule_id": "PD003", "message": item["message"],
                                "sample": item.get("sample")})
    if result.lineage.get("break_chains"):
        result.warnings.append({"rule_id": "PD015",
                                "message": "检出断链 %d 条" % len(result.lineage["break_chains"]),
                                "sample": result.lineage["break_chains"][:20]})
    if has_explicit_genetic_generation:
        for item in check_base_group_distribution(working, gen_col, missing_token):
            result.warnings.append({"rule_id": "PD010", "message": item["message"],
                                    "sample": item.get("sample_ids")})
    elif gen_col in working.columns:
        result.notes.append({"rule_id": "PD010",
                             "message": "世代列未声明为遗传世代，不据此判断基础群分布"})
    else:
        result.notes.append({"rule_id": "PD010", "message": "无世代列，跳过基础群分布校验"})

    min_individuals = rules.get("PD013", {}).get("min_individuals", 50)
    result.minimum_size_passed = len(working) >= min_individuals
    if not result.minimum_size_passed:
        result.warnings.append({"rule_id": "PD013",
                                "message": "剔除后有效系谱个体数 %d 低于任务准入下限 %d；后续评估将阻断"
                                           % (len(working), min_individuals),
                                "retained_records": len(working),
                                "minimum_required": min_individuals})
    result.final_graph_valid = (not find_cycles(working, missing_token)
                                and all(p == missing_token or p in set(working["ID"])
                                        for p in list(working["Sire"]) + list(working["Dam"])))
    if not result.final_graph_valid:
        raise core.AbgesError("PD007", "剔除后系谱仍未通过最终有向图检查")
    phantom_ids = [i for pattern in rules.get("PD011", {}).get("phantom_patterns", [])
                   for i in working["ID"] if re.match(pattern, str(i))]
    if phantom_ids:
        result.warnings.append({"rule_id": "PD011", "message": "检出疑似幻影组个体 %d 个" % len(set(phantom_ids)),
                                "sample": sorted(set(phantom_ids))[:20]})
    if rules.get("PD012", {}).get("compute_inbreeding", True):
        result.inbreeding = compute_inbreeding(working, missing_token)
        threshold = rules.get("PD012", {}).get("inbreeding_flag_threshold", 0.0375)
        flagged = [i for i, entry in result.inbreeding.items() if entry["F"] >= threshold]
        if flagged:
            result.warnings.append({"rule_id": "PD012",
                                    "message": "近交系数达到 %.4f 的个体 %d 个" % (threshold, len(flagged)),
                                    "sample": sorted(flagged)[:20]})
    order = topological_order(working, missing_token)
    rank = dict((identifier, index) for index, identifier in enumerate(order))
    working = working.assign(_rank=working["ID"].map(rank)).sort_values("_rank").drop(columns=["_rank"]).reset_index(drop=True)
    result.log_change("PD009", "REORDER", len(working), "按亲本优先拓扑序重排")
    result.statistics["after"] = _pedigree_statistics(working, missing_token, result.inbreeding)
    result.statistics["after"]["ExcludedRecords"] = len(result.exclusions)
    result.statistics["after"]["FinalGraphValid"] = result.final_graph_valid
    result.statistics["after"]["MinimumSizePassed"] = result.minimum_size_passed
    result.exclusion_summary["retained_records"] = int(len(working))
    result.frame = working
    return result


def _pedigree_statistics(ped_frame, missing_token="NA", inbreeding=None):
    """产出 pedigree-qc.yaml statistics 段要求的全部指标。"""
    total = int(ped_frame.shape[0])
    known_sire = int((ped_frame["Sire"] != missing_token).sum())
    known_dam = int((ped_frame["Dam"] != missing_token).sum())
    both_known = int(((ped_frame["Sire"] != missing_token)
                      & (ped_frame["Dam"] != missing_token)).sum())
    founders = int(((ped_frame["Sire"] == missing_token)
                    & (ped_frame["Dam"] == missing_token)).sum())
    depth = compute_generation_depth(ped_frame, missing_token) if total else {}
    stats = {
        "Individuals": total,
        "DuplicateIDs": int(ped_frame["ID"].duplicated().sum()),
        "KnownSireRate": round(known_sire / total, 6) if total else 0.0,
        "KnownDamRate": round(known_dam / total, 6) if total else 0.0,
        "BothParentsKnownRate": round(both_known / total, 6) if total else 0.0,
        "SelfAsParent": int(((ped_frame["ID"] == ped_frame["Sire"])
                             | (ped_frame["ID"] == ped_frame["Dam"])).sum()),
        "FounderCount": founders,
        "MaxGenerationDepth": max(depth.values()) if depth else 0,
        "FinalValidIndividuals": total,
    }
    if inbreeding:
        values = [entry["F"] for entry in inbreeding.values()]
        stats["MeanInbreeding"] = round(sum(values) / len(values), 8) if values else 0.0
        stats["MaxInbreeding"] = round(max(values), 8) if values else 0.0
    return stats


# ===============================================================
# 五、引擎输入文件构建
# ===============================================================

def build_engine_pedigree(qc_result, output_path, missing_token="NA",
                          engine_missing="0"):
    """
    生成 HIBLUP 系谱输入文件：三列 ID/Sire/Dam，未知亲本写 0。

    HIBLUP 系谱输入中，未知亲本统一使用 engine_missing 表示。
    维持写 0 的既有口径以保证全量任务输出可追溯。
    """
    frame = qc_result.frame[["ID", "Sire", "Dam"]].copy()
    for column in ("Sire", "Dam"):
        frame[column] = frame[column].replace({missing_token: engine_missing})
    dataio.write_engine_table(frame, output_path, sep="\t", na_rep=engine_missing)
    return {
        "path": output_path,
        "rows": int(frame.shape[0]),
        "columns": ["ID", "Sire", "Dam"],
    }
