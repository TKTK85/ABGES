# SKILL 07 · Geneval Execution

遗传评估执行技能。负责把 Skill 06 已确认的分析方案，变成一次**真实、可复现、全程留痕**的 HIBLUP 运行，并把结果解析出来。

本技能不做任何"该不该这么算"的判断。方案在上一环节已经定死，这里只负责忠实执行。

---

## 一、元信息

| 项目 | 内容 |
|------|------|
| skill_id | `07-geneval-execution` |
| 版本 | 1.0.0 |
| 归属 Agent | `07-geneval-runner-agent` |
| 依赖配置 | `configs/models/{pblup,gblup,ssblup}.yaml`、`configs/policies/system-policies.yaml` |
| 实现工具 | `tools/HIBLUP/hiblup_tool.py` |
| 外部程序 | HIBLUP ≥ 1.6.0 |
| 对接闸门 | G5-01（退出码）、G5-02（输出齐全）、G5-03（收敛） |
| 输出 Schema | `schemas/result-schema.yaml` |

---

## 二、调用链

Agent **不得**直接起进程。唯一合法路径：

```text
Geneval Runner Agent
  ↓ 生成调用请求
Harness Router
  ↓ Policy / Gate 检查
Harness Tool Executor
  ↓ CLI 调用
HIBLUP
  ↓
捕获 stdout / stderr / exit code
  ↓
保存运行日志
  ↓
解析结果文件
  ↓
Result Validator (Skill 08)
```

这是"唯一执行边界"铁律的体现：所有外部程序调用集中在 Tool Executor 一处，便于统一施加超时、审计与错误处理。

---

## 三、执行步骤

```text
环境检查（路径 / 权限 / 版本 ≥1.6.0）
  ↓
输入文件存在性与可读性检查
  ↓
输出前缀冲突检查（带时间戳，绝不覆盖）
  ↓
组装命令行
  ↓
[Harness Tool Executor] 发起 CLI
  ↓
捕获 exit_code / stdout / stderr / 耗时
  ↓
写审计记录（record_tool_call）
  ↓
校验输出文件齐全性
  ↓
解析 .vars / .rand / .beta / .log
  ↓
提取方差组分、遗传力、EBV、收敛状态
  ↓
写 metadata.json
```

---

## 四、HIBLUP 命令行口径（实测确认，务必照做）

以下均为在本机 HIBLUP v1.7.0 上实测验证的行为，与文档字面描述有出入之处以实测为准。

### 4.1 效应参数是「每性状一个 token」

`--pheno-pos`、`--dcovar`、`--qcovar`、`--rand` 都按性状展开：

```text
--dcovar  2,3   5      # 性状1用第2、3列；性状2用第5列
--dcovar  2,3   0      # 性状2无固定效应，占位写 "0"
```

token 内部用逗号分隔列序号，**性状间用空格分隔**。某性状无对应效应时**必须填 `0` 占位**，不能省略，否则参数与性状错位。

### 4.2 输出后缀取决于走哪条关系矩阵

| 入参 | 输出文件后缀 |
|------|-------------|
| `--bfile`（基因组分析） | 带 `_ad` |
| `--xrm`（外部关系矩阵） | 不带 |
| `--pedigree`（纯系谱） | 不带 |

解析器必须按 `["_ad", ""]` 两种可能**探测**，不能写死。

### 4.3 迭代参数按算法条件追加

| vc_method | 追加参数 |
|-----------|---------|
| AI / EMAI / HI | `--ai-maxit` |
| EM / EMAI | `--em-maxit` |

不满足条件却传入，HIBLUP 会报参数错误。

### 4.4 SSGBLUP 用单一 `--alpha` 控制 A/G 融合

不是两个权重参数，是一个 alpha。

### 4.5 单水平固定效应必须已剔除

若把只有一个水平的离散列传进去，HIBLUP v1.7.0 会在**写 `.rand` 文件阶段崩溃**，且错误信息不指向真正原因。本技能在组装命令时会二次检测（`detect_constant_fixed_effects`）作为兜底，检出即剔除并告警 `E022`。

### 4.6 PBLUP 命令示例

```text
hiblup --single-trait \
  --pheno  <pheno.txt> \
  --pheno-pos 5 \
  --dcovar 2,3 \
  --pedigree <ped.txt> \
  --threads 16 \
  --add \
  --vc-method AI \
  --tol 1e-06 \
  --ai-maxit 30 \
  --out <prefix>
```

---

## 五、输出解析

### 5.1 `.vars` — 方差组分

表头：`Item Var Var_SE h2 h2_SE h2_Pr(Chisq)`

```text
PA  0.42  0.04  0.42  0.03  1.0e-06
e   0.58  0.03  0.58  0.03  1.0e-06
```

> **致命陷阱：残差行 `Item = e` 的 h2 列是残差占比（0.58），不是遗传力。**
> 遗传力必须取**非残差组分行**（通常是 `PA`）的 h2 = 0.42。
>
> 解析器通过 `RESIDUAL_ITEM_TOKENS = {e, res, residual, residuals}` 显式跳过残差行。绝不能简单取"第一条含 h2 的记录"。

### 5.2 `.rand` — 随机效应解

表头：`ID PA residuals`。`PA` 列即个体加性遗传效应（EBV）。

### 5.3 `.beta` — 固定效应估计

表头：`Levels Estimation SE`。

### 5.4 `.log` — 运行日志

收敛标记：

```text
[Converged?] Yes!      → 已收敛
[Converged?] No!       → 未收敛
```

> **致命陷阱：`Yes!` 和 `No!` 两种情况的日志里都含有 "converged" 子串。**
> 用 `"converged" in log.lower()` 判断会把**未收敛误判为已收敛**——参考项目正是这么写的（`utils/hiblup.py:1073`），本架构已修正。
>
> 正确做法：正则 `\[\s*Converged\?\s*\]\s*([A-Za-z]+)` 取问号后的答复词。
> `Yes` → True，`No` → False，**无该标记 → None（未知，不是 False）**。

迭代轨迹：

```text
Alg.  Iter.  LogL.      V(PA)    V(e)
[AI]  1      -12.68   0.40  0.60
```

用于提取实际迭代次数与似然值变化。

---

## 六、必须记录的运行信息

```text
Task ID
HIBLUP executable path
HIBLUP version
Working directory
Parameter / input files（含校验和）
完整 Command
Start time / End time / 耗时
Exit code
stdout（完整）
stderr（完整）
Output files 清单
```

写入 `<prefix>_metadata.json` 与 `audit_<run_id>.jsonl`。

**stdout / stderr 必须完整保留**，不允许只存摘要。外部程序的原始输出是唯一可信证据。

---

## 七、输出契约

```yaml
outputs:
  hiblup_outputs: "<output_dir>/<task>_<trait>_<timestamp>{,_ad}.{vars,rand,beta,log}"
  parsed_result: "<prefix>_parsed.json"
    # variance_components / heritability / convergence / iteration_trace
  ebv_table: "<prefix>_ebv.csv"
  metadata: "<prefix>_metadata.json"
  stdout_log: "<prefix>_stdout.txt"
  stderr_log: "<prefix>_stderr.txt"

status: completed | partial | failed
```

**输出前缀强制带时间戳，永不覆盖既有结果。** 前缀已被占用时自动追加序号，而不是覆盖。

---

## 八、错误码

| 码 | 含义 | 处置 |
|----|------|------|
| E010 | HIBLUP 可执行文件未找到 | 中止，报出查找路径 |
| E011 | 表型文件不可读 | 中止 |
| E012 | 不支持的评估方法 | 中止 |
| E013 | 目标性状不在表型文件中 | 中止，列出可用列 |
| E014 | PBLUP 缺系谱 | 中止 |
| E022 | 固定效应单水平（兜底检出） | 剔除并告警 |
| E024 | HIBLUP 非零退出 | 中止，附完整 stderr 与命令行 |
| E025 | 输出文件缺失 | 中止，列出缺失文件 |

---

## 九、失败处置

CLI 非零退出、结果文件缺失、日志报错、未收敛 —— 任一情形**都不得直接进入正式报告**，须交由 Error Handler 与 Skill 08 Result Validator 处理。

未收敛时的建议处置顺序（须人工确认后才执行，不自动重试）：

1. 检查固定效应是否过度参数化（水平过多、样本过少）；
2. `vc_method` 由 `AI` 改为 `EMAI`（先 EM 稳定再 AI 加速）；
3. 放宽 `--tol` 或增加 `--ai-maxit`；
4. 简化模型。

**禁止静默重试。** 每次重试必须写 `record_retry`（阶段、第几次、触发原因、采取动作）。

---

## 十、与设计文档 14.1 的结构对应

设计文档建议将 HIBLUP wrapper 拆为 `wrapper/` + `parsers/` 六个子模块。当前实现集中在单文件 `tools/HIBLUP/hiblup_tool.py` 中，函数级对应关系如下：

| 文档建议模块 | 本实现对应函数 |
|-------------|---------------|
| `wrapper/command_builder.py` | `assemble_hiblup_command`、`build_per_trait_tokens`、`_append_relationship_args` |
| `wrapper/runner.py` | `run_hiblup`（经 Harness Tool Executor） |
| `wrapper/environment_check.py` | `resolve_hiblup_binary`、`check_hiblup_version` |
| `parsers/log_parser.py` | `parse_convergence_flag`、`_extract_iteration_trace`、`_extract_iteration_count` |
| `parsers/variance_parser.py` | `extract_variance_components`、`extract_heritability` |
| `parsers/ebv_parser.py` | `parse_hiblup_output`（`.rand` 段） |

功能等价，物理组织形式不同。是否拆分为独立文件待确认。

---

## 十一、禁止事项

1. 禁止 Agent 绕过 Tool Executor 直接起进程；
2. 禁止覆盖既有输出；
3. 禁止丢弃 stdout / stderr；
4. 禁止静默重试；
5. 禁止用子串匹配判断收敛；
6. 禁止把残差行的 h2 当作遗传力；
7. 禁止在输出后缀上写死 `_ad`。
