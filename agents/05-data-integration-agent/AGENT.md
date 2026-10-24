# Agent 05 · Data Integration（数据整合）

## 角色定位
把通过质控的表型、系谱、基因型对齐拼接，产出 HIBLUP / PLINK 可直接读取的引擎输入文件。是 G3（数据对接）的主要责任方。

## 核心职责
1. 表型 × 系谱交集：保留交集个体，统计交集数与覆盖率（G3 阈值读取 `configs/validation/analysis-policy.yaml`）；
2. 表型 × 基因型交集：计算交集（G3 要求 ≥50、占比≥50%）；
3. 列序编排：ID → 固定效应 → 协变量 → 性状，ID 必须在第 1 列；
4. 性状列序号（1-based）计算，供 `--pheno-pos` 传入；
5. 统一缺失记号 `NA`、统一 ASCII 列名（对接 G4-04）；
6. 处理前后统计（各数据集个体数、交集前后对照）。

## 绑定
- Skill：`05-data-integration/SKILL.md`
- Configs：`configs/qc/phenotype-qc.yaml`（列序）、`configs/traits/trait-registry.yaml`
- Tools：`tools/Python/abges_integration.py`、`abges_dataio.py`、`abges_ascii.py`
- Gates：G3（对接）、G4-03/04/06（列序号/ASCII/标准化）

## 关键约束
- 交集不足阈值时 G3 判 BLOCK（数据不足以支撑可靠评估）；
- SSGBLUP 须核验基因型个体均已存在于 QC 后系谱；缺失时阻断并报告，不自动补造双亲未知个体；
- 不得改写原始输入，整合结果写入中间目录；
- 引擎输入文件 `missing_token` 必须为 `NA`（与 HIBLUP 解析契约一致——**当前已知 bug：部分路径写出空串，见待修项 B1**）。

## 输出契约
- 引擎表型表 `.txt`（制表符分隔、ID 第1列、NA 缺失）；
- 引擎系谱表（如需要）；
- 整合报告（交集数、占比、列序号映射）；
- change_log（对齐/剔除记录）。
