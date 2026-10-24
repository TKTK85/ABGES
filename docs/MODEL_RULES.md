# ABGES 模型规则（MODEL_RULES）

> 版本：1.0.0 ｜ 模型注册与自动选型以 `configs/models/` 为唯一权威。

## 1. 三模型注册

| 模型 | requires | 状态 | 说明 |
|------|----------|------|------|
| pblup | phenotype + pedigree | production |  pedigree-BLUP |
| gblup | phenotype + genotype | production | genomic-BLUP |
| ssblup | phenotype + pedigree + genotype | production | single-step genomic-BLUP |

每个模型的细节见 `configs/models/{pblup,gblup,ssblup}.yaml`（固定效应默认、标准化策略、vc_method、列序号计算等）。

## 2. 自动选型（MS001–MS005）

`router.select_model` 按 `model-registry#selection_rules` 顺序匹配（命中即停）：

- **MS001**：有 phenotype + pedigree + genotype，且基因型可用 → `ssblup`；
- **MS002**：有 phenotype + pedigree，无/不足基因型 → `pblup`；
- **MS003**：有 phenotype + genotype，无系谱 → `gblup`；
- **MS004**：仅有 phenotype（无可建亲缘/基因）→ 报错（无法构建关系矩阵）；
- **MS005**：genotyped_ratio 过低时降级或不选 ssblup（依 `genotyped_ratio_gte/lt` 条件）。

> 具体 rule_id 与条件以 `model-registry.yaml` 为准；规则未覆盖的数据组合 → 拒绝臆断（抛 E012）。

## 3. 显式指定 vs 自动

- 任务 `model: auto`（默认）→ 走 selection_rules；
- 任务显式指定 `pblup/gblup/ssblup`：
  - 数据满足其 `requires` → 采用（`source=explicit`）；
  - **数据不满足 → 抛 E012 并附可行模型清单，禁止静默改选**（`explicit_override.forbid_silent_switch=true`）。

## 4. 多性状

- `multi_trait.max_traits = 5`：超出报错；
- 多性状默认不标准化，各性状保持原始量纲（2026-10-12 起与单性状合同一致；h² 与遗传相关为尺度无关量，不受影响）；
- 量纲差异极大的性状组合可经 human-in-the-loop 恢复强制 z-score（PH007.multi_force_standardize）。

## 5. 引擎登记

`configs/models/model-registry.yaml#engines.hiblup` 登记已知问题：
- `HIBLUP-WIN-NONASCII`：非 ASCII 列名/水平崩溃 → 由 `abges_ascii` 三级转码 mitigate；
- `HIBLUP-SINGULAR-FE`：单水平固定效应 → 奇异，由 E022 自动剔除。

## 6. 选型决策可追溯

每次选型产出 `ModelDecision`（model_id / rule_id / reason / source / requires / missing / alternatives），进审计（`audit.event("model_selected", ...)`）。
