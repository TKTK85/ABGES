# ABGES Agents（Agent 总览）

> 本文档汇总 ABGES V1.0 的 10 个 Agent 角色。各 Agent 的详细定义见 `agents/<id>-<name>/AGENT.md`。
> 编排与边界以 `configs/agents/agent-registry.yaml` 为唯一权威。

## 调度拓扑
- **唯一调度方**：`00-supervisor`，其 `can_call` 含 01–09；其余 Agent `can_call` 均为空，横向调用以 `E016` 拒绝。
- **默认管线**：以 `configs/agents/agent-registry.yaml#default_pipeline` 为准：`00 → 04 → 01 → 02 → 03 → 05 → 06 → 07 → 08 → 09`；若性状为原始表型，04 按条件跳过。
- **并行组**：原始性状任务并行执行 01/02/03；派生性状任务先执行 04，再并行执行 02/03，01 消费派生性状结果。Harness 在主线程合并门禁和状态。
- **跳过条件**：02 无系谱且 gblup → 跳过；03 无基因型 → 跳过（可选）；04 目标性状全原始 → 跳过（可选）

## 角色一览

| ID | Agent | 职责 | 可选 | 绑定 Skill | 主要闸门 |
|----|-------|------|------|-----------|----------|
| 00 | supervisor | 总控调度、状态判定 | 否 | — | G1–G6 监督 |
| 01 | phenotype | 表型 QC、标准化、变更留痕 | 否 | 01-phenotype-qc | G1 / G2 / G4-04/06 |
| 02 | pedigree | 系谱 QC、拓扑排序、环检测 | 否* | 02-pedigree-qc | G1 / G2 |
| 03 | genotype | 基因型 QC（PLINK） | 是 | 03-genotype-qc | G1 / G2 / G3 |
| 04 | derived-trait | 衍生性状计算（ACL/MCL/CEN/FCR） | 是 | 04-derived-trait-calculation | G2 |
| 05 | data-integration | 表型×系谱×基因型对接，产出引擎输入 | 否 | 05-data-integration | G3 / G4-03/04/06 |
| 06 | geneval-planner | 模型落地、效应筛选、命令骨架 | 否 | 06-geneval-planning | G4 |
| 07 | geneval-runner | 调用 HIBLUP/PLINK 执行评估 | 否 | 07-geneval-execution | G4-06 / G5 前提 |
| 08 | result-validator | 结果验收（收敛/h²/EBV/基线） | 否 | 08-geneval-validation | G5 |
| 09 | reporter | 双通道报告与交付 | 否 | 09-geneval-reporting | G6 |

> *02 在"无系谱且为 gblup"时跳过；pblup/ssblup 无系谱会在更早的 G1 阻断。

## 通用约束（全部 Agent）
- `forbid_direct_subprocess: true`：外部程序调用一律经 `tool-executor`；
- `forbid_raw_data_write: true`：不得改写 `workspace/input/raw/`；
- `must_record_statistics: true`：数据增删改必须记录前后统计；
- `max_retry: 2`：阶段级重试上限。
- 输出契约遵循 `schemas/agent-output-schema.yaml`：status 三态、产物路径必须真实存在、告警必须附 `action_taken`。

## 阶段间数据传递
- 跨阶段不可序列化对象（DataFrame 等）经 `state-manager` 黑板（内存态）传递；
- 落盘一律为可读 JSON 与数据文件；
- 每次运行独立目录 `workspace/{task_id}/_runs/{run_id}`，永不复用。运行内部保留审计状态；面向用户的最终目录不含运行 JSON。
- 任务输入字段由 `schemas/task-schema.yaml` 定义，运行入口在 `abges_runtime/tasks.py` 按该契约校验；跨阶段阈值见 `configs/validation/analysis-policy.yaml`。

## 与 Harness 的关系
Agent 只声明"做什么、产出什么"；**实际执行由注入 Harness 的 `stage_runner` 完成**（见 `runners/pblup_stage_runner.py`）。Harness 负责编排、门禁、策略、状态、审计与工具调用边界——Agent 与 Harness 解耦。
