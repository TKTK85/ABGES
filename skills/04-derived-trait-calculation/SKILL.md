---
name: 04-derived-trait-calculation
description: >
  计算衍生性状。触发条件：用户要求计算 FCR、ACL、MCL、NCC、CEN 等衍生性状；
  询问 Skill 04 能计算哪些性状；修改/新增衍生性状定义。
  本技能负责从基础性状或原始记录聚合得出派生数值，
  公式必须已登记在 configs/derived-traits.yaml 中，Agent 不得临时发明公式。
agent_created: true
---

# SKILL 04 · 衍生性状计算

## 依赖文件

| 文件 | 路径 | 用途 |
|------|------|------|
| configs/derived-traits.yaml | 本目录 configs/ | 性状注册、算法定义 |
| tools/Python/abges_traits.py | 项目根 tools/Python/ | 算法实现（唯一真相） |
| references/clutch_algorithm.md | 本目录 references/ | 连产算法详细规则 |
| references/formula_rules.md | 本目录 references/ | 公式安全规则 |

## 两类衍生性状

| 类型 | 性状 | 算法 |
|------|------|------|
| formula | FCR（料重比） | 算术公式 |
| record | ACL、MCL、NCC | `clutch`（连产算法） |
| record | CEN（累计产蛋数） | `cumulative_egg_number` |

## 核心纪律

> Agent 负责判断"是否需要算"，Tool 负责"真正去算"。
> **大模型不得临时发明正式分析公式**。
> 任何未登记在 `configs/derived-traits.yaml` 中的公式，一律拒绝执行。

## 前置校验

1. 每个 `target_trait` 必须在 `derived-traits.yaml` 中登记 → 否则 `E040`
2. 公式型：`required_traits` 必须全部存在于表型中 → 否则 `E041`，报出缺哪列
3. 记录型：`egg_source` 必须提供且可读 → 否则 `E042`

**缺列时不得静默跳过。** 必须明确告知先生缺了什么、影响哪个性状，并询问处理策略。

## 合并回表型

衍生性状算完后需并回表型表。**必须返回匹配率**：

```yaml
match_rate: 0.970   # < 0.90 告警；< 0.50 请求人工确认
```

绝不对未匹配个体静默填 NA——未匹配意味着 ID 体系可能有问题，必须让人知道。

## 输出契约

```
derived_traits:  <output_dir>/<task>_derived_<timestamp>.csv
merged_phenotype: <prefix>_phenotype_with_derived.txt
statistics:       <prefix>_statistics.json
change_log:       <prefix>_change_log.jsonl
calc_report:      <prefix>_derived_report.json
metadata:         <prefix>_metadata.json
```

## 错误码

| 码 | 含义 |
|----|------|
| E040 | 性状未在 derived-traits 登记 |
| E041 | 基础性状缺失 |
| E042 | 逐日记录源不可读 |
| E043 | 公式含非法字符 |
| E044 | 记录列（ID/日期）识别失败 |

## 禁止事项

1. 禁止临时发明或修改分析公式
2. 禁止在基础性状缺失时静默跳过
3. 禁止对未匹配个体静默填充
4. 禁止绕过字符白名单执行公式
5. 禁止把 guards 拦截和 validation 越界混为一类统计

## 详细算法规则

- 连产算法详情（含决策表/统计窗口/回归锚点）：`references/clutch_algorithm.md`
- 公式安全规则（含白名单/guards vs validation）：`references/formula_rules.md`
