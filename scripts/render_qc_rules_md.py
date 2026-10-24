# -*- coding: utf-8 -*-
"""
ABGES QC 规则 yaml -> md 对照表生成器
====================================
唯一权威源是 configs/qc/*.yaml（程序直接读取）。本脚本把它渲染成
人类可读的 Markdown 对照表，便于审阅/批注。yaml 改动后重跑本脚本即可同步。

用法：
  python -S scripts/render_qc_rules_md.py
输出：
  docs/QC_RULES_表型_PH.md / QC_RULES_系谱_PD.md / QC_RULES_基因型_GT.md
"""
import sys, os
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
QC_DIR = os.path.join(ROOT, "configs", "qc")
DOCS_DIR = os.path.join(ROOT, "docs")

# 动作/级别的中文标注与强度
ACTION_LABEL = {
    "BLOCK": "🔴 阻断", "EXCLUDE": "剔除", "FLAG": "标记", "RECODE": "转码",
    "KEEP": "保留", "WARNING": "⚠️ 告警", "MODIFY": "修改",
    "DEDUPLICATE": "去重", "HUMAN_CONFIRMATION_REQUIRED": "🟡 人工确认",
}
LEVEL_LABEL = {"error": "🔴 阻断", "warning": "⚠️ 告警", "info": "ℹ️ 记录"}


def _fmt_value(v):
    if isinstance(v, list):
        return ", ".join(str(x) for x in v)
    if isinstance(v, dict):
        return ", ".join("%s=%s" % (k, x) for k, x in v.items())
    if isinstance(v, bool):
        return "是" if v else "否"
    return str(v)


def _clean(text):
    return " ".join(str(text).split())


def render_phenotype(cfg):
    rules = cfg.get("rules", {})
    rows = []
    for rid, r in rules.items():
        action = ACTION_LABEL.get(r.get("action", ""), r.get("action", ""))
        # 收集除通用键外的参数（merged_from 溯源仅存 yaml，不渲染进表格）
        params = {k: v for k, v in r.items()
                  if k not in ("name", "action", "description", "code", "enabled",
                               "merged_from")}
        rows.append((rid, r.get("name", ""), action, _clean(r.get("description", "")),
                     _fmt_value(params) if params else "—"))
    title = "表型清洗规则（PH001–PH%03d）" % len(rules)
    intro = ("权威源：`configs/qc/phenotype-qc.yaml`。\n\n"
             "固定效应默认列 `Gen/Batch/Sex`，协变量 `Age/BirthWt/AFE`；"
             "表头经 `header_map` 标准化（中文/异体→标准列名，大小写折叠）。")
    return title, intro, rows


def render_pedigree(cfg):
    rules = cfg.get("rules", {})
    rows = []
    for rid, r in rules.items():
        action = ACTION_LABEL.get(r.get("action", ""), r.get("action", ""))
        params = {k: v for k, v in r.items()
                  if k not in ("name", "action", "description", "merged_from")}
        rows.append((rid, r.get("name", ""), action, _clean(r.get("description", "")),
                     _fmt_value(params) if params else "—"))
    title = "系谱检查规则（PD001–PD%03d）" % len(rules)
    intro = ("权威源：`configs/qc/pedigree-qc.yaml`。\n\n"
             "三列 ID/Sire/Dam；缺失统一为 NA；最多向上追溯 %s 代。"
             % cfg.get("max_generations", "?"))
    return title, intro, rows


def render_genotype(cfg):
    rules = cfg.get("rules", [])
    th = cfg.get("thresholds", {})
    rows = []
    for r in rules:
        level = LEVEL_LABEL.get(r.get("level", ""), r.get("level", ""))
        rows.append((r.get("id", ""), r.get("name", ""), level,
                     _clean(r.get("check", "")), _clean(r.get("action", ""))))
    title = "基因型质控规则（GT001–GT008）"
    intro = ("权威源：`configs/qc/genotype-qc.yaml`（经 PLINK2）。\n\n"
             "**QC 阈值**：mind=%s（个体缺失率）→ geno=%s（位点缺失率）→ maf=%s → hwe=%s；"
             "规模下限 SNP≥%s、样本≥%s。执行顺序：先剔个体后剔位点。"
             % (th.get("mind"), th.get("geno"), th.get("maf"), th.get("hwe"),
                th.get("min_snp_after_qc"), th.get("min_sample_after_qc")))
    return title, intro, rows


def write_md(path, title, intro, rows, value_header):
    lines = ["# ABGES %s" % title, "",
             "> 本文件由 `scripts/render_qc_rules_md.py` 从 yaml 自动生成，请勿手改；",
             "> 修改请编辑 `configs/qc/*.yaml` 后重跑该脚本。", "",
             intro, "",
             "| 规则 | 名称 | 强度 | 说明 | %s |" % value_header,
             "|------|------|------|------|------|"]
    for rid, name, action, desc, extra in rows:
        lines.append("| **%s** | %s | %s | %s | %s |" % (rid, name, action, desc, extra))
    lines.append("")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines))
    return len(rows)


def main():
    os.makedirs(DOCS_DIR, exist_ok=True)
    jobs = [
        ("phenotype-qc.yaml", render_phenotype, "QC_RULES_表型_PH.md", "参数"),
        ("pedigree-qc.yaml", render_pedigree, "QC_RULES_系谱_PD.md", "参数"),
        ("genotype-qc.yaml", render_genotype, "QC_RULES_基因型_GT.md", "动作"),
    ]
    for yaml_name, renderer, out_name, vh in jobs:
        with open(os.path.join(QC_DIR, yaml_name), encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        title, intro, rows = renderer(cfg)
        n = write_md(os.path.join(DOCS_DIR, out_name), title, intro, rows, vh)
        print("生成 %s（%d 条规则）" % (out_name, n))
    print("RENDER_DONE")


if __name__ == "__main__":
    main()
