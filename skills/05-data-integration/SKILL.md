# SKILL 05 · Data Integration

多源数据整合技能。负责把表型、系谱、基因型三套数据按个体 ID 对齐，确定**最终进入分析的群体**，并说清楚每一个被排除的个体是因为什么被排除的。

绝大多数遗传评估失败并非模型问题，而是三套数据的 ID 对不上。本技能是最容易出问题、也最需要把话讲明白的一环。

---

## 一、元信息

| 项目 | 内容 |
|------|------|
| skill_id | `05-data-integration` |
| 版本 | 1.0.0 |
| 归属 Agent | `05-data-integration-agent` |
| 依赖配置 | `configs/gates/gate-rules.yaml`、`configs/models/model-registry.yaml` |
| 实现工具 | `tools/Python/abges_integration.py` |
| 对接闸门 | **G1（准入）、G3（对接）** —— 本技能是这两道闸门的责任方 |
| 输出 Schema | `schemas/task-schema.yaml` |

---

## 二、职责边界

### 本技能负责

```text
Phenotype ↔ Pedigree
Phenotype ↔ Genotype
Pedigree  ↔ Genotype
```

- ID 一致性与重复检查；
- 表型个体是否在系谱中；
- 基因型个体是否在系谱中；
- 性别一致性；
- 世代一致性；
- 最终分析群体确定；
- **ID 不匹配的原因诊断**。

### 本技能不负责

- 不做各源内部的质控（属 Skill 01/02/03）；
- 不修改任一数据源的内容——只做对齐与筛选；
- 不选择模型（属 Skill 06）。

---

## 三、输入契约

```yaml
input:
  phenotype_frame: DataFrame     # 已完成 Skill 01
  pedigree_frame: DataFrame|null # 已完成 Skill 02
  genotype_ids: [str]|null       # 已完成 Skill 03
  id_column: str                 # 默认 "ID"
  model: "pblup"|"gblup"|"ssgblup"
  restrict_phenotype_to_pedigree: bool   # 默认 false
```

### 模型对数据源的硬性要求

| 模型 | 表型 | 系谱 | 基因型 |
|------|------|------|--------|
| PBLUP | 必需 | 必需 | 不需要 |
| GBLUP | 必需 | 可选 | 必需 |
| SSGBLUP | 必需 | 必需 | 必需 |

缺少必需源 → `G1-05` 阻断，抛 `E050`，明确告知"无系谱且无基因型，无法构建亲缘关系矩阵"。

---

## 四、执行步骤

```text
记录各源整合前样本量
  ↓
Phenotype × Pedigree 求交集       → G3-01（阈值见 validation/analysis-policy.yaml）
  ↓
计算覆盖率 = 交集 / 表型记录数     → G3-02（阈值见 validation/analysis-policy.yaml）
  ↓
Phenotype × Genotype 求交集       → G3-03 (≥50，仅 G/SSG)
  ↓
Genotype ⊆ Pedigree 检查          → G3-04 (SSGBLUP)
  ↓
性别一致性 / 世代一致性交叉校验
  ↓
确定最终分析群体
  ↓
输出未匹配清单与原因
```

### 交集为 0 时的强制诊断

这是本技能最重要的功能。交集为 0 时**不能只报"交集为 0"**，必须自动诊断并给出可执行的修复建议：

| 诊断项 | 检测方法 | 典型提示 |
|--------|---------|---------|
| 前导零 | 去掉前导零后重新求交集 | "去掉前导零后可匹配 48 个，建议统一 ID 前导零处理" |
| 大小写 | 统一转小写后求交集 | "统一大小写后可匹配，建议规范 ID 大小写" |
| 前缀 | 检测一侧是否统一带品系前缀 | "系谱 ID 带 `SAMPLE_POPULATION_` 前缀而表型不带" |
| 空白字符 | 去首尾空白后求交集 | "存在首尾空白字符" |
| Excel 浮点 | 去 `.0` 尾巴后求交集 | "Excel 把 ID 读成了浮点数" |

诊断结果连同两侧各 10 个样例 ID 一并输出。

---

## 五、闸门判定事实

本技能须向 Gatekeeper 提供以下事实（`build_gate_g3_facts`）：

```yaml
G3-01:
  actual: 交集个体数
  threshold: 100
  on_fail: block
G3-02:
  actual: 覆盖率
  threshold: 0.80
  on_fail: pass_with_warning
  message: 两成以上表型个体不在系谱中，这部分个体不会获得可靠 EBV
G3-03:
  actual: 表型×基因型交集
  threshold: 50
  on_fail: block
G3-04:
  actual: 基因型个体在系谱内的比例
  on_fail: block
  action: 不自动补造系谱个体；先核实 ID 并补齐真实亲子关系后重跑
```

G3-04 发现基因型个体缺少系谱记录时阻断 SSGBLUP；系统不得把该个体伪作双亲未知的基础群。

---

## 六、处理前后统计（强制）

```text
Phenotype N
Pedigree N
Genotype N
      ↓
匹配数量（三种两两组合）
      ↓
未匹配数量及原因分布
      ↓
最终分析群体规模
```

未匹配个体清单必须落盘，不能只给数字。

---

## 七、输出契约

```yaml
outputs:
  analysis_population: "<output_dir>/<task>_population_<timestamp>.csv"
    # 最终进入分析的个体清单，含来源标记（有表型/有系谱/有基因型）
  unmatched:   "<prefix>_unmatched.csv"       # 个体ID, 缺失于哪一源, 推测原因
  integration_report: "<prefix>_integration.json"
  statistics:  "<prefix>_statistics.json"
  metadata:    "<prefix>_metadata.json"

status: completed | partial | failed
```

---

## 八、错误码

| 码 | 含义 | 处置 |
|----|------|------|
| E050 | 模型所需数据源缺失 | 中止（G1-05） |
| E051 | 表型与系谱交集为 0 | 中止，**必须附诊断结果** |
| E052 | 交集数量低于阈值 | 中止（G3-01） |
| E053 | 表型与基因型交集不足 | 中止（G3-03） |

---

## 九、人工确认触发点

- 覆盖率在 0.50–0.80 之间 → 确认是否接受这部分个体无可靠 EBV；
- SSGBLUP 的基因型个体有任一未出现在系谱中 → 阻断，核实身份并补齐真实系谱；
- 诊断出 ID 体系差异但自动修复可能有歧义 → 确认修复方案。

---

## 十、禁止事项

1. 禁止在交集为 0 时只报数字不做诊断；
2. 禁止自动"修复"ID（如自动补前导零）——只诊断并建议，实际修改需人工确认；
3. 禁止不落盘未匹配清单；
4. 禁止在最终群体规模不足时继续往下走；
5. 禁止修改任一数据源的原始内容。
