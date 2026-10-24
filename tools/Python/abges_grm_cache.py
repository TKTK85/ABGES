# -*- coding: utf-8 -*-
"""
ABGES GRM 缓存工具（O2，2026-08-24）
=====================================
同品系多性状 GBLUP/SSGBLUP 场景下，G 矩阵构建只与基因型数据有关，
与性状无关。预构建一次 GRM 后经 --xrm 复用，避免每个性状重复建矩阵。

HIBLUP --make-xrm（配合 --add/--dom）产出：
    <prefix>.<TYPE>.bin / <prefix>.<TYPE>.id / <prefix>.log
  其中 TYPE 为矩阵类型码（GA/GD/GAD/HA/HD/HAD）。
  例如 --make-xrm --add --out demo 产出 demo.GA.bin / demo.GA.id / demo.log。

【2026-10-13 实测修订】旧版 GRM_OUTPUT_SUFFIXES 写成 (".ginv", ".id", ".log")，
与 HIBLUP 实际产出（带 .TYPE 段的 .bin/.id）不符，导致：
  1) 缓存命中判定恒为假（缓存永远不命中，每次强制重建）；
  2) 返回的 prefix 缺少 .TYPE 段，--xrm 收到裸前缀后找不到 .id 文件而报错。
现统一以 `_grm_file_set(output_prefix, matrix_type)` 计算真实文件三元组，
且 get_or_build_grm 返回供 --xrm 消费的前缀 = output_prefix + "." + matrix_type。

命名约定（O9）：缓存前缀 = <strain_id>_<MATRIX_TYPE>
  - GA：加性 G 矩阵（默认）
  - GD：显性 D 矩阵
  - GAD：加性+显性
  - HA：SSGBLUP H 矩阵加性（需系谱）

缓存失效策略：
  - 以基因型 PLINK 前缀的 .bed/.bim/.fam 文件 mtime 作为指纹；
  - 指纹变化时自动重建，不依赖人工 invalidate；
  - cache_dir 下的 manifest.json 记录 strain_id -> {fingerprint, matrix_type, path, built_at}。
"""

import os
import json
import time
import logging
import uuid
from functools import wraps
from abges_runtime.storage import sha256, atomic_json, identifier, file_lock

logger = logging.getLogger(__name__)

import hiblup_tool

# 合法矩阵类型码（O9 命名约定）
VALID_MATRIX_TYPES = ("GA", "GD", "GAD", "HA", "HD", "HAD")


def _fingerprint_equal(stored, current):
    """
    比对两份基因型指纹是否一致。

    【2026-10-13 修订】指纹为 {后缀: (mtime_ns, size)} 的元组字典，
    存入 manifest.json 经 JSON 序列化后元组会退化为列表，直接 == 比较恒为假，
    导致缓存永不上命中。此处统一按 list 比较以消除序列化差异。
    """
    if not isinstance(stored, dict) or not isinstance(current, dict):
        return False
    if set(stored.keys()) != set(current.keys()):
        return False
    for key in stored:
        if list(stored[key]) != list(current[key]):
            return False
    return True


def _grm_file_set(output_prefix, matrix_type):
    """
    返回某矩阵类型在 --make-xrm 后的真实产出文件三元组 (bin, id, log)。

    HIBLUP 把矩阵类型码插入到前缀与后缀之间：
        <prefix>.<TYPE>.bin / <prefix>.<TYPE>.id / <prefix>.log
    旧版 GRM_OUTPUT_SUFFIXES 写成 (.ginv, .id, .log) 与实际不符，已废弃。
    """
    typed_base = output_prefix + "." + matrix_type
    return (typed_base + ".bin", typed_base + ".id", output_prefix + ".log")


def _genotype_fingerprint(genotype_prefix):
    """
    计算基因型数据指纹：PLINK 二进制三件套的 (mtime_ns, size) 元组。
    任一文件变化即指纹变化，触发 GRM 重建。
    """
    parts = {}
    for suffix in (".bed", ".bim", ".fam"):
        path = genotype_prefix + suffix
        if not os.path.isfile(path):
            return None
        stat = os.stat(path)
        parts[suffix] = [stat.st_size, sha256(path)]
    return parts


def _cache_manifest_path(cache_dir):
    return os.path.join(cache_dir, "grm_cache_manifest.json")


def _load_manifest(cache_dir):
    path = _cache_manifest_path(cache_dir)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("GRM 缓存 manifest 读取失败，视为空：%s", exc)
        return {}


def _save_manifest(cache_dir, manifest):
    path = _cache_manifest_path(cache_dir)
    os.makedirs(cache_dir, exist_ok=True)
    atomic_json(path, manifest)


def _cache_key(strain_id, matrix_type):
    return "%s__%s" % (strain_id, matrix_type)


def build_grm(genotype_prefix, output_prefix, matrix_type="GA",
              threads=16, hiblup_exe=None, timeout=3600,
              pedigree_path=None, alpha=0.05,
              correct_z_chromosome=False, corrected_genotype_prefix=None, executor=None):
    """
    调用 HIBLUP --make-xrm 预构建关系矩阵。

    参数：
      genotype_prefix: PLINK 二进制前缀（不含 .bed/.bim/.fam）
      output_prefix: 输出前缀，应符合 O9 命名：<strain>_<TYPE>
      matrix_type: GA/GD/GAD/HA/HD/HAD，决定 --add/--dom 组合
      threads: 线程数
      hiblup_exe: 可执行文件路径（None 时自动解析）
      timeout: 超时秒数
      pedigree_path: SSGBLUP H 矩阵需要系谱（matrix_type 以 H 开头时必填）
      alpha: H 矩阵融合参数（默认 0.05）
      correct_z_chromosome: 【2026-10-13 已移除，保留仅为向后兼容】
        必须传 False；为真时本函数直接报错，不做静默忽略。
        Z 染色体剂量补偿功能已删除，G 矩阵一律由 HIBLUP 从质控后 .bed 构建。
      corrected_genotype_prefix: 【已废弃】原剂量补偿后的 PLINK 前缀，不再使用

    返回：
      {"prefix": 供 --xrm 消费的前缀(output_prefix.TYPE), "files": [...],
       "duration": seconds, "exit_code": int}

    异常：
      matrix_type 非法 / H 矩阵缺系谱 / 基因型文件不存在 → AbgesError
    """
    if matrix_type not in VALID_MATRIX_TYPES:
        raise hiblup_tool.AbgesError(
            "E013", "非法矩阵类型 %r，合法值：%s" % (matrix_type, VALID_MATRIX_TYPES))

    # 【2026-10-13】Z 剂量补偿已移除：G 矩阵直接由 HIBLUP 从质控后 .bed 构建，
    # 交付前不做任何编码改写。为真时明确报错，避免调用方误以为补偿仍生效。
    if correct_z_chromosome:
        raise hiblup_tool.AbgesError(
            "E014", "Z 染色体剂量补偿已移除，G 矩阵直接由 HIBLUP 构建；"
                    "correct_z_chromosome 必须为 False")
    effective_prefix = genotype_prefix

    # 校验基因型文件
    for suffix in (".bed", ".bim", ".fam"):
        if not os.path.isfile(effective_prefix + suffix):
            raise hiblup_tool.AbgesError(
                "E014", "基因型文件缺失：%s%s" % (effective_prefix, suffix))

    # H 矩阵需要系谱
    if matrix_type.startswith("H") and not pedigree_path:
        raise hiblup_tool.AbgesError(
            "E014", "SSGBLUP H 矩阵（%s）需要系谱文件" % matrix_type)

    binary = hiblup_tool.resolve_hiblup_binary(hiblup_exe)
    command = [binary, "--make-xrm", "--bfile", effective_prefix]

    # 加性/显性组合
    if "A" in matrix_type:
        command.append("--add")
    if "D" in matrix_type:
        command.append("--dom")

    # H 矩阵参数
    if matrix_type.startswith("H"):
        command.extend(["--pedigree", pedigree_path, "--alpha", str(alpha)])

    command.extend(["--threads", str(threads), "--out", output_prefix])

    logger.info("O2 GRM 预构建：matrix_type=%s, genotype=%s, output=%s",
                 matrix_type, genotype_prefix, output_prefix)

    if executor is None:
        raise hiblup_tool.AbgesError("E032", "GRM construction requires the Harness ToolExecutor")
    executed = executor.run_hiblup(command, workdir=os.path.dirname(output_prefix) or ".",
                                   timeout=timeout, threads=threads)
    exit_code, stdout, stderr, duration = executed.exit_code, executed.stdout, executed.stderr, executed.duration

    if exit_code != 0:
        raise hiblup_tool.AbgesError(
            "E014", "GRM 预构建失败（exit=%d）：%s"
                    % (exit_code, (stderr or stdout or "")[:500]))

    # 校验产出：核心矩阵文件 .bin/.id 必须存在；.log 若存在一并列入
    bin_file, id_file, log_file = _grm_file_set(output_prefix, matrix_type)
    missing_core = [path for path in (bin_file, id_file) if not os.path.isfile(path)]
    if missing_core:
        raise hiblup_tool.AbgesError(
            "E014", "GRM 预构建核心矩阵文件缺失（exit=0 但无 %s.%s.bin/.id）：%s"
                    % (os.path.basename(output_prefix), matrix_type, missing_core))
    produced = [bin_file, id_file]
    if os.path.isfile(log_file):
        produced.append(log_file)

    return {
        "prefix": output_prefix + "." + matrix_type,  # 供 --xrm 消费（含 .TYPE 段）
        "files": produced,
        "duration": round(duration, 2),
        "exit_code": exit_code,
    }


def _cache_transaction(fn):
    @wraps(fn)
    def locked(strain_id, genotype_prefix, cache_dir, *args, **kwargs):
        with file_lock(os.path.join(cache_dir, ".cache.lock"), timeout=7200):
            return fn(strain_id, genotype_prefix, cache_dir, *args, **kwargs)
    return locked


@_cache_transaction
def get_or_build_grm(strain_id, genotype_prefix, cache_dir, matrix_type="GA",
                     threads=16, hiblup_exe=None, timeout=3600,
                     pedigree_path=None, alpha=0.05, force_rebuild=False,
                     correct_z_chromosome=False, corrected_genotype_prefix=None, executor=None):
    """
    获取或构建缓存 GRM。同品系同矩阵类型复用，基因型变化时自动重建。

    参数：
      strain_id: 品系/数据集标识（用于缓存键和命名，如 "634D"、"N532"）
      genotype_prefix: PLINK 基因型前缀（质控后 .bed/.bim/.fam）
      cache_dir: 缓存目录
      matrix_type: GA/GD/GAD/HA/HD/HAD
      force_rebuild: True 时忽略缓存强制重建
      correct_z_chromosome: 【2026-10-13 已移除，保留仅为向后兼容】
        必须为 False；为 True 时抛 E014 明确报错（不静默忽略）
      corrected_genotype_prefix: 【已废弃】原剂量补偿后的 PLINK 前缀，不再使用

    【2026-10-13 移除】Z 染色体剂量补偿已删除，G 矩阵一律由 HIBLUP 从
    质控后 .bed 直接构建，交付前不做任何编码改写。

    返回：
      {"prefix": 供 --xrm 消费的前缀(含 .TYPE 段), "cached": bool,
       "duration": float, "matrix_type": str, "fingerprint": dict}
    """
    # 【2026-10-13】Z 剂量补偿已移除：G 矩阵一律由 HIBLUP 从质控后 .bed 构建。
    # 检查前置到函数入口，确保调用方拿到的是"Z 补偿已移除"的准确语义，
    # 而非被后续指纹/文件校验先拦下产生误导性报错。
    if correct_z_chromosome:
        raise hiblup_tool.AbgesError(
            "E014",
            "Z 染色体剂量补偿已于 2026-10-13 移除，G 矩阵直接由 HIBLUP 构建；"
            "请将 correct_z_chromosome 置为 False。依据：HIBLUP 内部按位点实际均值"
            "重新中心化，交付前改写 .bed 会被完全抵消（实测 base 与 caseA 的 "
            "G 矩阵、h²、EBV 逐位相同）。详见 "
            "docs/Z染色体剂量补偿_AB方案实测对比_v1.0.md")

    identifier(strain_id)
    os.makedirs(cache_dir, exist_ok=True)
    key = _cache_key(strain_id, matrix_type)

    fingerprint = _genotype_fingerprint(genotype_prefix)
    if fingerprint is None:
        raise hiblup_tool.AbgesError(
            "E014", "基因型文件不完整，无法计算指纹：%s" % genotype_prefix)

    binary = hiblup_tool.resolve_hiblup_binary(hiblup_exe)
    if executor is None:
        raise hiblup_tool.AbgesError("E032", "GRM cache requires the Harness ToolExecutor")
    executor.assert_engine_integrity(binary, label="GRM")
    signature = {"schema": 2, "engine_sha256": sha256(binary), "alpha": alpha,
                 "pedigree_sha256": sha256(pedigree_path) if pedigree_path else None}
    manifest = _load_manifest(cache_dir)
    entry = manifest.get(key)
    # Each build owns immutable files: an active consumer may still use a prior build.
    output_prefix = (entry or {}).get("output_prefix", "")

    # 缓存命中条件：非强制重建 + entry 存在 + 指纹一致 + 矩阵类型一致 + 产出文件存在
    # 【2026-10-13 修订】移除旧的 z_correction 比对（恒 False 死逻辑）；
    # 产出文件判定改用 _grm_file_set（含 .TYPE 段），与实际产出对齐。
    if (not force_rebuild and entry
            and _fingerprint_equal(entry.get("fingerprint"), fingerprint)
            and entry.get("matrix_type") == matrix_type
            and entry.get("signature") == signature
            and all(os.path.isfile(path) and sha256(path) == digest for path, digest in entry.get("outputs", {}).items())
            and bool(entry.get("outputs"))
            and all(os.path.isfile(path)
                    for path in _grm_file_set(output_prefix, matrix_type))):
        logger.info("O2 GRM 缓存命中：strain=%s, type=%s, built_at=%s",
                     strain_id, matrix_type, entry.get("built_at"))
        return {
            "prefix": output_prefix + "." + matrix_type,  # 供 --xrm 消费（含 .TYPE 段）
            "cached": True,
            "duration": 0.0,
            "matrix_type": matrix_type,
            "fingerprint": fingerprint,
        }

    # 【2026-10-13】Z 剂量补偿已移除（函数入口已做 correct_z_chromosome 校验），
    # 此处直接使用原始质控后基因型前缀，不做任何编码改写。
    effective_genotype_prefix = genotype_prefix

    # 未命中 → 构建
    output_prefix = os.path.join(cache_dir, "%s_%s_%s" % (strain_id, matrix_type, uuid.uuid4().hex))
    logger.info("O2 GRM 缓存未命中，开始构建：strain=%s, type=%s",
                 strain_id, matrix_type)
    result = build_grm(
        effective_genotype_prefix, output_prefix, matrix_type=matrix_type,
        threads=threads, hiblup_exe=hiblup_exe, timeout=timeout,
        pedigree_path=pedigree_path, alpha=alpha,
        correct_z_chromosome=correct_z_chromosome,
        corrected_genotype_prefix=corrected_genotype_prefix, executor=executor)

    # 更新 manifest（【2026-10-13】清除 Z 补偿遗留死字段）
    manifest[key] = {
        "strain_id": strain_id,
        "matrix_type": matrix_type,
        "genotype_prefix": genotype_prefix,
        "output_prefix": output_prefix,
        "fingerprint": fingerprint,
        "signature": signature,
        "outputs": {path: sha256(path) for path in result["files"]},
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "duration": result["duration"],
    }
    _save_manifest(cache_dir, manifest)

    return {
        "prefix": output_prefix + "." + matrix_type,  # 供 --xrm 消费（含 .TYPE 段）
        "cached": False,
        "duration": result["duration"],
        "matrix_type": matrix_type,
        "fingerprint": fingerprint,
    }


def invalidate_grm_cache(strain_id, cache_dir, matrix_type=None):
    """
    失效指定品系的 GRM 缓存。matrix_type=None 时失效该品系所有类型。

    返回被删除的缓存条目数量。
    """
    manifest = _load_manifest(cache_dir)
    removed = 0
    keys_to_delete = []
    for key, entry in manifest.items():
        if entry.get("strain_id") != strain_id:
            continue
        if matrix_type and entry.get("matrix_type") != matrix_type:
            continue
        # 删除产出文件（【2026-10-13 修订】用 _grm_file_set 对齐真实后缀）
        prefix = entry.get("output_prefix", "")
        mtype = entry.get("matrix_type", "GA")
        for path in _grm_file_set(prefix, mtype):
            if os.path.isfile(path):
                try:
                    os.remove(path)
                except OSError as exc:
                    logger.warning("删除缓存文件失败 %s：%s", path, exc)
        keys_to_delete.append(key)
        removed += 1
    for key in keys_to_delete:
        del manifest[key]
    if removed:
        _save_manifest(cache_dir, manifest)
    logger.info("O2 GRM 缓存失效：strain=%s, type=%s, removed=%d",
                 strain_id, matrix_type or "ALL", removed)
    return removed


def list_cached_grms(cache_dir):
    """列出所有缓存 GRM 条目。"""
    manifest = _load_manifest(cache_dir)
    return [
        {
            "strain_id": v.get("strain_id"),
            "matrix_type": v.get("matrix_type"),
            "output_prefix": v.get("output_prefix"),
            "built_at": v.get("built_at"),
            "duration": v.get("duration"),
            "files_exist": all(
                os.path.isfile(path)
                for path in _grm_file_set(v.get("output_prefix", ""),
                                         v.get("matrix_type", "GA"))),
        }
        for v in manifest.values()
    ]
