# ABGES 架构（ARCHITECTURE）

> 版本：1.0.0 ｜ 本文档描述 ABGES 的总体架构、分层与模块职责。代码与配置为唯一权威，本文为反推梳理。

## 1. 设计哲学

- **规则外置**：QC/结果阈值、性状定义、模型选型、系统策略都在 `configs/`，Harness 只读取、不臆断。默认阈值集中于 `configs/validation/analysis-policy.yaml`，并记录在每次运行元数据中。
- **执行边界唯一**：Agent 不得自起子进程，外部程序调用一律经 `tool-executor`（白名单 + 审计）。
- **可审计、可复现**：每次运行独立目录，完整命令行 + 原始数据 checksum 进元数据，全部事件进审计流水。
- **诚实交付**：状态三态、禁用模糊措辞、部分项永不接受人工放宽。

## 2. 三层分离

```
┌─────────────────────────────────────────────┐
│  Agents (10) + Skills (9)   ← 领域职责与逻辑  │
├─────────────────────────────────────────────┤
│  Harness (11 模块)          ← 编排/门禁/策略/  │
│                               状态/审计/工具   │
├─────────────────────────────────────────────┤
│  Configs + Schemas + Tools   ← 规则/契约/实现  │
│  (HIBLUP / PLINK 引擎)                        │
└─────────────────────────────────────────────┘
```

- **Harness 层**：`orchestrator` 按 `agent-registry#default_pipeline` 驱动各阶段；`router` 管 Agent 边界与模型选型；`gatekeeper` 管 6 闸门；`policy-engine` 管系统铁律；`state-manager` 管状态与黑板；`tool-executor` 管工具调用；`audit` 留痕；`error-handler` 分类异常；`hitl` 人工介入；`entrypoint` 是唯一命令行入口；`_bootstrap` 做环境引导。
- **Agents + Skills 层**：Agent 声明"做什么、产出什么"；Skill 是 Agent 的领域实现指引；**实际执行由注入 Harness 的 `stage_runner` 完成**（解耦）。
- **Configs + Schemas + Tools 层**：规则、契约、计算实现全外置。

## 3. 模块职责速查

| 模块 | 文件 | 职责 |
|------|------|------|
| 入口 | `harness/entrypoint/main.py` | 解析 task、校验、选模型、装配组件、驱动编排 |
| 编排 | `harness/orchestrator/orchestrator.py` | 阶段推进、并行/跳过、门禁、错误、人工介入、状态判定 |
| 路由 | `harness/router/router.py` | Agent 调用边界 + 模型自动选型（MS001–MS005） |
| 闸门 | `harness/gatekeeper/gatekeeper.py` | G1–G6 唯一执行点，三态判定 |
| 策略 | `harness/policy-engine/policy_engine.py` | 系统铁律唯一权威 |
| 状态 | `harness/state-manager/state_manager.py` | 运行目录 + state.json + 黑板 |
| 工具 | `harness/tool-executor/tool_executor.py` | 外部/内部工具唯一调用边界 |
| 审计 | `harness/audit/audit_hub.py` | 工具调用/门禁/事件/放宽留痕 |
| 错误 | `harness/error-handler/error_handler.py` | 异常分类 |
| 人工 | `harness/human-in-the-loop/hitl.py` | 人工确认（auto/interactive/deny） |
| 引导 | `harness/_bootstrap.py` | D 盘 venv、TEMP、组件加载 |

## 4. 数据流

```
task.yaml
   │  entrypoint 校验 + 选模型 + 原始数据 checksum 登记
   ▼
[01 表型QC] ─┐
[02 系谱QC] ─┼─→ [04 衍生性状（按需）] → [05 数据整合]
[03 基因型QC] ─┤
[04 派生性状] ─┘   (04 -> 01?????????? 01?? 02/03 当前模型适用分支由 Harness 并发执行，汇合后统一门禁)
   │  各阶段经 G1/G2 验收，产出标准化中间表
   ▼
[05 数据整合]  → 引擎输入表（G3 对接、G4 建模准入）
   ▼
[06 评估规划]  → 模型方案 + 命令骨架（G4）
   ▼
[07 评估执行]  → HIBLUP/PLINK（经 tool-executor，G5 前提）
   ▼
[08 结果验证]  → G5 验收 + 基线对照
   ▼
[09 报告]      → 双通道交付（G6：元数据/统计/告警/三态/原始未改）
   │
   ▼
result.json + 中文报告 + 元数据 + 统计 + 审计流水
```

## 4.1 并列数据 Agent 调度

- 原始性状时 `01/02/03` 并行；派生性状时先完成 `04`，再让 `02/03` 与派生结果分支并行，之后 `01` 消费派生表型。分支由 Router 按性状源选择。
- 并发的只是数据处理；状态写入、G1/G2 门禁合并、检查点与审计事件均由 Harness 在汇合点单线程提交。
- 每次并发均在审计日志中写入 `parallel_group_dispatched` 和 `parallel_group_joined`；后续 `05` 数据整合必须等待所有适用分支完成。
- PLINK 只属于 `03` 基因型分支；HIBLUP 只属于 `07` 评估执行阶段。

## 5. 运行隔离

- 每次运行：`workspace/{task_id}/_runs/{run_id}`，含 input/intermediate/output/logs/reports 等目录，永不复用；成功交付后保留 state、元数据、QC 与日志，只清除大体积 resume 检查点。
- 黑板（blackboard）：内存态，跨阶段传 DataFrame 等不可序列化对象；落盘一律 JSON/数据文件。
- 跳过分两种：`not_applicable`（设计内不适用）vs `upstream_failed`（上游失败中断）——前者不计入降级。

## 6. 配置体系

| 配置 | 文件 | 内容 |
|------|------|------|
| Agent 编排 | `configs/agents/agent-registry.yaml` | 10 Agent 职责/边界/管线/跳过 |
| 闸门 | `configs/gates/gate-rules.yaml` | G1–G6 共 30+ 检查项 |
| 模型 | `configs/models/model-registry.yaml` + `pblup/gblup/ssblup.yaml` | 模型注册 + 自动选型 |
| QC | `configs/qc/phenotype/pedigree/genotype-qc.yaml` | 三类 QC 规则 |
| 性状 | `configs/traits/trait-registry.yaml` + `derived-traits.yaml` | 性状字典 |
| 系统策略 | `configs/policies/system-policies.yaml` | 十二节铁律 |
| 跨阶段验证策略 | `configs/validation/analysis-policy.yaml` | 表型/基因型/对接阈值、遗传力界限与物种参考范围 |
| 基线 | `configs/baselines/*.json` | 历史回归基线 |

## 7. 数据契约

7 份 Schema（`schemas/`）：`task`（输入）、`result`（机器输出）、`agent-output`（阶段输出）、`phenotype`/`pedigree`/`genotype`（数据）、`trait`（性状定义）。Schema 与门禁、Agent 输出强一致，"不得另立标准"。

## 8. 工具层与引擎

- `tools/Python/`：8 个 `abges_*`（`core`/`dataio`/`pedigree`/`phenotype`/`traits`/`integration`/`validate`/`ascii`）+ `hiblup_tool` + `plink_tool`。
- `tools/HIBLUP/hiblup.exe`：HIBLUP 1.7.0（Windows x86_64）。
- `tools/PLINK/`：仅 `plink_tool.py`，需另行提供 `plink2.exe`。

## 9. 测试

`tests/` 下 7 类：`unit` / `integration` / `regression` / `skill` / `workflow` / `agent` / `benchmark`。


## ?????????

?????????????Harness ???? `01/02/03`?
????????????????Harness ???? `04/02/03`?04 ???????????????? `01` ????????? QC?????? 05 ???
???04 ??? 01 ?????????????????????????????
