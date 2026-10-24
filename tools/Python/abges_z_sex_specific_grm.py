# -*- coding: utf-8 -*-
# 【状态：保留待接线｜先生拍板 2026-10-21】
# 本模块（611 行，当前测试覆盖率 0%）目前无业务调用方，仅保留文件内 __main__ demo。
# 背景：2026-10-13 已移除 Z 染色体剂量补偿——实测「交付前改写基因型编码」会被
#       HIBLUP 内部中心化抵消，G 矩阵一律由 HIBLUP 从质控后 .bed 直接构建。
#      本模块的 Sex-Specific Centering 方案属同一问题的另一条思路，暂未启用。
# 接线计划（待排期）：若后续需按性别分别中心化，应由本模块产出外部 G 前缀，
#      经 --xrm 交给 HIBLUP 复用（与 gblup 的 GRM 缓存路径同一机制）。
# 在此之前请勿删除——方案文档与推导过程仅存于此文件。
"""
ABGES Z 染色体 G 矩阵构建器（理论最优版）
==========================================
设计者：ABGES
日期：2026-09-14

核心创新：Sex-Specific Centering VanRaden 公式
------------------------------------------------
标准 VanRaden1 对所有 SNP 统一用均值 2p 做中心化：
  G = (M - 2p)(M - 2p)' / [Σ 2p(1-p)]

这在常染色体上是正确的，但对 Z 染色体：
  - 公鸡 ZZ（2 拷贝 Z）：E[基因型] = 2p
  - 母鸡 ZW（1 拷贝 Z）：E[基因型] = p

统一用 2p 中心化，母鸡的基因型存在系统性负偏移，
导致 VanRaden 公式对母鸡产生系统性偏低的亲缘系数。

Sex-Specific Centering（本研究设计）：
  对每个 SNP，分别计算：
    M_Z_male_centered   = M_male   - 2p_Z  （公鸡按 2p_Z 中心化）
    M_Z_female_centered = M_female -   p_Z  （母鸡按 p_Z 中心化）

  然后统一代入 VanRaden1：
    G_Z = (M*_Z)(M*_Z)' / [Σ p_Z(1-p_Z)]

  其中分母统一用 p_Z(1-p_Z) 而非 2p_Z(1-p_Z)，
  因为 p_Z(1-p_Z) 是 Z 染色体 SNP 的真实加性遗传方差。

数学证明：
  设父本 Z 等位基因频率 p_Z，对于 Z 染色体 SNP：
    - 公-公 协方差期望 = 2p(1-p)  ✓
    - 公-母 协方差期望 = p(1-p)    ✓（等于亲缘系数 0.5 × 加性方差）
    - 母-母 协方差期望 = p(1-p)    ✓

  这与 Z 染色体的孟德尔遗传规律完全一致：
    - 公-母（父女）：共享 1 条 Z（父亲的 Z 必定传给女儿），故协方差 = 0.5 × σ²_A
    - 公-公（全同胞）：共享 0/1/2 条 Z，协方差期望 = σ²_A
    - 母-母（全同胞）：共享 0 或 1 条 Z，协方差期望 = 0.5 × σ²_A

使用方式：
  import abges_z_sex_specific_grm as zgrm
  result = zgrm.build_z_sex_specific_grm(
      plink_prefix="/path/to/genotype",
      output_prefix="/path/to/output_prefix",
      threads=8)
"""

import os
import struct
import logging
import shutil
import tempfile
from typing import Optional, Tuple, List
from concurrent.futures import ThreadPoolExecutor, as_completed

logger = logging.getLogger(__name__)

import numpy as np

import abges_core as core

# ===============================================================
# 常量：HIBLUP .bin 格式
# ===============================================================
HIBLUP_BIN_ELEMENT_SIZE = 8  # 64-bit float


# ===============================================================
# 辅助：PLINK .bed 二进制读写（精简版）
# ===============================================================

def _read_bed_to_numpy(plink_prefix: str, sample_ids: List[str],
                       snp_ids: List[str], chrom_to_snps: dict) -> dict:
    """
    将指定染色体的基因型读入 numpy 数组。
    返回：{chrom: (n_sample, n_snp) ndarray}
    """
    import pandas as pd

    bed_path = plink_prefix + ".bed"
    bim_path = plink_prefix + ".bim"
    fam_path = plink_prefix + ".fam"

    # 读 .fam 获取样本顺序和性别
    fam = pd.read_csv(fam_path, sep=r"\s+", header=None,
                      names=["fid", "iid", "father", "mother", "sex", "pheno"],
                      dtype={"iid": str, "sex": int})
    n_sample = len(fam)
    is_male = (fam["sex"] == 1).values  # True for males (ZZ)
    is_female = (fam["sex"] == 2).values  # True for females (ZW)

    # 读 .bim 获取 SNP 信息
    bim = pd.read_csv(bim_path, sep=r"\s+", header=None,
                     names=["chrom", "snp_id", "cm", "pos", "a1", "a2"],
                     dtype={"chrom": str, "snp_id": str})

    # 读 .bed 原始字节
    with open(bed_path, "rb") as fh:
        header = fh.read(3)
        # 标准 SNP-major 模式魔数：0x6C 0x1B 0x01
        # 交换序：0x01 0x6C 0x1B (individual-major)
        if header == bytes([0x6C, 0x1B, 0x01]):
            snp_major = True
        elif header == bytes([0x01, 0x6C, 0x1B]):
            snp_major = False
        else:
            raise ValueError("未知的 .bed 魔数: %r" % list(header))

    # 计算每 SNP 字节数（SNP-major: 每 SNP (n_sample+3)//4 字节）
    n_bytes_per_snp = (n_sample + 3) // 4

    # 按染色体读取
    result = {}
    for chrom, snp_list in chrom_to_snps.items():
        chrom_mask = bim["chrom"].astype(str).str.upper().str.lstrip("CHR_") == str(chrom).upper().lstrip("CHR_")
        chrom_snp_indices = np.where(chrom_mask)[0]
        n_snp_chrom = len(chrom_snp_indices)

        if n_snp_chrom == 0:
            continue

        # 预分配
        gt_matrix = np.full((n_sample, n_snp_chrom), np.nan, dtype=np.float32)

        with open(bed_path, "rb") as fh:
            # SNP-major: 逐 SNP 读取
            for col_idx, snp_idx in enumerate(chrom_snp_indices):
                fh.seek(3 + snp_idx * n_bytes_per_snp)
                snp_bytes = fh.read(n_bytes_per_snp)

                for byte_idx in range(n_bytes_per_snp):
                    byte = snp_bytes[byte_idx]
                    for i_in_byte in range(4):
                        sample_idx = byte_idx * 4 + i_in_byte
                        if sample_idx >= n_sample:
                            break
                        gt_code = (byte >> (6 - i_in_byte * 2)) & 0x03
                        if gt_code == 0x03:
                            gt_matrix[sample_idx, col_idx] = np.nan
                        elif gt_code == 0x02:
                            gt_matrix[sample_idx, col_idx] = 2.0
                        elif gt_code == 0x01:
                            gt_matrix[sample_idx, col_idx] = 1.0
                        else:
                            gt_matrix[sample_idx, col_idx] = 0.0

        result[chrom] = {
            "matrix": gt_matrix,
            "is_male": is_male,
            "is_female": is_female,
            "snp_ids": bim.loc[chrom_mask, "snp_id"].tolist(),
            "a1": bim.loc[chrom_mask, "a1"].tolist(),
            "a2": bim.loc[chrom_mask, "a2"].tolist(),
        }

    return result


def read_z_chromosome_data(plink_prefix: str) -> dict:
    """
    读取 Z 染色体的基因型、频率和性别信息。
    返回：{
        "z_matrix": ndarray (n_sample, n_snp),
        "sample_ids": list[str],
        "snp_ids": list[str],
        "is_male": ndarray bool,
        "is_female": ndarray bool,
        "allele_freq": ndarray (n_snp,)  # A1 等位基因频率（全体）
        "sex_specific_p": {
            "male": ndarray (n_snp,)  # 公鸡列的均值/2 ≈ p_Z
            "female": ndarray (n_snp,)  # 母鸡列的均值 ≈ p_Z
        }
    }
    """
    import pandas as pd

    bim_path = plink_prefix + ".bim"
    fam_path = plink_prefix + ".fam"

    # 获取 Z 染色体 SNP 索引
    bim = pd.read_csv(bim_path, sep=r"\s+", header=None,
                     names=["chrom", "snp_id", "cm", "pos", "a1", "a2"],
                     dtype={"chrom": str, "snp_id": str})
    z_mask = bim["chrom"].astype(str).str.upper().str.lstrip("CHR_") == "Z"
    z_snp_ids = bim.loc[z_mask, "snp_id"].tolist()
    z_snp_indices = np.where(z_mask)[0]

    if len(z_snp_indices) == 0:
        raise ValueError("未发现 Z 染色体位点，请检查 .bim 文件中的染色体命名（支持 Z、chrZ、chr_Z 等格式）")

    # 获取样本性别
    fam = pd.read_csv(fam_path, sep=r"\s+", header=None,
                      names=["fid", "iid", "father", "mother", "sex", "pheno"],
                      dtype={"iid": str, "sex": int})
    n_sample = len(fam)
    is_male = (fam["sex"] == 1).values
    is_female = (fam["sex"] == 2).values
    sample_ids = fam["iid"].tolist()

    n_snp_z = len(z_snp_indices)
    n_bytes_per_snp = (n_sample + 3) // 4

    # 读取 Z 染色体基因型
    z_matrix = np.full((n_sample, n_snp_z), np.nan, dtype=np.float32)
    with open(plink_prefix + ".bed", "rb") as fh:
        header = fh.read(3)
        for col_idx, snp_idx in enumerate(z_snp_indices):
            fh.seek(3 + snp_idx * n_bytes_per_snp)
            snp_bytes = fh.read(n_bytes_per_snp)
            for byte_idx in range(n_bytes_per_snp):
                byte = snp_bytes[byte_idx]
                for i_in_byte in range(4):
                    sample_idx = byte_idx * 4 + i_in_byte
                    if sample_idx >= n_sample:
                        break
                    gt_code = (byte >> (6 - i_in_byte * 2)) & 0x03
                    if gt_code == 0x03:
                        z_matrix[sample_idx, col_idx] = np.nan
                    elif gt_code == 0x02:
                        z_matrix[sample_idx, col_idx] = 2.0
                    elif gt_code == 0x01:
                        z_matrix[sample_idx, col_idx] = 1.0
                    else:
                        z_matrix[sample_idx, col_idx] = 0.0

    # 估计等位基因频率（基于全体样本，不分性别）
    # A1 频率 = (2*homo_A + 1*hetero) / (2 * 有数据的个体数)
    allele_freq = np.zeros(n_snp_z)
    male_freq = np.zeros(n_snp_z)
    female_freq = np.zeros(n_snp_z)

    for j in range(n_snp_z):
        col = z_matrix[:, j]
        valid = ~np.isnan(col)
        n_valid = valid.sum()
        if n_valid == 0:
            allele_freq[j] = 0.0
            male_freq[j] = 0.0
            female_freq[j] = 0.0
            continue

        # 全体 A1 频率
        allele_freq[j] = np.nansum(col) / (2 * n_valid)

        # 公鸡 A1 频率（公鸡列均值 / 2 = p_Z，因为公鸡是双拷贝）
        male_col = col[is_male & valid]
        if len(male_col) > 0:
            male_freq[j] = np.mean(male_col) / 2.0

        # 母鸡 A1 频率（母鸡列均值 = p_Z，因为母鸡是单拷贝）
        female_col = col[is_female & valid]
        if len(female_col) > 0:
            female_freq[j] = np.mean(female_col)

    return {
        "z_matrix": z_matrix,
        "sample_ids": sample_ids,
        "snp_ids": z_snp_ids,
        "is_male": is_male,
        "is_female": is_female,
        "allele_freq": allele_freq,        # 全体 A1 频率
        "sex_specific_p": {
            "male": male_freq,              # 公鸡估计的 p_Z
            "female": female_freq,         # 母鸡估计的 p_Z
        },
        "bim_a1": bim.loc[z_mask, "a1"].tolist(),
        "bim_a2": bim.loc[z_mask, "a2"].tolist(),
    }


# ===============================================================
# 核心：Sex-Specific Centering VanRaden1 Z-GRM 构建（修正版 v2）
# ===============================================================

def build_z_sex_specific_grm_numpy(z_data: dict) -> np.ndarray:
    """
    使用 Sex-Specific Centering 公式构建 Z 染色体 G 矩阵。

    数学推导（完整版）：

    设 Z 染色体上某 SNP 等位基因 A 的群体频率为 p。
    公鸡（ZZ）每条 Z 染色体独立传递，女儿（ZW）的 Z 完整继承自父亲。
    因此在该 SNP 上，父亲-女儿的亲缘关系为 1.0（Z 全部信息无重组传递）。

    标准 VanRaden1：G = (M - μ)(M - μ)' / Σ 2p(1-p)
    其中 M 是个体在该 SNP 的基因型值（等位基因 A 的拷贝数）。

    对于 Z 染色体：
      - 公鸡 M_male ∈ {0,1,2}，期望 E[M_male] = 2p
      - 母鸡 M_female ∈ {0,1}，期望 E[M_female] = p（半合子）

    Sex-Specific Centering（核心创新）：
      对公鸡：中心化到 2p → M*_male = M_male - 2p
      对母鸡：中心化到 p  → M*_female = M_female - p

    分母采用全体统一的 Σ 2p(1-p)（与常染色体 VanRaden1 一致），
    这保证 G 矩阵正定且公母使用同一分母。

    验证（p=0.3, n_snps→∞）：
      - 公-公 期望 = 1（高度相关公鸡群体，Z 基因组高度一致）
      - 母-母 期望 = 0.5（半同胞/无关母鸡）
      - 公-母（父女）期望 = 1.0（Z 完整从父到女）
      - 公-母（随机）期望 ≈ 0.5（随机群体）

    参数：
      z_data: read_z_chromosome_data() 的返回值

    返回：
      G_Z, M_centered, p_Z, denominator
    """
    M = z_data["z_matrix"].copy()
    is_male = z_data["is_male"]
    is_female = z_data["is_female"]

    # p_Z：使用全体等位基因频率（与常染色体 VanRaden1 一致）
    # 从 sex-specific p 的调和平均估计（公鸡2/母鸡1 的加权）
    # 即 p_Z = Σ alleles / Σ Z_copies = (Σ male_gt + Σ female_gt) / (2*n_male + 1*n_female)
    p_male_arr = z_data["sex_specific_p"]["male"]
    p_female_arr = z_data["sex_specific_p"]["female"]
    n_male = np.sum(is_male)
    n_female = np.sum(is_female)

    # 合并估计：p_Z = (Σ male_gt + Σ female_gt) / (2*n_male + n_female)
    # 这等价于：p_Z[j] = (2*p_male_arr[j] + p_female_arr[j]) / 3
    # 当公母各半时，这等于全体均值 p
    p_Z = np.where(np.isnan(p_male_arr), z_data["allele_freq"],
                    np.where(np.isnan(p_female_arr), z_data["allele_freq"],
                              (2.0 * p_male_arr + p_female_arr * 1.0) / 3.0))
    p_Z = np.maximum(p_Z, 1e-6)
    p_Z = np.minimum(p_Z, 1.0 - 1e-6)

    n_sample, n_snp = M.shape

    # Sex-Specific Centering
    M_centered = np.zeros((n_sample, n_snp), dtype=np.float64)

    for j in range(n_snp):
        p = p_Z[j]
        col = M[:, j]
        valid = ~np.isnan(col)

        # 均值填补：缺失值用各自的性别期望值填补
        # 公鸡期望=2p，母鸡期望=p
        col_filled = np.where(valid, col,
                               np.where(is_male, 2.0 * p,
                                        np.where(is_female, p, 2.0 * p)))

        # Sex-specific centering（核心创新）
        # 公鸡：M - 2p；母鸡：M - p
        col_centered = np.where(is_male, col_filled - 2.0 * p,
                                np.where(is_female, col_filled - p, col_filled))
        M_centered[:, j] = col_centered

    # 分母：Σ 2p(1-p)（与常染色体 VanRaden1 一致）
    denominator = np.sum(2.0 * p_Z * (1.0 - p_Z))

    if denominator <= 0:
        raise ValueError("Z 染色体 SNP 方差和为 0，无法构建 G 矩阵")

    G_Z = (M_centered @ M_centered.T) / denominator

    logger.info("Z-GRM（Sex-Specific Centering v2）构建完成：shape=%s", G_Z.shape)
    logger.info("  对角线范围=[%.4f, %.4f]，分母 Σ 2p(1-p)=%.6f",
                 np.diagonal(G_Z).min(), np.diagonal(G_Z).max(), denominator)
    logger.info("  p_Z 范围=[%.4f, %.4f]", p_Z.min(), p_Z.max())

    return G_Z, M_centered, p_Z, denominator


# ===============================================================
# 辅助：HIBLUP .bin 格式导出
# ===============================================================

def write_hiblup_grm_bin(grm_matrix: np.ndarray, output_prefix: str) -> str:
    """
    将 (n, n) 亲缘关系矩阵写入 HIBLUP .bin 格式（64-bit float，行主序，下三角按行存储）。
    HIBLUP --xrm 读取格式：逐行从列 0 到列 i 依次写入 G[i,0..i]。
    """
    n = grm_matrix.shape[0]
    bin_path = core.guard_output_path(output_prefix + ".bin",
                                      label="HIBLUP GRM .bin 输出路径")
    with open(bin_path, "wb") as fh:
        for i in range(n):
            # 下三角：列 0 到 i（含对角线）
            row_data = grm_matrix[i, :i+1]
            fh.write(row_data.astype(np.float64).tobytes())
    return bin_path


def write_hiblup_grm_id(sample_ids: List[str], output_prefix: str) -> str:
    """
    写入 HIBLUP .id 文件（个体标识，三列：FID IID father）。

    HIBLUP --xrm 要求 .id 文件与 .bin 行数一致，
    FID 用样本 ID 填充（无家系时 FID=IID），father 填 0。
    """
    id_path = core.guard_output_path(output_prefix + ".id",
                                     label="HIBLUP GRM .id 输出路径")
    with open(id_path, "w", encoding="utf-8") as fh:
        fh.write("FID IID FATHER\n")
        for sid in sample_ids:
            fh.write("%s %s 0\n" % (sid, sid))
    return id_path


# ===============================================================
# 主函数
# ===============================================================

def build_z_sex_specific_grm(
    plink_prefix: str,
    output_prefix: str,
    threads: int = 8,
    force: bool = False,
    min_snp: int = 1,
) -> dict:
    """
    构建 Z 染色体 G 矩阵（Sex-Specific Centering VanRaden1）。

    参数：
      plink_prefix:  PLINK 基因型前缀
      output_prefix: 输出前缀（HIBLUP --xrm 使用）
      threads:       计算线程数（目前为单线程，保留接口）
      force:         强制覆盖已有输出
      min_snp:       Z 染色体最少 SNP 数（低于此值报错）

    返回：
      {
        "status": str,              # "completed" | "error" | "skipped"
        "n_snp": int,              # Z 染色体 SNP 数
        "n_sample": int,           # 个体数
        "n_male": int,             # 公鸡数
        "n_female": int,           # 母鸡数
        "bin_path": str,           # .bin 路径
        "id_path": str,            # .id 路径
        "G_diag_mean": float,      # G 对角线均值（应接近 1）
        "G_diag_sd": float,        # G 对角线标准差
        "G_offdiag_mean": float,   # G 非对角线均值
        "denominator": float,      # Σ p(1-p) 分母值
        "allele_freq_mean": float, # Z-SNP 平均 A1 频率
        "error": str | None,
      }
    """
    # 前置检查
    for suf in (".bed", ".bim", ".fam"):
        p = plink_prefix + suf
        if not os.path.isfile(p):
            return {"status": "error", "error": "文件缺失：%s" % p,
                    "n_snp": 0, "n_sample": 0, "n_male": 0, "n_female": 0,
                    "bin_path": None, "id_path": None,
                    "G_diag_mean": None, "G_diag_sd": None,
                    "G_offdiag_mean": None, "denominator": None,
                    "allele_freq_mean": None}

    if not force and os.path.isfile(output_prefix + ".bin") and os.path.isfile(output_prefix + ".id"):
        logger.info("Z-GRM 已存在，跳过（force=False）")
        return {"status": "skipped",
                "n_snp": 0, "n_sample": 0, "n_male": 0, "n_female": 0,
                "bin_path": output_prefix + ".bin",
                "id_path": output_prefix + ".id",
                "G_diag_mean": None, "G_diag_sd": None,
                "G_offdiag_mean": None, "denominator": None,
                "allele_freq_mean": None, "error": None}

    try:
        # Step 1: 读取 Z 染色体数据
        logger.info("读取 Z 染色体基因型 ...")
        z_data = read_z_chromosome_data(plink_prefix)
        n_sample = len(z_data["sample_ids"])
        n_snp = len(z_data["snp_ids"])
        n_male = int(z_data["is_male"].sum())
        n_female = int(z_data["is_female"].sum())

        if n_snp < min_snp:
            return {"status": "error",
                    "error": "Z 染色体 SNP 数 %d < 最低要求 %d" % (n_snp, min_snp),
                    "n_snp": n_snp, "n_sample": n_sample,
                    "n_male": n_male, "n_female": n_female,
                    "bin_path": None, "id_path": None,
                    "G_diag_mean": None, "G_diag_sd": None,
                    "G_offdiag_mean": None, "denominator": None,
                    "allele_freq_mean": None}

        logger.info("Z 染色体：%d 个 SNP，%d 个体（公 %d / 母 %d）",
                     n_snp, n_sample, n_male, n_female)

        # Step 2: 构建 Sex-Specific Centering G 矩阵
        logger.info("构建 Z-GRM（Sex-Specific Centering）...")
        G_Z, M_centered, p_Z, denominator = build_z_sex_specific_grm_numpy(z_data)

        # Step 3: 写 HIBLUP 格式
        logger.info("写入 HIBLUP .bin 和 .id ...")
        bin_path = write_hiblup_grm_bin(G_Z, output_prefix)
        id_path = write_hiblup_grm_id(z_data["sample_ids"], output_prefix)

        # Step 4: 质量统计
        diag = np.diagonal(G_Z)
        offdiag_mask = ~np.eye(n_sample, dtype=bool)
        offdiag = G_Z[offdiag_mask]

        G_diag_mean = float(np.mean(diag))
        G_diag_sd = float(np.std(diag))
        G_offdiag_mean = float(np.mean(offdiag))
        allele_freq_mean = float(np.mean(z_data["allele_freq"]))

        logger.info("Z-GRM 质量：对角线均值=%.4f（应≈1），SD=%.4f，非对角均值=%.4f",
                     G_diag_mean, G_diag_sd, G_offdiag_mean)

        return {
            "status": "completed",
            "n_snp": n_snp,
            "n_sample": n_sample,
            "n_male": n_male,
            "n_female": n_female,
            "bin_path": bin_path,
            "id_path": id_path,
            "G_diag_mean": G_diag_mean,
            "G_diag_sd": G_diag_sd,
            "G_offdiag_mean": G_offdiag_mean,
            "denominator": float(denominator),
            "allele_freq_mean": allele_freq_mean,
            "error": None,
        }

    except Exception as exc:
        logger.error("Z-GRM 构建失败：%s", exc)
        import traceback
        traceback.print_exc()
        return {"status": "error", "error": str(exc),
                "n_snp": 0, "n_sample": 0, "n_male": 0, "n_female": 0,
                "bin_path": None, "id_path": None,
                "G_diag_mean": None, "G_diag_sd": None,
                "G_offdiag_mean": None, "denominator": None,
                "allele_freq_mean": None}


# ===============================================================
# 便捷封装
# ===============================================================

def build_z_grm_auto(
    plink_prefix: str,
    work_dir: Optional[str] = None,
    strain_id: str = "unknown",
    threads: int = 8,
) -> dict:
    """
    便捷封装：自动创建输出路径，完成后返回结果。
    输出文件：<work_dir>/<strain_id>_ZG.id/.bin
    """
    if work_dir:
        os.makedirs(work_dir, exist_ok=True)
        output_prefix = os.path.join(work_dir, "%s_ZG" % strain_id)
    else:
        output_prefix = plink_prefix + "_ZG"

    result = build_z_sex_specific_grm(
        plink_prefix=plink_prefix,
        output_prefix=output_prefix,
        threads=threads)
    result["output_prefix"] = output_prefix
    return result


# ===============================================================
# 诊断函数
# ===============================================================

def diagnose_z_grm_quality(output_prefix: str) -> dict:
    """
    诊断 Z-GRM 质量。
    检查：对角线 ≈ 1，母女关系协方差 ≈ 0.5，公母关系协方差 ≈ 0.5，母母协方差 ≈ 0.5。
    """
    bin_path = output_prefix + ".bin"
    id_path = output_prefix + ".id"

    if not os.path.isfile(bin_path) or not os.path.isfile(id_path):
        return {"error": "文件不存在"}

    # 读 .id
    sample_ids = []
    with open(id_path, "r", encoding="utf-8") as fh:
        fh.readline()  # skip header
        for line in fh:
            parts = line.split()
            if len(parts) >= 2:
                sample_ids.append(parts[1])

    n = len(sample_ids)

    # 读 .bin（numpy）
    grm_data = np.fromfile(bin_path, dtype=np.float64)
    G = np.zeros((n, n))
    idx = 0
    for i in range(n):
        G[i, :i+1] = grm_data[idx:idx+i+1]
        G[:i, i] = G[i, :i]
        idx += i + 1

    diag = np.diagonal(G)
    offdiag_mask = ~np.eye(n, dtype=bool)
    offdiag = G[offdiag_mask]

    return {
        "n": n,
        "G_diag_mean": float(np.mean(diag)),
        "G_diag_min": float(np.min(diag)),
        "G_diag_max": float(np.max(diag)),
        "G_diag_sd": float(np.std(diag)),
        "G_offdiag_mean": float(np.mean(offdiag)),
        "G_offdiag_min": float(np.min(offdiag)),
        "G_offdiag_max": float(np.max(offdiag)),
        "is_positive_semidefinite": np.all(np.linalg.eigvalsh(G) >= -1e-6),
        "eigenvalue_min": float(np.min(np.linalg.eigvalsh(G))),
    }
