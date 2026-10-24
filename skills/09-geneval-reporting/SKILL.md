# SKILL 09 · Geneval Reporting

报告生成技能。负责把整条流程的处理过程与结果，整理成一份**先生能直接看懂、并且能据以做育种决策**的正式报告。

报告的第一要求不是漂亮，是**诚实且可核查**：每一个数字都要能追溯到某个输出文件，每一处告警都要出现在报告里。

---

## 一、元信息

| 项目 | 内容 |
|------|------|
| skill_id | `09-geneval-reporting` |
| 版本 | 1.0.0 |
| 归属 Agent | `09-report-agent` |
| 依赖模板 | `templates/geneval-report/`、`templates/qc-report/`、`templates/validation-report/` |
| 实现工具 | `tools/Python/abges_validate.py`（结果汇总、排名）；`runners/docx_report.py`（Word 公文结果报告渲染，PBLUP/GBLUP/SSGBLUP 共用） |
| 对接闸门 | **G6（交付）** —— 本技能是该闸门的责任方 |

---

## 二、报告结构（16 节）

| # | 章节 | 数据来源 |
|---|------|---------|
| 1 | 任务信息 | task-schema、metadata |
| 2 | 输入数据概况 | 各源 describe_input_file（含校验和） |
| 3 | 表型 QC | Skill 01 qc_report + change_log |
| 4 | 系谱 QC | Skill 02 qc_report + inbreeding |
| 5 | 基因型 QC | Skill 03 qc_report（PBLUP 时标注 SKIPPED） |
| 6 | 数据处理前后比较 | 各 statistics.json 的 before/after/delta |
| 7 | 衍生性状说明 | Skill 04 calc_report（无则省略） |
| 8 | 数据整合结果 | Skill 05 integration_report |
| 9 | 遗传评估模型 | Skill 06 analysis_plan |
| 10 | HIBLUP 运行情况 | Skill 07 metadata（命令行、版本、耗时、退出码） |
| 11 | 方差组分与遗传力 | `.vars` 解析结果 |
| 12 | EBV / GEBV | `.rand` 解析结果 |
| 13 | 排名与候选个体 | Top 5% / Top 10% |
| 14 | 结果验证 | Skill 08 validation_report |
| 15 | 人工确认记录 | 全流程 human_confirmations |
| 16 | 最终结论 | 综合判定 |

跳过的节点（如 PBLUP 下的基因型 QC）**必须显式标注 SKIPPED 及原因**，不能整节消失——读者需要知道这一步是"没做"还是"不需要做"。

---

## 三、关键呈现要求

### 3.1 方差组分与遗传力

> 下列数值仅为合成演示值，不来自实际育种数据。

必须同时给出估计值与标准误，并**明确标注遗传力取自哪一组分**：

```text
加性遗传方差 V(PA) = 0.42 ± 0.047057
残差方差     V(e)  = 0.58 ± 0.036094
遗传力       h²    = 0.42 ± 0.03   （取自 PA 组分行）
表型方差     V(P)  = V(PA) + V(e) = 1.0
```

不得只报一个孤零零的 h² 数字。标准误决定了这个估计值可信到什么程度。

### 3.2 EBV 与排名

- EBV 分布：n、均值、标准差、最小值、最大值；
- 排名表：按 EBV 降序，含 ID、EBV、可靠性（若有 `--r2`）；
- Top 5% / Top 10% 候选个体清单。

**排名表的 ID 必须还原为中文标签**（若前处理做过 ASCII 转码），用 Skill 01 保存的映射表还原。先生看到的应该是"第3批"，不是"Di3Pi"。

### 3.3 告警汇总（G6-03）

流程中所有 `pass_with_warning` 必须汇总成一节，逐条列出：

```text
[G5-06] SE/h² = 0.095，估计稳定    ✓
[G3-02] 覆盖率 0.78 < 0.80         ⚠ 22% 表型个体不在系谱中，这部分个体 EBV 可靠性低
[E022]  固定效应 Sex 单水平已剔除    ⚠ 本次分析未校正性别效应
```

漏掉任一告警 → **G6-03 阻断**。

### 3.4 最终结论

必须包含三部分：

1. **状态**：`completed` / `partial` / `failed`（三选一，禁止模糊表述如"基本完成"）；
2. **可用性判断**：结果能否用于育种决策，若为 `partial` 须说明哪部分可用、哪部分不可用；
3. **剩余风险**：未解决的问题、未验证的环节、需要后续跟进的事项。

---

## 四、G6 交付闸门（本技能责任）

| 检查 | 内容 | 失败 |
|------|------|------|
| G6-01 | `*_metadata.json` 已生成且含完整命令行 | block |
| G6-02 | 处理前后统计已写入 `*_statistics.json` | block |
| G6-03 | 所有 pass_with_warning 已汇总进报告 | block |
| G6-04 | 交付状态取值属于 [completed, partial, failed] | block |
| G6-05 | 声明 completed 时 G5 全部为 pass | block |
| G6-06 | 原始输入文件未被修改（校验和一致） | block |

### G6-04 / G6-05 / G6-06 永不可放宽

`override_policy.never_overridable: [G6-04, G6-05, G6-06]`。

即便先生本人要求，这三项也不能绕过——它们是"诚实交付"与"原始数据只读"两条铁律的最后防线。可以放宽阈值类闸门，但不能放宽"如实说话"。

### G6-06 校验和比对

流程开始时记录每个输入文件的校验和，交付前重新计算比对。不一致说明原始数据在流程中被修改过，直接阻断并报出被改动的文件。

---

## 五、输入契约

```yaml
input:
  task_meta: dict
  qc_reports: {phenotype, pedigree, genotype}
  statistics: [dict]
  change_logs: [path]
  derived_report: dict|null
  integration_report: dict
  analysis_plan: dict
  execution_metadata: dict
  validation_report: dict
  human_confirmations: [dict]
  label_mapping: dict|null      # ASCII → 中文 还原映射
```

---

## 六、输出契约

```yaml
outputs:
  report_md:   "<output_dir>/<task>_geneval_report_<timestamp>.md"
  report_docx: "<prefix>_报告.docx"             # 面向育种专家的公文格式 Word 结果报告（软依赖，失败降级不阻断）
  report_json: "<prefix>_report.json"          # 结构化，供程序消费
  ebv_ranking: "<prefix>_ebv_ranking.csv"
  top_candidates: "<prefix>_top_candidates.csv"

status: completed | partial | failed
```

文件名规范：使用日期与用途，**禁止"最终版""final""最新版"等模糊命名**。

### 6.1 Word 公文结果报告（.docx）定稿规范

- **产出与降级**：每个任务在 09 阶段由 `runners/docx_report.py:render_run_docx` 自动生成 `<prefix>_报告.docx`，与 Markdown 报告并列；属软依赖，渲染异常仅记 `DOCX_REPORT_FAILED` 告警并保留 Markdown，**不阻断遗传评估主流程**；成功后登记为产物 `delivery_docx_report` 并计入 G6-03。
- **页面结构（固定顺序）**：① 封面单独第 1 页且不打页码——技能标识 → 主标题“遗传评估结果报告” → 任务名 → “项目/内容”信息表（任务编号、任务名称、目标性状、评估模型、报告生成时间、ABGES 版本、计算引擎、原始数据校验）→ 落款；② 第 2 页为“目录 + 主要符号与缩写”同页；③ 正文自第 3 页起。
- **目录直接生成**：保存时调用本机 Word 计算真实页码并把目录固化为静态文本（层级缩进、点前导、右对齐真实页码），任意查看器（含手机端、WPS）打开即见，**无需手动 F9**；非 Windows 或未安装 Word 时静默降级为“打开自动更新域”。
- **按模型动态输出**：评估模型名称、关系矩阵描述、系谱/基因型相关章节均依实际模型（PBLUP / GBLUP / SSGBLUP，取自 `metadata.model_id`）动态渲染；模型不涉及的环节（如 PBLUP 无基因型、GBLUP 无系谱近交）不渲染空表、不写死。
- **公文排版**：正文文字一律纯黑 RGB(0,0,0)；表格黑白（表头浅灰 D9D9D9、黑色细框、白底黑字），插图用 NPG 彩色；中文标题宋体、节标题黑体、小节楷体、正文仿宋_GB2312、表格宋体，西文与数字一律 Times New Roman。
- **内容铁律沿用全篇**：实际跑什么模型就输出什么，数字全部可溯源、不出空表；“表 1 核心评估结论一览”固定排在四点结论之后。

---

## 七、错误码

| 码 | 含义 |
|----|------|
| E080 | 缺少必需的上游报告 |
| E081 | 告警未完整汇总（G6-03） |
| E082 | 状态声明与 G5 结果矛盾（G6-05） |
| E083 | 原始文件校验和不一致（G6-06） |

---

## 八、禁止事项

1. 禁止使用模糊状态表述（"基本完成""大体没问题"）；
2. 禁止在存在告警时声明 `completed`；
3. 禁止遗漏告警；
4. 禁止让跳过的节点在报告中整节消失；
5. 禁止只报遗传力不报标准误；
6. 禁止在排名表中直接输出 ASCII 转码后的机器标签；
7. 禁止使用"最终版"类模糊文件名。
