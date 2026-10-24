
> MCP integration: see `docs/MCP_GATEWAY.md`. The MCP gateway is a safe adapter to this Harness; it does not replace the HIBLUP/PLINK CLI execution layer.
# ABGES · Automated-Breeding-Genetic-Evaluation-Skill

> 生产级、多 Agent 协同的家禽遗传评估框架（ABGES V1.0）
> 面向 PBLUP / GBLUP / SSGBLUP 三类 BLUP 评估，以 HIBLUP 1.7.0 为计算引擎，PLINK 2 为基因型质控引擎。

---

## 它解决什么问题

把"零散能跑一次的评估脚本"升级为**可审计、可配置、可复现、可拒绝放行**的评估生产系统：

- 数据进来先过 6 道闸门（G1–G6），缺依据一律不放行；
- 模型按数据可用性**自动选型**，不靠人工写死；
- 所有规则（QC 阈值、性状定义、系统策略）**外置到 configs**，代码不另立标准；任务运行时校验读取 `schemas/task-schema.yaml`，通用QC/结果默认阈值集中在 `configs/validation/analysis-policy.yaml`；
- 每次运行产出机器可读结果（json）+ 人类可读报告，**强可复现**（完整命令行 + 原始数据 checksum）；
- 诚实交付铁律：状态只有 `completed / partial / failed`，有告警不得谎称完成。

---

## 目录结构

```
ABGES/
├── harness/          # 独立执行框架（11 模块）：orchestrator/router/gatekeeper/
│                     #   policy-engine/state-manager/tool-executor/audit/
│                     #   error-handler/human-in-the-loop/entrypoint/_bootstrap
├── agents/           # 10 个 Agent 定义（AGENT.md）
├── skills/           # 9 个 Skill（SKILL.md）
├── configs/          # 配置体系：agent-registry / gate-rules / model-registry /
│                     #   qc / traits / system-policies / baselines
├── schemas/          # 7 份数据契约（task/result/agent-output/表型/系谱/基因型/性状）
├── tools/
│   ├── Python/       # 8 个 abges_* + hiblup_tool + plink_tool
│   ├── HIBLUP/       # hiblup.exe (v1.7.0) + 依赖 dll
│   └── PLINK/        # plink_tool.py（需另行提供 plink2.exe）
├── runners/          # pblup_stage_runner.py（真实阶段执行器）
├── scripts/          # 探针 / 验证脚本
├── tasks/            # 任务模板（tasks/templates/）
├── tests/            # 7 类测试
└── workspace/        # 每个任务固定为 <task_name>/{input,output,reports,_runs}
```

---

## 快速开始

```bash
# 1. 准备表型、系谱（基因型可选）放入 workspace/input/raw/
# 2. 编写任务实例（参考 tasks/templates/TEMPLATE_PE_repeat_records_PBLUP.yaml）
# 3. 先执行 scripts/bootstrap_abges_venv.ps1 创建项目独立环境，再用入口跑
.venv/Scripts/python.exe harness/entrypoint/main.py tasks/templates/TEMPLATE_PE_repeat_records_PBLUP.yaml \
    --runner runners.pblup_stage_runner:run --mode auto

# 命令模式（双通道）
#   validate 仅校验输入与选型
#   plan     ?? QC/??/???????? HIBLUP
#   run      执行评估
#   status   列出历史运行
# 机器通道加 --json 输出 result-schema
```

> 当前锁文件面向 Windows x64 / Python 3.14。使用项目 `.venv` 正常启动，不使用 `-S` 或注入外部 site-packages。部署前执行 `.venv/Scripts/python.exe scripts/check_environment.py --require-engines --json`。旧 pickle 检查点不能恢复，需新建运行。见 [修复与验证记录](docs/ABGES_代码审计修复与验证_2026-10-22.md)。

---

## 10 个 Agent

`00` 总控 → `01` 表型QC → `02` 系谱QC → `03` 基因型QC(可选) → `04` 衍生性状(可选) → `05` 数据整合 → `06` 评估规划 → `07` 评估执行 → `08` 结果验证 → `09` 报告。

详见 `docs/AGENTS.md` 与各 `agents/<id>/AGENT.md`。

---

## 六道闸门

| 闸门 | 名称 | 责任方 |
|------|------|--------|
| G1 | 数据准入 | 01 / 02 / 03 |
| G2 | 质控验收 | 01 / 02 / 03 / 04 |
| G3 | 数据对接 | 05 |
| G4 | 建模准入 | 06 |
| G5 | 结果验收 | 08 |
| G6 | 交付验收 | 09 |

三态判定 `pass / pass_with_warning / block`；缺适用检查依据一律 `block`。

---

## 设计文档索引

| 文档 | 内容 |
|------|------|
| `docs/ARCHITECTURE.md` | 总体架构、分层、模块职责 |
| `docs/AGENTS.md` | 10 Agent 总览 |
| `docs/SKILLS.md` | 9 Skill 总览 |
| `docs/WORKFLOWS.md` | 默认管线、并行/跳过、双通道 |
| `docs/HARNESS.md` | 11 模块、闸门、系统铁律 |
| `docs/MODEL_RULES.md` | 模型注册与自动选型 |
| `docs/QC_RULES.md` | 表型/系谱/基因型 QC 规则 |
| `docs/TRAIT_DICTIONARY.md` | 性状字典 |
| `docs/DATA_DICTIONARY.md` | 数据文件与 IO 规范 |
| `docs/AUDIT_SPEC.md` | 审计规范 |

---

## 已知限制

  **缓解路径**（按顺序尝试，每次只改一项）：
  1. 确认 `engine_params.reliability` 未被显式置 false（默认 true，即默认开启 `--r2` 逐个体输出 PEV/SE/R2；如需降低内存峰值可显式置 false 关闭 `--r2`，关闭后内存峰值约降为 1/6.7）；
  2. 在 `engine_params.step_size` 传入分块值（对应 HIBLUP `--step N`），从 1000 起试，失败再降到 500；
  3. 若仍 exit 127，用 `--keep` 只保留当前关注个体（`engine_params.keep_file`），或切 BLUPF90 作为替代引擎（HIBLUP 路径保留作方差组分估计）。
- PLINK 2 无随包 exe，需另行提供 `plink2.exe`。
- 表型输出缺失记号 bug（空串而非 `NA`）已登记待修（见 `docs/QC_RULES.md` 与项目待办）。

---

## 版本

ABGES V1.0（2026-10-24 固化基线；见 `docs/ABGES_V1.0_基线与输出契约.md`）


## Public repository contents

This repository contains ABGES source code, generic configuration, tests, and operator documentation. It intentionally excludes breeding datasets, run outputs, private baselines, local credentials, and HIBLUP/PLINK engine binaries. Provision licensed engines separately on the analysis machine.
