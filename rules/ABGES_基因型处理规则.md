# ABGES 基因型处理规则

> 版本：v1.0 ｜ 更新：2026-08-11 ｜ 阶段：Phase 1

## 1. 输入

PLINK 二进制（`.bed/.bim/.fam`），`--bfile <prefix>` 传入。不支持 VCF/TPED，需先用 PLINK 转换。

---

## 2. 质控（QC）

**过滤型（直接删除）**：

| 步骤 | 规则 | 参数 | 默认 |
|------|------|------|------|
| s1 | 仅常染色体双等位 + 删异常 SNP | `--chr 1-{autosomal_max} --max-alleles 2 --exclude` | 鸡 1-38 + Z（39条）|
| s2 | 个体检出率 ≥95% | `--mind 0.05` | 0.05 |
| s3 | SNP 检出率 ≥95% + 删单态 | `--geno 0.05` | 0.05 |
| s4 | MAF ≥0.01 | `--maf 0.01` | 0.01 |

**标记型（只标记不删）**：HWE P<1e-6（`--hardy`）；杂合度超 ±3SD（`--het`）。输出 CSV，人工决定。

**描述型统计**：HIBLUP 生成 `.afreq`/`.gfreq`/`.homo`/`.hete`。

每步输出 `qc_removed_{step}_indiv.csv` 和 `qc_removed_{step}_snp.csv`（ID/SNP, reason, threshold）。

---

### 3.1 Z 染色体剂量补偿（性连锁效应）

鸡为 ZW 型性决定：
- 公鸡 ZZ（2 拷贝 Z）：基因型 0/1/2，标准编码
- 母鸡 ZW（1 拷贝 Z）：基因型 0/1，半合子剂量

构建含 Z 标记的统一 G 矩阵时，须先执行剂量补偿：

| 性别 | 原始编码 | 补偿后 | 转换规则 |
|------|---------|--------|---------|
| 公鸡 Z/A/A | 0 | 0 | 保持不变 |
| 公鸡 Z/A/B | 1 | 1 | 保持不变 |
| 公鸡 Z/B/B | 2 | 2 | 保持不变 |
| 母鸡 Z/A | 0 | 0 | 等价于公 Z(A/A) |
| 母鸡 Z/B | 1 | 1 | 等价于公 Z(A/B) |
| 母鸡 Z/B/B（异常） | 2 | 1 | clamp 到杂合剂量 |

> **⚠️【2026-10-13 已移除】Z 染色体剂量补偿功能已整体删除。**
> 先生指令：*"现在 A 和 B 方案都不需要，直接删掉这个，G 矩阵直接由 HIBLUP 构建"*。
> 上表编码规则仅作历史留档，**不再执行**。
>
> 移除依据：该预处理不属于当前引擎计算流程；关系矩阵由 HIBLUP 从通过质控的基因型数据构建。
>
> **当前口径**：G 矩阵完全由 HIBLUP 从质控后 `.bed` 内部构建，交付前不做任何编码改写。
> `abges_grm_cache` 对 `correct_z_chromosome=True` 抛 **E014** 明确报错（不静默忽略）。
> 实现类 `abges_z_chromosome_corrector.py` **已于 2026-10-13 物理删除**（先生拍板），
>

### 3.2 G/H 矩阵

| 方法 | 命令 | 输出 |
|------|------|------|
| GBLUP | `--make-xrm --bfile <prefix> --add --code-method 1` | `.GA.bin`+`.GA.id` |
| SSGBLUP | `--make-xrm --bfile <prefix> --pedigree <ped> --alpha 0.05 --add-inv` | `.HAinv.sparse.bin` |

SSGBLUP 三要素：① 删旧 GENOTEST 残留（`pigs_bin.*`）② hiblup_exe 用 config 绝对路径 ③ phe/ped/bfile 传绝对路径。缺一即失败。

---

## 4. ID 一致性

基因型↔表型重叠率 <0.1 → 停止。基因型个体应在系谱中存在。fam IID 编码须与表型/系谱一致（前导零、前缀）。

---

## 5. 染色体参数

家禽 `chr_set=39`（鸡 38 常染 + Z=39，共 39 条），`--allow-extra-chr`。所有 PLINK 调用须传同一组参数。

## 定稿参数（依据 docs/ABGES_待确认参数总表.md）

- 质控阈值（定稿）：**MAF ≥ 0.01；call rate ≥ 0.95（缺失 ≤ 0.05）；染色体编号 1–38 常染色体 + Z（39 条）**。默认上值，允许 config 按品系覆盖。
- HWE 与杂合度：**HWE P < 1e-6、杂合度 ±3SD 均作标记（非直接删除）**，标记后交人工结合 MAF 复核。
