# ABGES 简明使用说明

## 这个项目做什么

ABGES 用来做家禽遗传评估，计算遗传力和 EBV（育种值）。

- PBLUP：有表型和系谱时使用。
- GBLUP：有表型和基因型时使用。
- ssGBLUP：表型、系谱、基因型都有时使用。

它会检查数据、生成模型、调用 HIBLUP 计算、保存结果。原始数据不会被修改。

## 你每天只做四步

### 第一步：选模板并改任务

在 `tasks` 文件夹中复制最接近的一份：

- `N631_BW56_pblup_r2_GenBatSex.yaml`：PBLUP。
- `HB_28WENS_GBLUP.yaml`：GBLUP。
- `Virginia_BW_ssgblup.yaml`：ssGBLUP。

改数据路径、性状名称、固定效应。体重常用固定效应为 `Gen`、`Batch`、`Sex`。

### 第二步：先检查，再计算

先把任务中的 `command: run` 改为：

```yaml
command: validate
```

运行命令：

```powershell
python harness\entrypoint\main.py tasks\你的任务.yaml --runner runners.pblup_stage_runner:run --mode deny
```

没有阻断错误后，改为：

```yaml
command: plan
```

再运行一次。`plan` 会检查数据和生成计算方案，但不会正式调用 HIBLUP。

确认方案后，改回：

```yaml
command: run
```

再运行才是正式计算。

GBLUP 的 runner 是 `runners.gblup_stage_runner:run`；ssGBLUP 的 runner 是 `runners.ssgblup_stage_runner:run`。

### 第三步：判断是否成功

去 `workspace/<任务名>/_runs/<run_id>/` 查看内部状态、门禁 JSON、元数据、QC 记录和日志。面向交付的最终报告与育种值表在任务 YAML 的 `output_dir` 下；运行 JSON 不会复制进最终交付目录。

- `completed`：通过验证，可进一步看遗传力和 EBV。
- `partial`：不完整；若刚刚运行 validate 或 plan，这是正常的。
- `failed`：失败，不能使用结果。

看见 Excel、Markdown、`.rand` 文件，不代表评估已经成功；必须以 `result.json` 的状态和验证报告为准。

### 第四步：看结果

重点文件：

- `validation_report.json`：遗传力、方差、收敛情况。
- `ebv_top20.json`：EBV 排名摘要。
- `result.json`：本次是否成功。

没有 `.rand`、没有 EBV、HIBLUP 没收敛，或状态不是 `completed`，就不能把结果用于选种。

## 失败后如何续跑

在运行目录找：

```text
agent_result_matrix.json
```

它是进度表。只看 `resume_action`：

- `reuse`：这一步已完成，不再重复。
- `skip`：这一步本来不需要做。
- `rerun`：这一步失败或被上游卡住，需要重跑。

修复问题后，用原任务文件运行：

```powershell
python harness\entrypoint\main.py tasks\你的任务.yaml --runner runners.pblup_stage_runner:run --mode deny --resume-run 运行编号
```

不能改任务内容；不能对 `completed` 或 `partial` 的运行续跑。这是为了避免不同数据或模型被混在一起。

## 最常见情况

| 现象 | 处理 |
|---|---|
| validate 或 plan 显示 partial | 正常，继续检查后再进行下一步。 |
| run 显示 failed | 打开矩阵，找到第一个 rerun 的错误码。 |
| 没有 EBV 或 .rand | 不能选种，检查 HIBLUP 收敛与验证报告。 |
| 遗传力差异很大 | 检查样本量、性状、固定效应、清洗规则和模型是否变化。 |

## MCP 的简单理解

智能体可以通过 MCP 提交任务和读取结果，但不能绕过系统直接运行 HIBLUP 或 PLINK。它也不会把原始表或完整日志塞进智能体上下文。正式 run 必须明确授权。


## 2026-08-12 ????

????MCP Worker ??????????? Worker ???????????????? Runner ??????????? `runners/common.py`???????????? `scripts/experiments/`???? workflow ????????? `workflows/README.md` ?????????
