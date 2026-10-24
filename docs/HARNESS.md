# ABGES Harness（执行框架）

> 版本：1.0.0 ｜ Harness 是独立于领域逻辑的"执行框架"，由 11 个模块组成，是 6 闸门与系统铁律的唯一落地点。

## 1. 模块总览

| 模块 | 路径 | 职责 |
|------|------|------|
| entrypoint | `entrypoint/main.py` | 唯一命令行入口，解析 task、校验、选模型、装配、驱动 |
| orchestrator | `orchestrator/orchestrator.py` | 阶段编排（管线/并行/跳过/门禁/错误/人工介入/状态） |
| router | `router/router.py` | Agent 调用边界 + 模型自动选型 |
| gatekeeper | `gatekeeper/gatekeeper.py` | G1–G6 唯一执行点 |
| policy-engine | `policy-engine/policy_engine.py` | 系统铁律唯一权威 |
| state-manager | `state-manager/state_manager.py` | 运行目录 + state.json + 黑板 |
| tool-executor | `tool-executor/tool_executor.py` | 外部/内部工具唯一调用边界 |
| audit | `audit/audit_hub.py` | 审计流水 |
| error-handler | `error-handler/error_handler.py` | 异常分类 |
| human-in-the-loop | `human-in-the-loop/hitl.py` | 人工确认 |
| _bootstrap | `_bootstrap.py` | 环境引导（D 盘 venv / TEMP / 组件加载） |

## 2. 入口与双通道

```bash
python harness/entrypoint/main.py <task.yaml> [--runner module:func] [--mode auto|interactive|deny]
```

- `command`：`validate`（输入预检）/ `plan`（生成计算方案）/ `run`（执行评估）/ `status`（列历史运行）。任务字段由 `schemas/task-schema.yaml` 声明，`abges_runtime.tasks` 在运行时读取该契约并做类型、必填项和额外字段校验。
- 双通道：机器通道（`--json` 输出 `result-schema`）+ 人类通道（中文报告，09-reporter 产出）。
- `--runner`：真实阶段执行器（如 `runners.pblup_stage_runner:run`）；缺省用占位执行器（仅验证框架，状态为 partial）。

## 3. 编排逻辑（orchestrator）

1. 按 `router.pipeline()` 顺序遍历 `default_pipeline`；
2. 上游失败 → 后续阶段标 `upstream_failed` 跳过，流程终止；
3. `router.should_skip` 命中 → 标 `not_applicable` 跳过；
4. 执行阶段（注入的 `stage_runner`）；异常经 `error-handler` 分类，标记 failed 并终止；
5. 合并黑板与可用性更新，登记产物（文件必须真实存在）；
6. 逐闸门 `gatekeeper.evaluate`：block → 终止；human → 发起 hitl；warning → 记告警；
7. 汇总 `result-schema`，状态判定依 G5 与告警。

**状态判定（`_decide_status`）**：
- `failed`：闸门阻断 / 存在错误 / 上游失败中断阶段；
- `partial`：存在 pass_with_warning / 未处置告警（action_taken 留空）/ G5 未全通过；
- `completed`：以上皆无（`not_applicable` 跳过不构成降级依据）。
- 诚实铁律：`policy.assert_status_allowed` 校验状态词合法。

## 4. 闸门（gatekeeper）

- 统一状态：`pass` / `pass_with_warning` / `human_confirmation_required` / `block` / `skip`。
- **缺某条适用检查的事实 → 一律 `block`**（拿不出依据就不放行）。
- 人工放宽：`never_overridable` 项（G6-04/05/06）永不接受；其余需 `reason`，否则拒绝。
- 汇总 `overall_summary` 对应 `result-schema#gate_summary`。

六道闸门见 `configs/gates/gate-rules.yaml` 与 `README.md` 速查表。

## 5. 系统铁律（policy-engine）

全部来自 `configs/policies/system-policies.yaml`，代码不硬编码第二套标准。十二节：

1. 执行边界唯一入口 + 二进制白名单（hiblup.exe / plink2.exe）；
2. 人机双通道（validate/run/status，--json）；
3. 数据只读（raw_data_readonly + md5 + 交付前 verify + copy_then_modify）；
4. 输出按任务固定到 `workspace/<task_name>/{input,output,reports,_runs}`；每次运行保留唯一 run_id，成功发布后清理旧 Run；
5. 运行元数据（*_metadata.json 含 full_command_line）；
6. 处理前后统计（*_statistics.json，require_before_after）；
7. 诚实交付（禁模糊措辞、三态、有告警不得 completed）；
8. 非 ASCII 三级降级转码（header_map → pypinyin → positional_alias，报告 restore）；
9. 路径资源（使用项目 .venv 正常启动，临时目录由运行配置解析、threads≤16、内存 guard）；
10. 超时（gblup 7200 / ssblup 10800）+ 重试（max2，engine_nonzero_exit 不重试）；
11. 人工介入（覆盖/放宽阈值/删产出/改原始/对外需确认）；
12. 审计 jsonl 365 天。

## 6. 状态与审计

- `state-manager`：每次运行独立目录，记录 stage 状态、输入指纹、产物、告警和统计；黑板在内存传 DataFrame，暂停/失败检查点用带 SHA-256 的数据 JSON 恢复。
- PBLUP、GBLUP 和 SSGBLUP 的多性状 G5 检查在 `runners/common.py` 共用同一合并器；G5 阈值与逐 ID 集合核验由 `tools/Python/abges_validate.py` 统一执行。
- 成功交付后对外目录不含运行 JSON；内部保留 state、元数据、QC 与日志，仅按策略清理大体积恢复检查点。失败和待决策运行保留可恢复检查点。
- `audit`：工具调用（命令/退出码/耗时）、门禁判定、事件、人工放宽全部进 `audit_{run_id}.jsonl`，保留 365 天。

## 7. 运行环境（_bootstrap）

- 不注入外部 site-packages；使用项目 `.venv`，拒绝 `python -S` 启动；
- TEMP/TMP 使用系统临时目录（`%TEMP%`）；
- 工具层（tools/Python、tools/HIBLUP、tools/PLINK）加入 sys.path；
- 连字符子目录（policy-engine 等）经 `load_component` 按路径加载并登记 sys.modules。

## 8. 工具执行边界（tool-executor）

- 所有 HIBLUP / PLINK 调用经 `run_hiblup` / `run_plink_qc`，先做白名单校验（`assert_binary_allowed`），统一注入 D 盘 TEMP 与线程数，完整命令行/退出码/耗时进审计；
- 不做静默重试，失败原样返回，由 error-handler 决定后续；
- 内部 Python 工具经 `call_python` 记录耗时与状态，异常原样抛出。
