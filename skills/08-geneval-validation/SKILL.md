# SKILL 08 · Geneval Validation

结果验证技能。负责在结果进入正式报告之前，判断它**到底能不能用**。

> 只有通过验证的结果才能进入正式报告。

本技能是诚实交付铁律的执行者。它的价值不在于让结果看起来好，而在于**在结果不可靠时能明确说出来**。

---

## 一、元信息

| 项目 | 内容 |
|------|------|
| skill_id | `08-geneval-validation` |
| 版本 | 1.0.0 |
| 归属 Agent | `08-result-validator-agent` |
| 依赖配置 | `configs/gates/gate-rules.yaml`、`configs/traits/trait-registry.yaml` |
| 实现工具 | `tools/Python/abges_validate.py` |
| 对接闸门 | **G5（结果验收）** —— 本技能是该闸门的责任方 |
| 输出 Schema | `schemas/result-schema.yaml` |

---

## 二、八项检查（G5-01 ~ G5-08）

| 检查 | 内容 | 失败判定 |
|------|------|---------|
| G5-01 | 引擎退出码为 0 | **block** |
| G5-02 | `.vars` `.rand` `.beta` `.log` 四件齐全且非空 | **block** |
| G5-03 | 算法已收敛 | pass_with_warning → 判 `partial`，报告须标注未收敛与迭代次数 |
| G5-04 | 遗传力落在 [0, 1] | **block** |
| G5-05 | 残差方差 > 0 | **block** |
| G5-06 | SE / h² 不超过项目策略配置阈值 | pass_with_warning → 估计不稳定 |
| G5-07 | EBV 个体 ID 集合与预期预测集合完全一致且无重复 | block |
| G5-08 | 遗传力落在该性状文献参考区间内 | pass_with_warning → 报告须提示核查 |

### 逐项要点

**G5-01 退出码**
退出码为 0 只说明进程正常结束，**不代表结果正确**。它是必要条件，不是充分条件。后面七项一个都不能省。

**G5-02 输出齐全**
四件套缺任一都判 block。特别注意 `_ad` 后缀问题：走 `--bfile` 的分析输出带 `_ad`，走 `--xrm`/`--pedigree` 的不带。检查时必须两种都探测。

**G5-03 收敛**
从 `.log` 的 `[Converged?]` 标记解析。三态：

| 解析结果 | 含义 | 处置 |
|---------|------|------|
| `True` | 已收敛 | pass |
| `False` | 未收敛 | warning，判 `partial` |
| `None` | 日志无该标记 | warning，**如实标注"收敛状态未知"**，不假定已收敛 |

**绝不允许用 `"converged" in log` 子串判断**——`[Converged?] No!` 同样含该子串。

**G5-04 遗传力范围**
h² 必须在 [0, 1]。超出范围说明方差组分估计出了问题（常见于样本量过小或模型过参数化）。这是数学有效性检查，无条件 block。

同时确认：**取的是非残差组分行的 h2**。残差行 `Item = e` 的 h2 是残差占比，若误取会得到 `1 - h²`，数值同样落在 [0,1] 内，**本检查抓不出来**。因此解析阶段的正确性至关重要。

**G5-05 残差方差**
必须 > 0。残差方差为 0 或负数意味着估计失败。

**G5-06 SE/h² 比值**
比值超过 `configs/validation/analysis-policy.yaml` 中的 `se_to_h2_warning` 说明标准误相对估计值过大，估计不稳定。典型原因：有效样本量不足、家系结构过于简单、固定效应过度参数化。不阻断，但必须在报告中显著提示。

**G5-07 EBV 个体 ID 集合**

逐个比对预期预测个体与 `.rand` 输出 ID，不能只比较行数。PBLUP/SSGBLUP 预期集合取 QC 后系谱个体；GBLUP 取 QC 后基因型个体。缺失、额外、空白或重复 ID 均阻断交付，并在报告中给出数量和样例。
`.rand` 文件行数应与分析群体（含系谱中的所有个体）一致。不一致往往意味着系谱与表型对接有遗漏。

**G5-08 文献区间**
`LITERATURE_H2_RANGES` 收录了 12 个常见家禽性状的文献遗传力区间。估计值落在区间外不代表错误——可能是本群体真实特点——但必须提示先生核查。

---

## 三、附加检查

除八项闸门外，还须检查：

- **NaN / Inf**：方差组分、遗传力、EBV 中不得出现；
- **ID 完整性**：`.rand` 的 ID 列无缺失、无重复；
- **结果与分析群体一致性**：EBV 的 ID 集合应为分析群体的子集或全集；
- **EBV 分布合理性**：均值应接近 0（加性遗传效应的期望为 0），标准差应与 `sqrt(Va)` 同量级。均值显著偏离 0 需提示。

---

## 四、状态判定规则

```text
存在任一 block        → failed
存在任一 warning      → partial
全部 pass             → completed
```

**这条规则不可协商。** 存在告警却报 `completed` 直接违反 G6-05，是诚实交付铁律中永不可放宽的三项之一（`never_overridable: [G6-04, G6-05, G6-06]`）。

---

## 五、回归基线比对

已跑通的历史结果作为回归锚点。当前基线（SAMPLE_POPULATION 品系 ACL 性状 PBLUP）：

```yaml
baseline_id: SAMPLE_POPULATION_ACL_PBLUP
model: "ACL = 1 + Gen(F) + Batch(F) + PA(R[A]) + e"
heritability: 0.42
heritability_se: 0.03
genetic_variance: 0.42      # Item = PA
residual_variance: 0.58     # Item = e
valid_records: 48
iterations: 4
vc_method: AI
converged: true
```

`compare_with_baseline(summary, baseline, tolerance=1e-4)` 逐值比对。任一项超出容差即报出差异明细，用于确认代码改动是否影响了数值结果。

---

## 六、输入契约

```yaml
input:
  parsed_result: dict          # Skill 07 的解析结果
  exit_code: int
  trait: str
  expected_individuals: int|null
  baseline: dict|null          # 提供时执行回归比对
```

---

## 七、输出契约

```yaml
outputs:
  validation_report: "<output_dir>/<task>_validation_<timestamp>.json"
```

内容：

```yaml
checks:
  - id: G5-01
    verdict: pass | warning | block
    actual: any
    threshold: any
    message: str
suggested_status: completed | partial | failed
heritability:
  estimate: 0.42
  se: 0.03
  item: PA                     # 明确标注取自哪一组分行
  genetic_variance: 0.42
  residual_variance: 0.58
convergence: true | false | null
iterations: 4
ebv_summary:
  column: PA
  n: 150
  mean: 0.077392
  sd: ...
baseline_comparison:
  match: true
  items_compared: 4
  differences: []
warnings: [...]
```

---

## 八、错误码

| 码 | 含义 |
|----|------|
| E070 | 解析结果缺关键字段 |
| E071 | 遗传力超出 [0,1] |
| E072 | 残差方差 ≤ 0 |
| E073 | 结果含 NaN / Inf |
| E074 | 回归基线比对不一致 |

---

## 九、人工确认触发点

- G5-03 未收敛 → 确认是接受 partial 结果还是调整模型重跑；
- G5-06 SE/h² 超过策略阈值 → 确认是否接受不稳定估计；
- G5-08 偏离文献区间 → 确认是群体特点还是数据问题；
- 回归基线不一致 → 确认是预期改动还是引入了缺陷。

---

## 十、禁止事项

1. 禁止把 warning 状态报告为 `completed`；
2. 禁止在收敛状态未知时假定已收敛；
3. 禁止仅凭退出码 0 判定结果可用；
4. 禁止跳过任何一项 G5 检查；
5. 禁止把残差行 h2 当作遗传力；
6. 禁止在验证不通过时让结果进入正式报告。
