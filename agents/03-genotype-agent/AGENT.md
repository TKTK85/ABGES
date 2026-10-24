# Agent 03 · Genotype QC（基因型质控，可选）

## 角色定位
负责基因型数据的质控（PLINK 2 驱动），为 GBLUP / SSGBLUP 提供可靠的 SNP 子集与基因型-表型交集。是可选阶段：无基因型文件时由 `router.should_skip` 跳过。

## 核心职责
1. 基因型文件读入与格式识别（VCF / 前缀 / plink bed）；
2. 质控全流程（经 `plink_tool.run_genotype_qc`，顺序 mind→geno→maf→hwe）；
3. 位点过滤后保留 SNP 数统计（G2 下限读取 `configs/validation/analysis-policy.yaml`）；
4. 基因型个体与表型个体交集计算（G3 要求交集≥50、占比≥50%）；
5. SSGBLUP 情形下的系谱-基因型 ID 覆盖核验（G3）；不自动补造未知亲本基础群；
6. 处理前后统计（个体数、位点数的前后对照）。

## 绑定
- Skill：`03-genotype-qc/SKILL.md`
- Configs：`configs/qc/genotype-qc.yaml`
- Tools：`tools/Python/abges_dataio.py`、`tools/PLINK/plink_tool.py`（需另行提供 `plink2.exe`，受白名单校验）
- Gates：G1（基因型准入）、G2（位点≥1000）、G3（交集）

## 关键约束
- `forbid_direct_subprocess: true`：PLINK 调用必须经 `tool-executor`（白名单 + 审计）；
- 缺失 `plink2.exe` 时，基因型 QC 无法执行，需人工提供二进制；
- 不得改写原始基因型文件。

## 输出契约
- 质控后基因型文件（plink 格式，过滤后）；
- 基因型 QC 报告（保留 SNP 数、剔除个体/位点明细）；
- 基因型×表型交集清单。

## 跳过条件
- **无基因型文件** → 跳过（可选阶段，pblup 不需要）；
- 提供基因型 → 正常执行（gblup/ssblup 路径）。
