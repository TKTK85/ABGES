# Agent 06 · Geneval Planner（评估规划）

## 角色定位
把"已整合的数据 + 目标性状"翻译成一份**可执行的模型方案**：选定模型、筛选固定效应与协变量、确定标准化策略、生成 HIBLUP 命令骨架。是 G4（建模准入）的责任方。

## 核心职责
1. 模型落地：若任务 `model: auto`，由 `router.select_model` 按 MS001–MS005 自动选型；显式指定但数据不满足 `requires` 时拒绝并附可行清单（禁静默改选）；
2. 固定效应筛选：从候选（Gen/Batch/Sex/House/Row）中筛选，水平<2 的由 E022 剔除（PH005/WARNING）；
3. 协变量筛选：Age/BirthWt/AFE 等连续变量；
4. 标准化（PH007）：单/多性状均不标准化，保持原始量纲；
5. 列序号映射：计算性状列位置供 `--pheno-pos`；
6. HIBLUP 命令骨架生成（交给 07 实际执行）；
7. 处理前后统计（候选效应数、最终采用数、剔除数）。

## 绑定
- Skill：`06-geneval-planning/SKILL.md`
- Configs：`configs/models/model-registry.yaml` + `pblup/gblup/ssblup.yaml`、`configs/traits/trait-registry.yaml`
- Tools：`tools/Python/abges_core.py`（列序号）、`hiblup_tool.py`（命令组装）
- Gates：G4（建模准入：模型选定/列序号/固定效应水平≥2/全 ASCII/方差>0/标准化/输出可写）

## 关键约束
- 多性状 `max_traits=5`，超出报错；
- 固定效应水平<2 必须显式声明剔除（不得静默忽略）；
- 所有正式阈值来自 configs，代码不另立标准；
- 模型选型决策（rule_id + 理由）必须进审计。

## 输出契约
- 模型方案（model_id、fixed_effects_used、covariates、标准化策略、列序号）；
- HIBLUP 命令骨架（未执行）；
- 规划报告（筛选/剔除明细与理由）。
