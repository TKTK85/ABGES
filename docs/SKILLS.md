# ABGES Skills（技能总览）

> 版本：1.0.0 ｜ 9 个 Skill 是各 Agent 的领域实现指引，详细定义见 `skills/<id>-<name>/SKILL.md`。

## 技能一览

| ID | Skill | 归属 Agent | 核心实现 |
|----|-------|-----------|----------|
| 01 | phenotype-qc | 01-phenotype-agent | `abges_phenotype.py` / `abges_dataio.py` / `abges_ascii.py` |
| 02 | pedigree-qc | 02-pedigree-agent | `abges_pedigree.py` |
| 03 | genotype-qc | 03-genotype-agent | `plink_tool.py` |
| 04 | derived-trait-calculation | 04-derived-trait-agent | `abges_traits.py` |
| 05 | data-integration | 05-data-integration-agent | `abges_integration.py` / `abges_dataio.py` / `abges_ascii.py` |
| 06 | geneval-planning | 06-geneval-planner-agent | `abges_core.py` / `hiblup_tool.py` |
| 07 | geneval-execution | 07-geneval-runner-agent | `hiblup_tool.py`（经 tool-executor） |
| 08 | geneval-validation | 08-result-validator-agent | `abges_validate.py` / `hiblup_tool.py` |
| 09 | geneval-reporting | 09-report-agent | `abges_core.py` / `abges_ascii.py` |

## 各 Skill 职责边界

### 01 phenotype-qc
把原始表型变成 HIBLUP 可读、可追溯的分析表。负责表头标准化、ID 归一、性状数值化、分组异常值剔除（PH006 单轮 ±3SD）、固定效应有效水平检查、处理前后统计、change_log。不负责衍生性状（属 04）、对接（属 05）、建模（属 06/07）。**禁止缺失值填补、禁止迭代式剔除、禁止猜列名。**

### 02 pedigree-qc
系谱结构校验、ID 命名空间一致性、完整性、环检测、补全（copy_then_modify）。环检测命中 → G2 阻断。

### 03 genotype-qc
PLINK 驱动的基因型质控（mind→geno→maf→hwe），位点/交集统计，SSGBLUP 系谱-基因型对齐。需另行提供 `plink2.exe`。

### 04 derived-trait-calculation
依 `derived-traits.yaml` 计算 ACL/MCL/CEN/FCR（record 聚合 / formula 公式）。依赖缺失时声明并询问，禁止静默跳过/填充。

### 05 data-integration
表型×系谱×基因型对齐，产出引擎输入（ID 第1列、NA 缺失、ASCII 列名、列序号映射）。交集不足 → G3 阻断。

### 06 geneval-planning
模型落地（auto 走 MS001–MS005，显式不满足则拒绝）、效应筛选（水平<2 由 E022 剔除）、标准化策略（单不标准化/多强制 z-score）、HIBLUP 命令骨架。

### 07 geneval-execution
经 `tool-executor` 调 HIBLUP/PLINK，监控退出码/超时/线程，收集四件输出（.vars/.rand/.beta/.log）。不做静默重试。

### 08 geneval-validation
G5 验收：退出码 0、四件齐全、收敛、h²∈[0,1]、残差>0、SE÷h²≤0.5、EBV 数一致、基线对照。未验过不得声称 completed。

### 09 geneval-reporting
G6 交付：双通道（json + 中文）、还原中文标签、元数据/统计/告警汇总、诚实交付（禁模糊措辞、三态、G6-04/05/06 永不放宽）。

## Skill 与 Agent 的契约

每个 Skill 的 SKILL.md 含：元信息、职责边界（负责/不负责）、输入契约、执行步骤、规则要点、统计强制项、变更记录、闸门对接、输出契约、错误码、人工确认触发点、禁止事项。所有正式阈值来自 `configs/`，Skill 不另立标准。
