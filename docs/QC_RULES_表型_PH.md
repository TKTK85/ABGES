# ABGES 表型清洗规则（PH001–PH010）

> 本文件由 `scripts/render_qc_rules_md.py` 从 yaml 自动生成，请勿手改；
> 修改请编辑 `configs/qc/*.yaml` 后重跑该脚本。

权威源：`configs/qc/phenotype-qc.yaml`。

固定效应默认列 `Gen/Batch/Sex`，协变量 `Age/BirthWt/AFE`；表头经 `header_map` 标准化（中文/异体→标准列名，大小写折叠）。

| 规则 | 名称 | 强度 | 说明 | 参数 |
|------|------|------|------|------|
| **PH001** | 必备列存在性检查（个体号与目标性状列） | 🔴 阻断 | 标准化后数据须同时具备个体标识列（ID，翅号/个体号）与本次分析请求的全部目标性状列， 二者分别为表型记录同系谱、基因型数据对接及遗传评估建模的前提。 任一缺失即整体阻断，并列出缺失列清单。 | — |
| **PH002** | 个体号缺失与重复记录处理 | 剔除 | 个体号缺失（空值、NA、纯空白）的记录无法参与个体间配对，直接剔除并记入 change_log； 同一个体号出现多条记录时按 duplicate_policy 处理：flag 仅标记不删， deduplicate 则按 keep 策略去重并记录 change_log。父母信息冲突的重复不自动择一， 升级人工确认。 | duplicate_policy=flag, keep=first |
| **PH003** | 性状数值化转码 | ⚠️ 告警 | 目标性状与协变量列须为数值型度量。发现无法转换为数值的取值（如混入的 文本、全角数字、单位残留）时，仅提醒并保留原值清单交由人工处置，**禁止 任何模块私自将其置换为 NA**；本规则触发即阻断后续流程，待人工清洗后 再行评估，不得静默放行。 | — |
| **PH004** | 缺失值保持策略 | 保留 | 性状及固定效应、协变量的缺失观测一律保持 NA，严禁任何无统计学依据的填补 （含前向填充、均值填补）；含 NA 记录由 HIBLUP 在混合模型方程组装配时自动剔除。 | forbid_imputation=True |
| **PH005** | 固定效应有效水平检查 | ⚠️ 告警 | 固定效应列（世代、批次、性别等）的有效水平数 < 2 时该效应在设计矩阵中不可估， 命令组装层将省略该列；省略必须在交付说明中显式声明，禁止静默忽略。 | min_levels=2 |
| **PH006** | 分组异常值剔除 | 剔除 | 在 Gen×Batch（世代×批次）分组内，按组均值 ± threshold 倍标准差识别并剔除 离群表型值；组内样本量低于 min_group_size 时统计量不可靠，该组不执行剔除。 性状被任务声明为"已按同一分组标准化"时，依 skip_when_pre_standardized 跳过二次剔除并记录，避免在标准化尺度上过度剔除。 | threshold=3.0, min_group_size=5, group_by=['Gen', 'Batch'], single_pass=True, skip_when_pre_standardized=True |
| **PH007** | 标准化 | 🔴 阻断 | 单性状分析模式必须恰好指定 1 个性状，默认不做标准化（EBV 保持原始观测尺度）； 多性状分析模式必须至少 2 个性状，同样不做标准化，各性状保持原始量纲， 以保证方差组分与育种值估计同原始尺度结果可比（h² 与遗传相关为尺度无关量，不受影响）。 【2026-10-12 修订，先生指令】原口径为"多性状强制 z-score 标准化，禁止关闭"， 现改为多性状默认不标准化；量纲差异极大的性状组合（如体重与产蛋数同模） 若出现收敛困难，可经 human-in-the-loop 将 multi_force_standardize 改回 true 恢复强制标准化，改动须记入 change_log。 | single_default_standardize=False, multi_force_standardize=False |
| **PH008** | 数值列重复/错位检测 | ⚠️ 告警 | 对所有数值型列两两计算 Pearson 相关系数与平均绝对差。若 |r| 大于等于 corr_threshold 且 mean(abs_diff) 小于等于 abs_diff_threshold，判定为两列在数值上完全相同 （典型场景：Excel 复制粘贴列、导出工具重复写出同一指标），会导致下游相关性矩阵 出现 r=1.000 的伪相关、模型设计矩阵共线。本规则仅告警并列出重复列对，不自动删除。 | corr_threshold=0.999, abs_diff_threshold=1e-6, skip_columns=['ID', 'Sire', 'Dam'] |
| **PH009** | 派生比率恒等检测 | ⚠️ 告警 | 对列名匹配比率类模式（含 率 / pct / ratio / rate 或单位为百分号）的列，检查其标准差。 若 std 小于等于 std_threshold（该列在全群几乎恒为同一值），判定为派生比率恒等—— 典型 bug 是屠体重=活体重导致屠宰率恒=100%。本规则仅告警，不自动修改。 | std_threshold=0.01, ratio_patterns=['率', 'pct', 'Pct', 'PCT', 'ratio', 'Ratio', 'RATIO', 'rate', 'Rate', 'RATE', '%'] |
| **PH010** | 分类列水平编码一致性 | ⚠️ 告警 | 对固定效应/分类列（Sex/Gen/Batch/House/Row 等），检测同一语义是否存在多种写法。 例如 Sex 列同时出现 母/雌/F/female，会导致同一水平被拆成多个 dummy 变量。 本规则基于内置同义词组检测，列出可疑水平值清单，不自动合并。 | synonym_groups={'sex_female': ['母', '雌', 'F', 'f', 'female', 'Female', 'FEMALE', '母鸡'], 'sex_male': ['公', '雄', 'M', 'm', 'male', 'Male', 'MALE', '公鸡'], True: ['是', 'Y', 'y', 'yes', 'Yes', 'YES', '1', 'TRUE', 'true', 'True'], False: ['否', 'N', 'n', 'no', 'No', 'NO', '0', 'FALSE', 'false', 'False']} |
