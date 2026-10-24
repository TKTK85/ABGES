# -*- coding: utf-8 -*-
"""ABGES 报告 docx「版式指纹」提取器（只读，不修改任何源文件）。

用途（P2-3 拆分 runners/docx_report.py 的验收基准）：
    拆分前生成 baseline 指纹，拆分后重新渲染再生成 after 指纹，
    两者必须逐字节一致，方可判定「版式零变更」。

指纹覆盖一切影响排版呈现的要素：
    · 节页面设置（纸张/页边距/方向/装订线）
    · 正文块序列（段落 / 表格，保持原始顺序）
    · 段落：样式名、对齐、行距、段前段后、缩进
    · 文本 run：文字、粗斜体、中西文字体、字号、颜色（RGB）
    · 表格：样式、对齐、行列数、列宽、单元格底纹/垂直对齐/边框、单元格内 run 全量
    · 页眉页脚：文字与字段
    · 嵌入媒体：数量 + 每个媒体的 SHA256（防图被换/丢）

指纹**不**覆盖（每次运行必然变化、且与版式无关的易变项）：
    · docProps/core.xml 的 created/modified
    · 运行编号 run_NNN_YYYYMMDD_HHMMSS 与时间戳
    · 输出文件绝对路径
    这些在归一化阶段统一替换为占位符。

用法：
    # 生成指纹
    python scripts/docx_layout_fingerprint.py <docx_path> <fingerprint_out.json>
    # 与基线比对（返回 0 表示版式零变更，1 表示有漂移）
    python scripts/docx_layout_fingerprint.py <docx_path> --compare <baseline.json>

【用途】P2-3 把 runners/docx_report.py 拆成 report_layout / report_stats /
report_sections 三个模块时，本工具是「版式零变更」的验收闸门：
拆分前跑一次存基线，拆分后重渲染再跑一次比对，两者必须完全一致。

⚠ **不宜直接放进 CI**：基准指纹（tests/fixtures/docx_layout_baseline.json）
取自一台装了 Microsoft Word 的机器——目录(TOC)已被 Word 实体化为静态文本
（24 个 toc 1/toc 2 段落 + 页码），zip 内亦有 Word 另存留下的 word/media/
目录项。无 Word 的环境跑不出同样的产物，会误报漂移。
故定位为**本地/发布前的人工验收工具**，需 Word 的机器上执行。
"""

import os
import re
import sys
import json
import hashlib
import zipfile
from datetime import datetime


# ---------------------------------------------------------------- 归一化
# 易变文本统一替换为占位符，保证「同版式不同运行」指纹一致
_VOLATILE_PATTERNS = [
    # 运行编号：run_008_20261021_171842_SAMPLE_POPULATION_EN_PBLUP_GenBatch
    (re.compile(r"run_\d{3}_\d{8}_\d{6}(?:_[A-Za-z0-9_]+)?"), "<RUN_ID>"),
    # 其余内嵌时间戳（引擎命令行 --out …\EN_20261021_175136、文件名 EN_20261021_172142 等）
    (re.compile(r"\d{8}_\d{6}"), "<TS>"),
    # 裸日期时间：2026-10-21 17:21:47 / 2026-10-21T17:21:47
    (re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}"), "<DATETIME>"),
    # 纯日期
    (re.compile(r"\d{4}-\d{2}-\d{2}"), "<DATE>"),
]


def _norm(text):
    s = "" if text is None else str(text)
    for pat, rep in _VOLATILE_PATTERNS:
        s = pat.sub(rep, s)
    return s


def _rgb(color):
    """python-docx 的 RGBColor / None -> 稳定字符串。"""
    try:
        if color is None:
            return None
        if getattr(color, "rgb", None) is None:
            # theme color / auto
            return "auto:%s" % (getattr(color, "theme_color", None),)
        return str(color.rgb)
    except Exception:
        return "?"


def _font_desc(run):
    """run 的字体描述：中西文分别取值，避免中文回退默认字体被漏检。"""
    f = run.font
    try:
        name = f.name
    except Exception:
        name = None
    try:
        size = f.size.pt if f.size is not None else None
    except Exception:
        size = None
    ea = None
    try:
        rpr = run._element.get_or_add_rPr()
        node = rpr.find(
            "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}rFonts")
        if node is not None:
            ea = node.get(
                "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}eastAsia")
    except Exception:
        ea = None
    return {
        "name": name,
        "eastAsia": ea,
        "size_pt": round(float(size), 3) if size is not None else None,
        "bold": run.bold,
        "italic": run.italic,
        "underline": run.underline,
        "color": _rgb(f.color),
    }


def _paragraph_desc(p):
    fmt = p.paragraph_format
    pf = {}
    for attr in ("alignment", "line_spacing", "line_spacing_rule",
                 "space_before", "space_after", "first_line_indent",
                 "left_indent", "right_indent", "keep_together",
                 "page_break_before", "widow_control"):
        try:
            v = getattr(fmt, attr)
            pf[attr] = round(float(v.pt), 3) if hasattr(v, "pt") and v is not None else (
                str(v) if v is not None else None)
        except Exception:
            pf[attr] = "?"
    # 编号/项目符号（w:numPr）
    numpr = None
    try:
        ppr = p._element.find(
            "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}pPr")
        if ppr is not None and ppr.find(
                "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}numPr") is not None:
            numpr = True
    except Exception:
        pass
    return {
        "kind": "paragraph",
        "style": p.style.name if p.style is not None else None,
        "format": pf,
        "numPr": numpr,
        "runs": [{"text": _norm(r.text), "font": _font_desc(r)} for r in p.runs],
        "text": _norm(p.text),
        "fields": _field_codes(p),
    }


def _field_codes(p):
    """抽取 w:instrText（TOC / PAGE / PAGEREF 等域），域是目录能否自动更新的关键。"""
    out = []
    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    try:
        for node in p._element.iter(ns + "instrText"):
            out.append(_norm(node.text))
    except Exception:
        pass
    return out


def _cell_desc(cell):
    shading = None
    try:
        tcpr = cell._tc.get_or_add_tcPr()
        node = tcpr.find(
            "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}shd")
        if node is not None:
            shading = node.get(
                "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}fill")
    except Exception:
        pass
    va = None
    try:
        va = str(cell.vertical_alignment)
    except Exception:
        pass
    return {
        "text": _norm(cell.text),
        "shading": shading,
        "vertical_alignment": va,
        "paragraphs": [
            {
                "alignment": (str(p.alignment) if p.alignment is not None else None),
                "runs": [{"text": _norm(r.text), "font": _font_desc(r)}
                         for r in p.runs],
            }
            for p in cell.paragraphs
        ],
    }


def _table_desc(t):
    col_widths = []
    try:
        col_widths = [round(float(c.width.mm), 3) if c.width is not None else None
                      for c in t.columns]
    except Exception:
        pass
    rows = []
    for row in t.rows:
        cells = []
        for cell in row.cells:
            try:
                cells.append(_cell_desc(cell))
            except Exception:
                cells.append({"text": "?"})
        try:
            h = round(float(row.height.mm), 3) if row.height is not None else None
        except Exception:
            h = None
        rows.append({"height_mm": h, "cells": cells})
    return {
        "kind": "table",
        "style": t.style.name if t.style is not None else None,
        "alignment": str(t.alignment) if t.alignment is not None else None,
        "n_rows": len(t.rows),
        "n_cols": len(t.columns) if t.columns else 0,
        "col_widths_mm": col_widths,
        "rows": rows,
    }


def _section_desc(sec):
    def _len(v):
        try:
            return round(float(v.mm), 3) if v is not None else None
        except Exception:
            return None
    return {
        "page_width_mm": _len(sec.page_width),
        "page_height_mm": _len(sec.page_height),
        "orientation": str(sec.orientation) if sec.orientation is not None else None,
        "left_margin_mm": _len(sec.left_margin),
        "right_margin_mm": _len(sec.right_margin),
        "top_margin_mm": _len(sec.top_margin),
        "bottom_margin_mm": _len(sec.bottom_margin),
        "header_distance_mm": _len(sec.header_distance),
        "footer_distance_mm": _len(sec.footer_distance),
    }


def _header_footer_desc(part):
    if part is None:
        return None
    try:
        return [{"text": _norm(p.text), "fields": _field_codes(p)}
                for p in part.paragraphs]
    except Exception:
        return "?"


def _media_hashes(path):
    """嵌入媒体的 SHA256：图被替换 / 丢失 / 重排都会改变此项。"""
    out = {}
    try:
        with zipfile.ZipFile(path) as zf:
            names = [n for n in zf.namelist() if n.startswith("word/media/")]
            for name in sorted(names):
                out[name] = hashlib.sha256(zf.read(name)).hexdigest()
    except Exception:
        pass
    return out


def fingerprint(docx_path):
    from docx import Document
    doc = Document(str(docx_path))

    # 正文块保序：python-docx 未直接暴露混合流，需按 XML 顺序遍历 body 子元素
    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    blocks = []
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    for child in doc.element.body.iterchildren():
        if child.tag == ns + "p":
            blocks.append(_paragraph_desc(Paragraph(child, doc)))
        elif child.tag == ns + "tbl":
            blocks.append(_table_desc(Table(child, doc)))
        elif child.tag == ns + "sectPr":
            continue

    sections = [_section_desc(s) for s in doc.sections]

    return {
        "_meta": {
            "source": os.path.basename(str(docx_path)),
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "python": sys.version.split()[0],
            "note": "版式指纹；易变项（run 编号/时间戳/绝对路径）已归一化",
        },
        "sections": sections,
        "headers": [_header_footer_desc(s.header) for s in doc.sections],
        "footers": [_header_footer_desc(s.footer) for s in doc.sections],
        "blocks": blocks,
        "media": _media_hashes(docx_path),
        "counts": {
            "paragraphs": sum(1 for b in blocks if b["kind"] == "paragraph"),
            "tables": sum(1 for b in blocks if b["kind"] == "table"),
            "media": len(_media_hashes(docx_path)),
            "sections": len(sections),
        },
    }


def _body(fp):
    """剔除 _meta（指纹自身的生成时间/文件名），只留文档本体。"""
    return {k: v for k, v in fp.items() if k != "_meta"}


def _dump(obj):
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)


def compare(src, baseline_path):
    """与基线比对；返回 (是否一致, 差异摘要)。"""
    fp = _body(fingerprint(src))
    with open(baseline_path, "r", encoding="utf-8") as handle:
        base = _body(json.load(handle))
    if fp == base:
        return True, "版式零变更"
    lines = []
    if fp["counts"] != base["counts"]:
        lines.append("块计数变化：基线 %s -> 当前 %s"
                     % (json.dumps(base["counts"], ensure_ascii=False),
                        json.dumps(fp["counts"], ensure_ascii=False)))
    if fp["sections"] != base["sections"]:
        lines.append("节页面设置变化")
    if fp["headers"] != base["headers"]:
        lines.append("页眉变化")
    if fp["footers"] != base["footers"]:
        lines.append("页脚变化")
    if fp["media"] != base["media"]:
        only_base = sorted(set(base["media"]) - set(fp["media"]))
        only_new = sorted(set(fp["media"]) - set(base["media"]))
        changed = [k for k in sorted(set(base["media"]) & set(fp["media"]))
                   if base["media"][k] != fp["media"][k]]
        if only_base:
            lines.append("媒体缺失：%s" % only_base)
        if only_new:
            lines.append("媒体新增：%s" % only_new)
        if changed:
            lines.append("媒体内容变化：%s" % changed)
    # 正文块逐块定位首个差异
    ba, bb = base["blocks"], fp["blocks"]
    for i, (x, y) in enumerate(zip(ba, bb)):
        if x != y:
            kind = x.get("kind")
            lines.append("正文块 #%d（%s）首现差异：%s"
                         % (i, kind, (x.get("text") or "")[:60]))
            break
    if len(ba) != len(bb) and not lines:
        lines.append("正文块数量变化：%d -> %d" % (len(ba), len(bb)))
    return False, "\n".join(lines) or "存在未归类差异"


def main():
    args = sys.argv[1:]
    if len(args) >= 3 and args[1] == "--compare":
        ok, message = compare(args[0], args[2])
        print(message)
        print("结果：%s" % ("一致（版式零变更）" if ok else "存在漂移"))
        return 0 if ok else 1
    if len(args) < 2:
        print(__doc__)
        return 2
    src, out = args[0], args[1]
    fp = fingerprint(src)
    with open(out, "w", encoding="utf-8") as handle:
        json.dump(fp, handle, indent=2, ensure_ascii=False,
                  sort_keys=True, default=str)
    print("fingerprint -> %s" % out)
    print("counts: %s" % json.dumps(fp["counts"], ensure_ascii=False))
    print("body sha256: %s" % hashlib.sha256(
        _dump(_body(fp)).encode("utf-8")).hexdigest())
    return 0


if __name__ == "__main__":
    sys.exit(main())
