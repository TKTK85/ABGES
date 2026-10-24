# -*- coding: utf-8 -*-
"""
ABGES PLINK 工具封装
====================
负责基因型数据的格式识别、转换与质控。质控只做过滤，不参与方差组分估计。

质控顺序固定为：先剔个体（mind）后剔位点（geno/maf/hwe）。
颠倒顺序会让高缺失个体拉低整体位点通过率，导致误剔大量位点。

等位基因频率与 HWE 统计口径（2026-10-12 新增，依据 plink-ng 源码实证）：
PLINK2 的 --maf 与 --hwe 默认【仅基于 founder（基础群）】计算，需显式
--nonfounders 才纳入全群。育种家系数据基础群通常仅数十只，沿用默认会使
HWE 检验功效不足、剔除数恒为 0，形成静默失效。故 ABGES 默认采用
all_samples 口径，由 freq_hwe_scope 控制并留痕。

家禽为非人类物种，PLINK2 必须显式指定 --chr-set，否则染色体编号会被拒绝。
"""

import os
import sys
import time
import logging
import subprocess

logger = logging.getLogger(__name__)

sys.path.insert(0, os.path.abspath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), os.pardir, "Python")))

from abges_core import (AbgesError, resolve_project_path, ensure_dir,
                        resolve_thread_count, read_text_with_fallback)

DEFAULT_CHR_SET = 39          # 家禽：38 常染色体 + Z 性染色体（不含 W）
PLINK_BINARY_SUFFIXES = (".bed", ".bim", ".fam")

# 等位基因频率与 HWE 检验的统计口径（2026-10-12 新增）
# PLINK2 源码实证（plink2.cc L2728 / L2836）：--maf 与 --hwe 默认仅基于 founder 计算，
# 需显式 --nonfounders 才纳入全群。育种家系数据基础群通常仅数十只，
# 沿用默认会使 HWE 检验功效不足（剔除数恒为 0，规则静默失效）。
VALID_FREQ_HWE_SCOPES = ("all_samples", "founders_only")
DEFAULT_FREQ_HWE_SCOPE = "all_samples"
# .fam 为 PLINK 固定格式：FID IID PAT MAT SEX PHENO，父母列即第 3、4 列
FAM_COL_PAT = 2
FAM_COL_MAT = 3


# ===============================================================
# 一、可执行文件与格式识别
# ===============================================================

def resolve_plink_binary(configured_path=None):
    """解析 PLINK2 可执行文件路径。"""
    candidates = []
    if configured_path:
        candidates.append(configured_path)
    for name in ("plink2.exe", "plink2", "plink.exe", "plink"):
        candidates.append(resolve_project_path("tools", "PLINK", name))
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate
    return "plink2"


def verify_plink(plink_exe=None, timeout=60):
    """核验 PLINK 可用性，返回 (是否可用, 版本文本)。"""
    binary = resolve_plink_binary(plink_exe)
    try:
        completed = subprocess.run([binary, "--version"],
                                   capture_output=True, text=True,
                                   encoding="utf-8", errors="replace",
                                   timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, "PLINK 不可用：%s" % exc
    output = ((completed.stdout or "") + (completed.stderr or "")).strip()
    return completed.returncode == 0, output[:200]


def detect_genotype_format(input_path):
    """
    识别基因型输入格式，返回 (格式名, 供 PLINK 使用的参数列表)。
    input_path 可以是二进制前缀，也可以是 VCF 文件路径。
    """
    if all(os.path.exists(input_path + suffix) for suffix in PLINK_BINARY_SUFFIXES):
        return "plink_binary", ["--bfile", input_path]
    if os.path.exists(input_path + ".ped") and os.path.exists(input_path + ".map"):
        return "plink_text", ["--file", input_path]
    lowered = str(input_path).lower()
    if lowered.endswith(".vcf") or lowered.endswith(".vcf.gz"):
        if not os.path.exists(input_path):
            raise AbgesError("E030", "VCF 文件不存在：%s" % input_path)
        return "vcf", ["--vcf", input_path]
    raise AbgesError("E030", "无法识别基因型格式，请检查路径与文件完整性",
                     {"input": input_path,
                      "expect": "同名 .bed/.bim/.fam 三件套，或 .ped/.map，或 .vcf"})


def build_chromosome_args(chr_set=DEFAULT_CHR_SET):
    """家禽等非人类物种必须显式声明染色体集合。"""
    return ["--chr-set", str(chr_set), "no-xy", "--allow-extra-chr"]


# ===============================================================
# 二、基础统计
# ===============================================================

def count_genotype_scale(bfile_prefix):
    """统计二进制基因型的个体数与位点数。"""
    fam_path = bfile_prefix + ".fam"
    bim_path = bfile_prefix + ".bim"
    for path in (fam_path, bim_path):
        if not os.path.exists(path):
            raise AbgesError("E031", "基因型文件缺失：%s" % path)
    return {
        "n_sample": _count_lines(fam_path),
        "n_snp": _count_lines(bim_path),
    }


def count_founders(bfile_prefix):
    """
    统计基础群（founder）个体数：.fam 中父母列均为空（0）的个体。

    用途：PLINK2 的 --maf/--hwe 默认仅基于 founder 计算，基础群过小时
    频率估计不可靠、HWE 检验功效不足。ABGES 默认改用全群口径，
    但在 founders_only 口径下必须量化该风险，不得静默通过。
    """
    fam_path = bfile_prefix + ".fam"
    if not os.path.exists(fam_path):
        raise AbgesError("E031", "找不到 .fam 文件：%s" % fam_path)
    content, _ = read_text_with_fallback(fam_path)
    founder_count = 0
    for line in content.splitlines():
        parts = line.split()
        if len(parts) > FAM_COL_MAT:
            if parts[FAM_COL_PAT] == "0" and parts[FAM_COL_MAT] == "0":
                founder_count += 1
    return founder_count


def resolve_freq_hwe_scope_flags(config):
    """
    按 freq_hwe_scope 口径返回需追加给 --maf/--hwe 的 PLINK2 参数。

    all_samples   -> ["--nonfounders"]（纳入全群，推荐）
    founders_only -> []（沿用 PLINK2 默认，仅基础群）
    非法取值直接报错，不做静默回退。
    """
    scope = str(config.get("freq_hwe_scope", DEFAULT_FREQ_HWE_SCOPE)).strip().lower()
    if scope not in VALID_FREQ_HWE_SCOPES:
        raise AbgesError(
            "E030",
            "非法的 freq_hwe_scope：%r，合法值：%s"
            % (scope, " / ".join(VALID_FREQ_HWE_SCOPES)),
            {"freq_hwe_scope": scope, "valid": list(VALID_FREQ_HWE_SCOPES)})
    return ["--nonfounders"] if scope == "all_samples" else []


def _count_lines(path):
    """按行计数，容忍编码异常。"""
    count = 0
    with open(path, "rb") as handle:
        for _ in handle:
            count += 1
    return count


def compute_missing_rate(bfile_prefix, output_dir, stamp,
                         plink_exe=None, threads=None, timeout=3600):
    """
    O6（2026-08-24）：统计基因型缺失率（不剔除任何样本/位点）。

    背景：HIBLUP 对缺失基因型强制编码为杂合(1)且无填充功能，
    缺失率必须先量化才能审计其偏差风险。
    实现：plink --missing 产生 .imiss（样本级）/ .lmiss（位点级），
    解析 F_MISS 列均值与最大值。失败时如实返回 None 字段，不阻断流程。
    """
    miss_prefix = os.path.join(output_dir, "geno_missing_%s" % stamp)
    # 注：不走 run_plink_step——它强制 --make-bed，与报告类参数 --missing
    # 组合会被 plink 拒绝。此处独立调用，仅产出 .imiss/.lmiss 报告。
    binary = resolve_plink_binary(plink_exe)
    thread_count = resolve_thread_count(threads)
    command = [binary, "--bfile", bfile_prefix, "--missing",
               "--threads", str(thread_count), "--out", miss_prefix]
    logger.info("执行 PLINK 缺失率统计：%s", " ".join(command))
    started = time.time()
    try:
        completed = subprocess.run(command, capture_output=True, text=True,
                                   encoding="utf-8", errors="replace",
                                   timeout=timeout)
        code = completed.returncode
    except FileNotFoundError:
        code = -2
    except subprocess.TimeoutExpired:
        code = -1
    logger.info("PLINK --missing 完成（%.1fs，exit=%s）", time.time() - started, code)

    report = {
        "exit_code": code,
        "mean_sample_missing_rate": None,
        "max_sample_missing_rate": None,
        "mean_snp_missing_rate": None,
        "max_snp_missing_rate": None,
    }
    if code != 0:
        report["message"] = "plink --missing 执行失败，缺失率未知"
        return report

    def _fmiss_column(path):
        """读取 FMISS 列（末列）为浮点列表；无该列返回空。"""
        if not os.path.exists(path):
            return []
        content, _ = read_text_with_fallback(path)
        values = []
        for line in content.splitlines()[1:]:   # 跳过表头
            parts = line.split()
            if parts:
                try:
                    values.append(float(parts[-1]))
                except ValueError:
                    continue
        return values

    sample_miss = _fmiss_column(miss_prefix + ".imiss")
    snp_miss = _fmiss_column(miss_prefix + ".lmiss")
    if sample_miss:
        report["mean_sample_missing_rate"] = round(sum(sample_miss) / len(sample_miss), 6)
        report["max_sample_missing_rate"] = round(max(sample_miss), 6)
    if snp_miss:
        report["mean_snp_missing_rate"] = round(sum(snp_miss) / len(snp_miss), 6)
        report["max_snp_missing_rate"] = round(max(snp_miss), 6)
    return report


def read_genotype_ids(bfile_prefix):
    """读取 .fam 的 IID（第 2 列），ABGES 统一以此为个体主键。"""
    fam_path = bfile_prefix + ".fam"
    if not os.path.exists(fam_path):
        raise AbgesError("E031", "找不到 .fam 文件：%s" % fam_path)
    content, _ = read_text_with_fallback(fam_path)
    identifiers = []
    for line in content.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            identifiers.append(parts[1])
    return identifiers


# ===============================================================
# 三、质控执行
# ===============================================================

def run_plink_step(plink_args, output_prefix, workdir=None, plink_exe=None,
                   threads=None, timeout=3600, chr_set=DEFAULT_CHR_SET):
    """
    执行一步 PLINK 操作，输出为二进制格式。
    返回 (退出码, 输出前缀, 日志文本, 耗时秒)。
    """
    binary = resolve_plink_binary(plink_exe)
    thread_count = resolve_thread_count(threads)
    ensure_dir(os.path.dirname(output_prefix))

    command = [binary]
    command.extend(plink_args)
    command.extend(build_chromosome_args(chr_set))
    command.extend(["--threads", str(thread_count)])
    command.extend(["--make-bed", "--out", output_prefix])

    logger.info("执行 PLINK：%s", " ".join(command))
    started = time.time()
    try:
        completed = subprocess.run(command, capture_output=True, text=True,
                                   encoding="utf-8", errors="replace",
                                   cwd=workdir, timeout=timeout)
    except FileNotFoundError:
        return -2, output_prefix, "PLINK_NOT_FOUND", 0.0
    except subprocess.TimeoutExpired:
        return -1, output_prefix, "TIMEOUT", time.time() - started

    duration = time.time() - started
    log_text = (completed.stdout or "") + (completed.stderr or "")
    if completed.returncode != 0:
        logger.error("PLINK 失败，退出码=%d：%s", completed.returncode,
                      log_text[-1500:])
    return completed.returncode, output_prefix, log_text, duration


def run_genotype_qc(input_path, output_dir, thresholds=None, plink_exe=None,
                    threads=None, timeout=3600, chr_set=DEFAULT_CHR_SET,
                    timestamp=None):
    """
    执行完整基因型质控流程。

    顺序：格式转换（如需）-> mind -> geno -> maf -> hwe
    返回结果字典，含每步的保留规模、被剔除数量与最终输出前缀。
    任一步失败立即中止并如实返回，不做静默跳过。
    """
    from abges_core import make_timestamp

    config = _load_qc_thresholds(thresholds)
    stamp = timestamp or make_timestamp()
    ensure_dir(output_dir)

    fmt, input_args = detect_genotype_format(input_path)
    logger.info("基因型格式识别为：%s", fmt)

    # 频率/HWE 统计口径：默认全群，避免 founder 过小导致 HWE 规则静默失效
    scope_flags = resolve_freq_hwe_scope_flags(config)
    scope = str(config.get("freq_hwe_scope", DEFAULT_FREQ_HWE_SCOPE)).strip().lower()
    logger.info("等位基因频率/HWE 统计口径：%s（追加参数 %s）",
                 scope, scope_flags or "无，沿用 PLINK2 默认")

    result = {
        "input": input_path,
        "format": fmt,
        "thresholds": config,
        "freq_hwe_scope": scope,
        "freq_hwe_scope_flags": scope_flags,
        "steps": [],
        "status": "failed",
    }

    working_args = list(input_args)
    current_prefix = None

    # 若非二进制，先转换
    if fmt != "plink_binary":
        convert_prefix = os.path.join(output_dir, "geno_binary_%s" % stamp)
        code, current_prefix, log_text, duration = run_plink_step(
            working_args, convert_prefix, plink_exe=plink_exe, threads=threads,
            timeout=timeout, chr_set=chr_set)
        result["steps"].append(_summarize_step("convert", code, current_prefix,
                                               duration, log_text))
        if code != 0:
            result["message"] = "格式转换失败"
            return result
        working_args = ["--bfile", current_prefix]
    else:
        current_prefix = input_path

    scale_before = count_genotype_scale(current_prefix)
    # 基础群规模：founders_only 口径下的风险量化依据，全群口径下亦留档备查
    n_founder = count_founders(current_prefix)
    scale_before["n_founder"] = n_founder
    result["before"] = scale_before
    result["n_founder_raw"] = n_founder

    min_founders = config.get("min_founders_warning", 50)
    if scope == "founders_only" and n_founder < min_founders:
        warning = (
            "基础群仅 %d 个个体（低于告警下限 %d），且当前为 founders_only 口径："
            "--maf 频率估计不可靠、--hwe 检验功效不足，HWE 规则大概率剔除数为 0，"
            "建议改用 all_samples 口径。" % (n_founder, min_founders))
        logger.warning(warning)
        result.setdefault("warnings", []).append(warning)

    # O6（2026-08-24）：QC 前统计缺失基因型比例。
    # HIBLUP 无填充功能，缺失基因型会被强制编码为杂合(1)，先量化才能审计。
    # 只做统计不做剔除，缺失率进 gate_facts 供上层告警。
    result["missing_rate"] = compute_missing_rate(
        current_prefix, output_dir, stamp, plink_exe=plink_exe,
        threads=threads, timeout=timeout)

    # --nonfounders 只作用于频率与 HWE 计算，对 --mind/--geno 缺失率无影响，
    # 故仅追加在 maf / hwe 两步。
    qc_steps = (
        ("mind", ["--mind", str(config["mind"])]),
        ("geno", ["--geno", str(config["geno"])]),
        ("maf", ["--maf", str(config["maf"])] + scope_flags),
        ("hwe", ["--hwe", str(config["hwe"])] + scope_flags),
    )
    for step_name, step_args in qc_steps:
        step_prefix = os.path.join(output_dir, "geno_%s_%s" % (step_name, stamp))
        code, step_prefix, log_text, duration = run_plink_step(
            ["--bfile", current_prefix] + step_args, step_prefix,
            plink_exe=plink_exe, threads=threads, timeout=timeout,
            chr_set=chr_set)
        summary = _summarize_step(step_name, code, step_prefix, duration, log_text)
        result["steps"].append(summary)
        if code != 0:
            result["message"] = "质控步骤 %s 失败" % step_name
            return result
        current_prefix = step_prefix

    scale_after = count_genotype_scale(current_prefix)
    result["after"] = scale_after
    result["output_prefix"] = current_prefix
    result["removed"] = {
        "n_sample_removed": scale_before["n_sample"] - scale_after["n_sample"],
        "n_snp_removed": scale_before["n_snp"] - scale_after["n_snp"],
    }

    # 规模下限判定：不满足即 failed，不得继续构建关系矩阵
    if scale_after["n_snp"] < config["min_snp_after_qc"]:
        result["message"] = "质控后位点数 %d 低于下限 %d" % (
            scale_after["n_snp"], config["min_snp_after_qc"])
        return result
    if scale_after["n_sample"] < config["min_sample_after_qc"]:
        result["message"] = "质控后个体数 %d 低于下限 %d" % (
            scale_after["n_sample"], config["min_sample_after_qc"])
        return result

    result["status"] = "completed"
    return result


def _summarize_step(step_name, exit_code, prefix, duration, log_text):
    """汇总单步质控结果，日志尾部保留供追溯。"""
    summary = {
        "step": step_name,
        "exit_code": exit_code,
        "output_prefix": prefix,
        "duration_seconds": round(duration, 2),
        "log_tail": (log_text or "")[-800:],
    }
    if exit_code == 0 and os.path.exists(prefix + ".fam"):
        summary.update(count_genotype_scale(prefix))
    return summary


def _load_qc_thresholds(overrides=None):
    """
    加载基因型质控阈值。默认取自 configs/qc/genotype-qc.yaml，
    调用方可覆盖，覆盖须由上层记入审计。
    """
    from abges_core import load_yaml_config
    defaults = {
        "mind": 0.10, "geno": 0.10, "maf": 0.01, "hwe": 1e-6,
        "min_snp_after_qc": 1000, "min_sample_after_qc": 50,
        "freq_hwe_scope": DEFAULT_FREQ_HWE_SCOPE,
        "min_founders_warning": 50,
    }
    try:
        config = load_yaml_config("configs/qc/genotype-qc.yaml")
        defaults.update(config.get("thresholds") or {})
        # freq_hwe_scope / min_founders_warning 是顶层字段，不在 thresholds 段内
        if config.get("freq_hwe_scope"):
            defaults["freq_hwe_scope"] = config["freq_hwe_scope"]
        if config.get("min_founders_warning") is not None:
            defaults["min_founders_warning"] = config["min_founders_warning"]
    except AbgesError:
        logger.warning("未能加载基因型质控配置，使用内置默认阈值")
    if overrides:
        defaults.update(overrides)
    return defaults


def compute_id_overlap(bfile_prefix, phenotype_ids):
    """
    统计基因型与表型的 ID 交集，交集为零时给出诊断样例，
    便于快速定位 FID/IID 错位或前导零丢失。
    """
    genotype_ids = set(read_genotype_ids(bfile_prefix))
    phenotype_set = set(str(item) for item in phenotype_ids)
    overlap = genotype_ids & phenotype_set
    diagnostic = {}
    if not overlap:
        diagnostic = {
            "genotype_sample": sorted(genotype_ids)[:10],
            "phenotype_sample": sorted(phenotype_set)[:10],
            "hint": "常见原因：FID/IID 顺序错、前导零丢失、编号带前后缀",
        }
    return {
        "n_genotype": len(genotype_ids),
        "n_phenotype": len(phenotype_set),
        "n_overlap": len(overlap),
        "diagnostic": diagnostic,
    }
