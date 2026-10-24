# -*- coding: utf-8 -*-
"""报告版式常量与绘制辅助层（P2-3 从 docx_report.py 抽出）。

职责：把"往 docx 上画东西"的全部低层操作收在一处——
字体设定、行距、缩进、与下段同页、大纲级别、单元格底纹、
表格边框、禁止跨页断行、建表/代码块/题注/插图。

设计：原先这些辅助函数是 ``render_run_docx`` 的**嵌套闭包**，
隐式捕获 doc / black / tbl_no / fig_no / sec_no 等运行期状态，
既无法单独测试，也让 1,400 行的渲染主体难以切分。
现统一收进 :class:`ReportLayout`：

  * ``doc`` / ``black`` 在构造时显式传入；
  * 三个自动编号计数器（表 / 图 / 二级标题）改为实例属性，
    语义与原先的 ``[0]`` 单元素列表完全一致；
  * **方法名与原先嵌套函数名逐一相同**，故 ``render_run_docx``
    只需建实例 + 取局部别名，正文中上千行调用点零改动
    （版式因此可保证逐字节不变，已由指纹门禁验证）。

依赖：python-docx 在本模块顶层导入；调用方须在 docx 可用的前提下
再导入本模块，以维持"python-docx 为软依赖"的既有契约。
"""

from __future__ import annotations

import os

from docx.shared import Pt, Cm, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
from docx.enum.table import WD_TABLE_ALIGNMENT, WD_ALIGN_VERTICAL
from docx.oxml.ns import qn
from docx.oxml import OxmlElement

# ------------------------------------------------------------------
# 一、版式常量（公文统一格式 · 纯黑 · 黑白学术表格）
# ------------------------------------------------------------------
DEFAULT_TITLE = "遗传评估结果报告"

SZ_TITLE, SZ_H1, SZ_H2, SZ_SUBTITLE = 22, 16, 14, 16
SZ_BODY, SZ_TBL, SZ_CODE = 12, 10.5, 9
SZ_TOC_TITLE, SZ_TOC1, SZ_TOC2 = 18, 11.5, 10.5
LINE_PT = 22
BLACK = (0, 0, 0)
# 表格统一黑白学术风格：表头浅灰底+黑色加粗字、黑色细边框、白底数据行，全表文字纯黑；
# 学术图仍为 NPG 彩色（图与表相互独立）。
HEADER_FILL = "D9D9D9"
BORDER_COLOR = "000000"

F_TITLE, F_H1, F_H2, F_BODY, F_TBL = "宋体", "黑体", "楷体", "仿宋_GB2312", "宋体"
F_WEST = "Times New Roman"

_CN_NUM = "一二三四五六七八九十"


def _ind_twips(p, first_line_chars=200, size=SZ_BODY):
    """设置首行缩进 2 字符（按字号换算 twips），符合中文公文排版习惯。"""
    pPr = p._p.get_or_add_pPr()
    ind = pPr.find(qn("w:ind"))
    if ind is None:
        ind = OxmlElement("w:ind")
        pPr.append(ind)
    ind.set(qn("w:firstLineChars"), str(first_line_chars))
    ind.set(qn("w:firstLine"), str(int(size * 2 * 20)))


def _fixed_spacing(p, pt=LINE_PT):
    """固定行距（磅），段前后 0——公文正文行距不随内容浮动。"""
    pf = p.paragraph_format
    pf.line_spacing_rule = WD_LINE_SPACING.EXACTLY
    pf.line_spacing = pt if hasattr(pt, "twips") else Pt(pt)
    pf.space_before = Pt(0)
    pf.space_after = Pt(0)


def _auto_spacing(p):
    """自适应行距：图片等高度不定的段落用固定行距会被裁切，改用单倍行距。"""
    pf = p.paragraph_format
    pf.line_spacing_rule = WD_LINE_SPACING.SINGLE
    pf.space_before = Pt(2)
    pf.space_after = Pt(2)


def _keep_next(p):
    """与下段同页：避免标题出现在页尾、正文被推到下一页。"""
    p._p.get_or_add_pPr().insert(0, OxmlElement("w:keepNext"))


def _outline(p, lvl):
    """大纲级别：供目录(TOC)按 \\u 抓取；顺序在 ind 之后，符合 OOXML pPr 序列。"""
    pPr = p._p.get_or_add_pPr()
    ol = OxmlElement("w:outlineLvl")
    ol.set(qn("w:val"), str(lvl))
    pPr.append(ol)


def _shade(cell, fill):
    """单元格底纹（表头浅灰 / 反白等）。"""
    tcPr = cell._tc.get_or_add_tcPr()
    sh = OxmlElement("w:shd")
    sh.set(qn("w:val"), "clear")
    sh.set(qn("w:fill"), fill)
    tcPr.append(sh)


def _borders(tbl, color=BORDER_COLOR):
    """表格边框：外框略粗、内框细线，纯黑。"""
    bd = OxmlElement("w:tblBorders")
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        e = OxmlElement("w:" + edge)
        e.set(qn("w:val"), "single")
        e.set(qn("w:sz"), "6" if edge in ("top", "bottom") else "4")
        e.set(qn("w:color"), color)
        bd.append(e)
    tbl._tbl.tblPr.append(bd)


def _row_nosplit(row, header=False):
    """行禁止跨页断行；表头行另加"跨页重复表头"。"""
    trPr = row._tr.get_or_add_trPr()
    trPr.append(OxmlElement("w:cantSplit"))
    if header:
        th = OxmlElement("w:tblHeader")
        th.set(qn("w:val"), "true")
        trPr.append(th)


def _set_update_fields(doc):
    """写入 w:updateFields，让 Word/WPS 打开时自动刷新目录域页码。"""
    try:
        uf = OxmlElement("w:updateFields")
        uf.set(qn("w:val"), "true")
        doc.settings.element.append(uf)
    except Exception:
        pass


def _ensure_toc_style(doc, sid, sname, sz, indent_twips=0):
    """写入紧凑的 TOC 段落样式。

    默认模板无 TOC1/2（latent），Word 更新目录会用宽松内置默认导致导读页溢出；
    主动写入紧凑样式：固定行距、黑色、TNR、点前导右对齐页码位。
    """
    styles_el = doc.styles.element
    for st in styles_el.findall(qn("w:style")):
        if st.get(qn("w:styleId")) == sid:
            # Replace built-in/latent defaults so Word respects this hierarchy.
            styles_el.remove(st)
            break
    st = OxmlElement("w:style")
    st.set(qn("w:type"), "paragraph")
    st.set(qn("w:styleId"), sid)
    nm = OxmlElement("w:name")
    nm.set(qn("w:val"), sname)
    st.append(nm)
    bo = OxmlElement("w:basedOn")
    bo.set(qn("w:val"), "Normal")
    st.append(bo)
    pPr = OxmlElement("w:pPr")
    tabs = OxmlElement("w:tabs")
    tb = OxmlElement("w:tab")
    tb.set(qn("w:val"), "right")
    # 与论文目录的点状引导线一致；不要使用 underscore（连续实线）。
    tb.set(qn("w:leader"), "dot")
    # Reference thesis A4 layout: 15.98 cm from paragraph start to right page number.
    tb.set(qn("w:pos"), "9060")
    tabs.append(tb)
    pPr.append(tabs)
    sp = OxmlElement("w:spacing")
    sp.set(qn("w:before"), "0")
    sp.set(qn("w:after"), "30")
    sp.set(qn("w:line"), "312")
    sp.set(qn("w:lineRule"), "exact")
    pPr.append(sp)
    if indent_twips:
        ind = OxmlElement("w:ind")
        ind.set(qn("w:left"), str(indent_twips))
        pPr.append(ind)
    st.append(pPr)
    rPr = OxmlElement("w:rPr")
    rf = OxmlElement("w:rFonts")
    rf.set(qn("w:ascii"), F_WEST)
    rf.set(qn("w:hAnsi"), F_WEST)
    rf.set(qn("w:cs"), F_WEST)
    rf.set(qn("w:eastAsia"), F_TBL)
    rPr.append(rf)
    sz_el = OxmlElement("w:sz")
    sz_el.set(qn("w:val"), str(int(sz * 2)))
    rPr.append(sz_el)
    szc = OxmlElement("w:szCs")
    szc.set(qn("w:val"), str(int(sz * 2)))
    rPr.append(szc)
    if sid == "TOC1":
        bold = OxmlElement("w:b")
        bold.set(qn("w:val"), "1")
        rPr.append(bold)
    col = OxmlElement("w:color")
    col.set(qn("w:val"), "000000")
    rPr.append(col)
    st.append(rPr)
    styles_el.append(st)


def _toc_field(doc, set_font):
    """插入 TOC 域（\\o "1-2" \\h \\z \\u），Word 打开时据正文标题生成目录。"""
    p = doc.add_paragraph()
    _fixed_spacing(p, 18)
    run = p.add_run()
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = r'TOC \o "1-2" \h \z \u'
    sep = OxmlElement("w:fldChar")
    sep.set(qn("w:fldCharType"), "separate")
    hint = OxmlElement("w:t")
    hint.set(qn("xml:space"), "preserve")
    hint.text = ""
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    for el in (begin, instr, sep, hint, end):
        run._r.append(el)
    set_font(run, F_BODY, SZ_TBL)


# ------------------------------------------------------------------
# 二、绘制辅助（持有 doc 与三个自动编号计数器）
# ------------------------------------------------------------------
class ReportLayout(object):
    """Word 报告的低层绘制工具集；一个实例对应一份正在生成的文档。"""

    def __init__(self, doc):
        self.doc = doc
        self.black = RGBColor(*BLACK)
        self.tbl_no = 0
        self.fig_no = 0
        self.sec_no = 0

    # -------- 文本 run 级 --------
    def set_font(self, run, cn=F_BODY, size=SZ_BODY, bold=False,
                 italic=False, color=None):
        run.font.name = F_WEST
        rf = run._element.get_or_add_rPr().get_or_add_rFonts()
        rf.set(qn("w:eastAsia"), cn)
        rf.set(qn("w:ascii"), F_WEST)
        rf.set(qn("w:hAnsi"), F_WEST)
        rf.set(qn("w:cs"), F_WEST)
        run.font.size = Pt(size)
        run.font.bold = bold
        run.font.italic = italic
        # 默认纯黑；仅表头反白等明确场景传入 color（如白色）
        run.font.color.rgb = RGBColor(*color) if color else self.black

    # -------- 段落级 --------
    def fixed(self, p, pt=LINE_PT):
        _fixed_spacing(p, pt)

    def auto_line(self, p):
        _auto_spacing(p)

    def indent2(self, p):
        _ind_twips(p)

    def keep_next(self, p):
        _keep_next(p)

    def outline(self, p, lvl):
        _outline(p, lvl)

    # -------- 表格级 --------
    def shade(self, cell, fill):
        _shade(cell, fill)

    def borders(self, tbl):
        _borders(tbl)

    def row_nosplit(self, row, header=False):
        _row_nosplit(row, header=header)

    def table(self, headers, rows, widths=None, left_cols=(), size=SZ_TBL):
        if not rows:
            return None
        doc = self.doc
        t = doc.add_table(rows=1, cols=len(headers))
        t.alignment = WD_TABLE_ALIGNMENT.CENTER
        t.autofit = False
        t.allow_autofit = False
        # 固定表格布局：严格按指定列宽渲染，避免 Word 因内容自动压缩列宽导致换行
        layout = OxmlElement("w:tblLayout")
        layout.set(qn("w:type"), "fixed")
        t._tbl.tblPr.append(layout)
        self.borders(t)
        self.row_nosplit(t.rows[0], header=True)
        self.keep_next(t.rows[0].cells[0].paragraphs[0])
        for j, htext in enumerate(headers):
            c = t.rows[0].cells[j]
            c.vertical_alignment = WD_ALIGN_VERTICAL.CENTER
            self.shade(c, HEADER_FILL)
            p = c.paragraphs[0]
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            self.fixed(p, 13)
            self.set_font(p.add_run(str(htext)), F_TBL, size, True)
        for ri, r in enumerate(rows):
            self.row_nosplit(t.add_row())
            last_row = (ri == len(rows) - 1)
            for j, val in enumerate(r):
                c = t.rows[-1].cells[j]
                c.vertical_alignment = WD_ALIGN_VERTICAL.CENTER
                p = c.paragraphs[0]
                self.fixed(p, 13)
                p.alignment = (WD_ALIGN_PARAGRAPH.LEFT if j in left_cols
                               else WD_ALIGN_PARAGRAPH.CENTER)
                if not last_row:
                    self.keep_next(p)
                self.set_font(p.add_run(str(val)), F_TBL, size)
        if widths:
            # 固定布局下 Word 以 tblGrid/gridCol 为列宽依据，必须与单元格宽度同步写入
            grid = t._tbl.find(qn("w:tblGrid"))
            if grid is not None:
                for gc, w in zip(grid.findall(qn("w:gridCol")), widths):
                    gc.set(qn("w:w"), str(int(Cm(w).twips)))
            for j, w in enumerate(widths):
                for r in t.rows:
                    r.cells[j].width = Cm(w)
        return t

    def code_block(self, text, size=SZ_CODE):
        return self.table([""], [[text]], widths=[15.6], left_cols=(0,),
                          size=size)

    # -------- 结构块 --------
    def title(self, text):
        doc = self.doc
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        self.fixed(p, 34)
        p.paragraph_format.space_after = Pt(4)
        for i, ln in enumerate(text.split("\n")):
            if i:
                p.add_run().add_break()
            self.set_font(p.add_run(ln), F_TITLE, SZ_TITLE, True)

    def subtitle(self, text):
        doc = self.doc
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        self.fixed(p)
        p.paragraph_format.space_after = Pt(8)
        self.set_font(p.add_run(text), F_H2, SZ_SUBTITLE)

    def h1(self, text):
        self.sec_no = 0
        doc = self.doc
        p = doc.add_paragraph()
        self.fixed(p)
        p.paragraph_format.space_before = Pt(8)
        self.indent2(p)
        self.keep_next(p)
        self.outline(p, 0)
        self.set_font(p.add_run(text), F_H1, SZ_H1, True)

    def h2(self, text):
        self.sec_no += 1
        doc = self.doc
        p = doc.add_paragraph()
        self.fixed(p)
        self.indent2(p)
        self.keep_next(p)
        self.outline(p, 1)
        self.set_font(p.add_run("（%s）%s" % (_CN_NUM[self.sec_no - 1], text)),
                  F_H2, SZ_H2, True)

    def body(self, text, indent=True, bold=False):
        doc = self.doc
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
        self.fixed(p)
        if indent:
            self.indent2(p)
        self.set_font(p.add_run(text), F_BODY, SZ_BODY, bold)
        return p

    def caption(self, text):
        self.tbl_no += 1
        doc = self.doc
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        self.fixed(p, 16)
        self.keep_next(p)
        self.set_font(p.add_run("表%d　%s" % (self.tbl_no, text)), F_TBL, SZ_TBL,
                    True)

    def figure(self, image_path, width_cm=14.5):
        if not image_path or not os.path.isfile(image_path):
            return
        doc = self.doc
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        self.auto_line(p)
        self.keep_next(p)
        run = p.add_run()
        run.add_picture(image_path, width=Cm(width_cm))

    def fig_caption(self, text):
        self.fig_no += 1
        doc = self.doc
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        self.fixed(p, 15)
        self.set_font(p.add_run("图%d　%s" % (self.fig_no, text)), F_TBL, SZ_TBL)

    def toc_field(self):
        _toc_field(self.doc, self.set_font)

    def set_update_fields(self):
        _set_update_fields(self.doc)

    def ensure_toc_styles(self):
        _ensure_toc_style(self.doc, "TOC1", "TOC 1", SZ_TOC1)
        _ensure_toc_style(self.doc, "TOC2", "TOC 2", SZ_TOC2, indent_twips=420)

    # -------- 封面小块 --------
    def cover_blank(self, n=1, pt=16):
        for _ in range(n):
            bp = self.doc.add_paragraph()
            self.fixed(bp, pt)

    def cover_line(self, text, cn, sz, bold=False, pt=24):
        cp = self.doc.add_paragraph()
        cp.alignment = WD_ALIGN_PARAGRAPH.CENTER
        self.fixed(cp, pt)
        self.set_font(cp.add_run(text), cn, sz, bold)
        return cp

    # -------- 规则线（封面标题下分隔线）--------
    def rule(self):
        doc = self.doc
        rp = doc.add_paragraph()
        rp.alignment = WD_ALIGN_PARAGRAPH.CENTER
        self.fixed(rp, 8)
        pbdr = OxmlElement("w:pBdr")
        btm = OxmlElement("w:bottom")
        btm.set(qn("w:val"), "single")
        btm.set(qn("w:sz"), "10")
        btm.set(qn("w:space"), "1")
        btm.set(qn("w:color"), BORDER_COLOR)
        pbdr.append(btm)
        rp._p.get_or_add_pPr().append(pbdr)
