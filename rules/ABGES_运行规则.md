# ABGES 运行规则

> 版本：v1.0 ｜ 更新：2026-08-11 ｜ 阶段：Phase 0

## 1. 意图解析

用户触发后确认以下参数再运行，不得假设：

| 参数 | 可选值 | 默认 |
|------|--------|------|
| 品系 | 312 / F / B / H / SAMPLE_POPULATION / N631 … | 必填 |
| 性状 | BW / EW / EN / ER / ACL / MCL / FCR / CEN | 必填 |
| 模式 | single（恰好1性状，不标准化）/ multi（≥2性状，强制标准化，禁 `--no-standardize`） | single |
| 方法 | pblup / gblup / ssgblup | pblup |
| 固定效应 | Gen, Batch, Sex … | Gen, Batch |
| 衍生性状 | --acl / --fcr | 不计算 |
| 阶段 | data / analysis / full | full |

---

## 2. 环境检查

Python ≥3.8（强制 D 盘 venv）；HIBLUP config 路径指向项目内 exe，运行前 `verify_hiblup` 验证 ≥1.6.0；GBLUP/SSGBLUP 需 PLINK；TEMP/TMP 使用系统临时目录。依赖：pandas, openpyxl, numpy, scipy, chardet, pyyaml, xlrd, matplotlib, pypinyin。环境不满足时停止，不降级。

---

## 3. 数据发现

`data/<品系>/` 下自动发现：表型（`phenotype_raw.xlsx` → 含"表型/pheno"关键词）、系谱（含"系谱/pedigree"关键词）、连产明细（`计算连产产蛋数据/` 子目录）、基因型（`.bed/.bim/.fam` 前缀）。也可 config `input.*_file` 显式指定。详细读取规则见各处理规则文件。

---

## 4. dry-run 校验

实际运行前必须先 `--dry-run --json`，通过后才执行：

| 校验 | 码 | 处理 |
|------|----|------|
| 输入文件不存在 | E001 | 停止 |
| ID 列缺失 | E003 | 停止 |
| 请求性状列缺失 | E004 | 停止 |
| 列名重复 | E005 | 停止 |
| 重复键（ID+Gen） | E006 | 停止 |
| 性状列含非数值 | E012 | 停止 |
| multi + `--no-standardize` | E013 | 停止 |
| 固定效应仅一水平 | E022 | 警告，命令层省略该列 |
| ID 匹配率 <10% | — | 停止 |
| HIBLUP 版本不符 | — | 停止 |
| ID 匹配率 <95% | W001 | 警告 |

---

## 5. 长任务与中断恢复

预计 >5 分钟必须用 Job Runner（`--confirm-long-run`）。中断后先读 `status.json` + 日志 + checkpoint，从断点继续。历史结果不覆盖，新结果带时间戳。

---

## 6. 执行纪律

Agent 不直接用 pandas 改用户数据，统一调 CLI。原始数据永不修改。退出码 0 ≠ 成功——必须核验 `.vars`、收敛标志、预期产物。失败不得报告为成功。交付时强制输出 ✓完成 / ✗缺失 / ⚠风险。

## 定稿参数（依据 docs/ABGES_待确认参数总表.md）

- 品系 / 性状枚举：当前为示例清单，**以本规则 §1 示例清单作为初始白名单**；先生后续提供真实品系/性状后扩为完整枚举（总表 #32）。
- multi 模式强制标准化：**默认标准化；允许 config 特例关闭，但 EBV 必须注明尺度**（总表 #33）。
