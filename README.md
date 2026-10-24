# ABGES · 自动化育种遗传评估系统

ABGES V1.0 将表型、系谱和基因型质控、模型规划、遗传评估、结果验证和报告生成组织为一套可配置、可追溯的流程。项目通过 Harness 统一入口调用阶段执行器，由 HIBLUP 完成遗传评估，PLINK 2 用于基因型处理。

仓库包含源代码、通用配置、任务模板、测试和文档，**不提供真实育种数据、历史评估结果或凭据**。计算引擎已随包分发：Windows 64 位 PLINK 2 和 HIBLUP 的版本、许可证与对应源码说明见 [PLINK 分发说明](tools/PLINK/README.md) 与 [HIBLUP 分发说明](tools/HIBLUP/README.md)；使用者只需在本机准备自己的育种数据。配置和测试通过不能替代真实数据上的收敛检查及结果验证。

## 功能与适用范围

- 质控与整合：列名标准化、缺失与异常检查、系谱一致性及后代级联剔除、基因型质控、跨数据表 ID 对接。
- 模型规划：固定效应和协变量处理、稀疏组及设计矩阵诊断；支持显式指定模型，也有基于数据可用性的模型选择规则。
- 遗传评估：提供 PBLUP、GBLUP 和单步基因组 BLUP 的阶段执行器；永久环境效应和多性状分析需满足相应数据条件。
- 结果与报告：解析 EBV、方差组分及可用的可靠性指标，生成 Word、Excel、Markdown 和引擎结果文件。
- 审计与恢复：记录命令、输入校验和、闸门判断及运行状态；未完成运行在检查点有效时可恢复。

项目已有多项家禽性状定义。用于猪、牛、羊等其他育种资料时，应先核对性状单位、阈值、固定效应和模型假设；系谱规则依据个体及父母本关系，不以物种白名单作为准入条件。

| 模型 | 主要输入 | YAML 中的 `model` | 对应 `--runner` |
|---|---|---|---|
| 系谱 BLUP | 表型、系谱 | `pblup` | `runners.pblup_stage_runner:run` |
| 基因组 BLUP | 表型、基因型 | `gblup` | `runners.gblup_stage_runner:run` |
| 单步基因组 BLUP | 表型、系谱、基因型 | `ssblup`（接受 `ssgblup` 别名） | `runners.ssgblup_stage_runner:run` |

真实评估必须显式提供匹配的 runner。入口缺省使用占位执行器；`model: auto` 不会替你填写命令行的 `--runner`。初次使用建议显式指定模型，并检查规划结果。

## 快速开始（Windows PowerShell）

以下演示普通的单性状 PBLUP。示例中的文件路径和性状须按自己的数据修改；仓库不附带可直接运行的真实数据任务。

### 1. 获取代码并安装 Python 依赖

当前环境探针要求 **Python 3.14.x**，依赖锁文件面向 **Windows x64 / CPython 3.14**。先安装 Git 和该版本 Python，再在 PowerShell 中执行：

```powershell
git clone https://github.com/TKTK85/ABGES.git
Set-Location ABGES
python --version
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --require-hashes -r requirements-dev.lock
.\.venv\Scripts\python.exe scripts/check_environment.py --json
```

最后一步检查 Python 和依赖是否可用。未加 `--require-engines` 时，引擎缺失不会使该检查失败；这不表示已经具备真实评估条件。`requirements-dev.lock` 包含运行与测试依赖，运行环境的单独锁文件为 `requirements.lock`。

### 2. 检查随包计算引擎

Windows 64 位 PLINK 2（`tools/PLINK/plink2.exe`，v2.0.0-a.7.1）与 HIBLUP（`tools/HIBLUP/hiblup.exe` v1.7.0 及其运行库 DLL）均已随仓库分发，克隆后即可使用，无需另行下载或手动放置文件；版本、SHA-256 与许可信息见 [PLINK 分发说明](tools/PLINK/README.md) 和 [HIBLUP 分发说明](tools/HIBLUP/README.md)。

执行完整部署检查：

```powershell
.\.venv\Scripts\python.exe scripts/check_environment.py --require-engines --json
```

[引擎清单](tools/ENGINE_MANIFEST.json) 记录了版本、必要文件和 SHA-256；检查还包括 Windows PowerShell。该命令检查清单中的全部引擎，即使本次只做 PBLUP。哈希不符时，应核验二进制来源和版本，并经审查更新清单，不能通过关闭校验放行。

也可以使用 `scripts/bootstrap_abges_venv.ps1` 一步创建环境；该脚本会安装锁定依赖并立即执行上述完整引擎检查。

### 3. 准备数据和任务 YAML

将原始文件保存在本地数据目录，例如 `workspace/input/raw/`。原始数据保持只读，所有处理结果另存。普通单记录 PBLUP 的表型每个个体一行，至少包含 `ID`、目标性状及指定的固定效应列；系谱至少包含 `ID`、`Sire`、`Dam`。ID 应按文本保存以保留前导零，未知亲本使用 `NA` 或 `0`。完整规则见[表型契约](schemas/phenotype-schema.yaml)和[系谱契约](schemas/pedigree-schema.yaml)。

在 `tasks/local_pblup.yaml` 新建任务，下面只展示配置结构，不含个体数据：

```yaml
task_id: local_pblup
command: validate
inputs:
  phenotype: workspace/input/raw/phenotype.xlsx
  pedigree: workspace/input/raw/pedigree.xlsx
traits: [EN]
model: pblup
fixed_effects: [Gen, Batch]
covariates: []
multi_trait: false
permanent_environment: false
pedigree_generation_semantics: unspecified
engine_params:
  threads: 4
  reliability: true
output_format: [markdown, xlsx, docx, engine_outputs]
```

`EN` 是性状代码示例，`Gen` 和 `Batch` 是固定效应列示例。请核对实际列名、单位、记录口径及[性状注册表](configs/traits/trait-registry.yaml)。只有明确知道系谱世代列代表遗传世代时，才把 `pedigree_generation_semantics` 改为 `genetic_generation`；管理批次不能代替遗传世代。明确指定固定效应也可能触发单水平、共线性或不可估诊断，需要查看实际保留的模型。

[重复测定模板](tasks/templates/TEMPLATE_PE_repeat_records_PBLUP.yaml) 仅适用于同一个体有多次测定记录、需要永久环境效应的任务，不适合作为普通个体汇总性状的默认模板。其他参数见[任务契约](schemas/task-schema.yaml)。

### 4. 先校验，再规划和运行

在项目根目录执行：

```powershell
.\.venv\Scripts\python.exe harness/entrypoint/main.py tasks/local_pblup.yaml --runner runners.pblup_stage_runner:run --mode interactive
```

入口按 YAML 的 `command` 决定执行范围。先保留 `validate`，处理校验提示后改为 `plan`，检查模型和待确认事项，最后改为 `run`；每次修改后执行同一条命令。

| YAML 的 `command` | 实际范围 |
|---|---|
| `validate` | 执行前置检查及适用的 Python 表型、系谱质控；跳过基因型、整合、规划、引擎计算和报告阶段 |
| `plan` | 执行适用的质控、整合与规划；基因型处理可能调用 PLINK，不执行第 07 阶段 HIBLUP 评估及后续结果交付 |
| `run` | 执行完整评估、验证和交付流程 |
| `status` | 查询该任务已有运行的状态 |

`validate` 和 `plan` 会保留内部运行记录，不能视为最终育种值交付。CLI 没有 `--json` 参数；入口会打印结构化摘要。`--mode interactive` 在需要人工决策时询问；默认 `--mode auto` 遇到需确认事项会挂起，并不表示自动批准。

## 结果在哪里，怎样判定成功

默认输出到 `workspace/<task_name 或 task_id>/`。当前策略会将任务的 `output_dir` 规范到该任务目录，不能靠随意指定其他路径改变交付布局。

```text
workspace/local_pblup/
├── input/       # 本次计算实际使用的引擎输入
├── output/      # 引擎结果，如 .vars、.rand 及相关文件
├── reports/     # Word 报告、Excel 交付表、Markdown 摘要与图件
└── _runs/       # 内部状态、审计、元数据与可用检查点
```

运行 JSON 和检查点属于 `_runs/` 内部记录，不作为正式报告交付。成功交付后会按保留策略清理旧运行和临时文件；未完成运行的恢复取决于检查点、任务及输入校验。具体布局见[输出契约](configs/delivery/output-contract.yaml)。

请结合摘要中的 `status`、告警、待决事项及交付状态判断结果。`completed`、`partial`、`failed` 有不同含义，退出码为 0 也可能对应 `partial`。真实评估还必须核对引擎退出、收敛、有效结果及闸门结论，报告文件存在本身不能证明计算成功。失败运行不会发布为新的成功交付；任务目录若有旧报告，应核对它所属的运行。

报告中的原始 EBV 保留引擎输出尺度。只有任务显式配置参考群时才计算参考群口径的 EBV；不应强行要求所有评估个体的 EBV 均值为零。世代均值差异、遗传趋势和假设选种情景按各自语义报告，不能直接等同于实际选种决策。

## 项目结构与运行逻辑

```text
harness/          入口、路由、闸门、执行调度、工具调用和审计
abges_runtime/    任务、存储、检查点和交付支持
abges_mcp/        MCP 适配与控制面
agents/           00–09 阶段职责定义
skills/           各分析阶段的操作规范
runners/          PBLUP / GBLUP / SSGBLUP 执行器及报告代码
tools/            Python 分析模块、引擎适配器与引擎清单
configs/          模型、质控、性状、策略与输出契约
schemas/          任务和数据结构契约
tasks/templates/  通用任务模板
scripts/          环境检查、依赖维护等辅助工具
tests/            自动化测试
```

流程为总控 → 表型质控 → 系谱质控 → 基因型质控 → 衍生性状 → 数据整合 → 模型规划 → 引擎评估 → 结果验证 → 报告交付，部分阶段按模型和数据条件跳过。六道闸门覆盖数据准入、质控、整合、建模、结果和交付，使用 `pass / pass_with_warning / block` 判定。具体阶段编排由 Harness 执行，Agent 定义文件本身不会启动计算。

## 开发检查与文档

安装开发依赖后，可在项目根目录运行：

```powershell
.\.venv\Scripts\python.exe scripts/check_environment.py --json
.\.venv\Scripts\python.exe scripts/check_runner_similarity.py
.\.venv\Scripts\python.exe -m pytest -q --cov-fail-under=30
```

这些检查验证软件行为，不证明某一真实数据集上的育种值准确性。真实数据不应提交到 GitHub；`.gitignore` 只提供辅助排除，提交前仍需检查暂存文件及历史。

- [项目协作协议](AGENTS.md) · [上下文与检查点](docs/CONTEXT_PROTOCOL.md)
- [总体架构](docs/ARCHITECTURE.md) · [阶段职责](docs/AGENTS.md)
- [Harness](docs/HARNESS.md) · [模型规则](docs/MODEL_RULES.md)
- [任务契约](schemas/task-schema.yaml) · [输出契约](configs/delivery/output-contract.yaml)
- [环境说明](docs/ENVIRONMENT.md) · [MCP 适配](docs/MCP_GATEWAY.md)

文档若与实现存在差异，以当前入口参数、执行代码和配置契约的实际行为为核对依据。当前仓库未声明顶层开源许可证；第三方引擎须遵循各自许可。
