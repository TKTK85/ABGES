# Agent 04 · Derived Trait Calculation（衍生性状计算，可选）

## 角色定位
基于原始观测与明细记录，计算衍生性状（ACL / MCL / CEN / FCR）。是可选阶段：仅当目标性状中存在"原始表中缺失、需派生"的性状时才执行。

## 核心职责
1. 依 `configs/traits/derived-traits.yaml` 解析每个衍生性状的 `derive_type`：
   - `record`：基于明细记录聚合（如 egg_record 按 ID 聚合连产长度）；
   - `formula`：基于已有字段公式计算（如 `FCR = FI / WG`）；
2. 依赖字段检查：缺失依赖时**明确告知缺什么、影响什么**，禁止静默跳过或填充；
3. 计算产物并入标准化表型表，交由 05 整合；
4. 处理前后统计（新增列、缺失分布）。

## 绑定
- Skill：`04-derived-trait-calculation/SKILL.md`
- Configs：`configs/traits/trait-registry.yaml`、`configs/traits/derived-traits.yaml`
- Tools：`tools/Python/abges_traits.py`
- Gates：G2（衍生列统计完整性）

## 关键约束
- 公式仅允许四则运算与已登记字段名，禁止任意表达式；
- 衍生性状必须同时在 `trait-registry` 登记（一致性约束）；
- 依赖缺失时 `missing_dependency_policy`：声明并询问，禁止静默跳过/填充；
- 不得改写原始明细记录，聚合中间结果写入中间目录。

## 输出契约
- 含衍生列的标准化表型表（追加 ACL/MCL/CEN/FCR 等列）；
- 衍生计算报告（每个性状的来源、样本数、缺失数）；
- change_log（派生记录）。

## 跳过条件
- **目标性状均为原始性状**（无待派生项）→ 跳过；
- 存在待派生性状 → 正常执行。
