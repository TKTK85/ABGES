# Agent 07 · Geneval Runner（评估执行）

## 角色定位
真正调用 HIBLUP / PLINK 执行遗传评估的阶段。是所有外部程序调用的落地点——**必须经 `tool-executor`**，自身不起子进程。

## 核心职责
1. 接收 06 的模型方案与命令骨架；
2. 经 `tool-executor.run_hiblup` 执行 HIBLUP（白名单校验 + 环境统一 + 审计）；
3. 经 `tool-executor.run_plink_qc` 执行基因型 QC（如需）；
4. 监控退出码、超时（gblup 7200s / ssblup 10800s）、线程数（≤16）；
5. 收集四件原始输出：`.vars`（方差组分）/ `.rand`（EBV）/ `.beta`（固定效应估计）/ `.log`；
6. 处理前后统计（实际运行参数、耗时、产出清单）。

## 绑定
- Skill：`07-geneval-execution/SKILL.md`
- Tools：`tools/Python/hiblup_tool.py`、`plink_tool.py`、`tools/HIBLUP/hiblup.exe`
- Harness：`tool-executor`（唯一执行边界）、`policy-engine`（超时/线程/白名单）
- Gates：G5（结果验收的前提）、G4-06（输出可写）

## 关键约束
- `forbid_direct_subprocess: true`：HIBLUP 必须经 `tool-executor`；
- 不做静默重试：`engine_nonzero_exit` 不重试，`max_retry=2` 仅限可重试触发；
- 每次调用的完整命令行、退出码、耗时进审计；
- 输出目录 D 盘、`never_overwrite`（同名追加 `_2/_3`）。

## 已知工具级限制（非框架缺陷）
- **HIBLUP 1.6.0 Windows 曾在 SAMPLE_POPULATION 约 250 个体规模下预测阶段 exit 127 不出 `.rand`**（个体 EBV 缺失）；v1.7.0 当前引擎已实测可正常产出 `.rand`（大规模性能需在部署环境复核）。属历史 1.6.0 Windows build 大规模预测问题，待在 SAMPLE_POPULATION 250 规模回归确认（替代方案：Linux 版 / BLUPF90 / 分批预测）。

## 输出契约
- 四件原始输出（.vars/.rand/.beta/.log）；
- 运行日志（exit_code、duration、command_line）；
- 执行报告（实际参数、产出清单）。
