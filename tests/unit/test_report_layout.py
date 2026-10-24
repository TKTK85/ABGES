# -*- coding: utf-8 -*-
"""runners/report_layout.py 单元测试（P2-3 拆分后新增的可测点）。

拆分前这些绘制辅助是 render_run_docx 的嵌套闭包，隐式捕获 doc / black /
三个计数器，**无法单独构造、无法断言**。抽出为 ReportLayout 后，
此处对真正影响排版呈现的要素逐项锁死：

  * 三个自动编号（表 / 图 / 二级标题）的递增与 h1 归零；
  * 固定表格布局、边框、表头浅灰底纹、gridCol 与单元格宽度同步；
  * TOC 域指令文本与 TOC1/TOC2 样式写入；
  * updateFields 让 Word 打开即刷新目录；
  * 字体中西文分别设定、默认纯黑。

⚠ 本文件是「版式零变更」的一道防线：任何导致报告外观漂移的重构，
都应先在这里暴露，而不是等到出报告才被发现。
"""
import sys
import os
from pathlib import Path

ROOT = str(Path(__file__).resolve().parents[2])
for _p in (ROOT, os.path.join(ROOT, 'tools', 'Python'),
           os.path.join(ROOT, 'harness')):
    if _p not in sys.path:
        sys.path.insert(0, _p)
os.chdir(ROOT)

import pytest  # noqa: E402

docx = pytest.importorskip("docx")

from docx import Document  # noqa: E402
from docx.oxml.ns import qn  # noqa: E402

from runners import report_layout as rl  # noqa: E402


@pytest.fixture()
def lo():
    return rl.ReportLayout(Document())


# ----------------------------------------------------------------------
# 自动编号：表 / 图 / 二级标题
# ----------------------------------------------------------------------
def test_caption_numbers_are_sequential(lo):
    lo.caption("甲")
    lo.caption("乙")
    assert lo.tbl_no == 2
    assert lo.doc.paragraphs[-1].text == "表2　乙"
    assert lo.doc.paragraphs[-2].text.startswith("表1　")


def test_figure_caption_numbers_are_sequential(lo):
    lo.fig_caption("甲")
    lo.fig_caption("乙")
    assert lo.fig_no == 2
    assert lo.doc.paragraphs[-1].text.startswith("图2　")


def test_h1_resets_section_counter(lo):
    lo.h2("子节甲")
    lo.h2("子节乙")
    assert lo.sec_no == 2
    assert lo.doc.paragraphs[-1].text.startswith("（二）")
    lo.h1("新章")
    assert lo.sec_no == 0
    lo.h2("子节丙")
    assert lo.doc.paragraphs[-1].text.startswith("（一）")


def test_outline_levels(lo):
    lo.h1("章")
    lo.h2("节")
    levels = []
    for p in lo.doc.paragraphs:
        pPr = p._p.find(qn("w:pPr"))
        node = pPr.find(qn("w:outlineLvl")) if pPr is not None else None
        if node is not None:
            levels.append(node.get(qn("w:val")))
    assert levels == ["0", "1"]


# ----------------------------------------------------------------------
# 表格：结构、边框、底纹、固定布局、列宽
# ----------------------------------------------------------------------
def test_table_returns_none_without_rows(lo):
    assert lo.table(["列"], []) is None


def test_table_structure_and_header(lo):
    t = lo.table(["甲", "乙"], [("1", "2"), ("3", "4")],
                 widths=[3.0, 4.0], left_cols=(1,))
    assert t is not None
    assert (len(t.rows), len(t.columns)) == (3, 2)
    # 表头浅灰底纹 + 加粗
    header_cell = t.rows[0].cells[0]
    shd = header_cell._tc.get_or_add_tcPr().find(qn("w:shd"))
    assert shd is not None and shd.get(qn("w:fill")) == rl.HEADER_FILL
    assert header_cell.paragraphs[0].runs[0].bold is True
    # 固定布局
    layout = t._tbl.tblPr.find(qn("w:tblLayout"))
    assert layout is not None and layout.get(qn("w:type")) == "fixed"
    # gridCol 与单元格宽度同步写入（固定布局下列宽以 gridCol 为准）
    grid = t._tbl.find(qn("w:tblGrid"))
    cols = grid.findall(qn("w:gridCol"))
    assert len(cols) == 2
    assert cols[0].get(qn("w:w")) == str(int(docx.shared.Cm(3.0).twips))


def test_table_borders_present(lo):
    t = lo.table(["甲"], [("1",)])
    bd = t._tbl.tblPr.find(qn("w:tblBorders"))
    assert bd is not None
    edges = {node.tag.split("}")[-1] for node in bd}
    assert {"top", "left", "bottom", "right", "insideH", "insideV"} <= edges


def test_row_nosplit_and_header_repeat(lo):
    t = lo.table(["甲"], [("1",)])
    for row in t.rows:
        trPr = row._tr.find(qn("w:trPr"))
        assert trPr is not None
        assert trPr.find(qn("w:cantSplit")) is not None
    first_trPr = t.rows[0]._tr.find(qn("w:trPr"))
    assert first_trPr.find(qn("w:tblHeader")) is not None


def test_code_block_is_single_cell_table(lo):
    t = lo.code_block("hello")
    assert t is not None
    assert (len(t.rows), len(t.columns)) == (2, 1)
    assert t.rows[1].cells[0].text == "hello"


# ----------------------------------------------------------------------
# 字体：中西文分别设定 + 默认纯黑
# ----------------------------------------------------------------------
def test_set_font_sets_eastasia_and_default_black(lo):
    p = lo.doc.add_paragraph()
    run = p.add_run("中文")
    lo.set_font(run, rl.F_TBL, rl.SZ_TBL)
    rFonts = run._element.get_or_add_rPr().get_or_add_rFonts()
    assert rFonts.get(qn("w:eastAsia")) == rl.F_TBL
    assert rFonts.get(qn("w:ascii")) == rl.F_WEST
    assert run.font.color.rgb == lo.black


def test_set_font_accepts_explicit_color(lo):
    p = lo.doc.add_paragraph()
    run = p.add_run("反白")
    lo.set_font(run, rl.F_TBL, rl.SZ_TBL, color=(255, 255, 255))
    assert str(run.font.color.rgb) == "FFFFFF"


# ----------------------------------------------------------------------
# 目录相关：TOC 样式、TOC 域、updateFields
# ----------------------------------------------------------------------
def test_ensure_toc_styles_creates_toc1_and_toc2(lo):
    lo.ensure_toc_styles()
    ids = [st.get(qn("w:styleId"))
           for st in lo.doc.styles.element.findall(qn("w:style"))]
    assert "TOC1" in ids and "TOC2" in ids
    # 幂等：重复调用不应产生重复样式
    before = len(lo.doc.styles.element.findall(qn("w:style")))
    lo.ensure_toc_styles()
    assert len(lo.doc.styles.element.findall(qn("w:style"))) == before


def test_toc_field_writes_instruction_text(lo):
    lo.toc_field()
    xml = lo.doc.element.xml
    assert r'TOC \o "1-2" \h \z \u' in xml
    assert "w:instrText" in xml


def test_set_update_fields(lo):
    lo.set_update_fields()
    uf = lo.doc.settings.element.find(qn("w:updateFields"))
    assert uf is not None and uf.get(qn("w:val")) == "true"


# ----------------------------------------------------------------------
# 封面小块
# ----------------------------------------------------------------------
def test_cover_blank_and_line(lo):
    lo.cover_blank(3)
    p = lo.cover_line("标题", rl.F_H2, 14, True)
    assert p.runs[0].bold is True
    assert p.runs[0].text == "标题"
    assert p.alignment is not None


def test_rule_underline(lo):
    lo.rule()
    pPr = lo.doc.paragraphs[-1]._p.find(qn("w:pPr"))
    pbdr = pPr.find(qn("w:pBdr"))
    assert pbdr is not None
    assert pbdr.find(qn("w:bottom")) is not None


# ----------------------------------------------------------------------
# 行距 / 缩进
# ----------------------------------------------------------------------
def test_fixed_and_auto_line_spacing(lo):
    p = lo.doc.add_paragraph()
    lo.fixed(p, 20)
    assert p.paragraph_format.line_spacing_rule is not None
    assert p.paragraph_format.space_before.pt == 0
    p2 = lo.doc.add_paragraph()
    lo.auto_line(p2)
    assert p2.paragraph_format.space_before.pt == 2


def test_indent2_sets_first_line_chars(lo):
    p = lo.doc.add_paragraph()
    lo.indent2(p)
    ind = p._p.get_or_add_pPr().find(qn("w:ind"))
    assert ind is not None
    assert ind.get(qn("w:firstLineChars")) == "200"


def test_keep_next(lo):
    p = lo.doc.add_paragraph()
    lo.keep_next(p)
    assert p._p.get_or_add_pPr().find(qn("w:keepNext")) is not None
