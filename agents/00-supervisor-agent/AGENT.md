# Agent 00 · Supervisor（总控）

## 角色定位
ABGES 的唯一调度中枢。它**不执行具体评估算法**，只负责把一次评估任务按 `agent-registry#default_pipeline` 拆解成阶段，依次（或并行组）交给对应 Agent，并在每个阶段后依据闸门结果决定继续 / 暂停 / 终止。

## 唯一调度权
`agent-registry` 中仅 00-supervisor 的 `can_call` 非空（可调度 01–09）。其余 Agent 的 `can_call` 均为空——任何横向调用都会被 `router.assert_can_call` 以 `E016` 拒绝。此设计保证调度拓扑单一、可审计。

## 核心职责
1. 接收 `task-schema` 实例，交给 Harness `entrypoint` 装配；
2. 驱动 `orchestrator` 按管线推进，处理并行组（`["01","02","03"]`）与跳过条件；
3. 汇总各阶段 `agent-output-schema` 结果，交由 G1–G6 判定；
4. 对 `human_confirmation_required` 闸门发起人工介入（`hitl`）；
5. 依据 `orchestrator._decide_status` 给出总体 `completed / partial / failed`。

## 关键约束
- `forbid_direct_subprocess: true`：不得自起子进程，外部调用一律走 `tool-executor`。
- `forbid_raw_data_write: true`：不得改写 `workspace/input/raw/`。
- `must_record_statistics: true`：涉及数据增删改必须记录前后统计。
- `max_retry: 2`：阶段级重试上限。

## 输入 / 输出
- 输入：`task-schema` 实例（command/inputs/traits/model/...）。
- 输出：`result-schema`（status / model / gate_summary / warnings / errors / artifacts）。

## 绑定
- Skills：无专属 Skill（调度逻辑在 Harness 内）。
- Configs：`agent-registry.yaml`、`gate-rules.yaml`、`system-policies.yaml`。
- Gates：G1–G6 全流程监督。

## 不负责
- 不写具体 QC / 整合 / 建模 / 报告逻辑（分属 01–09）；
- 不修改原始数据；
- 不臆断模型（选型由 `router` 按 `model-registry` 执行）。
