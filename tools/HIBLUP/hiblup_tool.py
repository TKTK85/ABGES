# -*- coding: utf-8 -*-
"""
ABGES HIBLUP 工具封装
=====================
把 HIBLUP 命令行引擎包装为受控工具，对外提供四个能力：
  1. verify_engine            —— 引擎可用性与版本核验
  2. assemble_hiblup_command  —— 按模型配置组装命令行
  3. run_hiblup               —— 受控执行（超时、线程、进程树终止）
  4. parse_hiblup_output      —— 解析 .vars/.rand/.beta/.log

【迁移留痕｜2026-10-12，先生拍板"迁移"】
  来源：ABGES_新项目/tools/HIBLUP/hiblup_tool.py
        迁移前 SHA256：e7e1d7d011e98658e3db9488dc65913887c8e28164635c6bedc4b167341807b4
  原因：ABGES 主项目缺失本模块，但 12 个文件在引用它（三个 stage runner、
        abges_grm_cache.py、abges_validate.py、harness 两个、多个脚本），
        导致 PBLUP/GBLUP/SSGBLUP 三条主链路导入即失败。
  迁移范围：**仅迁移本 .py，不迁移 hiblup.exe**。
        ABGES 侧 hiblup.exe 为 v1.7.0（2026-10-01 Release，可用）；
        ABGES_新项目 侧 hiblup.exe 已过期（执行即报 out of date）。
        故保留 ABGES 侧较新的可执行文件。
  兼容性核对（迁移前已完成）：
        - 本模块仅依赖标准库 + abges_core，两侧 abges_core 同源
          （AbgesError 均定义于 L40，require_readable_file 均定义于 L175）。
        - ABGES 侧实际调用的 10 个接口（extract_heritability、
          parse_hiblup_output、assemble_hiblup_command、AbgesError、run_hiblup、
          extract_genetic_correlations、resolve_hiblup_binary、verify_engine、
          EXIT_TIMEOUT、EXIT_NOT_FOUND）本模块全部提供，无缺口。
        - ABGES 侧全部为 `import hiblup_tool` + 属性访问，无 `from ... import` 形式。
  ✅ 已复核（v1.6.0 口径 → v1.7.0）：本模块若干解析口径源自 HIBLUP v1.6.0 实测，
        2026-10-13 已在 ABGES 侧 v1.7.0（2026-10-01 Release）上逐一实测复核：
        1. parse_convergence_flag —— 日志 "[Converged?] Yes!" 标记格式【一致】，
           判定返回 True，迭代数正常。
        2. .anova 表头 —— Factors Df SumSq MeanSq F Pr(>F)【一致】，残差行 F/P 仍为 "."。
        3. .vars 表头 —— Item Var Var_SE h2 h2_SE h2_Pr(Chisq)【一致】。
        4. 输出文件 _ad 后缀：v1.7.0 走 --bfile 的 GBLUP【不再产出 _ad 后缀】
           （v1.6.0 会加）。resolve_output_suffix 为探测式（"_ad" → ""），
           已自动兼容，实测文件定位与解析均正常，无需改动。
        5. detect_constant_fixed_effects —— 单水平列规避逻辑保留（v1.7.0 未复现崩溃，保留无害）。
        6. verify_engine / _extract_version —— 正确识别 "1.7.0"。
        7. 端到端验证应在配备相应引擎的隔离环境中完成；仓库不携带真实育种数据结果。

  【v1.7.0 新变化｜2026-10-13 已支持，先生指令"多性状 .anova 要支持"】
        v1.7.0 在多性状模式下产出【逐性状 .anova】（prefix.TRAIT.anova），
        而 v1.6.0 仅单性状产出 prefix.anova。原逻辑 `if not is_multi` 才解析，
        导致多性状的固定效应 F 检验被静默忽略。现改为逐性状解析，并提供两种视图：
          result["anova_by_trait"] = {性状: [记录]}            逐性状精取
          result["anova"]          = 合并列表，每条带 trait 字段  向后兼容
        ⚠ anova 必须保持【列表】：下游 runners/breeding_insights.anova_table()
          遍历它并对每项调 rec.get(...)，若改为 dict 会遍历字符串键而崩溃。
          单性状时 trait 为空串，runners/docx_report.py 据此不加"性状"列，版式不变。
        回归测试：tests/unit/test_multi_trait_anova.py（6 用例）。

关键实测口径（不得凭直觉简化）：
  - --pheno-pos / --dcovar / --qcovar / --rand 均为"每性状一个 token"，
    token 内部用逗号分隔列序号，该性状无对应效应时填 "0"。
  - 走 --bfile 的 GBLUP/SSGBLUP，HIBLUP 输出文件名带 _ad 后缀；
    走 --xrm 时不带。解析须按 ["_ad", ""] 顺序探测。
  - SSGBLUP 的 A 与 G 融合由单一 --alpha 控制，不是 omega/tau 两参数。
  - --ai-maxit 仅在 vc_method ∈ {AI, EMAI, HI} 时追加；
    --em-maxit 仅在 vc_method ∈ {EM, EMAI} 时追加。
  - 单水平离散固定效应不可估，且会让 HIBLUP 在写结果时崩溃，必须剔除。
"""

import os
import io
import re
import sys
import time
import logging
import subprocess

logger = logging.getLogger(__name__)

sys.path.insert(0, os.path.abspath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), os.pardir, "Python")))

from abges_core import (AbgesError, resolve_project_path, resolve_thread_count,
                        read_text_with_fallback, require_readable_file)

# 走 --bfile 的基因组分析，HIBLUP 会给输出文件加 _ad 后缀
GENOMIC_METHODS = {"gblup", "ssblup", "ssgblup"}
OUTPUT_SUFFIX_PROBE_ORDER = ("_ad", "")
REQUIRED_OUTPUT_EXTENSIONS = (".vars", ".rand", ".beta", ".log")

# 退出码语义（负值为 ABGES 自定义）
EXIT_TIMEOUT = -1
EXIT_NOT_FOUND = -2
EXIT_CANCELLED = -3


# ===============================================================
# 一、引擎核验
# ===============================================================

def resolve_hiblup_binary(configured_path=None):
    """
    解析 HIBLUP 可执行文件路径。
    顺序：显式传入 -> 项目 tools/HIBLUP 目录 -> 系统 PATH。
    """
    candidates = []
    if configured_path:
        candidates.append(configured_path)
    candidates.append(resolve_project_path("tools", "HIBLUP", "hiblup.exe"))
    candidates.append(resolve_project_path("tools", "HIBLUP", "hiblup"))
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate
    return "hiblup"


def verify_engine(hiblup_exe=None, timeout=60):
    """
    核验引擎可用性。返回 (是否可用, 版本文本)。
    不抛异常，由调用方决定如何处置。
    """
    binary = resolve_hiblup_binary(hiblup_exe)
    try:
        completed = subprocess.run([binary, "--version"],
                                   capture_output=True, text=True,
                                   encoding="utf-8", errors="replace",
                                   timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, "引擎不可用：%s" % exc
    output = (completed.stdout or "") + (completed.stderr or "")
    version = _extract_version(output)
    return completed.returncode == 0, version or output.strip()[:200]


def _extract_version(text):
    """从版本输出中提取形如 1.6.0 的版本号。"""
    match = re.search(r"(\d+\.\d+\.\d+)", text or "")
    return match.group(1) if match else None


# ===============================================================
# 二、命令行组装
# ===============================================================

def read_column_positions(phenotype_path):
    """
    读取表型文件表头，返回 {列名: 1-based 列序号}。
    HIBLUP 的所有 --*covar 参数都以列序号表达，本函数是唯一换算入口。
    """
    import pandas as pd
    require_readable_file(phenotype_path, "E011")
    header_frame = pd.read_csv(phenotype_path, sep="\t", nrows=1)
    return {str(column).strip(): index + 1
            for index, column in enumerate(header_frame.columns)}


def detect_constant_fixed_effects(phenotype_path, candidate_columns):
    """
    检出水平数不足 2 的离散固定效应（规则 E022）。
    这类列不可估，且会让 HIBLUP v1.6.0 在写结果阶段崩溃，必须剔除。
    返回需剔除的列名集合。
    """
    import pandas as pd
    if not candidate_columns:
        return set()
    usable = [column for column in dict.fromkeys(candidate_columns)]
    frame = pd.read_csv(phenotype_path, sep="\t", usecols=usable)
    if len(frame) < 2:
        return set()
    return {column for column in usable if frame[column].dropna().nunique() < 2}


def build_per_trait_tokens(effects_by_trait, trait_columns, column_positions):
    """
    构造"每性状一个 token"的效应参数。
    某性状无对应效应时填 "0"，token 内部列序号用逗号分隔。
    """
    tokens = []
    for trait in trait_columns:
        columns = effects_by_trait.get(trait) or []
        positions = []
        for column in columns:
            position = column_positions.get(column)
            if position:
                positions.append(str(position))
            else:
                logger.warning("效应列不在表型文件中，已忽略：%s", column)
        tokens.append(",".join(positions) if positions else "0")
    return tokens


def _normalize_effect_mapping(effects, trait_columns):
    """把 list 或 dict 形式的效应声明统一为 {性状: [列名]}。"""
    if not effects:
        return {}
    if isinstance(effects, dict):
        return {trait: list(columns) for trait, columns in effects.items()}
    return {trait: list(effects) for trait in trait_columns}


def assemble_hiblup_command(phenotype_path, output_prefix, model_id="pblup",
                            trait_columns=None, fixed_effects=None,
                            covariates=None, random_effects=None,
                            pedigree_path=None, genotype_prefix=None,
                            external_grm=None, mode="single",
                            vc_method="AI", tolerance=1e-6, max_iter=30,
                            em_max_iter=200, threads=None, threads_cap=16,
                            alpha=0.05, code_method=1, hiblup_exe=None,
                            reliability=True, extra_args=None,
                            step_size=None, keep_file=None, remove_file=None):
    """
    组装 HIBLUP 命令行。返回 (命令列表, 组装说明字典)。

    说明字典含实际生效的性状列序号、固定效应、被剔除的单水平列等，
    供审计与报告使用。

    O5（2026-08-24）：--step <N> 分块参数，大任务内存下降；仅 GBLUP/SSGBLUP
      场景有意义，PBLUP 透传但 HIBLUP 忽略。step_size 为正整数。
    O7（2026-08-24）：--keep/--remove 一等配置，审计可复现。keep_file/remove_file
      为个体 ID 列表文件路径（每行一个 ID），二者互斥，同时传时以 keep 为准。
    """
    method = str(model_id).lower()
    if method not in {"pblup", "gblup", "ssblup", "ssgblup"}:
        raise AbgesError("E012", "不支持的评估方法：%s" % model_id)

    column_positions = read_column_positions(phenotype_path)

    if isinstance(trait_columns, str):
        trait_columns = [trait_columns]
    if not trait_columns:
        raise AbgesError("E013", "未指定目标性状")

    missing_traits = [trait for trait in trait_columns
                      if trait not in column_positions]
    if missing_traits:
        raise AbgesError("E013", "目标性状不在表型文件中：%s" % ", ".join(missing_traits),
                         {"available_columns": list(column_positions.keys())})

    fixed_map = _normalize_effect_mapping(fixed_effects, trait_columns)
    covariate_map = _normalize_effect_mapping(covariates, trait_columns)
    random_map = _normalize_effect_mapping(random_effects, trait_columns)

    # E022：剔除单水平离散固定效应
    requested_fixed = [column for columns in fixed_map.values()
                       for column in columns if column in column_positions]
    dropped_columns = detect_constant_fixed_effects(phenotype_path, requested_fixed)
    if dropped_columns:
        for column in sorted(dropped_columns):
            logger.warning("固定效应 '%s' 水平数不足 2，已剔除（E022）", column)
        fixed_map = {trait: [column for column in columns
                             if column not in dropped_columns]
                     for trait, columns in fixed_map.items()}

    binary = resolve_hiblup_binary(hiblup_exe)
    thread_count = resolve_thread_count(threads, threads_cap)

    command = [binary]
    command.append("--single-trait" if mode == "single" else "--multi-trait")
    command.extend(["--pheno", phenotype_path])

    command.append("--pheno-pos")
    command.extend([str(column_positions[trait]) for trait in trait_columns])

    dcovar_tokens = build_per_trait_tokens(fixed_map, trait_columns, column_positions)
    if not all(token == "0" for token in dcovar_tokens):
        command.append("--dcovar")
        command.extend(dcovar_tokens)

    qcovar_tokens = build_per_trait_tokens(covariate_map, trait_columns, column_positions)
    if covariate_map and not all(token == "0" for token in qcovar_tokens):
        command.append("--qcovar")
        command.extend(qcovar_tokens)

    rand_tokens = build_per_trait_tokens(random_map, trait_columns, column_positions)
    if random_map and not all(token == "0" for token in rand_tokens):
        command.append("--rand")
        command.extend(rand_tokens)

    output_suffix = _append_relationship_args(
        command, method, pedigree_path, genotype_prefix, external_grm,
        output_prefix, alpha, code_method)

    command.extend(["--threads", str(thread_count)])
    # 【2026-10-13】--add 仅对需要基因型/G 矩阵的模型有意义；
    # 纯系谱 PBLUP 无基因型，HIBLUP 会忽略该标志，此处显式排除以免产生无效参数
    if method != "pblup":
        command.append("--add")
    command.extend(["--vc-method", vc_method])
    command.extend(["--tol", str(tolerance)])

    normalized_vc = str(vc_method).upper()
    if normalized_vc in ("AI", "EMAI", "HI"):
        command.extend(["--ai-maxit", str(max_iter)])
    if normalized_vc in ("EM", "EMAI"):
        command.extend(["--em-maxit", str(em_max_iter)])
    if reliability:
        command.append("--r2")
    # O5：--step 分块参数（大任务内存优化）
    if step_size is not None:
        step_n = int(step_size)
        if step_n > 0:
            command.extend(["--step", str(step_n)])
    # O7：--keep/--remove 个体筛选（互斥，keep 优先）
    if keep_file:
        command.extend(["--keep", str(keep_file)])
    elif remove_file:
        command.extend(["--remove", str(remove_file)])
    if extra_args:
        command.extend([str(item) for item in extra_args])

    command.extend(["--out", output_prefix])

    plan = {
        "method": method,
        "mode": mode,
        "trait_columns": list(trait_columns),
        "trait_positions": [column_positions[trait] for trait in trait_columns],
        "fixed_effects_used": {trait: columns for trait, columns in fixed_map.items()},
        "fixed_effects_dropped": sorted(dropped_columns),
        "covariates_used": covariate_map,
        "threads": thread_count,
        "reliability_requested": bool(reliability),
        "step_size": int(step_size) if step_size is not None else None,
        "keep_file": keep_file,
        "remove_file": remove_file,
        "expected_output_suffix": output_suffix,
        "command_line": " ".join(command),
    }
    logger.info("HIBLUP 命令已组装：%s", plan["command_line"])
    return command, plan


def _append_relationship_args(command, method, pedigree_path, genotype_prefix,
                              external_grm, output_prefix, alpha, code_method):
    """
    按方法追加亲缘关系相关参数，返回预期的输出文件后缀。
    走 --bfile 的基因组分析输出带 _ad，走 --xrm 或纯系谱不带。
    """
    if method == "pblup":
        if not pedigree_path:
            raise AbgesError("E014", "PBLUP 需要系谱文件")
        command.extend(["--pedigree", pedigree_path])
        return ""

    if method == "gblup":
        if external_grm:
            command.extend(["--xrm", _relativize_for_engine(external_grm, output_prefix)])
            return ""
        if not genotype_prefix:
            raise AbgesError("E014", "GBLUP 需要 --bfile 基因型前缀或 --xrm 关系矩阵")
        command.extend(["--bfile", genotype_prefix,
                        "--code-method", str(code_method)])
        return "_ad"

    # ssblup / ssgblup
    if external_grm:
        command.extend(["--xrm", _relativize_for_engine(external_grm, output_prefix)])
        return ""
    if not (genotype_prefix and pedigree_path):
        raise AbgesError("E014", "SSGBLUP 需要系谱与 --bfile 基因型，或直接提供 --xrm")
    command.extend(["--pedigree", pedigree_path,
                    "--bfile", genotype_prefix,
                    "--alpha", str(alpha),
                    "--code-method", str(code_method)])
    return "_ad"


def _relativize_for_engine(matrix_path, output_prefix):
    """
    Windows 下把绝对路径转为相对输出目录的路径。
    HIBLUP 的部分参数以冒号为分隔符，盘符冒号会被误解析。

    O9（2026-08-24 实测记录，--xrm 命名约定）：
    external_grm 传入的前缀必须含矩阵类型名，即 <prefix>.<TYPE> 形式
    （如 demo.GA、demoPA.PA）。--make-xrm 产出 demo.GA.bin/.id 后，
    分析命令应写 --xrm demo.GA；若只传裸前缀 demo，HIBLUP 报错
    "can not open the file [demo.id]"。走 --xrm 时 .vars/.rand 的
    列名为 <prefix>.<TYPE>（如 demoPA.PA），非固定 PA；
    下游 EBV 汇总已按"首个非 ID/residuals 列"自动兼容。
    """
    if os.name == "nt" and os.path.isabs(matrix_path):
        base = os.path.dirname(os.path.abspath(output_prefix)) or "."
        return os.path.relpath(matrix_path, base)
    return matrix_path


# ===============================================================
# 三、受控执行
# ===============================================================

def run_hiblup(command, workdir=None, timeout=3600, threads=None,
               cancel_flag_path=None, heartbeat_callback=None):
    """
    执行 HIBLUP。返回 (退出码, 标准输出, 标准错误, 耗时秒)。

    - 超时后终止整个进程树，返回 EXIT_TIMEOUT
    - 检测到取消标记文件时终止，返回 EXIT_CANCELLED
    - 引擎不存在返回 EXIT_NOT_FOUND
    绝不因失败而静默重试，重试由上层策略显式发起并记入审计。
    """
    if not command:
        raise AbgesError("E015", "命令为空，无法执行")

    thread_count = resolve_thread_count(threads)
    environment = os.environ.copy()
    environment["OMP_NUM_THREADS"] = str(thread_count)
    environment["MKL_NUM_THREADS"] = str(thread_count)
    # 本机硬约束：临时数据一律写 D 盘
    for temp_key in ("TEMP", "TMP"):
        environment.setdefault(temp_key, os.environ.get("TEMP") or os.environ.get("TMP") or os.getcwd())

    logger.info("启动 HIBLUP，工作目录=%s，线程=%d，超时=%ds",
                 workdir or os.getcwd(), thread_count, timeout)
    started = time.time()
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True,
                                   encoding="utf-8", errors="replace",
                                   cwd=workdir, env=environment)
    except FileNotFoundError:
        logger.error("找不到 HIBLUP 可执行文件：%s", command[0])
        return EXIT_NOT_FOUND, "", "HIBLUP_NOT_FOUND", 0.0

    while True:
        try:
            stdout, stderr = process.communicate(timeout=1)
            exit_code = process.returncode
            break
        except subprocess.TimeoutExpired:
            elapsed = time.time() - started
            if heartbeat_callback:
                heartbeat_callback(elapsed)
            if cancel_flag_path and os.path.exists(cancel_flag_path):
                terminate_process_tree(process)
                stdout, stderr = _drain_output(process)
                logger.warning("检测到取消标记，已终止 HIBLUP")
                return EXIT_CANCELLED, stdout, "CANCELLED", elapsed
            if elapsed >= timeout:
                terminate_process_tree(process)
                stdout, stderr = _drain_output(process)
                logger.error("HIBLUP 超时（%.1fs ≥ %ds），已终止", elapsed, timeout)
                return EXIT_TIMEOUT, stdout, "TIMEOUT", elapsed

    duration = time.time() - started
    logger.info("HIBLUP 退出码=%d，耗时 %.1fs", exit_code, duration)
    if stdout:
        for line in stdout.strip().split("\n")[-15:]:
            logger.info("  引擎输出: %s", line)
    if exit_code != 0 and stderr:
        logger.warning("引擎错误输出: %s", stderr[:2000])
    return exit_code, stdout or "", stderr or "", duration


def terminate_process_tree(process):
    """终止本任务启动的进程树，不波及其他 HIBLUP 实例。"""
    if process is None or process.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           capture_output=True, text=True, timeout=15)
        else:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
    except (OSError, subprocess.SubprocessError):
        try:
            process.kill()
        except OSError:
            pass


def _drain_output(process):
    """终止后收集残余输出，失败则返回空串。"""
    try:
        return process.communicate(timeout=15)
    except subprocess.TimeoutExpired:
        return "", ""


# ===============================================================
# 四、输出解析
# ===============================================================

def resolve_output_suffix(output_prefix, method="pblup"):
    """
    探测实际生效的输出后缀。
    基因组方法优先试 _ad，未命中则退回无后缀。
    """
    probe_order = OUTPUT_SUFFIX_PROBE_ORDER \
        if str(method).lower() in GENOMIC_METHODS else ("",)
    for suffix in probe_order:
        for extension in REQUIRED_OUTPUT_EXTENSIONS:
            if os.path.exists(output_prefix + suffix + extension):
                return suffix
    return ""


def read_engine_table(table_path):
    """
    读取 HIBLUP 的空白分隔结果表。
    HIBLUP 在 Windows 上可能输出多种编码，逐个尝试避免中文乱码。
    """
    import pandas as pd
    content, _ = read_text_with_fallback(table_path)
    return pd.read_csv(io.StringIO(content), sep=r"\s+"), content


def _extract_pev_column(columns):
    """
    从列名中识别【个体 EBV】的 PEV 列（HIBLUP v1.7.0 为 PA_PEV）。

    ⚠ 必须与 tools/Python/abges_validate.py::_find_pev_column 保持口径一致：
        两者都接受 PA_PEV / PEV / *_pev / pev*，否则会出现"解析层识别、
        校验层不识别（或反之）"的不一致。此处不直接 import abges_validate，
        以避免模块循环依赖。

    优先精确匹配 EBV 的 PEV 列，避免误取残差列 residuals_PEV。
    """
    preferred = ("pa_pev", "pev_pa", "ebv_pev", "pev_ebv", "pev")
    normalized_all = []
    for column in columns:
        normalized = "".join(
            char for char in str(column).lower() if char.isalnum() or char == "_")
        normalized_all.append((column, normalized))
    for column, normalized in normalized_all:
        if normalized in preferred:
            return column
    for column, normalized in normalized_all:
        if normalized.startswith("pev") or normalized.endswith("_pev"):
            return column
    return None


def parse_hiblup_output(output_prefix, method="pblup", mode="single",
                        trait_names=None):
    """
    解析 HIBLUP 输出，返回结构化结果字典。

    返回字段：
      output_suffix / vars / rand / beta / log / converged /
      model_formula / missing_files
    缺失文件如实列在 missing_files 中，不做任何掩饰。
    """
    result = {
        "output_prefix": output_prefix,
        "method": str(method).lower(),
        "mode": mode,
        "trait_names": list(trait_names or []),
        "missing_files": [],
    }
    suffix = resolve_output_suffix(output_prefix, method)
    result["output_suffix"] = suffix

    vars_path = output_prefix + suffix + ".vars"
    if os.path.exists(vars_path):
        try:
            frame, content = read_engine_table(vars_path)
            result["vars"] = frame.to_dict(orient="records")
            result["vars_raw"] = content
        except Exception as exc:
            logger.warning("解析 .vars 失败：%s", exc)
            result["vars"] = []
    else:
        result["missing_files"].append(vars_path)

    # ---- .rand ----
    # 多性状：.rand 按性状分文件（prefix.TRAIT.rand）；单性状：单文件
    is_multi = (mode == "multi" and trait_names and len(trait_names) >= 2)
    if is_multi:
        result["rand_by_trait"] = {}
        result["rand_columns_by_trait"] = {}
        for _trait in trait_names:
            _rand_path = output_prefix + suffix + "." + _trait + ".rand"
            if os.path.exists(_rand_path):
                try:
                    _frame, _ = read_engine_table(_rand_path)
                    result["rand_by_trait"][_trait] = _frame.to_dict(orient="records")
                    result["rand_columns_by_trait"][_trait] = list(_frame.columns)
                except Exception as exc:
                    logger.warning("解析 %s.rand 失败：%s", _trait, exc)
                    result["rand_by_trait"][_trait] = []
            else:
                result["missing_files"].append(_rand_path)
        # 兼容：首个性状记录放入 rand，使 G5-07 等通用检查无需分支
        _first = trait_names[0]
        result["rand"] = result["rand_by_trait"].get(_first, [])
        result["rand_columns"] = result["rand_columns_by_trait"].get(_first, [])
    else:
        rand_path = output_prefix + suffix + ".rand"
        if os.path.exists(rand_path):
            try:
                frame, _ = read_engine_table(rand_path)
                result["rand"] = frame.to_dict(orient="records")
                result["rand_columns"] = list(frame.columns)
            except Exception as exc:
                logger.warning("解析 .rand 失败：%s", exc)
                result["rand"] = []
        else:
            result["missing_files"].append(rand_path)

    # ---- 可靠性（PEV / R2）----
    # HIBLUP v1.7.0：--r2 的逐个体 PEV/SE/R2【并入 .rand】，EBV 对应 PA_* 列。
    #   实测表头: ID PA PA_SE PA_PEV PA_R2 residuals residuals_SE residuals_PEV residuals_R2
    #   多性状 .rand 按性状分文件，result["rand"] 取首个性状（与既有兼容口径一致）。
    # HIBLUP v1.6.0 及更早：独立产出 .r2 / .pev / .reliability，保留其探测作为兼容回退。
    result["reliability_records"] = []
    result["reliability_path"] = None
    result["reliability_columns"] = []
    _rand_records = result.get("rand") or []
    if _rand_records:
        _rand_columns = list(_rand_records[0].keys())
        if _extract_pev_column(_rand_columns):
            _rand_source = output_prefix + suffix + ".rand"
            if is_multi and trait_names:
                _rand_source = output_prefix + suffix + "." + trait_names[0] + ".rand"
            result["reliability_records"] = _rand_records
            result["reliability_columns"] = _rand_columns
            result["reliability_path"] = _rand_source
    if not result["reliability_records"]:
        # 旧版口径回退：独立可靠性文件
        for extension in (".r2", ".pev", ".reliability"):
            candidate = output_prefix + suffix + extension
            if not os.path.exists(candidate):
                continue
            try:
                frame, _ = read_engine_table(candidate)
                result["reliability_records"] = frame.to_dict(orient="records")
                result["reliability_columns"] = list(frame.columns)
                result["reliability_path"] = candidate
                break
            except Exception as exc:
                logger.warning("Failed to parse optional reliability output %s: %s", candidate, exc)
    # ---- .beta ----
    if is_multi:
        result["beta_by_trait"] = {}
        for _trait in trait_names:
            _beta_path = output_prefix + suffix + "." + _trait + ".beta"
            if os.path.exists(_beta_path):
                try:
                    _frame, _ = read_engine_table(_beta_path)
                    result["beta_by_trait"][_trait] = _frame.to_dict(orient="records")
                except Exception as exc:
                    logger.warning("解析 %s.beta 失败：%s", _trait, exc)
                    result["beta_by_trait"][_trait] = []
            else:
                result["missing_files"].append(_beta_path)
        result["beta"] = result["beta_by_trait"].get(trait_names[0], [])
    else:
        beta_path = output_prefix + suffix + ".beta"
        if os.path.exists(beta_path):
            try:
                frame, _ = read_engine_table(beta_path)
                result["beta"] = frame.to_dict(orient="records")
            except Exception as exc:
                logger.warning("解析 .beta 失败：%s", exc)
                result["beta"] = []
        else:
            result["missing_files"].append(beta_path)

    # ---- .anova（固定效应/协变量 F 检验；缺文件不报错、不阻断）----
    # HIBLUP 实测表头（v1.6.0 与 v1.7.0 一致）：Factors Df SumSq MeanSq F Pr(>F)，
    # 残差行（Factors=e）的 F/P 输出为 "."，读取后为字符串，由提取层按缺失处理。
    #
    # v1.7.0 起，多性状模式产出【逐性状】 .anova：prefix.TRAIT.anova
    # （v1.6.0 仅单性状产出 prefix.anova）。故多性状按性状逐个解析。
    # 结果同时提供两种视图：
    #   anova_by_trait = {性状: [记录]}              逐性状精取（新增，2026-10-13）
    #   anova          = 合并列表，每条带 trait 字段   向后兼容
    # ⚠ anova 必须保持【列表】：下游 runners/breeding_insights.anova_table()
    #   会遍历它并对每项调用 rec.get(...)，若改为 dict 会遍历字符串键而崩溃。
    result["anova"] = []
    result["anova_by_trait"] = {}
    if not is_multi:
        anova_path = output_prefix + suffix + ".anova"
        if os.path.exists(anova_path):
            try:
                anova_frame, _ = read_engine_table(anova_path)
                result["anova"] = anova_frame.to_dict(orient="records")
                if trait_names:
                    result["anova_by_trait"][trait_names[0]] = result["anova"]
            except Exception as exc:
                logger.warning("解析 .anova 失败：%s", exc)
                result["anova"] = []
    else:
        merged_anova = []
        for _trait in trait_names:
            _anova_path = output_prefix + suffix + "." + _trait + ".anova"
            if not os.path.exists(_anova_path):
                result["anova_by_trait"][_trait] = []
                continue
            try:
                _frame, _ = read_engine_table(_anova_path)
                _records = _frame.to_dict(orient="records")
                for _record in _records:
                    _record["trait"] = _trait
                result["anova_by_trait"][_trait] = _records
                merged_anova.extend(_records)
            except Exception as exc:
                logger.warning("解析 %s.anova 失败：%s", _trait, exc)
                result["anova_by_trait"][_trait] = []
        result["anova"] = merged_anova
        if not merged_anova:
            # 不静默：明确告知未取到逐性状 F 检验（可能引擎 < v1.7.0 或未产出）
            logger.info("多性状模式未发现逐性状 .anova"
                         "（v1.7.0+ 应产出 %s.TRAIT.anova）",
                         output_prefix + suffix)

    # ---- .covars（遗传相关，多性状独有）----
    if is_multi:
        covars_path = output_prefix + suffix + ".covars"
        if os.path.exists(covars_path):
            try:
                frame, content = read_engine_table(covars_path)
                result["covars"] = frame.to_dict(orient="records")
                result["covars_raw"] = content
            except Exception as exc:
                logger.warning("解析 .covars 失败：%s", exc)
                result["covars"] = []
        else:
            result["missing_files"].append(covars_path)
            result["covars"] = []

    log_path = output_prefix + suffix + ".log"
    if os.path.exists(log_path):
        log_text, _ = read_text_with_fallback(log_path)
        result["log_excerpt"] = log_text[:8000]
        result["converged"] = parse_convergence_flag(log_text)
        result["n_iterations"] = _extract_iteration_count(log_text)
        result["model_formula"] = _extract_model_formula(log_text)
        result["iteration_trace"] = _extract_iteration_trace(log_text)
    else:
        result["missing_files"].append(log_path)
        result["converged"] = None

    return result


def parse_convergence_flag(log_text):
    """
    判定收敛状态。

    HIBLUP v1.6.0 在日志中写 "[Converged?] Yes!" 或 "[Converged?] No!"，
    两者都包含 "converged" 一词。仅用子串匹配会把未收敛误判为已收敛，
    因此必须解析问号后的答复。
    返回 True / False / None（日志中无该标记，无法判定）。
    """
    match = re.search(r"\[\s*Converged\?\s*\]\s*([A-Za-z]+)", log_text,
                      re.IGNORECASE)
    if match:
        return match.group(1).strip().lower().startswith("y")
    if re.search(r"not\s+converged|fail(?:ed)?\s+to\s+converge", log_text,
                 re.IGNORECASE):
        return False
    if re.search(r"\bconverged\b", log_text, re.IGNORECASE):
        return True
    return None


def _extract_model_formula(log_text):
    """从日志中提取模型公式行。"""
    for line in log_text.split("\n"):
        stripped = line.strip()
        if "=" in stripped and ("+" in stripped) and \
                ("R[" in stripped or "(F)" in stripped or "mu" in stripped.lower()):
            return stripped
    return None


def _extract_iteration_trace(log_text):
    """
    提取 REML 迭代轨迹。
    HIBLUP 日志格式：
        Alg.  Iter.  LogL.   V(PA)  V(e)
        [AI]  1      -12.68  0.40  0.60
    返回 [{"algorithm": "AI", "iteration": 1, "logL": -12.68}, ...]。
    """
    trace = []
    for line in log_text.split("\n"):
        match = re.match(r"\s*\[([A-Za-z]+)\]\s+(\d+)\s+(-?[\d.eE+]+)", line)
        if match:
            try:
                trace.append({
                    "algorithm": match.group(1),
                    "iteration": int(match.group(2)),
                    "logL": float(match.group(3)),
                })
            except ValueError:
                continue
    return trace


def _extract_iteration_count(log_text):
    """
    提取实际迭代次数。优先取迭代轨迹最后一行的迭代号，
    其次退回文本匹配（如 "converged in 12 iterations"）。
    """
    trace = _extract_iteration_trace(log_text)
    if trace:
        return trace[-1]["iteration"]
    match = re.search(r"(\d+)\s+iterations?", log_text, re.IGNORECASE)
    if match:
        return int(match.group(1))
    return None


# .vars 表中代表残差的 Item 取值，不参与遗传力提取
RESIDUAL_ITEM_TOKENS = {"e", "res", "residual", "residuals"}


def extract_variance_components(parsed, trait=None):
    """
    解析 .vars 表为结构化方差组分列表。

    HIBLUP v1.6.0 实测表头：
        Item  Var  Var_SE  h2  h2_SE  h2_Pr(Chisq)
    其中 Item=e 为残差行，其余为随机效应（如 PA 加性遗传、PE 永久环境）。

    多性状模式（2026-08-24 实测）：Item 形如 "TRAIT_EFFECT"
    （如 EN_PA、EN_e），.vars 为全性状共用单文件。
    传入 trait 时仅保留该性状的行，并剥出净效应名 effect
    （EN_e -> effect="e"），使残差判定对多性状同样成立。
    """
    components = []
    trait_prefix = (str(trait) + "_") if trait else None
    for record in parsed.get("vars") or []:
        item = str(record.get("Item", record.get("item", ""))).strip()
        effect = item
        if trait_prefix:
            if item.startswith(trait_prefix):
                effect = item[len(trait_prefix):]
            else:
                # 不属于目标性状的行，跳过
                continue
        entry = {
            "item": item,
            "effect": effect,
            "trait": trait,
            "is_residual": effect.lower() in RESIDUAL_ITEM_TOKENS,
            "variance": _to_float(record.get("Var", record.get("var"))),
            "variance_se": _to_float(record.get("Var_SE", record.get("Var_se"))),
            "h2": _to_float(record.get("h2", record.get("H2"))),
            "h2_se": _to_float(record.get("h2_SE", record.get("h2_se"))),
            "p_value": _to_float(record.get("h2_Pr(Chisq)")),
        }
        components.append(entry)
    return components


def extract_heritability(parsed, trait=None):
    """
    提取加性遗传力与标准误。

    O16（2026-08-24 修订）：优先按 Item 精确匹配加性遗传行——
      单性状：Item 为 "PA"（纯系谱），或走 --xrm 时为 "前缀.PA"（如 demo.GA、demoPA.PA）。
      多性状：Item 为 "TRAIT_PA"（如 EN_PA）。
    仅当无 PA 行时才退回"第一条非残差行"（兼容无系谱标记的旧输出）。
    残差行 Item=e（多性状为 TRAIT_e）的 h2 是残差占比，绝不能当作遗传力返回。
    返回 {"estimate", "se", "item", "genetic_variance", "residual_variance"}。
    """
    components = extract_variance_components(parsed, trait=trait)
    genetic = None
    residual = None
    fallback = None
    for entry in components:
        if entry["is_residual"]:
            residual = entry
            continue
        item = entry.get("item") or ""
        effect = entry.get("effect") or item
        if trait is not None:
            # 多性状：剥掉性状前缀后按净效应名匹配
            matched = (effect == "PA" or effect.endswith(".PA")
                       or effect == "GA" or effect.endswith(".GA")
                       or effect == "HA" or effect.endswith(".HA"))
        else:
            # 精确匹配：PA 或 前缀.PA / 前缀.HA（--xrm 场景，HA 为 SSGBLUP H 矩阵加性）
            matched = (item == "PA" or item.endswith(".PA")
                       or item == "HA" or item.endswith(".HA"))
        if matched:
            # 注意不能 break：.vars 中残差行(e)通常排在 PA 行之后，
            # 提前退出会漏读 residual。首次命中即锁定，循环跑完。
            if genetic is None:
                genetic = entry
            continue
        if fallback is None and entry["h2"] is not None:
            fallback = entry
    if genetic is None:
        genetic = fallback
    if genetic is None:
        return {"estimate": None, "se": None, "item": None,
                "genetic_variance": None,
                "residual_variance": residual["variance"] if residual else None}
    return {
        "estimate": genetic["h2"],
        "se": genetic["h2_se"],
        "item": genetic["item"],
        "genetic_variance": genetic["variance"],
        "residual_variance": residual["variance"] if residual else None,
    }


def _to_float(value):
    """安全转浮点，不可转返回 None。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def extract_genetic_correlations(parsed):
    """
    提取多性状间的相关系数（.covars，多性状独有输出）。

    2026-08-24 实测（SAMPLE_POPULATION EN+ER 探针，HIBLUP v1.6.0）：
        Item  COVar  COVar_SE  r  r_SE  r_Pr(Chisq)
        Item 形如 "EN:ER_PA"（加性遗传相关）或 "EN:ER_e"（残差相关），
        r 列即相关系数（遗传相关取 EN:ER_PA 行）。

    返回列表，每项含：
      trait_a / trait_b / effect / is_genetic /
      correlation / se / covariance / covariance_se / p_value / item
    """
    correlations = []
    for record in parsed.get("covars") or []:
        item = str(record.get("Item", record.get("item", ""))).strip()
        trait_a, trait_b, effect = item, None, None
        match = re.match(r"^(.+?):(.+)_([A-Za-z]+)$", item)
        if match:
            trait_a, trait_b, effect = match.group(1), match.group(2), match.group(3)
        correlations.append({
            "item": item,
            "trait_a": trait_a,
            "trait_b": trait_b,
            "effect": effect,
            "is_genetic": bool(effect) and (
                effect == "PA" or effect.endswith(".PA")
                or effect == "GA" or effect.endswith(".GA")
                or effect == "HA" or effect.endswith(".HA")),
            "correlation": _to_float(record.get("r")),
            "se": _to_float(record.get("r_SE", record.get("r_se"))),
            "covariance": _to_float(record.get("COVar", record.get("Covar"))),
            "covariance_se": _to_float(record.get("COVar_SE", record.get("Covar_SE"))),
            "p_value": _to_float(record.get("r_Pr(Chisq)")),
        })
    return correlations


def _is_number(value):
    """判断取值可转为浮点数。"""
    try:
        float(value)
        return True
    except (TypeError, ValueError):
        return False


def verify_required_outputs(output_prefix, method="pblup", mode="single",
                            trait_names=None):
    """
    核验必需输出是否齐全且非空。
    返回 (是否齐全, 缺失文件列表)。这是判定 completed 的硬前提之一。

    多性状模式（2026-08-24 实测）：
      - .vars / .log / .covars 为全性状共用单文件；
      - .rand / .beta 按性状分文件（prefix.TRAIT.rand / prefix.TRAIT.beta），
        不存在无性状后缀的 .rand/.beta。
    """
    suffix = resolve_output_suffix(output_prefix, method)
    is_multi = (mode == "multi" and trait_names and len(trait_names) >= 2)
    if is_multi:
        paths = [output_prefix + suffix + ".vars",
                 output_prefix + suffix + ".log",
                 output_prefix + suffix + ".covars"]
        for trait in trait_names:
            paths.append(output_prefix + suffix + "." + trait + ".rand")
            paths.append(output_prefix + suffix + "." + trait + ".beta")
    else:
        paths = [output_prefix + suffix + extension
                 for extension in REQUIRED_OUTPUT_EXTENSIONS]
    missing = []
    for path in paths:
        if not os.path.exists(path) or os.path.getsize(path) == 0:
            missing.append(path)
    return (not missing), missing


# ===============================================================
# O11（2026-08-24）：--ibc 近交系数交叉校验
# ===============================================================
# 【状态：保留待接线｜先生拍板 2026-10-21】
# 本段（run_ibc_check / parse_ibc_output / cross_validate_inbreeding，约 238 行）
# 目前无调用方：ABGES 现行链路用 Python 自算（Meuwissen-Luo）产出近交系数，
# 未经 HIBLUP --ibc 交叉校验。
# 接线计划（待排期）：在阶段 08（系谱质控）之后追加一道交叉校验闸门，
# 比对两套近交系数（阈值待先生定），不一致时记 warning 并进入 G5 人工确认。
# 在此之前请勿删除——历史审计报告 ABGES_项目审计报告_20260824.md 已登记 O11 为既有能力。

def run_ibc_check(pedigree_path, output_prefix, threads=None,
                   hiblup_exe=None, timeout=600):
    """
    调用 HIBLUP --ibc 计算近交系数，用于与 ABGES Python 自算
    （Meuwissen-Luo）结果交叉校验。

    HIBLUP --ibc 仅需系谱输入，输出 <prefix>.ibc（个体近交系数表）。
    返回 {"exit_code", "output_prefix", "duration", "ibc_file"}。
    """
    binary = resolve_hiblup_binary(hiblup_exe)
    thread_count = resolve_thread_count(threads)
    command = [binary, "--ibc", "--pedigree", pedigree_path,
               "--threads", str(thread_count), "--out", output_prefix]
    logger.info("O11 HIBLUP --ibc 交叉校验：%s", " ".join(command))
    exit_code, stdout, stderr, duration = run_hiblup(
        command, workdir=os.path.dirname(output_prefix) or ".",
        timeout=timeout, threads=thread_count)
    ibc_file = output_prefix + ".ibc"
    return {
        "exit_code": exit_code,
        "output_prefix": output_prefix,
        "duration": round(duration, 2),
        "ibc_file": ibc_file if os.path.isfile(ibc_file) else None,
        "stderr": (stderr or "")[:500],
    }


def parse_ibc_output(output_prefix):
    """
    解析 HIBLUP --ibc 输出（.ibc 文件）。
    预期格式：空格/制表符分隔，首列 ID，末列或 F 列为近交系数。
    返回 {id: F} 字典；文件不存在或解析失败返回空字典。
    """
    ibc_path = output_prefix + ".ibc"
    if not os.path.isfile(ibc_path):
        return {}
    result = {}
    try:
        with open(ibc_path, "r", encoding="utf-8", errors="replace") as fh:
            header = fh.readline().split()
            # 定位 F 列：优先找名为 F/Inbreeding/F_i 的列，否则取末列
            f_col = len(header) - 1
            for idx, col in enumerate(header):
                if col.upper() in ("F", "INBREEDING", "F_I", "F_PED"):
                    f_col = idx
                    break
            id_col = 0
            for idx, col in enumerate(header):
                if col.upper() in ("ID", "IID", "INDIVIDUAL"):
                    id_col = idx
                    break
            for line in fh:
                parts = line.split()
                if len(parts) <= max(id_col, f_col):
                    continue
                try:
                    result[parts[id_col]] = float(parts[f_col])
                except (ValueError, IndexError):
                    continue
    except OSError as exc:
        logger.warning("O11 解析 .ibc 失败：%s", exc)
    return result


def cross_validate_inbreeding(abges_inbreeding, hiblup_ibc, tolerance=1e-4):
    """
    交叉校验 ABGES Python 自算近交系数与 HIBLUP --ibc 结果。

    参数：
      abges_inbreeding: {id: {"F": float, ...}} 或 {id: float}
      hiblup_ibc: {id: float}
      tolerance: 允许的最大绝对偏差

    返回：
      {"n_common", "n_abges_only", "n_hiblup_only", "max_abs_diff",
       "mean_abs_diff", "rmse", "correlation", "passed": bool,
       "divergent": [(id, abges_f, hiblup_f, diff), ...]}
    """
    # 归一化 abges 输入
    abges_f = {}
    for key, val in (abges_inbreeding or {}).items():
        if isinstance(val, dict):
            abges_f[str(key)] = float(val.get("F", val.get("f", 0.0)))
        else:
            abges_f[str(key)] = float(val)
    hiblup_f = dict((str(k), float(v)) for k, v in (hiblup_ibc or {}).items())

    common_ids = set(abges_f) & set(hiblup_f)
    abges_only = set(abges_f) - common_ids
    hiblup_only = set(hiblup_f) - common_ids

    diffs = []
    divergent = []
    for iid in sorted(common_ids):
        diff = abs(abges_f[iid] - hiblup_f[iid])
        diffs.append(diff)
        if diff > tolerance:
            divergent.append((iid, abges_f[iid], hiblup_f[iid], diff))

    n = len(diffs)
    max_diff = max(diffs) if diffs else 0.0
    mean_diff = sum(diffs) / n if n else 0.0
    rmse = (sum(d * d for d in diffs) / n) ** 0.5 if n else 0.0

    # Pearson 相关系数
    correlation = None
    if n >= 2:
        xs = [abges_f[i] for i in common_ids]
        ys = [hiblup_f[i] for i in common_ids]
        mx = sum(xs) / n
        my = sum(ys) / n
        cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
        vx = sum((x - mx) ** 2 for x in xs)
        vy = sum((y - my) ** 2 for y in ys)
        if vx > 0 and vy > 0:
            correlation = cov / (vx * vy) ** 0.5

    return {
        "n_common": n,
        "n_abges_only": len(abges_only),
        "n_hiblup_only": len(hiblup_only),
        "max_abs_diff": round(max_diff, 8),
        "mean_abs_diff": round(mean_diff, 8),
        "rmse": round(rmse, 8),
        "correlation": round(correlation, 8) if correlation is not None else None,
        "tolerance": tolerance,
        "passed": (max_diff <= tolerance) if n > 0 else False,
        "divergent": divergent[:20],  # 最多返回前 20 条
        "divergent_total": len(divergent),
    }


# ===============================================================
# O12（2026-08-24）：--mating 选配支持（实验性）
# ===============================================================

def assemble_mating_command(pedigree_path, output_prefix,
                            ebv_file=None, candidates_male=None,
                            candidates_female=None, n_mates=1,
                            method="min_kinship", constraints=None,
                            threads=None, hiblup_exe=None):
    """
    组装 HIBLUP --mating 选配命令（实验性，O12）。

    HIBLUP --mating 基于系谱 A 矩阵（或预构建 GRM）优化选配方案，
    支持最小亲缘（min_kinship）、最大遗传进展（max_genetic_gain）等策略。

    参数：
      pedigree_path: 系谱文件路径（三列 ID/Sire/Dam）
      output_prefix: 输出前缀
      ebv_file: EBV 文件路径（可选，用于遗传进展优化）
      candidates_male: 候选公畜 ID 列表文件（每行一个）
      candidates_female: 候选母畜 ID 列表文件
      n_mates: 每头公畜配种母畜数（默认 1）
      method: 选配策略（min_kinship / max_genetic_gain / balanced）
      constraints: 额外约束字典（如 {"max_inbreeding": 0.0625}）
      threads: 线程数
      hiblup_exe: 可执行文件路径

    返回：
      (command_list, plan_dict)

    注意：本函数为实验性接口，--mating 的具体参数名以 HIBLUP 官方文档为准；
    未确认的参数经 extra_args 透传。使用前须在小样本上验证输出格式。
    """
    binary = resolve_hiblup_binary(hiblup_exe)
    thread_count = resolve_thread_count(threads)
    command = [binary, "--mating", "--pedigree", pedigree_path]

    if ebv_file:
        command.extend(["--ebv", ebv_file])
    if candidates_male:
        command.extend(["--keep-male", candidates_male])
    if candidates_female:
        command.extend(["--keep-female", candidates_female])

    command.extend(["--n-mates", str(n_mates)])
    command.extend(["--mating-method", method])

    if constraints:
        for key, value in constraints.items():
            command.extend(["--%s" % key, str(value)])

    command.extend(["--threads", str(thread_count), "--out", output_prefix])

    plan = {
        "method": "mating",
        "strategy": method,
        "pedigree": pedigree_path,
        "ebv_file": ebv_file,
        "candidates_male": candidates_male,
        "candidates_female": candidates_female,
        "n_mates": n_mates,
        "constraints": constraints or {},
        "command_line": " ".join(command),
        "experimental": True,
    }
    logger.info("O12 --mating 选配命令已组装（实验性）：%s", plan["command_line"])
    return command, plan


def parse_mating_output(output_prefix):
    """
    解析 HIBLUP --mating 输出（选配方案表）。
    预期格式：每行一个配对，含 sire_id / dam_id / predicted_inbreeding /
    predicted_ebv / kinship 等列。
    返回 {"pairs": [...], "n_pairs": int, "summary": dict}。
    """
    mating_file = output_prefix + ".mating"
    if not os.path.isfile(mating_file):
        # 尝试其他可能的后缀
        for alt in (".mate", ".plan", ".txt"):
            if os.path.isfile(output_prefix + alt):
                mating_file = output_prefix + alt
                break
    if not mating_file or not os.path.isfile(mating_file):
        return {"pairs": [], "n_pairs": 0, "summary": {},
                "warning": "未找到选配输出文件"}

    pairs = []
    header = []
    try:
        with open(mating_file, "r", encoding="utf-8", errors="replace") as fh:
            header = fh.readline().split()
            for line in fh:
                parts = line.split()
                if not parts:
                    continue
                pair = dict(zip(header, parts)) if header else {"raw": parts}
                pairs.append(pair)
    except OSError as exc:
        logger.warning("O12 解析选配输出失败：%s", exc)

    summary = {
        "n_pairs": len(pairs),
        "columns": header,
    }
    return {"pairs": pairs, "n_pairs": len(pairs), "summary": summary}
