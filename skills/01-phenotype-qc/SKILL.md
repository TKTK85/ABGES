# SKILL 01 · Phenotype QC

表型数据质量控制技能。负责把任意来源的原始表型表，变成一份**可被 HIBLUP 直接读取、且每一处改动都可追溯**的分析用表型文件。

---

## 一、元信息

| 项目 | 内容 |
|------|------|
| skill_id | `01-phenotype-qc` |
| 版本 | 1.0.0 |
| 归属 Agent | `01-phenotype-agent` |
| 依赖配置 | `configs/qc/phenotype-qc.yaml`、`configs/traits/trait-registry.yaml` |
| 实现工具 | `tools/Python/abges_phenotype.py`、`tools/Python/abges_dataio.py`、`tools/Python/abges_ascii.py` |
| 对接闸门 | G1（准入）、G2（质控）、G4-04/G4-06（ASCII 与标准化 PH007） |
| 输出 Schema | `schemas/phenotype-schema.yaml` |

---

## 二、职责边界

### 本技能负责

- 表头识别与标准化（中文表头 → 标准英文字段）；
- 个体 ID 归一（去空白、去 Excel 浮点尾巴 `.0`、缺失记号统一）；
- 目标性状存在性与数值化；
- 生物学合理范围检查；
- 重复记录识别；
- 分组异常值剔除；
- 固定效应候选与有效水平检查；
- 协变量检查；
- 处理前后描述性统计与对照；
- 变更明细（change log）与 QC 结论。

### 本技能不负责

- 不计算衍生性状（属 Skill 04）；
- 不做表型与系谱/基因型的对接（属 Skill 05）；
- 不选择模型、不生成 HIBLUP 命令（属 Skill 06/07）；
- 不修改 `workspace/input/raw/` 下的任何原始文件。

---

## 三、输入契约

```yaml
input:
  phenotype_path: str        # 原始表型文件（.xlsx/.xls/.csv/.txt），只读
  sheet: int|str             # Excel 工作表，默认 0
  traits: [str]              # 目标性状 ID，须存在于 trait-registry
  fixed_effects: [str]|null  # 缺省取 phenotype-qc.yaml 的 fixed_effect_cols
  covariates: [str]|null     # 缺省取配置
  mode: "single"|"multi"     # 单性状 / 多性状
  output_dir: str            # 输出目录，须位于 workspace/ 之下
```

**前置校验（不满足直接判 failed，不做猜测）**

1. 文件存在且可读 → 否则 `E002`；
2. 文件可解析出表头与至少 1 行数据 → 否则 `E011`；
3. `traits` 非空且全部在 `trait-registry.yaml` 中登记 → 否则 `E013`。

---

## 四、执行步骤

严格按下列顺序执行。顺序本身是规则的一部分——先做存在性检查再做取值处理，避免在残缺结构上做无意义的计算。

```text
读取原始表 (只读)
  ↓
Before Statistics ────────────── 强制，缺失即 G2-04 阻断
  ↓
PH001 ID 列存在性              BLOCK
  ↓
PH001 性状列存在性              BLOCK
  ↓
PH007 标准化校验              BLOCK
  ↓
PH002 ID 缺失排除               EXCLUDE
  ↓
PH002 ID 重复处理               FLAG / DEDUPLICATE
  ↓
PH003 性状与协变量数值化         WARNING（仅提醒，不私自置 NA）
  ↓
PH006 分组异常值剔除             EXCLUDE(置缺失，单轮)
  ↓
PH004 缺失值保持                 KEEP，禁止填补
  ↓
PH005 固定效应有效水平检查        WARNING(E022)
  ↓
PH007 标准化执行(默认关闭)        RECODE
  ↓
After Statistics ─────────────── 强制
  ↓
Before vs After 对照
  ↓
Gate G2
```

### 各规则要点

| 规则 | 动作 | 关键约定 |
|------|------|----------|
| PH001 | BLOCK | 标准化后必须存在 ID 列，缺失则无法与系谱关联，直接中止 |
| PH002 | EXCLUDE | ID 为空 / NA / 纯空白的记录整条排除 |
| PH002 | FLAG | 默认只标记不删；`duplicate_policy=deduplicate` 时按 `keep=first/last` 去重并记录 |
| PH001 | BLOCK | 目标性状列必须存在，不允许用近似列名替代 |
| PH003 | WARNING | 无法转数值时**仅提醒，禁止任何模块私自置 NA**；原值必须保留并写入 change log；触发即阻断流程，待人工清洗 |
| PH004 | KEEP | 缺失保持 NA。**禁止任何形式的填补**（均值、前向、插值一律不允许） |
| PH006 | EXCLUDE | 组内 `mean ± 3SD` 剔除，组样本 < 5 不执行；**只做一轮**，禁止迭代式剔除 |
| PH005 | WARNING | 有效水平 < 2 的固定效应不可估，命令层省略；必须在交付说明中显式声明 |
| PH007 | BLOCK | single 恰好 1 性状且不标准化；multi 至少 2 性状且同样不标准化 |

### 两条容易踩的硬约束

1. **标准化（PH007）**
   单/多性状分析均不做标准化，各性状保持原始量纲，方差组分与 EBV 同原始尺度结果可比；h² 与遗传相关为尺度无关量，不受影响。（2026-10-12 起按先生指令修订，原"多性状强制 z-score"约定废止；量纲差异极大的性状组合若收敛困难，经 human-in-the-loop 可恢复强制标准化。）

2. **ASCII 强制（E023，对接 G4-04）**
   HIBLUP v1.7.0 遇到非 ASCII 的列名或固定效应水平值会崩溃。本技能在输出引擎表前必须完成转码，转码映射表须随输出一并保存，供报告阶段把结果还原成中文标签。

---

## 五、处理前后统计（强制）

连续性状**逐个**统计以下 17 项，处理前后各一份：

```text
N  ValidN  Missing  MissingPct
Mean  SD  Variance  CV
Min  P1  P5  P25  Median  P75  P95  P99  Max
```

另按分组字段（`Gen`、`Batch`、`Sex`、`Line` 中实际存在者）给出每组 N 与均值，用于识别组间不平衡。

统计缺失或 before/after 任一为空 → **G2-04 阻断**。这是可追溯铁律，不接受"数据没变所以不用统计"的理由。

---

## 六、变更记录

每一次 EXCLUDE / RECODE / DEDUPLICATE / FLAG 都必须落一条记录：

```yaml
- task_id: str
  record_id: str        # 个体 ID
  field: str            # 涉及字段
  before: any
  after: any
  action: KEEP|FLAG|EXCLUDE|MODIFY|RECODE|DEDUPLICATE|DERIVE
  reason: str
  rule_id: str          # PH001–PH007
  agent: 01-phenotype-agent
  timestamp: ISO8601
```

---

## 七、闸门对接

| 闸门 | 本技能须提供的事实 |
|------|-------------------|
| G1-01/02/03 | 文件可解析、行列数、ID 列已识别且重复已处理 |
| G1-04 | 编码检测置信度 |
| G2-01 | 是否存在未处理的 error 级规则 |
| G2-02 | 质控后有效记录数（阈值 100） |
| G2-03 | 剔除比例（阈值 0.30，超出告警） |
| G2-04 | before/after 统计完整性 |
| G4-03 | 各固定效应有效水平数 |
| G4-04 | 列名与水平值是否全 ASCII |
| G4-06 | 标准化是否符合 mode 约定（PH007） |

---

## 八、输出契约

```yaml
outputs:
  engine_phenotype: "<output_dir>/<task>_phenotype_<timestamp>.txt"
    # 制表符分隔；列序固定为 ID → 固定效应 → 协变量 → 性状
    # ID 必须在第 1 列；缺失统一写 NA
  statistics: "<prefix>_statistics.json"      # before / after / delta
  change_log: "<prefix>_change_log.jsonl"
  qc_report:  "<prefix>_phenotype_qc.json"
  metadata:   "<prefix>_metadata.json"        # 含输入校验和、规则版本、耗时

status: completed | partial | failed
```

`status` 判定：出现 BLOCK → `failed`；出现 WARNING 或人工确认待办 → `partial`；全部通过 → `completed`。**不允许把 partial 报告成 completed。**

---

## 九、错误码

| 码 | 含义 | 处置 |
|----|------|------|
| E002 | 文件不存在或不可读 | 中止，报出路径 |
| E011 | 表型文件无法解析 | 中止，报出解析器与编码探测结果 |
| E013 | 目标性状不存在 | 中止，列出可用列名，**不猜测近似列** |
| E022 | 固定效应单水平 | 告警并剔除该效应，写入交付说明 |
| E023 | 存在非 ASCII 字符 | 转码后继续；转码失败则中止 |

---

## 十、人工确认触发点

- PH002 检出重复 ID 且策略为 `deduplicate` → 确认保留 first 还是 last；
- PH006 组内离群剔除比例异常（如某批次被大量置缺失）→ 确认是否为单位错误（如体重记成克而非千克）或分组口径问题。

确认请求须同时给出：触发规则、涉及记录数、示例记录（≤10 条）、可选项及各自后果。

---

## 十一、禁止事项

1. 禁止修改或覆盖 `workspace/input/raw/` 下的原始文件；
2. 禁止任何形式的缺失值填补；
3. 禁止迭代式异常值剔除；
4. 禁止在列名匹配不上时"猜"一个相近的列；
5. 禁止跳过 before/after 统计；
6. 禁止把有告警的结果报告为 `completed`。
