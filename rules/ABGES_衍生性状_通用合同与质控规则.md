# ABGES 衍生性状规则 · 通用合同与质控

> 版本：v1.0 ｜ 更新：2026-08-11 ｜ 配套：ACL/MCL 规则、FCR/CEN 规则
> 覆盖：跨性状共用合同、异常值标准化、CLI、输出审计、错误码汇总、测试要求。

## 一、共用执行合同（摘要）

1. 用户须明确启用性状；2. 输入先表头与 ID 标准化；3. 结果只按 `ID` 合并禁按行号；4. 不改原始数据；5. 记录输入目录 / 文件数 / 有效数 / 异常数 / 匹配率 / 参数；6. 捡蛋与表型 ID 重叠率 <10% 终止；7. 衍生性状在 Step7 复用同一异常值与标准化 PH007；8. 单性状保留原始尺度，多性状强制 z-score；9. 无法计算输出缺失 / 空，禁伪造 0（仅业务明确允许才输出 0）。

## 二、与异常值和标准化的关系

衍生列合并后进入 Step7：
- 默认按 `Batch×Gen` 做 均值 ± 3σ 异常值处理；
- 分组或有效数 <5 时跳过；
- single 默认不标准化（保留 ACL/MCL 天数、FCR 比值、CEN 枚数）；
- multi 强制 z-score；
- 统计量写入 `<品系>_std_stats.json`；
- 标准化 EBV 须说明尺度并按组统计量解释。

Step7 对所有已识别有效性状执行，任一性状异常可致整行删除。

## 三、CLI 示例

```powershell
python index.py --strain A362 --phase data --acl
python index.py --strain A362 --phase data --acl --fcr
python index.py --strain A362 --phase data --cen --target-day-age 300
python index.py --strain A362 --phase data --cen --target-week-age 50
# 多性状 ACL+CEN（强制标准化）
python index.py --strain A362 --phase full --mode multi --traits ACL,CEN --acl --cen --target-day-age 300 --fixed-effects "Gen,Batch;Gen,Batch"
```
推荐先 `--dry-run --json` 同参校验。

## 四、输出与审计

衍生列并入 `projects/<品系>/input/<品系>_pheno.txt`；标准化时统计量写入 `<品系>_std_stats.json`。
日志记：启用项、读取目录、异常日、ACL/MCL 个体数与均值、FCR 有效 / 错误数、CEN 目标 / 均值 / 观察不足数、ID 匹配率、合并行数、Step7 状态。流水线汇总标记 ACL/FCR/CEN 是否执行；表型质控报告含衍生描述统计与异常淘汰记录。

## 五、错误与阻断（汇总）

| 问题 | 行为 |
|------|------|
| 捡蛋目录不存在(ACL) | 警告，不生成 |
| 捡蛋目录不存在(CEN) | FileNotFoundError 阻断 |
| 无出生日期来源 | ValueError 阻断 |
| CEN 目标缺失 / 双给 / 非正 | E013/ValueError 阻断 |
| ID 匹配率 <10% | ValueError 阻断合并 |
| FI/WG 列缺失 | 警告，FCR 不生成 |
| FI/WG 非正 | FCR 缺失计数量 |
| 日期无法解析 | 删除；全无效则空结果 |
| 一天 ≥3 条(ACL) | 该日删除并警告 |

不得把空结果当性状值 0，日志有错不得报衍生性状完成。

## 六、开发与测试要求

新增 / 修改前须定义：生物学含义、输入与单位、时间窗、缺失 / 零 / 负 / 重复规则、ID 合并、输出类型、标准化策略、日志 / 错误码、手算样例、自动化测试。
现有测试至少覆盖：中 / 西 / 紧凑日期；ACL 双记录前移、前日已有、≥3 异常；单枚 / 多个体 / 无有效记录；FCR 有效 / 零 / 负 / 缺失；CEN 日龄 / 周龄 / 上取整；出生日期回退与观察不足；CEN 两目标互斥与正数；目标内 0 枚；ID 低匹配率。

## 七、当前实现与后续项

已实现：多 CSV / 编码 / 灵活日期；ACL/MCL 校正与连产段；FCR 有效性；CEN 目标互斥 / 出生回退 / 年龄 / 观察不足；ID 安全线与左连；衍生进入异常 / 标准化 / 报告 / HIBLUP 输入。
待完善：每性状独立 QC JSON；稳定错误码；CEN 可选"按记录计蛋 / 按日封顶"；捡蛋文件 Schema；单位与时间窗强校验；观察不足结构化逐个体清单。

## 八、定稿参数（依据 docs/ABGES_待确认参数总表.md）

- 异常值 3σ 阈值与 `Batch×Gen` 分组：**全项目统一口径**，与表型处理规则完全一致（同总表 #1–#6）。
- 标准化统计量：**随产物一并交付评审**（写入 `<品系>_std_stats.json` 并纳入质控报告）。
