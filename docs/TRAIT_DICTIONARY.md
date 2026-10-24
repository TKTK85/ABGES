# ABGES 性状字典（TRAIT_DICTIONARY）

> 版本：1.0.0 ｜ 性状定义以 `configs/traits/trait-registry.yaml` + `derived-traits.yaml` 为唯一权威。Agent 不得临时发明性状或公式。

## 1. 标识符与系谱字段

| trait_id | 中文 | 角色 | 说明 |
|----------|------|------|------|
| ID | 个体号 | id | 翅号，前导零必须保留 |
| Sire | 父本号 | pedigree | 双亲 NA = founder |
| Dam | 母本号 | pedigree | 双亲 NA = founder |

## 2. 固定效应候选

| trait_id | 中文 | 离散 | 备注 |
|----------|------|------|------|
| Gen | 世代 | 是 | 水平<2 由 E022 剔除 |
| Batch | 批次 | 是 | 水平<2 由 E022 剔除 |
| Sex | 性别 | 是 | 允许值 M/F/U（公/母/未知） |
| House | 栋舍 | 是 | |
| Row | 笼排 | 是 | |

## 3. 协变量候选（连续）

| trait_id | 中文 | 单位 | value_range |
|----------|------|------|-------------|
| Age | 测定日龄 | d | [0, 800] |
| BirthWt | 初生重 | g | [10, 120] |
| AFE | 开产日龄 | d | [90, 300] |

## 4. 目标性状（raw）

| trait_id | 中文 | 单位 | value_range | 适用性别 |
|----------|------|------|-------------|----------|
| BW | 体重 | g | [100, 8000] | all |
| EW | 蛋重 | g | [20, 90] | F |
| EN | 产蛋数 | 枚 | [0, 400] | F |
| ER | 产蛋率 | % | [0, 100] | F |
| FI | 采食量 | g | [0, 20000] | all |
| WG | 增重 | g | [0, 8000] | all |

## 5. 目标性状（derived）

计算规则见 `derived-traits.yaml`。

| trait_id | 中文 | 单位 | 来源 | value_range |
|----------|------|------|------|-------------|
| ACL | 平均连产长度 | 枚/连产 | egg_record 聚合 | [1, 100] |
| MCL | 最大连产长度 | 枚 | egg_record 聚合 | [1, 200] |
| CEN | 累计产蛋数 | 枚 | egg_record 累计 | [0, 400] |
| FCR | 料重比 | g/g | formula: FI/WG | [0.5, 10] |

## 6. 字段规范（trait-schema）

每条性状条目具备：`code`（ASCII，正则 `^[A-Za-z][A-Za-z0-9_]*$`）、`name_cn`、`category`（growth/production/...）、`data_type`（continuous/count/binary/ordinal）、`source`（raw/derived）、`unit`、`aliases`、`valid_range`、`h2_reference`、`default_fixed_effects`、`default_covariates`、`repeated_measure`。

衍生性状附加：`derive_type`（record/formula）、`depends_on`、公式/聚合规则；**依赖缺失时声明并询问，禁止静默跳过/填充**。

## 7. 一致性约束

- trait-registry 中每个 code 在表型文件最多对应一列；
- derived-traits 中定义的性状必须同时在 trait-registry 登记；
- aliases 全表范围不得重复；
- valid_range 下界 < 上界；
- h2_reference ∈ [0, 1]。
