# SKILL 06 · Geneval Planning

遗传评估方案规划技能。负责回答一个问题：

> **应该怎么算。**

本技能只出方案，**不运行任何软件**。方案经必要的人工确认后，交由 Skill 07 执行。

---

## 一、元信息

| 项目 | 内容 |
|------|------|
| skill_id | `06-geneval-planning` |
| 版本 | 1.0.0 |
| 归属 Agent | `06-geneval-planner-agent` |
| 依赖配置 | `configs/models/model-registry.yaml`、`configs/models/{pblup,gblup,ssblup}.yaml`、`configs/traits/trait-registry.yaml` |
| 实现工具 | `tools/Python/abges_core.py`（配置加载与校验） |
| 对接闸门 | **G4（建模准入）** —— 本技能是该闸门的责任方 |
| 输出 Schema | `schemas/task-schema.yaml` |

---

## 二、职责边界

### 本技能负责

- 判断使用 PBLUP / GBLUP / SSGBLUP；
- 确定目标性状；
- 确定固定效应；
- 确定协变量；
- 确定随机效应（含永久环境效应）；
- 组装模型结构表达式；
- 确定分析群体纳入规则；
- 标记必要的人工确认点。

### 本技能不负责

- 不调用 HIBLUP，不生成最终命令行（属 Skill 07）；
- 不修改数据；
- 不硬编码模型选择逻辑——一切依 `model-registry.yaml` 决策。

---

## 三、模型选择

严格按 `model-registry.yaml` 的 `selection_rules` **顺序匹配，命中即停**：

| 规则 | 条件 | 选择 | 理由 |
|------|------|------|------|
| MS001 | 表型+系谱+基因型，已测型占比 < 95% | `ssblup` | 单步法可同时利用测型与未测型个体 |
| MS002 | 表型+系谱+基因型，已测型占比 ≥ 95% | `gblup` | 绝大多数已测型，直接用 G 矩阵 |
| MS003 | 表型+基因型，无系谱 | `gblup` | 无系谱可用 |
| MS004 | 表型+系谱，无基因型 | `pblup` | 经典路线 |
| MS005 | 仅表型 | — | **判定 failed**，告知缺少的数据类型 |

### 用户显式指定模型时

允许指定，但若数据不满足该模型的 `requires`：

```text
拒绝执行 → 说明缺什么 → 给出本表推荐的可行模型
```

**禁止静默改选**（`forbid_silent_switch: true`）。用户要 GBLUP 而没有基因型，就明说做不了，不能悄悄换成 PBLUP 然后交一份看起来正常的结果。

---

## 四、模型结构

```text
y = mu + 固定效应 + 协变量 + 加性遗传效应 + [永久环境效应] + e
```

PBLUP 模板：

```text
{trait} = 1 + {fixed_effects} + {covariates} + PA(R[A]) + e
```

### 固定效应确定

1. 候选来自 `configs/qc/phenotype-qc.yaml#fixed_effect_cols`（默认 `Gen`、`Batch`、`Sex`）；
2. 剔除有效水平数 < 2 的列（E022）——**这是 G4-03 的硬性要求**；
3. 剔除后若无任何固定效应，模型仅含截距，须告警。

### 协变量确定

来自配置的 `covariate_cols`，必须为连续数值型。离散变量误当协变量会得到无意义的线性斜率。

### 永久环境效应

触发条件：同一 ID 在表型文件中出现多次（重复记录性状，如逐期产蛋）。
由 planner 自动置 `permanent_environment.enabled = true`，追加 `--rand`。

单条记录性状**不得**启用，否则永久环境效应与残差完全混淆、不可分离。

### 多性状

- `--multi-trait`，最多 5 个性状；
- 表型不标准化，各性状保持原始量纲（2026-10-12 起与单性状合同一致；如需恢复强制 z-score，走 human-in-the-loop 改 PH007 配置）；
- 超过 3 个性状时必须给出告警：计算量与不收敛风险显著上升。

---

## 五、G4 建模准入闸门（本技能责任）

出方案前必须自查全部 7 项：

| 检查 | 内容 | 失败 |
|------|------|------|
| G4-01 | 模型已从 registry 选定且数据满足 requires | block |
| G4-02 | 目标性状列已定位，列序号（1-based）与表头一致 | block |
| G4-03 | 所有固定效应水平数 ≥ 2 | block |
| G4-04 | 列名与固定效应水平值全为 ASCII | block |
| G4-05 | 目标性状方差 > 0 | block |
| G4-06 | single/multi 均未标准化 | block |
| G4-07 | 输出目录存在可写，前缀带时间戳且不冲突 | block |

### G4-04 为什么是 block 而不是告警

HIBLUP v1.7.0 遇到非 ASCII 列名或水平值会**直接崩溃**，而且崩溃信息不指向真正原因。必须在进入引擎前完成转码，并保存映射表供报告阶段还原中文标签。

### G4-02 列序号约定

HIBLUP 用**列序号（1-based）**而非列名定位性状与效应。列序号必须由程序从实际表头读取，**禁止人工推算或写死**——表型文件列序一变，写死的序号就会静默指向错误的列，而 HIBLUP 不会报错。

---

## 六、输入契约

```yaml
input:
  analysis_population: DataFrame   # Skill 05 输出
  engine_phenotype_path: str       # Skill 01 输出的引擎表
  data_availability:
    has_phenotype: bool
    has_pedigree: bool
    has_genotype: bool
    genotyped_ratio: float
  requested_traits: [str]
  requested_model: str|null        # 用户显式指定，可为空
  mode: "single"|"multi"
```

---

## 七、输出契约

```yaml
outputs:
  analysis_plan: "<output_dir>/<task>_plan_<timestamp>.json"
```

方案内容：

```yaml
model_id: pblup
model_selected_by: MS004          # 命中的选型规则
model_selection_reason: str
mode: single
traits:
  - id: ACL
    column_index: 5               # 1-based，读自实际表头
fixed_effects: [Gen, Batch]
fixed_effects_dropped:
  - column: Sex
    reason: 有效水平数 1 < 2 (E022)
covariates: []
random_effects:
  - name: PA
    type: additive_genetic
    relationship: A
permanent_environment: false
standardization: none             # single 模式
model_formula: "ACL = 1 + Gen + Batch + PA(R[A]) + e"
vc_method: AI
max_iter: 30
gate_g4:
  G4-01: pass
  ...
human_confirmations_required: [...]
status: completed | partial | failed
```

---

## 八、错误码

| 码 | 含义 | 处置 |
|----|------|------|
| E012 | 不支持的评估方法 | 中止 |
| E013 | 目标性状不存在 | 中止，列出可用列 |
| E060 | 数据不满足所选模型 requires | 中止，**不静默改选** |
| E061 | 无可用固定效应 | 告警，仅截距模型 |
| E062 | 目标性状方差为 0 | 中止（G4-05） |

---

## 九、人工确认触发点

- 用户指定模型与自动选型结果不一致 → 确认以哪个为准；
- 多性状分析超过 3 个性状 → 确认接受收敛风险；
- 固定效应被剔除后仅剩截距 → 确认模型是否仍有意义；
- 永久环境效应自动启用 → 确认该性状确为重复记录性状。

---

## 十、禁止事项

1. 禁止运行任何外部程序——本技能只出方案；
2. 禁止硬编码模型选择逻辑；
3. 禁止在数据不满足时静默改选模型；
4. 禁止人工推算或写死列序号；
5. 禁止对单条记录性状启用永久环境效应；
6. 禁止擅自对多性状启用 z-score 标准化（如需恢复，走 human-in-the-loop 修订 PH007 配置并记入 change_log）。
