# ABGES 命令组装规则

> 版本：v1.0 ｜ 更新：2026-08-11 ｜ 阶段：Phase 2

## 1. 方法选择

| 方法 | 必需输入 | 关系矩阵 |
|------|----------|----------|
| PBLUP | 表型+系谱 | A（系谱构建） |
| GBLUP | 表型+基因型 | G（`--bfile`/`--xrm`） |
| SSGBLUP | 表型+系谱+基因型 | H（A+G 融合） |

---

## 2. 命令结构

```bash
hiblup --single-trait|--multi-trait|--mme \
  --pheno <path> --pheno-pos <列号> \
  [--dcovar <离散固定效应>] [--qcovar <连续协变量>] \
  [--rand <随机效应>] [--rand-gxe <环境列:矩阵前缀>] \
  --pedigree <path> | --bfile <prefix> | --xrm <prefix> \
  --add [--dom] --threads <N> \
  [--vc-method AI|EM|EMAI|HI|HE] [--tol 1e-6] \
  [--ai-maxit 30] [--em-maxit 200] [--vc-prior <初值>] \
  [--r2] [--pcg] [--all-levels] --out <prefix>
```

列号从 `.phe` 表头 1-based 位置确定。效应列空时填 `0`。仅一水平的离散固定效应不可估（E022），自动省略。

---

## 3. 输入编码（强制）

固定效应/协变量的**列名与水平值必须 ASCII**。HIBLUP v1.7.0 Windows 写 `.rand` 时用列名构造标签，中文触发 0xC0000409 崩溃（REML 收敛但 `.rand` 缺失）。`sanitize_pheno_for_hiblup()` 自动执行：常见中文→语义短名（栋舍→House）；其他非 ASCII→pypinyin 拼音；未装→占位符 FE1。原始表型不改；映射写日志+metadata；临时文件运行后删除。纯英文表头原样放行。

---

## 4. REML 算法

| 算法 | 迭代参数 | 场景 |
|------|----------|------|
| AI（默认） | `--ai-maxit 30` | 单性状常规 |
| EMAI | `--ai-maxit 30 --em-maxit 200` | 多性状/边界方差 |
| EM | `--em-maxit 200` | 诊断兜底 |
| HI | `--ai-maxit 30` | 初值困难 |
| HE | 无迭代 | 快速近似 |

tol 默认 1e-6。

---

## 5. MME 与高级参数

`--mme` 使用给定方差组分直接求解（跳过 REML），必须同时传 `--vc-prior`，与 `--single/multi-trait` 互斥。`--dom` 仅 GBLUP/SSGBLUP+`--bfile`。`--rand-gxe` 启动前校验环境列和 XRM 文件。三项在真实数据验证前标记部分实现。

### 5.1 --xrm 命名约定（O9，2026-08-24 实测修订）

使用预构建 GRM 经 `--xrm <prefix>` 复用时，**前缀必须包含关系矩阵类型名**，裸前缀会导致 HIBLUP 报错或 `.rand` 列名不可解析。

| 矩阵类型 | 正确前缀示例 | 错误（裸前缀） | 输出 .vars Item |
|---|---|---|---|
| 加性 G | `strain_GA` / `demo.GA` | `strain` / `demo` | `前缀.GA` |
| 显性 D | `strain_GD` | `strain` | `前缀.GD` |
| 加性+显性 | `strain_GAD` | `strain` | `前缀.GA`, `前缀.GD` |
| SSGBLUP H | `strain_HA` | `strain` | `前缀.HA` |

**规则**：
1. `--xrm` 前缀 = `<品系/数据集标识>_<矩阵类型码>`，类型码取 `GA/GD/GAD/HA/HD/HAD`；
2. 预构建 GRM 文件必须成对存在：`<prefix>.ginv`（逆矩阵）或 `<prefix>.grm`（原始矩阵），HIBLUP 自动识别；
3. `assemble_hiblup_command(external_grm=...)` 传入的路径在内部会被 `_relativize_for_engine` 处理为无前缀路径，调用方须确保文件名本身符合上述命名；
4. O16 `extract_heritability` 按 `Item.endswith(".PA"/".GA"/".HA")` 匹配，命名不合规会导致遗传力提取回退到"第一条非残差行"，存在误取 PE 行风险。

---

## 6. 调用

子进程调用，设 `OMP/MKL_NUM_THREADS`。超时 3600s，超时 `taskkill /T /F`（Windows）。支持心跳回调和取消信号。

---

## 7. 输出

`.vars`（方差+遗传力）、`.rand`（EBV）、`.beta`（固定效应）、`.anova`（F 检验）、`.covars`（遗传相关）、`.log`（日志）。GBLUP/SSGBLUP 带后缀 `_ad`。

## 定稿参数（依据 docs/ABGES_待确认参数总表.md）

- REML 容差 `tol=1e-6` 与迭代上限：**全局统一**（AI=30、EM=200、tol=1e-6），不按性状/数据量调整；特例可由 config 覆盖。
- `--mme` / `--dom` / `--rand-gxe`：**默认关闭**；需显式开启且经真实数据验证后才标为可用（当前均 🟡 partial，实机仅 FileS3 小样本且缺 EBV）。
