# SKILL 03 · Genotype QC

基因型数据质量控制技能。负责把 PLINK/VCF 格式的原始基因型，处理成**可用于构建基因组关系矩阵（GRM）**的干净数据集。

仅 GBLUP / SSGBLUP 流程需要。PBLUP 流程中本技能被 Harness 标记为 `SKIPPED` 并继续——跳过必须显式记录，不得静默略过。

---

## 一、元信息

| 项目 | 内容 |
|------|------|
| skill_id | `03-genotype-qc` |
| 版本 | 1.0.0 |
| 归属 Agent | `03-genotype-agent` |
| 依赖配置 | `configs/qc/genotype-qc.yaml` |
| 实现工具 | `tools/PLINK/plink_tool.py` |
| 外部程序 | PLINK 2.0（经 Harness Tool Executor 调用） |
| 对接闸门 | G2-06（规模下限）、G3-03（与表型交集）、G3-04（SSGBLUP 系谱覆盖；缺失时阻断，不自动补系谱） |
| 输出 Schema | `schemas/genotype-schema.yaml` |

---

## 二、职责边界

### 本技能负责

- 基因型格式识别与统一转换为 PLINK 二进制；
- 样本 ID 标准化与可对接性检查；
- SNP ID 去重与缺失补全；
- 个体缺失率（mind）、位点缺失率（geno）、MAF、HWE 过滤；
- 单态位点与非常染色体位点处理；
- 高度相似样本检出（仅告警）；
- 性别一致性检查；
- 质控前后样本数与位点数统计、被剔除清单。

### 本技能不负责

- 不构建 GRM 数值矩阵（由 HIBLUP 完成）；
- 不做填充（imputation）；
- 不自动剔除高度相似样本——家禽全同胞群体本身高度相关，自动剔除会误伤。

---

## 三、输入契约

```yaml
input:
  genotype_input: str      # .bed 前缀 / .ped 前缀 / .vcf 路径
  phenotype_ids: [str]     # 用于 GT002 对接检查
  pedigree_ids: [str]|null # 用于 SSGBLUP 覆盖检查
  thresholds: dict|null    # 覆盖默认阈值，覆盖须记入 audit
  output_dir: str
```

### 格式识别

| 格式 | 必需文件 | PLINK 参数 | 处理 |
|------|----------|-----------|------|
| plink_binary | `.bed` `.bim` `.fam` | `--bfile` | 直接使用（推荐） |
| plink_text | `.ped` `.map` | `--file` | 先转二进制 |
| vcf | `.vcf` / `.vcf.gz` | `--vcf` | 先转二进制 |

识别不出时抛 `E030`，报出查找路径与期望文件名，**不做任何猜测性回退**。

### 染色体设置

家禽非人类物种，PLINK2 默认会拒绝其染色体编号，必须显式传：

```text
--chr-set 40 no-xy --allow-extra-chr
```

GRM 构建默认**仅用常染色体**（1–39），Z/W 性染色体排除并单独统计。

---

## 四、执行步骤

```text
格式识别 (GT001/GT001)
  ↓
Before Statistics ────────────── 强制
  ↓
GT002 ID 可对接性              error ← 交集为 0 即中止
  ↓
GT003 个体 ID 重复             error
  ↓
GT004 SNP ID 重复              保留首个，其余改名 <id>_dup<N>
  ↓
GT004 SNP ID 缺失/占位          按 chr:pos 重命名
  ↓
GT004 位置为零或缺失            标记
  ↓
GT005 非常染色体位点            从 GRM 排除，计数
  ↓
质控四步（顺序不可调换）
  mind 0.10 → geno 0.10 → maf 0.01 → hwe 1e-6
  ↓
GT005 单态位点统计
  ↓
GT006 质控后规模下限            error ← SNP≥1000 且 样本≥50
  ↓
GT007 高度相似样本              仅列清单，不自动剔除
  ↓
GT008 性别一致性                仅列清单，不阻断
  ↓
After Statistics + 对照
  ↓
Gate G2-06 / G3-03
```

### 为什么质控顺序不可调换

**先剔个体（mind），后剔位点（geno）**。若反过来，少数高缺失个体会把大量本来合格的位点拉到缺失率阈值之上，造成位点被过度剔除。这是基因型质控的通行做法，本流程强制固定该顺序。

---

## 五、阈值

| 参数 | 默认值 | 含义 |
|------|--------|------|
| `mind` | 0.10 | 个体缺失率上限 |
| `geno` | 0.10 | 位点缺失率上限 |
| `maf` | 0.01 | 最小等位基因频率下限 |
| `hwe` | 1e-6 | HWE 检验 P 值下限 |
| `king_cutoff` | 0.354 | 重复样本判定（仅告警） |
| `min_snp_after_qc` | 1000 | 质控后最少位点 |
| `min_sample_after_qc` | 50 | 质控后最少个体 |

阈值可被任务级参数覆盖，但**覆盖必须写入 audit 并附理由**（`AuditTrail.record_override`，理由为空直接抛 `E008`）。

---

## 六、ID 主键约定

ABGES 统一以 `.fam` 文件的**第二列 IID** 作为个体主键，FID 不参与匹配。

GT002 交集为 0 时的常见原因，须在错误信息中一并给出：

1. FID / IID 顺序装反；
2. 前导零在 Excel 中被吞掉（`00123` → `123`）；
3. 品系前缀有无不一致（`SAMPLE_POPULATION_xxx` vs `xxx`）；
4. 大小写不一致。

错误信息须输出两侧各 10 个样例 ID 供人工比对，而不是只说"交集为 0"。

---

## 七、处理前后统计（强制）

**处理前**

```text
n_sample_raw  n_snp_raw  n_chromosome
call_rate_overall  format_detected
```

**处理后**

```text
n_sample_kept  n_snp_kept
n_sample_removed_by_mind
n_snp_removed_by_geno
n_snp_removed_by_maf
n_snp_removed_by_hwe
n_snp_monomorphic
n_snp_non_autosome_excluded
maf_mean  maf_min  call_rate_after_qc
id_overlap_with_phenotype
id_overlap_with_pedigree
```

---

## 八、输出契约

```yaml
outputs:
  qc_genotype: "<output_dir>/<task>_geno_qc_<timestamp>.{bed,bim,fam}"
  qc_report:        "<prefix>_qc_report.json"
  removed_samples:  "<prefix>_removed_samples.txt"
  removed_snps:     "<prefix>_removed_snps.txt"
  plink_log:        "<prefix>_plink.log"      # PLINK 原始日志，完整保留
  statistics:       "<prefix>_statistics.json"
  metadata:         "<prefix>_metadata.json"

status: completed | partial | failed
```

PLINK 原始日志必须完整保留。日志是外部程序真实行为的唯一证据，不允许只保留解析后的摘要。

---

## 九、错误码

| 码 | 含义 | 处置 |
|----|------|------|
| E030 | 无法识别基因型格式 | 中止，报出查找路径与期望文件名 |
| E031 | 三件套缺文件 | 中止，报出缺失的具体文件 |
| E032 | 与表型 ID 交集为 0 | 中止，输出两侧样例 ID 与四类常见原因 |
| E033 | 质控后规模不足 | 判定 failed，明确告知阈值与实际值，**不得继续构建 GRM** |
| E034 | PLINK 非零退出 | 中止，附完整 stderr 与命令行 |

---

## 十、人工确认触发点

- GT007 检出高度相似样本对 → 确认是真重复样本还是全同胞；
- GT008 性别冲突 → 确认是否影响后续分析；
- 质控剔除位点比例 > 30% → 确认阈值是否过严；
- 任何阈值覆盖请求 → 确认理由并记录。

---

## 十一、禁止事项

1. 禁止做基因型填充（imputation）；
2. 禁止自动剔除高度相似样本；
3. 禁止调换 mind / geno 的执行顺序；
4. 禁止在规模不足时继续构建 GRM；
5. 禁止丢弃 PLINK 原始日志；
6. 禁止在格式识别失败时回退到"猜一个格式试试"。
