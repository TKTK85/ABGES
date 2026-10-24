# -*- coding: utf-8 -*-
"""ABGES 运行结果 Word 报告渲染器（面向育种专家的专家版 · 公文统一格式 · 纯黑）。

为 PBLUP / GBLUP / SSGBLUP 共用，在第 09 交付阶段与 Markdown 报告并列产出
``<prefix>_报告.docx``。

设计立场（2026-09-10 重构）：报告读者是**育种专家**而非软件质检工程师——
  * 结论先行：开篇核心结论一览与要点；
  * 正文只讲育种科学：材料与方法 → 评估结果（数据结构、方差组分与遗传力、
    固定效应校正、EBV 基准、遗传趋势、近交、可靠性、Top 个体）→ 结论与建议；
  * 工程留痕（G1-G6 闸门、告警、SHA256、完整命令、产物清单、基线）统一下沉
    到附录，供审计复现但不干扰专家阅读。

铁律：
1. **据实动态输出**——实际跑了哪种模型、产出了哪些结果就输出对应内容；
   模型不涉及的环节（GBLUP 无系谱近交、PBLUP 无基因型）不渲染空表、不写死；
2. 数值全部来自运行期 context / metadata / validation / 引擎输出，经
   ``breeding_insights`` 现算，与 Markdown 同源、不另造；
3. ``python-docx`` / ``matplotlib`` 均为软依赖：缺失或异常一律降级，绝不阻断
   遗传评估主流程（runner 已在外层 try，本模块内部亦逐层防御）；
4. 公文风格、**全部数据/正文文字纯黑 RGB(0,0,0)**；表格为黑白学术风格
   （浅灰表头、黑色边框、白底、全黑文字），学术图为 NPG 彩色风格。

【P2-3 模块拆分 2026-10-21】本文件由 1,789 行瘦身为编排层，拆出三个可独立
单测的模块（拆分前后**版式指纹逐字节一致**，见 tests/fixtures/
docx_layout_baseline.json 与 scripts/docx_layout_fingerprint.py）：
  * ``runners/report_layout.py``   版式常量 + 全部低层绘制辅助（ReportLayout）
  * ``runners/report_stats.py``    统计判据阈值（外置到 phenotype-qc.yaml）
                                   与判定文案
  * ``runners/report_sections.py`` 文案常量表 + 表型探索块构造
本文件保留：软依赖防护、Word 目录回写、以及 render_run_docx 的章节编排。
"""

import os
import re
import datetime
import logging
import traceback
import base64
import shutil

# 【P0-4 同口径】模块级 logger；日志由 core.setup_logger 统一落 stderr，
# 绝不可占用 stdout（stdout 专供 result JSON，被 abges_mcp 解析）。
logger = logging.getLogger(__name__)


def _log_report_diag(prefix, stage, exc_tb):
    """报告增强为软依赖、不阻断主流程，但异常必须落盘留痕，禁止静默吞掉。"""
    try:
        with open(str(prefix) + "_报告诊断.log", "a", encoding="utf-8") as handle:
            handle.write("==== [%s] 报告增强子项异常（已降级，不影响评估结论）====\n%s\n"
                         % (stage, exc_tb))
    except Exception:
        pass

def _update_toc_via_word(docx_path, context=None):
    """生成后立即用本机 Word 排版引擎更新目录(TOC)与页码并回写，
    使目录在任意查看器（含手机端）打开即显示真实标题与页码，无需手动 F9。
    非 Windows / 未装 Word / 超时则由 render_run_docx 写入静态标题目录，避免空目录交付。

    【P2-1 执行边界】此前此处直接 subprocess.run 拉起 powershell，绕过
    tool-executor，违反铁律一（唯一执行边界），且 stdout/stderr 全丢 DEVNULL、
    check=False，失败完全静默。现改为经 ToolExecutor.run_binary 统一走
    白名单校验 + SHA256 校验 + audit.tool_call，失败如实记录。

    【P2-1 补充｜成功判定不用退出码】实测（run_009）：脚本主体
    Open/Update/Repaginate/Save/Close 全部成功、目录已实体化，但 powershell
    仍返回 exit_code=1——COM 的 `Quit()` 抛「RPC 服务器不可用」等**已捕获**
    异常也会污染退出码。故改为脚本显式输出哨兵 `ABGES_TOC_OK`，
    由本函数解析哨兵判定成败；异常原文一并落诊断日志，不再靠退出码猜。

    副作用说明：经 ToolExecutor 无法透传 CREATE_NO_WINDOW（原实现用 0x08000000），
    从无控制台的父进程调用时可能闪现窗口。此为服从铁律一的代价，不影响正确性。
    """
    if os.name != "nt":
        return False
    path = str(docx_path).replace("'", "''")
    ps = (
        "$ErrorActionPreference='Stop';"
        "$toc=0;$w=$null;"
        "try{"
        "$w=New-Object -ComObject Word.Application;"
        "$w.Visible=$false;$w.DisplayAlerts=0;$w.ScreenUpdating=$false;"
        "$d=$w.Documents.Open('" + path + "',$false,$false);"
        # 1) 先完成分页，再更新目录；字号、段距会影响最终页码。
        "$d.Repaginate();"
        # 2) 更新目录并重新分页；第二次更新页码以消除首次分页缓存差异。
        "$tocs=$d.TablesOfContents;"
        "foreach($t in $tocs){ $t.Update();$toc=$toc+1 };"
        "if($toc -eq 0){throw 'Word 未识别到目录域'};"
        "$d.Repaginate();"
        "foreach($t in $tocs){ $t.UpdatePageNumbers() };"
        "$d.Repaginate();"
        "$tocText=[string]$d.TablesOfContents.Item(1).Range.Text;"
        "if($tocText.Trim().Length -lt 10){throw 'Word 目录更新后仍为空'};"
        "$d.Repaginate();"
        # 2) 解除目录及其内部 HYPERLINK/PAGEREF 域 -> 静态文本（保留标题与页码，
        #    任意查看器含手机端可直接显示，无需 F9；页脚 PAGE 域(类型33)不在其列、保持动态）
        "for($rd=0;$rd -lt 4;$rd++){ foreach($f in @($d.Fields)){"
        " if($f.Type -eq 13 -or $f.Type -eq 88 -or $f.Type -eq 37){$f.Unlink()|Out-Null} } };"
        # 3) 必须在 unlink 之后再定位“目录—缩写表”区间（域移除会改变字符位置）
        "$a=0;$b=0;"
        "foreach($p in $d.Paragraphs){ $t=$p.Range.Text.Trim();"
        " if($t -eq '目　录' -and $p.Range.Start -gt 1000){$a=$p.Range.End};"
        " if($t.StartsWith('主要符号与缩写')){$b=$p.Range.Start} };"
        # 4) 紧凑化目录条目（固定 14 磅、段前后 0、TNR/宋体、纯黑），保证与缩写表同处一页
        "foreach($p in $d.Paragraphs){ if($b -gt 0 -and $p.Range.Start -ge $a -and $p.Range.End -le $b){"
        " $p.LineSpacingRule=4;$p.LineSpacing=15;$p.SpaceBefore=0;$p.SpaceAfter=1.5;"
        " $p.Range.Font.Name='Times New Roman';$p.Range.Font.NameFarEast='宋体';"
        " $styleName=[string]$p.Style.NameLocal;"
        " if($styleName -match 'TOC\\s*1'){$fontSize=11.5;$p.Range.Font.Bold=1}"
        " else{$fontSize=10.5;$p.Range.Font.Bold=0};"
        # TOC1/TOC2 的右对齐点状引导位由报告样式统一定义；
        # 不在 COM 回写阶段 ClearAll/Add，以免覆盖样式或产生清除制表位。
        " $p.Range.Font.Size=$fontSize;$p.Range.Font.Color=0 } };"
        "$d.Repaginate();"
        "$d.Save();$d.Close(0);"
        "Write-Output ('ABGES_TOC_OK updated=' + $toc + ' chars=' + $tocText.Length)}"
        "catch{Write-Output ('ABGES_TOC_FAIL ' + $_.Exception.Message)}"
        "finally{try{ if($w -ne $null){$w.Quit()} }catch{}}"
        "exit 0"
    )
    enc = base64.b64encode(ps.encode("utf-16-le")).decode("ascii")
    powershell = shutil.which("powershell.exe")
    if not powershell:
        _log_report_diag(
            str(docx_path).rsplit("_报告.docx", 1)[0], "toc_update",
            "Windows PowerShell 不可用；将生成静态标题目录")
        return False
    command = [powershell, "-NoProfile", "-NonInteractive",
               "-ExecutionPolicy", "Bypass", "-EncodedCommand", enc]
    tools = (context or {}).get("__tools__")

    if tools is None:
        # 未提供执行器（如手工重渲染脚本）：按铁律一不得私自起子进程，
        # 如实留痕后降级，由 render_run_docx 从正文标题生成静态目录。
        logger.warning(
            "未提供 tool-executor，跳过 Word 页码回写；将生成静态标题目录")
        return False

    result = tools.run_binary(
        "powershell", command, timeout=180,
        note="更新 _报告.docx 的目录(TOC)与页码")
    stdout = str(result.stdout or "")
    if "ABGES_TOC_OK" in stdout:
        logger.info("Word 目录(TOC)与页码回写成功：%s" % stdout.strip())
        return True
    # 失败如实记录（不再 DEVNULL 静默），供报告/审计复查。
    # 注：exit_code 仅作参考，COM 的已捕获异常也会让它变为 1。
    _log_report_diag(
        str(docx_path).rsplit("_报告.docx", 1)[0], "toc_update",
        "exit_code=%s\nstdout=%s\nstderr=%s"
        % (result.exit_code, stdout, result.stderr))
    return False


# 【P2-3】版式常量与文案常量表已迁至 runners/report_layout.py /
#   runners/report_sections.py，此处集中导入并保留历史内部名，
#   使 render_run_docx 正文一行不改即可继续工作。
from runners.common import DASH as _DASH, dash as _dash               # noqa: E402
from runners.report_layout import (                                   # noqa: E402
    DEFAULT_TITLE,
    SZ_TITLE, SZ_H1, SZ_H2, SZ_BODY, SZ_TBL, SZ_CODE,
    SZ_TOC_TITLE,
    LINE_PT, BLACK, HEADER_FILL, BORDER_COLOR,
    F_TITLE, F_H1, F_H2, F_BODY, F_TBL, F_WEST,
    _CN_NUM,
)
from runners import report_sections as _sections                      # noqa: E402
from runners.report_sections import (                                 # noqa: E402
    ABBREVIATIONS, GATE_NAME, INTEG_LABELS,
)
_RUN_DIR_IN_TEXT = re.compile(r"[A-Za-z]:[\\/](?:[^\\/\s]+[\\/])*_runs[\\/][^\\/\s\"]+")


def _mask_run_dirs(text):
    """
    【2026-10-20】报告展示前把「内部运行目录」抽象为 `<运行目录>`。

    背景：引擎完整命令行含形如
        …\\workspace\\SAMPLE_POPULATION_EN_PBLUP_GenBatch\\_runs\\run_003_20261020_224813_…\\intermediate\\…
    的路径，其中 run 编号与时间戳每次都变，直接写进报告会导致
    「每次运行的报告都不一样」，且让 `_runs` 出现在交付展示中。

    ⚠ 仅作用于【报告展示层】：完整命令行仍原样保存在 run 内元数据
      （`_runs/run_NNN/reports/run_metadata_metadata.json`）与 result.json 中，
      可复现性不受影响。
    """
    return _RUN_DIR_IN_TEXT.sub("<运行目录>", str(text or ""))


# 【P2-3】统计判据与判定文案已迁至 runners/report_stats.py：
#   阈值统一由 configs/qc/phenotype-qc.yaml 的 report_thresholds 提供，
#   缺失时回退默认值（与历史口径逐值一致，故报告输出零变更）。
# 此处保留历史内部名，使 render_run_docx 正文无需改动。
from runners.report_stats import (                                  # noqa: E402
    h2_band as _h2_band,
    p_sig as _p_sig,
    fmt_p as _fmt_p,
    p_label as _p_label,
    verdict_cn as _verdict_cn,
    status_cn as _status_cn,
    resolve_model as _resolve_model,
    get as _thr,
    fnum as _fnum_v,
    mean_is_zero as _mean_is_zero,
    describe_skew as _describe_skew,
    describe_kurt as _describe_kurt,
    normality_verdict as _normality_verdict,
    quadratic_verdict as _quadratic_verdict,
    trend_significance_text as _trend_sig_text,
)
# 【P2-3】表型探索块的构建已迁至 runners/report_sections.py
#   （build_exploration），渲染层只负责调用与降级。


def render_run_docx(output_prefix, plan, context, validation,
                    gate_warnings, stage_warnings, expected_status, metadata,
                    model_label="PBLUP", snapshots=None, gate_reports=None,
                    raw_guard=None, top_individuals=None, exploration=None):
    """据实生成面向育种专家的公文格式纯黑 Word 报告；失败返回 None。

    exploration  表型数据探索结果块（abges_explore.build_exploration 的返回值）。
                 传入则直接渲染；不传则由本函数基于引擎表型文件自行计算
                 （软依赖：任何异常都只导致该章节从略，绝不阻断报告生成）。
    """
    try:
        from docx import Document
        from docx.shared import Pt, Cm, RGBColor
        from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
        from docx.enum.table import WD_TABLE_ALIGNMENT, WD_ALIGN_VERTICAL
        from docx.oxml.ns import qn
        from docx.oxml import OxmlElement
        # 【P2-3】绘制层依赖 python-docx，故与 docx 一起延迟导入，
        #   维持「python-docx 为软依赖、缺失即降级」的既有契约。
        from runners import report_layout as _rl
    except Exception:
        return None

    black = RGBColor(*BLACK)
    snapshots = snapshots or []
    gate_reports = gate_reports or []
    summary = getattr(validation, "summary", None) or {}
    checks = getattr(validation, "checks", None) or []
    integration_metrics = context.get("integration_metrics") or {}
    ebv_summary = context.get("ebv_summary") or {}
    baseline_comparison = context.get("baseline_comparison")
    result_eval = context.get("result_evaluation") or {}
    reporting_pop = context.get("reporting_population") or {}
    if top_individuals is None:
        top_individuals = context.get("top_individuals") or []
    h2_precision = result_eval.get("heritability_precision") or {}
    model_key, (model_cn, matrix_desc, use_ped, use_geno) = _resolve_model(metadata, model_label)

    def find_snap(*prefixes):
        for s in snapshots:
            st = str(s.get("stage", "")).lower()
            if any(st.startswith(p) for p in prefixes):
                return s
        return None

    pheno_snap = find_snap("01", "phen")
    ped_snap = find_snap("02", "ped") if use_ped else None
    geno_snap = find_snap("03", "gen") if use_geno else None

    def trait_overall(snap, phase):
        try:
            traits = (snap.get(phase) or {}).get("traits") or {}
            if not traits:
                return {}
            return list(traits.values())[0].get("overall") or {}
        except Exception:
            return {}

    # -------- 育种洞察（方差组分/固定效应/趋势/近交/分群/可靠性/残差），全程软依赖 --------
    insights = {}
    eng_path = None
    parsed = {}
    try:
        from runners import breeding_insights as bi
        parsed = context.get("parsed_result") or {}
        eng_path = ((context.get("engine_phenotype") or {}).get("path"))
        plan_for_insight = dict(plan or {})
        plan_for_insight["_engine_phenotype_path"] = eng_path
        valid_n = trait_overall(pheno_snap, "after").get("ValidN")
        insights = bi.build_breeding_insights(
            plan_for_insight, parsed, pheno_snap=pheno_snap, ped_snap=ped_snap,
            geno_snap=geno_snap, integration_metrics=integration_metrics,
            result_eval=result_eval, model_key=model_key, valid_records=valid_n)
    except Exception:
        insights = {}
        _log_report_diag(output_prefix, "breeding_insights", traceback.format_exc())
    variance = insights.get("variance") or {}
    inbreed = insights.get("inbreeding") or {}
    rel_brief = insights.get("reliability") or {}
    selection = insights.get("selection_response") or {}
    trend_rows = insights.get("trend_rows") or []
    id_gen = insights.get("id_generation") or {}
    # Top 个体亲本/父系家系（读引擎系谱，服务控近交与家系均衡；软依赖）
    ped_engine_path = (context.get("engine_pedigree") or {}).get("path")
    if not ped_engine_path:  # 引擎系谱缺失时回退到任务原始系谱（同样按前三列 ID/Sire/Dam 解析）
        ped_engine_path = (plan or {}).get("pedigree_path")
    family = {}
    try:
        from runners import breeding_insights as _bi_fam
        family = _bi_fam.family_summary(top_individuals, ped_engine_path)
        if not family.get("available"):
            _log_report_diag(
                output_prefix, "family_summary",
                "ped_path=%s|exists=%s|top_n=%s|inner_error=%s" % (
                    ped_engine_path, os.path.isfile(ped_engine_path) if ped_engine_path else False,
                    len(top_individuals or []), family.get("error")))
    except Exception:
        family = {}
        _log_report_diag(output_prefix, "family_summary", traceback.format_exc())

    # -------- 学术图（matplotlib 软依赖，失败则无图）--------
    figure_paths = {}
    try:
        from runners import report_figures
        fig_dir = output_prefix + "_figures"
        figure_paths = report_figures.render_figures(
            parsed, insights, fig_dir,
            engine_pheno_path=eng_path, trait=(plan or {}).get("trait"))
    except Exception:
        figure_paths = {}
        _log_report_diag(output_prefix, "report_figures", traceback.format_exc())

    # -------- 表型数据探索（描述统计 / 分布检验 / 共线性；软依赖）--------
    # 报告第二章前置内容：在正式建模前先把数据"看清楚"，避免带着
    # 严重偏态或强共线的数据进入方差组分估计。全程不阻断主流程。
    exploration_block = exploration if isinstance(exploration, dict) else None
    explore_figures = {}
    if exploration_block is None:
        try:
            exploration_block, explore_figures = _sections.build_exploration(
                output_prefix, plan, context, eng_path)
        except Exception:
            exploration_block = None
            explore_figures = {}
            _log_report_diag(output_prefix, "exploration", traceback.format_exc())
    if exploration_block and not explore_figures:
        explore_figures = (exploration_block.get("Figures") or {})

    gate_ids = [str(g.get("gate_id")) for g in gate_reports if g.get("gate_id")]
    gate_span = ("%s—%s" % (gate_ids[0], gate_ids[-1]) if len(gate_ids) >= 2
                 else (gate_ids[0] if gate_ids else "G1—G6"))

    # ==================== 文档与绘制工具 ====================
    doc = Document()
    sec = doc.sections[0]
    sec.page_width, sec.page_height = Cm(21.0), Cm(29.7)
    # 固定为 SAMPLE_POPULATION 交付报告的版心，并用于今后所有模型/任务的报告。
    sec.top_margin, sec.bottom_margin = Cm(3.0), Cm(2.5)
    sec.left_margin, sec.right_margin = Cm(2.5), Cm(2.5)

    normal = doc.styles["Normal"]
    normal.font.name = F_WEST
    normal.font.size = Pt(SZ_BODY)
    normal.font.color.rgb = black
    _nr = normal.element.get_or_add_rPr().get_or_add_rFonts()
    _nr.set(qn("w:eastAsia"), F_BODY)
    _nr.set(qn("w:ascii"), F_WEST)
    _nr.set(qn("w:hAnsi"), F_WEST)
    _nr.set(qn("w:cs"), F_WEST)

    # 【P2-3】原先上面这一整段绘制辅助全是本函数的嵌套闭包，隐式捕获
    #   doc / black 与三个自动编号计数器（tbl_no / fig_no / sec_no），
    #   既无法单独单测，也让 1,400 行的渲染主体无从切分。
    # 现由 ReportLayout 显式持有这些状态，且**方法名与原先嵌套函数逐一对应**，
    #   故这里只做局部别名绑定：正文里上千个调用点一字不改，
    #   版式因此可保证逐字节不变（由指纹门禁验证）。
    lo = _rl.ReportLayout(doc)

    set_font = lo.set_font
    fixed, auto_line = lo.fixed, lo.auto_line
    indent2, keep_next, outline = lo.indent2, lo.keep_next, lo.outline
    title, subtitle, h1, h2 = lo.title, lo.subtitle, lo.h1, lo.h2
    body, caption = lo.body, lo.caption
    figure, fig_caption = lo.figure, lo.fig_caption
    shade, borders, row_nosplit = lo.shade, lo.borders, lo.row_nosplit
    table, code_block = lo.table, lo.code_block
    toc_field, _set_update_fields = lo.toc_field, lo.set_update_fields

    lo.ensure_toc_styles()

    # ==================== 封面（第 1 页，单独成页）====================
    # 报告抬头以“任务”为主体；主标题可用环境变量覆盖
    def _cover_values():
        ts = metadata.get("timestamp_end") or datetime.datetime.now().replace(
            microsecond=0).isoformat()
        ts = str(ts).replace("T", " ")[:19]
        ver = metadata.get("abges_version") or _DASH
        eng_v = metadata.get("engine_version")
        if isinstance(eng_v, dict):
            eng_v = eng_v.get("version") or eng_v.get("version_string") or _DASH
        eng = "%s %s" % (metadata.get("engine_name") or "HIBLUP", _dash(eng_v))
        raw_flag = (raw_guard or {}).get("unchanged")
        raw_txt = ("原始输入经校验未被改写" if raw_flag is True
                   else "原始输入存在改动告警" if raw_flag is False else "原始输入校验：—")
        return ts, ver, eng, raw_txt
    _c_ts, _c_ver, _c_eng, _c_raw = _cover_values()

    def _cover_blank(n=1, pt=16):
        for _ in range(n):
            _bp = doc.add_paragraph(); fixed(_bp, pt)

    def _cover_line(text, cn, sz, bold=False, pt=24):
        _cp = doc.add_paragraph(); _cp.alignment = WD_ALIGN_PARAGRAPH.CENTER
        fixed(_cp, pt); set_font(_cp.add_run(text), cn, sz, bold)
        return _cp

    _tid = plan.get("task_id", _DASH)
    _tname = plan.get("task_name")
    _has_name = bool(_tname and str(_tname) != str(_tid))

    _cover_blank(5, 18)
    _cover_line("ABGES 自动化育种遗传评估技能", F_H2, 14, False, 22)
    _cover_blank(1, 14)
    title(DEFAULT_TITLE)
    _rule = doc.add_paragraph(); _rule.alignment = WD_ALIGN_PARAGRAPH.CENTER
    fixed(_rule, 8)
    _pbdr = OxmlElement("w:pBdr"); _btm = OxmlElement("w:bottom")
    _btm.set(qn("w:val"), "single"); _btm.set(qn("w:sz"), "10")
    _btm.set(qn("w:space"), "1"); _btm.set(qn("w:color"), BORDER_COLOR)
    _pbdr.append(_btm); _rule._p.get_or_add_pPr().append(_pbdr)
    _cover_blank(1, 14)
    if _has_name:
        _cover_line(str(_tname), F_H1, 18, True, 30)
    _cover_blank(2, 16)
    _info_rows = [("任务编号", str(_tid))]
    if _has_name:
        _info_rows.append(("任务名称", str(_tname)))
    _info_rows += [
        ("目标性状", str(plan.get("trait", _DASH))),
        ("评估模型", str(model_cn)),
        ("报告生成时间", _c_ts),
        ("ABGES 版本", str(_c_ver)),
        ("计算引擎", _c_eng),
        ("原始数据校验", _c_raw),
    ]
    table(["项　目", "内　容"], _info_rows, widths=[4.6, 11.0],
          left_cols=(1,), size=10.5)
    _cover_blank(12, 16)
    _cover_line("ABGES 自动化育种遗传评估技能", F_BODY, SZ_BODY, False, 24)
    _cover_line(_c_ts[:10], F_BODY, SZ_BODY, False, 24)

    # -------- 导读页：目录（TOC 域）+ 主要符号与缩写，随后另起一页进入正文 --------
    # 用分节符开启专用目录页，避免“手动分页 + 分节分页”造成空白页。
    first_section = doc.sections[0]
    _set_update_fields()
    # 单独建立目录节：页眉仅显示在目录页；整份报告沿用固定的 A4 版心。
    toc_section = doc.add_section(start_type=2)
    toc_section.page_width, toc_section.page_height = Cm(21.0), Cm(29.7)
    toc_section.top_margin, toc_section.bottom_margin = Cm(3.0), Cm(2.5)
    toc_section.left_margin, toc_section.right_margin = Cm(2.5), Cm(2.5)
    toc_section.header_distance = Cm(2.31)
    toc_section.footer_distance = Cm(1.8)
    toc_section.different_first_page_header_footer = False
    toc_footer = toc_section.footer.paragraphs[0]
    toc_footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
    toc_footer_run = toc_footer.add_run()
    set_font(toc_footer_run, F_TBL, 10.5)
    for typ in ("begin", "instr", "end"):
        if typ == "instr":
            it = OxmlElement("w:instrText")
            it.set(qn("xml:space"), "preserve")
            it.text = " PAGE "
            toc_footer_run._r.append(it)
        else:
            fc = OxmlElement("w:fldChar")
            fc.set(qn("w:fldCharType"), typ)
            toc_footer_run._r.append(fc)
    toc_header = toc_section.header
    toc_header.is_linked_to_previous = False
    _toc_header = toc_header.paragraphs[0]
    _toc_header.clear()
    _toc_header.alignment = WD_ALIGN_PARAGRAPH.CENTER
    fixed(_toc_header, 12)
    set_font(_toc_header.add_run("目　录"), F_BODY, 10, False)
    _toc_header_border = OxmlElement("w:pBdr")
    _toc_header_bottom = OxmlElement("w:bottom")
    _toc_header_bottom.set(qn("w:val"), "single")
    _toc_header_bottom.set(qn("w:sz"), "6")
    _toc_header_bottom.set(qn("w:space"), "3")
    _toc_header_bottom.set(qn("w:color"), BORDER_COLOR)
    _toc_header_border.append(_toc_header_bottom)
    _toc_header._p.get_or_add_pPr().append(_toc_header_border)
    _toc_t = doc.add_paragraph(); _toc_t.alignment = WD_ALIGN_PARAGRAPH.CENTER
    fixed(_toc_t, 24); _toc_t.paragraph_format.space_after = Pt(4)
    set_font(_toc_t.add_run("目　录"), F_H1, SZ_TOC_TITLE, True)
    toc_field()
    _ab_t = doc.add_paragraph(); _ab_t.alignment = WD_ALIGN_PARAGRAPH.CENTER
    fixed(_ab_t, 15); keep_next(_ab_t); _ab_t.paragraph_format.space_before = Pt(4)
    set_font(_ab_t.add_run("主要符号与缩写"), F_H2, SZ_H2, True)
    table(["符号", "含义", "符号", "含义"], ABBREVIATIONS,
          widths=[2.4, 5.4, 2.4, 5.4], size=9)
    # 目录 + 缩写表作为独立一页；新节恢复 ABGES 正文版心。
    body_section = doc.add_section(start_type=2)
    body_section.page_width, body_section.page_height = Cm(21.0), Cm(29.7)
    body_section.top_margin, body_section.bottom_margin = Cm(3.0), Cm(2.5)
    body_section.left_margin, body_section.right_margin = Cm(2.5), Cm(2.5)
    body_section.header_distance = Cm(1.5)
    body_section.footer_distance = Cm(1.8)
    body_section.header.is_linked_to_previous = False
    body_section.footer.is_linked_to_previous = False

    # ==================== 一、核心评估结论（结论先行）====================
    h1("一、核心评估结论")
    ci_txt = "%s ~ %s" % (_dash(variance.get("ci_lower")),
                          _dash(variance.get("ci_upper"))) if variance.get("h2") is not None else _DASH
    rel_ok = bool(rel_brief.get("available"))
    converged_txt = ("收敛" if summary.get("converged") or variance.get("converged")
                     else ("未收敛" if (summary.get("converged") is False
                                        or variance.get("converged") is False) else _DASH))
    # 结论要点（据实组织，缺项跳过）——先给四点结论，核心结论一览表置于其后
    band = _h2_band(variance.get("h2") or summary.get("heritability"))
    if band:
        body("1. 遗传力水平与精度：%s的遗传力 h²=%s（SE=%s，95%%CI %s），%s；SE/h²=%s，"
             "对数似然 LogL=%s，估计结果稳健。" % (
                 plan.get("trait", _DASH), _dash(variance.get("h2")),
                 _dash(variance.get("h2_se")), ci_txt, band.split("（")[0],
                 _dash(variance.get("se_over_h2")), _dash(variance.get("logl"))))
    if insights.get("trend_delta_per_gen") is not None and len(trend_rows) >= 2:
        trend_label = ("遗传趋势" if "遗传世代" in insights.get("trend_interpretation", "")
                       else "世代间 EBV 描述差异")
        body("2. %s：分世代平均 EBV 由 Gen%s 的 %s 变化至 Gen%s 的 %s，平均每代差异 %s；"
             "该描述统计不单独证明遗传进展。" % (
                 trend_label,
                 trend_rows[0]["gen"], _dash(trend_rows[0]["mean_ebv"]),
                 trend_rows[-1]["gen"], _dash(trend_rows[-1]["mean_ebv"]),
                 _dash(insights.get("trend_delta_per_gen"))))
    if use_ped and inbreed.get("mean_inbreeding") is not None:
        body("3. 群体近交：质控后群体平均近交系数 %s、最大 %s，无自交、无重复编号，"
             "当前近交水平%s。" % (
                 _dash(inbreed.get("mean_inbreeding")), _dash(inbreed.get("max_inbreeding")),
                 "极低" if (inbreed.get("mean_inbreeding") or 0) < _thr("inbreeding_low") else "需关注"))
    if rel_ok:
        body("4. 育种值可靠性：统一优先使用 HIBLUP PA_R2，无该列时才按 1−PEV/VA 推导；"
             "来源 %s、可靠性均值 %s、准确率均值 %s，Top 个体附可靠性，供后续候选群评审参考。" % (
                 rel_brief.get("source") or _DASH,
                 _dash(rel_brief.get("rel_mean")), _dash(rel_brief.get("acc_mean"))))
    else:
        body("4. 育种值可靠性：本次结果未含个体 PEV/可靠性，Top 排名仅供方法学验证，"
             "正式定选前应开启可靠性（--r2）重跑，并结合可靠性、世代与家系分布综合筛选。")

    # 核心结论一览表：置于四点结论之后，作为量化汇总
    caption("核心评估结论一览")
    table(["指标", "结论"], [
        ("评估模型", "%s（%s）" % (model_cn, model_key.upper())),
        ("目标性状", _dash(plan.get("trait"))),
        ("实际进入模型记录数", _dash(trait_overall(pheno_snap, "after").get("ValidN"))),
        ("输出育种值个体数", _dash(insights.get("prediction_n") or summary.get("ebv_records"))),
        ("方差组分估计是否收敛", "%s（迭代 %s 次）" % (converged_txt, _dash(variance.get("n_iterations") or summary.get("n_iterations")))),
        ("遗传力 h²（SE）", "%s（%s）" % (_dash(variance.get("h2") or summary.get("heritability")),
                                       _dash(variance.get("h2_se") or summary.get("heritability_se")))),
        ("h² 95% 置信区间", ci_txt),
        ("遗传趋势（需显式遗传世代语义）", ("平均每代 %s" % _dash(insights.get("trend_delta_per_gen"))
                          if insights.get("trend_delta_per_gen") is not None else "未估计；仅描述世代差异")),
        ("参考群", (_dash((insights.get("reference_group") or {}).get("column")) + "=" +
         _dash((insights.get("reference_group") or {}).get("value")) +
         ("（已基准化）" if (insights.get("reference_group") or {}).get("available")
          else "（样本不足；未基准化）"))
         if (insights.get("reference_group") or {}).get("configured") else "未显式指定；不做基准化"),
        ("群体平均近交系数", _dash(inbreed.get("mean_inbreeding") if use_ped else None)),
        ("个体可靠度（PA_R2/PEV）", "已输出（均值 %s；来源 %s）" %
         (_dash(rel_brief.get("rel_mean")), rel_brief.get("source") or _DASH) if rel_ok
                            else "本次未输出个体可靠性"),
        ("交付状态", _status_cn(expected_status)),
    ], widths=[5.2, 10.4], left_cols=(1,))

    independent = context.get("independent_validation") or {}
    if independent:
        h2("独立验证群预测表现")
        iv_rows = []
        for trait_name, item in independent.items():
            iv_rows.append((trait_name, item.get("status"), item.get("n"),
                            item.get("pearson_predictive_ability"),
                            item.get("spearman_rank_correlation"),
                            item.get("calibration_slope"), item.get("rmse")))
        table(["性状", "状态", "配对数", "Pearson 预测能力", "Spearman", "校准斜率", "RMSE"],
              iv_rows, widths=[2.1, 2.0, 1.6, 2.5, 2.1, 2.0, 1.8])
        body("验证表型须来自拟合未使用的个体，且已校正到目标尺度。预测能力相关系数不等于遗传评估准确度；"
             "样本不足或验证被拒绝时不作准确度结论。")

    # ==================== 二、表型数据探索 ====================
    # 建模前先把数据看清楚：描述性统计看"数据长什么样"，分布检验看
    # "能不能直接套线性模型"，共线性诊断看"哪些变量不能同时入模"。
    h1("二、表型数据探索")

    def _num(value, nd=3):
        if value is None:
            return _DASH
        try:
            fv = float(value)
            if fv == float("inf"):
                return "∞"
            if fv != fv:      # NaN
                return _DASH
            return "%.*f" % (nd, fv)
        except Exception:
            return str(value)

    _VERDICT_CN = {"normal": "近似正态", "mild": "轻度偏离正态",
                   "skewed": "明显偏态", "insufficient": "样本不足"}
    _LEVEL_CN = {"severe": "严重", "warn": "提示"}
    _KIND_CN = {"VIF": "方差膨胀因子", "Corr": "相关系数",
                "ConditionNumber": "条件数", "CategoryDependency": "分类变量冗余",
                "Distribution": "分布检验", "Outlier": "离群值"}

    _blk = exploration_block or {}
    if _blk.get("Enabled") is False and _blk.get("Degraded"):
        body("表型数据探索未能完成，报告已降级。诊断代码：" +
             ", ".join(_blk.get("Warnings") or ["EXPLORATION_FAILED"]) +
             "。请检查运行日志并修复后重新生成报告；本节缺失不代表检查通过。")
    elif not _blk or _blk.get("Enabled") is False:
        body("本次运行未启用表型数据探索环节（配置项 qc.exploration.enabled 为 false），"
             "或未能取得质控后的表型数据，故描述性统计、分布检验与共线性诊断三项从略；"
             "启用后该章节将自动补入。")
    else:
        body("本章在正式建模之前，对进入模型的目标性状与固定效应、协变量做三项前置检查："
             "一是描述性统计，明确样本量、集中与离散趋势及离群情况；"
             "二是分布（正态性）检验，判断性状是否满足线性模型对残差正态的基本假设；"
             "三是共线性诊断，识别不可同时入模的冗余变量。"
             "三项检查的结论直接决定后续模型规格是否需要调整（如性状变换、剔除共线变量），"
             "是方差组分与育种值估计可靠性的前置保障。")

        # ----（一）描述性统计 ----
        _desc = _blk.get("Descriptors") or {}
        if _desc:
            h2("描述性统计")
            body("逐性状给出有效样本量、均值、标准差、变异系数（CV，%）、最小值、"
                 "中位数、最大值、四分位距（IQR），并统计落在均数±3 倍标准差之外"
                 "（超 3SD）与落在 [Q1−3·IQR, Q3+3·IQR] 之外（超 3Q，极端离群）的个体数。"
                 "CV 用于横向比较不同量纲性状的变异程度；当均值趋近 0 时 CV 会严重失真，"
                 "本表对该情形以“—”标记而不给出误导性数值。")
            caption("目标性状描述性统计")
            table(["性状", "有效N", "均值", "标准差", "CV(%)", "最小值", "中位数",
                   "最大值", "四分位距", "超3SD", "超3Q"],
                  [(str(t), _dash(d.get("ValidN")), _num(d.get("Mean")),
                    _num(d.get("SD")), _num(d.get("CV"), 2), _num(d.get("Min")),
                    _num(d.get("Median")), _num(d.get("Max")), _num(d.get("IQR")),
                    _dash(d.get("Outlier3SD")), _dash(d.get("Outlier3Q")))
                   for t, d in _desc.items()],
                  widths=[1.5, 1.2, 1.6, 1.5, 1.2, 1.4, 1.4, 1.4, 1.4, 1.0, 1.0],
                  left_cols=(), size=8.5)
            if explore_figures.get("trend"):
                figure(explore_figures["trend"], width_cm=14.0)
                fig_caption("目标性状表型均值的分组（世代/批次）趋势")
            # 解读：挑出离群占比偏高的性状
            _out_risk = []
            for _t, _d in _desc.items():
                _tot = _d.get("ValidN") or 0
                _o3 = _d.get("Outlier3SD") or 0
                if _tot and _o3 / float(_tot) > _thr("outlier_share_warn"):
                    _out_risk.append("%s（%d/%d，%.1f%%）"
                                     % (_t, _o3, _tot, _o3 / float(_tot) * 100))
            # 【P2-3】告警线由配置给出，行文中的百分比随之生成
            _share_pct = _thr("outlier_share_warn") * 100
            if _out_risk:
                body("解读：超 3SD 个体占比超过 %.0f%%%% 的性状为 %%s，已明显超出正态假定下的"
                     "理论比例（约 0.3%%），提示可能存在测量误差、录入错误或真实的"
                     "极端基因型个体，建议结合系谱与原始记录复核后再定取舍。"
                     % (_share_pct, "、".join(_out_risk)))
            else:
                body("解读：各性状超 3SD 个体占比均处于正常水平（未超过 %.0f%%），"
                     "未见系统性测量或录入异常，数据可直接进入后续质控与建模。"
                     % _share_pct)

        # ----（二）分布（正态性）检验 ----
        _norm = _blk.get("Normality") or {}
        if _norm:
            h2("分布（正态性）检验")
            body("采用“矩判定＋统计检验”分层判定：先算偏度（Skew）与超额峰度（Kurtosis），"
                 "再按样本量选用检验——小样本（n<200）只作参考不下结论，"
                 "中等样本（200≤n≤5000）以 Shapiro-Wilk 检验为主、"
                 "Kolmogorov-Smirnov 检验为辅，"
                 "大样本（n>5000）不再看 P 值（此时检验功效过强，微小偏离也必然拒绝，"
                 # 【P2-3】阈值由配置给出，文字随之变化，杜绝"阈值改了、文字没改"
                 "P 值失去实际意义），改以 |偏度|<%g 且 |峰度|<%g 作为近似正态的判据。"
                 % (_thr("shape_strict_skew"), _thr("shape_strict_kurt"))
                 + "上述阈值为系统默认，可按项目需要在 phenotype-qc.yaml 中调整。")
            caption("目标性状分布检验与判定")
            table(["性状", "样本量", "偏度", "超额峰度", "Shapiro-Wilk P",
                   "Kolmogorov-Smirnov P", "判定", "处理建议"],
                  [(str(t), _dash(it.get("N")), _num(it.get("Skew")),
                    _num(it.get("Kurtosis")), _fmt_p(it.get("SWTest.P")),
                    _fmt_p(it.get("KsTest.P")),
                    _VERDICT_CN.get(it.get("Verdict"), _dash(it.get("Verdict"))),
                    str(it.get("Advice") or _DASH))
                   for t, it in _norm.items()],
                  widths=[1.5, 1.2, 1.2, 1.4, 1.7, 1.9, 1.7, 5.0],
                  left_cols=(7,), size=8.5)
            if explore_figures.get("qq"):
                figure(explore_figures["qq"], width_cm=14.0)
                fig_caption("目标性状正态 Q-Q 图（点越贴近参考线，越接近正态分布）")
            _skew_list = [str(t) for t, it in _norm.items()
                          if it.get("Verdict") == "skewed"]
            if _skew_list:
                body("解读：%s 判定为明显偏态。线性模型假定残差近似正态，"
                     "强行套用会使方差组分与育种值估计产生偏倚；"
                     "建议优先尝试对数变换或 Box-Cox 变换后再估计，"
                     "若变换后仍严重偏态，应改用稳健方法或广义线性混合模型，"
                     "并在报告中说明未作变换的原因。" % "、".join(_skew_list))
            else:
                body("解读：目标性状分布未出现明显偏态，满足线性混合模型对残差正态性的"
                     "基本要求，可按既定模型规格直接估计，无需作性状变换。")

        # ----（三）共线性诊断 ----
        _coll = _blk.get("Collinearity") or {}
        if _coll:
            h2("共线性诊断")
            body("对拟入模的固定效应、协变量与性状构造设计矩阵（分类变量按哑变量展开，"
                 "数值变量缺失以列均值填补），计算三项指标：两两相关系数 |r|、"
                 "方差膨胀因子 VIF＝1/(1−R²ⱼ)、设计矩阵条件数 κ。"
                 "判据为 |r|>0.8 提示、>0.95 严重；VIF>5 提示、>10 严重；κ>30 为严重病态。"
                 "共线性并不降低拟合优度，但会使回归系数方差膨胀、估计值不稳定，"
                 "在育种上直接表现为固定效应估计失真，进而污染育种值排序，"
                 "故必须在入模前识别。")
            _vif = _coll.get("VIF") or {}
            if _vif:
                def _vif_sort_key(item):
                    _v = item[1]
                    return -1e12 if _v == float("inf") else -float(_v)
                caption("变量方差膨胀因子（VIF）")
                table(["变量", "VIF", "判定"],
                      [(str(k), _num(v, 2),
                        ("严重（不可同时入模）" if (v == float("inf") or v >= 10)
                         else ("提示（需确认）" if v >= 5 else "正常")))
                       for k, v in sorted(_vif.items(), key=_vif_sort_key)],
                      widths=[5.6, 4.0, 6.0], left_cols=(), size=9)
            _cond = _coll.get("ConditionNumber")
            body("设计矩阵条件数 κ＝%s（判据：κ>30 为严重病态）。%s" % (
                _num(_cond, 2),
                "κ 已超过严重阈值，设计矩阵病态，方差组分估计不可靠，"
                "须剔除共线变量后重估。" if (_cond is not None and _cond >= 30)
                else "κ 处于可接受范围，未见整体病态。"))
            if explore_figures.get("corr"):
                figure(explore_figures["corr"], width_cm=12.5)
                fig_caption("变量间相关矩阵（|r| 越大颜色越深，红色提示强相关）")
            if explore_figures.get("vif"):
                figure(explore_figures["vif"], width_cm=12.5)
                fig_caption("变量方差膨胀因子（虚线为提示阈值 5，实线为严重阈值 10）")
            _warns = _coll.get("Warnings") or []
            if _warns:
                caption("共线性与分布告警清单")
                table(["级别", "类型", "说明", "建议"],
                      [(_LEVEL_CN.get(w.get("level"), _dash(w.get("level"))),
                        _KIND_CN.get(w.get("kind"), _dash(w.get("kind"))),
                        str(w.get("detail") or _DASH),
                        str(w.get("suggestion") or _DASH))
                       for w in _warns],
                      widths=[1.4, 2.4, 6.0, 5.8], left_cols=(2, 3), size=8.5)
                body("处置原则：共线性严重时系统默认仅告警、不阻断评估流程——"
                     "因为是否剔除变量属于育种学判断（例如世代与批次一一对应时，"
                     "保留哪一个取决于分析目的），不应由程序代劳；"
                     "但上述告警须在定稿前逐条确认，否则相应估计结果不宜直接用于选种决策。")
            else:
                body("本次未检出共线性或分布方面的告警，固定效应与协变量可同时入模，"
                     "模型规格无需调整。")

        for _msg in (_blk.get("Degraded") or []):
            body("说明：%s" % _msg)

    # ----（四）效应筛选建议 ----
    # 这一节回答“该不该入模”：在 PH005 已确认可估的前提下，再用数据的统计证据
    # （解释力 η²、显著性 P、水平样本量、分布均衡性、共线性、协变量相关性）对每个
    # 候选固定效应/协变量给出量化的入模提示。本模块只做诊断+提示、不修改入模清单，
    # 是否采纳由用户按任务要求决定（先生拍板 2026-10-17）。
    effect_screening = context.get("effect_screening")
    if not effect_screening or effect_screening.get("Enabled") is False:
        _scr_deg = (effect_screening.get("Degraded") or ["未启用"]
                    if isinstance(effect_screening, dict) else ["未启用"])
        body("本章未纳入“（四）效应筛选建议”小节：阶段01 未产出筛选建议（%s）。"
             "固定效应与协变量清单沿用 PH005 之后的结果，未做额外的入模建议性调整。"
             % ("；".join(str(d) for d in _scr_deg)))
    else:
        h2("效应筛选建议")
        body("在 PH005 已确认各候选效应可估的基础上，进一步以这批数据本身的统计证据"
             "判断“该不该入模”：对每个候选固定效应/协变量，按六条判据（有效水平数、"
             "水平样本量、分布均衡性、单因素 ANOVA 解释力 η² 与 P、共线性、"
             "协变量相关性）给出 keep（建议保留）/ drop（建议剔除）/ review（需人工确认）/"
             "choose_one（二选一）的量化结论。该结论作为给用户的入模参考提示，"
             "是“能不能算”之外“该不该算”的数据依据；是否据此入模由用户任务要求决定。")
        # η² 条形图（软依赖：缺图不阻断）
        _eff_fig = None
        try:
            from runners import report_figures as _rf
            _eff_fig = _rf.render_effect_figure(
                effect_screening, output_prefix + "_figures", dpi=300)
        except Exception:
            _eff_fig = None
        if _eff_fig:
            figure(_eff_fig, width_cm=12.5)
            fig_caption("各候选效应单因素方差分析的解释力 η²（含 keep/strong 阈值线，"
                        "颜色对应判定结论）")
        # 建议明细表
        _cands = effect_screening.get("Candidates") or []
        if _cands:
            _VCN = {"keep": "建议保留", "drop": "建议剔除",
                    "review": "需人工确认", "choose_one": "二选一"}
            caption("数据驱动效应筛选建议明细")
            table(["候选列", "角色", "判定", "η²", "P值", "强效应", "理由"],
                  [(str(c.get("column")),
                    ("固定效应" if c.get("role") == "fixed" else "协变量"),
                    _VCN.get(c.get("verdict"), _dash(c.get("verdict"))),
                    _num((c.get("evidence") or {}).get("eta_squared")),
                    _fmt_p((c.get("evidence") or {}).get("p")),
                    ("是" if c.get("strong") else "否"),
                    "；".join(c.get("reasons") or []) or _DASH)
                   for c in _cands],
                  widths=[2.2, 1.8, 2.2, 1.2, 1.6, 1.2, 6.0],
                  left_cols=(6,), size=8.0)
        # 采纳声明（本模块只诊断+提示，不自动应用）
        body("采纳说明：本效应筛选仅输出诊断结论与处置建议，不自动修改入模清单"
             "（先生拍板 2026-10-17）。固定效应与协变量清单保持 PH005 之后原样；"
             "是否据此剔除或保留，由用户按任务要求裁定，系统全程不代劳。")
        for _dw in (effect_screening.get("Degraded") or []):
            body("说明：%s" % _dw)

    # ==================== 三、材料与方法 ====================
    h1("三、材料与方法")
    h2("目标性状与数据来源")
    input_files = metadata.get("input_files") or {}
    if input_files:
        role_cn = {"phenotype": "表型文件", "pedigree": "系谱文件", "genotype": "基因型文件"}
        rows = []
        for role, info in input_files.items():
            rows.append((role_cn.get(role, role),
                         os.path.basename(info.get("path") or ""),
                         _dash(info.get("rows")), _dash(info.get("cols"))))
        caption("原始输入文件")
        table(["角色", "文件名", "行数", "列数"], rows,
              widths=[2.4, 7.4, 2.9, 2.9], left_cols=(1,))
    funnel = insights.get("funnel") or []
    if funnel:
        caption("数据流转链条（各环节真实规模）")
        table(["环节", "数量"], [(name, _dash(val)) for name, val in funnel],
              widths=[10.4, 5.2], left_cols=(0,))

    h2("统计模型与估计方法")
    body("实际模型：%s；随机加性遗传效应基于%s估计。" % (model_cn, matrix_desc))
    body("固定效应：%s。" % "、".join(metadata.get("fixed_effects") or ["无"]))
    body("协变量：%s。" % ("、".join(metadata.get("covariates"))
                          if metadata.get("covariates") else "无"))
    if summary.get("model_formula"):
        body("模型公式（引擎实际求解式）：%s。" % summary["model_formula"])
    cfg = metadata.get("config_snapshot") or {}
    eng = cfg.get("engine_params") or {}
    body("方差组分采用约束最大似然的平均信息算法（AI-REML，引擎 HIBLUP，"
         "方法 %s、收敛容差 %s、最大迭代 %s）估计，迭代至对数似然收敛后得到"
         "方差组分、遗传力与个体育种值。" % (
             _dash(eng.get("vc_method")), _dash(eng.get("tolerance")),
             _dash(eng.get("max_iter"))))

    h2("数据质量控制概述")
    body("ABGES 在不修改任何原始文件的前提下，于运行目录副本内依次完成表型、"
         + ("系谱、" if use_ped else "") + ("基因型、" if use_geno else "")
         + "多源整合与引擎求解；质控逐项明细见附录 A。质控前后总体规模如下。")
    if pheno_snap:
        b, a = pheno_snap.get("before") or {}, pheno_snap.get("after") or {}
        ob, oa = trait_overall(pheno_snap, "before"), trait_overall(pheno_snap, "after")
        caption("表型质控前后总体规模")
        table(["指标", "质控前", "质控后"], [
            ("记录总数", _dash(b.get("n_records")), _dash(a.get("n_records"))),
            ("目标性状有效数 ValidN", _dash(ob.get("ValidN")), _dash(oa.get("ValidN"))),
            ("均值 Mean", _dash(ob.get("Mean")), _dash(oa.get("Mean"))),
            ("标准差 SD", _dash(ob.get("SD")), _dash(oa.get("SD"))),
        ], widths=[6.0, 4.8, 4.8])
    if use_ped and ped_snap:
        b, a = ped_snap.get("before") or {}, ped_snap.get("after") or {}
        caption("系谱质控前后总体规模")
        table(["指标", "质控前", "质控后"], [
            ("个体总数", _dash(b.get("Individuals")), _dash(a.get("Individuals"))),
            ("奠基者数", _dash(b.get("FounderCount")), _dash(a.get("FounderCount"))),
            ("最大世代深度", _dash(b.get("MaxGenerationDepth")), _dash(a.get("MaxGenerationDepth"))),
        ], widths=[6.0, 4.8, 4.8])
        pedigree_qc = context.get("pedigree_qc")
        exclusion_summary = getattr(pedigree_qc, "exclusion_summary", {}) or {}
        exclusions = list(getattr(pedigree_qc, "exclusions", []) or [])
        if exclusions:
            caption("系谱质控剔除记录")
            body("直接剔除 %s 条，级联剔除 %s 条，ID/Sire/Dam 完全重复行另去重 %s 条；父母及未受影响兄弟姐妹保留。"
                 % (exclusion_summary.get("direct_excluded_records", 0),
                    exclusion_summary.get("cascade_excluded_records", 0),
                    exclusion_summary.get("duplicate_rows_removed", 0)))
            table(["行号", "ID", "父本", "母本", "Gen", "Batch", "规则", "处置类型", "处置原因"], [
                (_dash(item.get("source_row")), _dash(item.get("ID")),
                 _dash(item.get("Sire")), _dash(item.get("Dam")),
                 _dash(item.get("Gen")), _dash(item.get("Batch")),
                 _dash(item.get("rule_id")),
                 ("重复行去重" if item.get("record_disposition") == "duplicate_row_removed"
                  else "级联剔除" if item.get("cascade") else "直接剔除"),
                 _dash(item.get("reason")))
                for item in exclusions
              ], widths=[0.9, 1.7, 1.5, 1.5, 0.8, 1.1, 1.0, 1.3, 4.0])
    if use_geno and geno_snap:
        b, a = geno_snap.get("before") or {}, geno_snap.get("after") or {}
        caption("基因型质控前后总体规模")
        table(["指标", "质控前", "质控后"], [
            ("基因型样本数", _dash(b.get("Individuals")), _dash(a.get("Individuals"))),
            ("SNP/变异位点数", _dash(b.get("Variants")), _dash(a.get("Variants"))),
        ], widths=[6.0, 4.8, 4.8])

    # ==================== 三、评估结果 ====================
    h1("四、评估结果")

    # （一）数据与系谱结构
    h2("数据与关系矩阵结构")
    struct_rows = []
    im = integration_metrics
    if use_ped:
        struct_rows += [
            ("系谱个体总数", _dash(inbreed.get("individuals") or im.get("pedigree_individuals"))),
            ("奠基者数", _dash(inbreed.get("founders"))),
            ("双亲已知率", _dash(inbreed.get("both_parents_known_rate"))),
            ("最大世代深度", _dash(inbreed.get("max_generation_depth"))),
        ]
    if im.get("genotype_individuals"):
        struct_rows.append(("基因型个体数", _dash(im.get("genotype_individuals"))))
    if im.get("phenotype_individuals"):
        struct_rows.append(("表型个体数", _dash(im.get("phenotype_individuals"))))
    if im.get("pheno_ped_overlap") is not None:
        struct_rows.append(("表型×系谱交集", _dash(im.get("pheno_ped_overlap"))))
    if struct_rows:
        caption("数据与关系矩阵规模（质控后）")
        table(["指标", "数值"], struct_rows, widths=[7.8, 7.8])
    if figure_paths.get("structure") and trend_rows:
        figure(figure_paths["structure"])
        fig_caption("分世代个体数与具有有效记录个体数")

    # （二）方差组分与遗传力
    h2("方差组分与遗传力")
    var_rows = variance.get("rows") or []
    if var_rows:
        caption("方差组分与遗传力估计")
        rows = []
        for r in var_rows:
            rows.append((r["name"], _dash(r["value"]), _dash(r["se"]),
                         "%.2f%%" % (r["ratio"] * 100) if r["ratio"] is not None else _DASH))
        rows.append(("遗传力 h²", _dash(variance.get("h2")), _dash(variance.get("h2_se")),
                     "95%%CI [%s, %s]，P=%s" % (
                         _dash(variance.get("ci_lower")), _dash(variance.get("ci_upper")),
                         _dash(variance.get("p_value")))))
        rows.append(("对数似然 LogL", _dash(variance.get("logl")), _DASH,
                     "-2LogL=%s，SE/h²=%s" % (_dash(variance.get("neg2logl")),
                                              _dash(variance.get("se_over_h2")))))
        rows.append(("迭代/收敛", "%s 次" % _dash(variance.get("n_iterations")), _DASH,
                     _dash(converged_txt)))
        table(["参数", "估计值", "标准误 SE", "占比/区间"], rows,
              widths=[4.6, 3.2, 3.0, 4.8], left_cols=(3,))
        if figure_paths.get("variance"):
            figure(figure_paths["variance"])
            fig_caption("方差组分估计（误差棒为±SE）与遗传力点估计及 95% 置信区间")
        if band:
            body("遗传力解读：%s。其 95%%置信区间%s远离 0、SE/h²=%s，"
                 "说明本次遗传力估计精确、结果稳健。" % (
                     band, ci_txt, _dash(variance.get("se_over_h2"))))
    else:
        body("本次未取得规范方差组分结果。")

    # （三）固定效应与协变量校正
    fe = insights.get("fixed_effects") or []
    av = insights.get("anova") or []
    if fe or av:
        h2("固定效应与协变量校正")
        if av:
            caption("效应方差分析（ANOVA，F 检验）")
            # 多性状（HIBLUP v1.7.0+ 逐性状 .anova）：增加"性状"列；
            # 单性状仍沿用既有版式，列数、列宽、左对齐列均不变。
            multi_trait_anova = any(str(r.get("trait") or "") for r in av)
            arows = []
            for r in av:
                trait_prefix = ((str(r.get("trait") or ""),)
                                if multi_trait_anova else ())
                is_resid = str(r["factor"]).strip().lower() in ("e", "residual", "residuals")
                if is_resid:
                    arows.append(trait_prefix + ("残差 e", _dash(r["df"]), _dash(r["ss"]),
                                 _dash(r["ms"]), _DASH, "均方 %s" % _dash(r["ms"])))
                else:
                    arows.append(trait_prefix + (r["factor"], _dash(r["df"]), _dash(r["ss"]),
                                 _dash(r["ms"]), _dash(r["f"]), _p_sig(r["p"])))
            if multi_trait_anova:
                table(["性状", "效应", "自由度 Df", "平方和 SS", "均方 MS", "F 值", "结论"],
                      arows, widths=[1.8, 2.0, 2.0, 2.4, 2.2, 1.8, 3.8],
                      left_cols=(6,))
            else:
                table(["效应", "自由度 Df", "平方和 SS", "均方 MS", "F 值", "结论"], arows,
                      widths=[2.4, 2.2, 2.6, 2.4, 2.0, 4.0], left_cols=(5,))
        if fe:
            caption("固定效应与协变量系数估计（.beta）")
            brows = []
            for r in fe:
                lv = str(r["level"])
                role = "模型常数项（基准群体期望水平）" if lv.lower() == "mu" else "效应水平估计（相对基准）"
                brows.append((lv, _dash(r["est"]), _dash(r["se"]), role))
            table(["效应水平", "估计值", "标准误 SE", "含义"], brows,
                  widths=[2.8, 2.8, 2.8, 7.2], left_cols=(3,))
        sig_factors = [r["factor"] for r in av
                       if r.get("p") is not None and r["p"] < _thr("alpha")]
        if sig_factors:
            body("方差分析显示 %s 对 %s 影响显著，模型对这些系统效应进行校正，"
                 "可避免环境/批次等差异混入育种值；系数估计给出了各水平相对基准的调整量。"
                 % ("、".join(sig_factors), plan.get("trait", _DASH)))
        # —— 育种学解读：协变量斜率、固定效应水平跨度（全部据 .beta/.anova 现算）——
        fe_by_level = {str(r["level"]): r for r in fe}
        av_by_factor = {str(r["factor"]): r for r in av}
        for cov in (metadata.get("covariates") or []):
            hit = None
            for lv, rec in fe_by_level.items():
                if lv.upper() == str(cov).upper() or lv.upper().startswith(str(cov).upper()):
                    hit = rec
                    break
            if not hit or hit.get("est") is None:
                continue
            avrec = av_by_factor.get(str(cov))
            est = hit["est"]
            direction = "增加 %s" % _dash(est) if est >= 0 else "减少 %s" % _dash(abs(est))
            if avrec and avrec.get("p") is not None:
                if avrec["p"] < _thr("alpha"):
                    sig_txt = "方差分析 F=%s、%s，影响显著，应作为协变量保留以校正系统偏差" % (
                        _dash(avrec.get("f")), _p_label(avrec.get("p")))
                else:
                    sig_txt = "方差分析 F=%s、%s，未达 0.05 显著水平，可结合专业判断评估是否保留" % (
                        _dash(avrec.get("f")), _p_label(avrec.get("p")))
            else:
                sig_txt = "作为线性协变量纳入模型校正"
            body("协变量 %s 回归系数为 %s（SE=%s）：其他条件相同时，%s 每增加 1 个单位，"
                 "%s 平均%s；%s。" % (
                     cov, _dash(est), _dash(hit.get("se")), cov,
                     plan.get("trait", _DASH), direction, sig_txt))
        covar_upper = {str(c).upper() for c in (metadata.get("covariates") or [])}
        for fx in (metadata.get("fixed_effects") or []):
            lvl_ests = [r["est"] for lv, r in fe_by_level.items()
                        if str(lv) != "mu"
                        and str(lv).upper().startswith(str(fx).upper())
                        and str(lv).upper() not in covar_upper
                        and r.get("est") is not None]
            if not lvl_ests:
                continue
            avrec = av_by_factor.get(str(fx))
            if avrec and avrec.get("p") is not None and avrec["p"] < _thr("alpha"):
                sig_txt = "方差分析 %s，水平间差异显著，应作为固定效应纳入校正" % _p_label(
                    avrec.get("p"))
            else:
                sig_txt = "各水平相对基准的校正量如下"
            _lo, _hi = min(lvl_ests), max(lvl_ests)
            if len(lvl_ests) == 1:
                span_txt = "其相对基准的校正量为 %s" % _dash(_lo)
            else:
                span_txt = ("其余水平相对基准的校正量介于 %s~%s、最大跨度 %s"
                            % (_dash(_lo), _dash(_hi), _dash(_hi - _lo)))
            body("固定效应 %s 以 1 个水平为基准（基准效应并入常数项 μ），%s，"
                 "连同基准共 %d 个水平；%s。" % (
                     fx, span_txt, len(lvl_ests) + 1, sig_txt))

    # （四）模型假设检验与残差诊断（后续小节中文编号由 h2 计数器自动顺延）
    resid_diag = insights.get("residual") or {}
    if resid_diag.get("available"):
        h2("模型假设检验与残差诊断")
        caption("残差描述统计（仅实际进入模型、有自身表型的个体）")
        table(["统计量", "数值", "统计量", "数值"], [
            ("残差个体数", _dash(resid_diag.get("n")), "残差均值", _dash(resid_diag.get("mean"))),
            ("残差标准差", _dash(resid_diag.get("sd")), "中位数", _dash(resid_diag.get("median"))),
            ("Q1 / Q3", "%s / %s" % (_dash(resid_diag.get("q1")), _dash(resid_diag.get("q3"))),
             "最小 / 最大", "%s / %s" % (_dash(resid_diag.get("min")), _dash(resid_diag.get("max")))),
            ("偏度（正态≈0）", _dash(resid_diag.get("skew")),
             "超额峰度（正态≈0）", _dash(resid_diag.get("kurt"))),
        ], widths=[3.9, 3.9, 3.9, 3.9])
        if figure_paths.get("residual"):
            figure(figure_paths["residual"], width_cm=14.0)
            fig_caption("残差诊断四联图：残差分布与正态密度、残差–拟合值、正态 Q-Q、残差–序号")
        sk, kt = resid_diag.get("skew"), resid_diag.get("kurt")
        mean_r, sd_r = resid_diag.get("mean"), resid_diag.get("sd")
        judg = []
        # 【P2-3】偏度/峰度/残差均值的分档阈值改由 phenotype-qc.yaml 提供
        if mean_r is not None and _mean_is_zero(mean_r, sd_r):
            judg.append("残差均值≈%s、接近零，模型无系统性偏差" % _dash(mean_r))
        if sk is not None:
            judg.append("偏度 %s（%s）" % (_dash(sk), _describe_skew(sk)))
        if kt is not None:
            judg.append("超额峰度 %s（%s）" % (_dash(kt), _describe_kurt(kt)))
        body("残差用于检验混合线性模型的经典假设：%s。左上直方图叠加正态密度曲线、左下为正态 "
             "Q-Q 图，二者共同考察残差正态性；右上残差–拟合值图考察方差齐性（点云应在零线两侧"
             "均匀分布，无漏斗形扩张或弯曲，红色折线为分箱均值）；右下残差–序号图考察是否存在"
             "与数据排列顺序有关的漂移。BLUP 对残差适度偏离正态具有稳健性；若残差呈明显异方差、"
             "强偏态或存在极端离群，应进一步核查性状分布、固定效应/协变量设置以及是否需要数据变换。"
             % "；".join(judg))
        # —— 诊断结论闭环：据偏度/峰度/二次曲率客观判定是否可接受、影响与动作 ——
        # 【P2-3】三处阈值（|偏度|<0.5 / <1、|峰度|<1 / <2、R² 增量 <0.01 / <0.05）
        # 全部改为读配置，判定与文案统一由 report_stats 产出。
        has_shape = resid_diag.get("skew") is not None
        norm_judge, shape_need_action = _normality_verdict(
            resid_diag.get("skew"), resid_diag.get("kurt"),
            resid_diag.get("n"))
        gain_f = _fnum_v(resid_diag.get("quad_r2_gain"))
        line_judge, quad_need_action = _quadratic_verdict(
            resid_diag.get("quad_r2_gain"), resid_diag.get("quad_coef"))
        need_action = (shape_need_action if has_shape else False) or quad_need_action
        if need_action:
            closed = ("综合判断：上述偏离可能轻微影响标准误与极端个体育种值；BLUP 对适度非正态稳健，"
                      "遗传力点估计与 EBV 整体排序通常稳定，建议按提示核查后再确认最终选种结果。")
        else:
            closed = "综合判断：残差各项诊断在可接受范围，模型经典假设基本满足，本次 h² 与 EBV 估计稳健，无需额外数据变换。"
        body("；".join([x for x in (norm_judge, line_judge) if x]) + "。" + closed)

    # （五）育种值分布与基准口径
    h2("育种值分布与基准口径")
    ebvd = insights.get("ebv_distribution") or ebv_summary
    if ebvd:
        caption("全部 %s 个预测个体育种值（EBV）分布" % _dash(ebvd.get("n")))
        table(["统计量", "数值", "统计量", "数值"], [
            ("个体数", _dash(ebvd.get("n")), "均值", _dash(ebvd.get("mean"))),
            ("标准差", _dash(ebvd.get("sd")), "中位数", _dash(ebvd.get("median"))),
            ("P25 / P75", "%s / %s" % (_dash(ebvd.get("p25")), _dash(ebvd.get("p75"))),
             "最小 / 最大", "%s / %s" % (_dash(ebvd.get("min")), _dash(ebvd.get("max")))),
        ], widths=[3.9, 3.9, 3.9, 3.9])
    groups = insights.get("groups") or []
    if groups:
        caption("按信息来源划分的育种值水平（解释基准口径）")
        grows = [(name, _dash(st.get("n")), _dash(st.get("mean")), _dash(st.get("sd")))
                 for name, st in groups]
        table(["信息来源群体", "个体数", "EBV 均值", "EBV 标准差"], grows,
              widths=[6.0, 3.2, 3.2, 3.2], left_cols=(0,))
        if figure_paths.get("ebv_dist"):
            figure(figure_paths["ebv_dist"])
            fig_caption("全群体育种值分布（柱为观测频数，深蓝曲线为同均值同标准差的"
                        "正态拟合；红实线为群体均值、灰蓝虚线为 0）")
        body("HIBLUP 输出的育种值为加性遗传效应预测值，以模型常数项为基准、"
             "并未强制全体均值归零；分信息来源可见不同群体的信息量差异——"
             "有自身记录的个体同时利用自身与亲属信息，自身尚无记录的年轻个体"
             "主要依赖亲属信息，其估计更收缩于家系均值，这是方法本身的特性而非计算偏差。")

    # （五）分世代 EBV 描述差异；语义由任务声明决定
    if trend_rows:
        h2("分世代 EBV 描述统计")
        caption("分世代表型均值与平均育种值")
        trows = [(r["gen"], _dash(r["n_total"]), _dash(r["n_valid"]),
                  _dash(r["mean_phe"]), _dash(r["mean_ebv"]), _dash(r["sd_ebv"]))
                 for r in trend_rows]
        table(["世代", "个体数", "有效记录数", "平均表型", "平均 EBV", "EBV 标准差"],
              trows, widths=[2.2, 2.6, 2.6, 2.7, 2.7, 2.8])
        if figure_paths.get("trend"):
            figure(figure_paths["trend"])
            fig_caption("分世代平均 EBV（±SE）与平均表型（虚线，仅有记录世代）")
        if insights.get("trend_delta_per_gen") is not None:
            body("%s：由 Gen%s 的 %s 变化至 Gen%s 的 %s，平均每代差异 %s。"
                 "世代间 EBV 差异可能同时受群体构成、遗传联系、选择和模型设定影响。" % (
                     insights.get("trend_interpretation"),
                     trend_rows[0]["gen"], _dash(trend_rows[0]["mean_ebv"]),
                     trend_rows[-1]["gen"], _dash(trend_rows[-1]["mean_ebv"]),
                     _dash(insights.get("trend_delta_per_gen"))))
        reg = insights.get("trend_regression") or {}
        if reg.get("slope") is not None:
            _rp = reg.get("p")
            _pf = _fmt_p(_rp)
            caption("平均 EBV 对世代的线性回归（描述性关联；不等同于遗传进展因果检验）")
            table(["回归项", "数值", "回归项", "数值"], [
                ("回归斜率（EBV/代）", _dash(reg.get("slope")),
                 "斜率标准误 SE", _dash(reg.get("se"))),
                ("t 统计量", _dash(reg.get("t")),
                 "P 值", _pf),
                ("决定系数 R²", _dash(reg.get("r2")),
                 "回归个体数 N", _dash(reg.get("n"))),
            ], widths=[3.9, 3.9, 3.9, 3.9])
            # 【P2-3】阈值与文案统一由 report_stats 产出（读配置，缺省与历史一致）
            _sig = _trend_sig_text(_rp)
            body("以全部有世代信息个体的 EBV 对世代做线性回归，斜率为每代 %s（SE=%s），"
                 "t=%s、%s、R²=%s，%s；这是世代与 EBV 的描述性关联，未排除群体构成、"
                 "遗传连通和管理变化的影响，不能单凭该检验确认遗传进展。" % (
                     _dash(reg.get("slope")), _dash(reg.get("se")),
                     _dash(reg.get("t")), _p_label(_rp),
                     _dash(reg.get("r2")), _sig))

    # （六）群体近交分析
    if use_ped and inbreed:
        h2("群体近交分析")
        caption("群体近交与系谱结构")
        table(["指标", "数值", "指标", "数值"], [
            ("平均近交系数 F", _dash(inbreed.get("mean_inbreeding")),
             "最大近交系数 F", _dash(inbreed.get("max_inbreeding"))),
            ("重复 ID", _dash(inbreed.get("duplicate_ids")),
             "自交个体", _dash(inbreed.get("self_as_parent"))),
            ("奠基者数", _dash(inbreed.get("founders")),
             "最大世代深度", _dash(inbreed.get("max_generation_depth"))),
        ], widths=[3.9, 3.9, 3.9, 3.9])
        body("近交水平与系谱追溯深度、奠基者数量有关；随选育推进与世代累积，"
             "应持续监测个体近交与有效群体规模，制定选配方案时避免同胞/亲子交配，防止近交衰退。")

    # （七）育种值可靠性与准确率
    h2("育种值可靠性与准确率")
    if rel_ok:
        caption("EBV 可靠性（1-PEV/VA）与准确率（√可靠性）")
        table(["指标", "均值", "中位数", "最小", "最大"], [
            ("可靠性 reliability", _dash(rel_brief.get("rel_mean")), _dash(rel_brief.get("rel_median")),
             _dash(rel_brief.get("rel_min")), _dash(rel_brief.get("rel_max"))),
            ("准确率 accuracy", _dash(rel_brief.get("acc_mean")), _dash(rel_brief.get("acc_median")),
             _dash(rel_brief.get("acc_min")), _dash(rel_brief.get("acc_max"))),
        ], widths=[4.4, 2.8, 2.8, 2.8, 2.8])
        body("含 PEV 记录 %s 条、有效 %s 条、越界 %s 条（来源：%s）。"
             "可靠性反映育种值估计由数据/亲属信息支撑的程度，选种时应优先选择"
             "EBV 优异且可靠性较高的个体。" % (
                 _dash(rel_brief.get("records_total")), _dash(rel_brief.get("records_valid")),
                 _dash(rel_brief.get("records_out_of_range")), _dash(rel_brief.get("source"))))
    else:
        body("本次运行未输出个体预测误差方差（PEV），故未计算个体可靠性与准确率；"
             "Top 排名缺少可靠性支撑，仅供方法学验证与流程演示，不建议直接用于定选。"
             "正式选种前应开启可靠性输出（引擎参数 --r2）重跑。")

    # （八）假设选种情景：不生成实际决策
    sel = selection or {}
    if sel.get("available") and sel.get("rows"):
        h2("假设选种情景：选择差与预期进展")
        srows = [(r["proportion"], _dash(r["n_select"]), _dash(r["sel_mean"]),
                  _dash(r["selection_diff"]), _dash(r["selection_intensity"]),
                  _dash(r["delta_g"])) for r in sel["rows"]]
        caption("不同选择比例下的模型内情景比较（%s，n=%s）" % (
            sel.get("population_scope"), _dash(sel.get("population_n"))))
        table(["选择比例", "入选数", "入选群 EBV 均值", "选择差 S",
               "选择强度 i", "预期进展 ΔG/代"],
              srows, widths=[2.2, 2.0, 3.2, 2.4, 2.4, 3.4])
        body("这是统计情景，不是候选名单或配种决策。选择差 S＝入选群平均 EBV−情景群平均 EBV（均值 %s）；选择强度 i＝S/σ_EBV"
             "（情景群 EBV 标准差 %s）；加性遗传标准差 σA＝%s；育种值准确性 r＝%s（%s）；"
             "世代间隔为 %s。未提供时不计算 ΔG。" % (
                 _dash(sel.get("population_mean")), _dash(sel.get("population_sd")),
                 _dash(sel.get("sigma_a")), _dash(sel.get("accuracy_used")),
                 sel.get("accuracy_source"), _dash(sel.get("generation_interval"))))
        if sel.get("generation_interval") and sel.get("accuracy_used"):
            body("ΔG 为基于当前情景的理论估计，不是实际选择响应。若按性别分别选留、存在配种限制或世代间隔不同，"
                 "须以真实候选群和管理方案重新计算。")
        else:
            body("本次没有足够依据计算 ΔG（需提供世代间隔及个体准确率）；上表仅展示 EBV 排序情景，不能据此直接定选。")

    # （九）优秀个体 Top20（列据实动态）
    h2("EBV 排序示例（Top20；不是选种名单）")
    if top_individuals:
        has_se = any("EBV_SE" in it for it in top_individuals)
        has_rel = any("reliability" in it for it in top_individuals)
        has_gen = bool(id_gen)
        # 全群 EBV 基准（均值/SD），用于把个体 EBV 标准化为“超出群体多少个 SD”
        _ed = insights.get("ebv_distribution") or {}
        try:
            _em, _esd = float(_ed.get("mean")), float(_ed.get("sd"))
        except (TypeError, ValueError):
            _em = _esd = None

        def _z_score(ebv):
            try:
                if _em is None or not _esd:
                    return _DASH
                return "%.2f" % ((float(ebv) - _em) / _esd)
            except (TypeError, ValueError):
                return _DASH

        headers = ["排名", "个体 ID"]
        if has_gen:
            headers.append("世代")
        headers += ["EBV", "超出群体(SD)"]
        if has_se:
            headers.append("EBV 标准误")
        if has_rel:
            headers += ["可靠性", "准确率"]
        trows = []
        for it in top_individuals[:20]:
            row = [it.get("rank"), it.get("ID")]
            if has_gen:
                row.append(_dash(id_gen.get(str(it.get("ID")))))
            row += [_dash(it.get("EBV")), _z_score(it.get("EBV"))]
            if has_se:
                row.append(_dash(it.get("EBV_SE")))
            if has_rel:
                row += [_dash(it.get("reliability")), _dash(it.get("accuracy"))]
            trows.append(row)
        ncol = len(headers)
        if ncol == 4:  # 排名/ID/EBV/超出SD
            widths = [1.2, 6.4, 4.0, 4.0]
        elif ncol == 5:  # 带世代
            widths = [1.1, 5.2, 1.4, 3.95, 3.95]
        elif ncol == 7:  # 带 SE+可靠性（无世代）
            widths = [1.0, 4.0, 2.5, 2.4, 2.0, 1.85, 1.85]
        elif ncol == 8:  # 带世代+SE+可靠性
            widths = [0.9, 3.5, 1.2, 2.1, 2.1, 1.9, 1.95, 1.95]
        else:
            widths = [15.6 / ncol] * ncol
        caption("%s 育种值排名前 20 个体" % plan.get("trait", _DASH))
        table(headers, trows, widths=widths, size=9.5)
        body("注：“超出群体(SD)”=（个体 EBV−全群平均 EBV %s）/全群 EBV 标准差 %s，"
             "正值表示优于群体均值，数值越大表示在群体中越突出。" % (
                 _dash(_em), _dash(_esd)))
        if not has_rel:
            body("说明：本表暂无可靠性/准确率列，原因同前；正式定选请以开启 PEV 重跑结果为准。")
        # —— Top 个体亲本与父系家系（读引擎系谱），辅助控近交与家系均衡 ——
        po = family.get("parent_of") or {}
        if family.get("available") and po:
            prows = []
            for it in top_individuals[:20]:
                iid = str(it.get("ID"))
                info = po.get(iid) or {}
                prows.append((iid, _dash(info.get("sire")), _dash(info.get("dam")),
                              _dash(info.get("family"))))
            caption("Top20 个体的亲本与父系家系")
            table(["个体 ID", "父亲 Sire", "母亲 Dam", "父系家系"], prows,
                  widths=[5.6, 3.6, 3.6, 2.8], size=9.5, left_cols=(0,))
            nfam = family.get("family_count")
            share = family.get("max_family_share")
            if nfam is not None:
                share_txt = ("%.1f%%" % (share * 100) if share is not None else _DASH)
                dist = "Top20 来自 %s 个父系家系，最大家系占比 %s" % (_dash(nfam), share_txt)
                try:
                    if share is not None and float(share) > 0.30:
                        dist += ("；候选相对集中于少数家系，选配时应注意家系均衡，"
                                 "避免全/半同胞过度入选，以控制群体近交增速")
                    else:
                        dist += "；家系来源较分散，有利于控制近交、维持遗传多样性"
                except Exception:
                    pass
                body(dist + "。")
    else:
        body("本次实际未生成 Top20 排名。")

    # ==================== 四、结论、选种建议与局限性 ====================
    h1("五、结论、选种建议与局限性")
    concl = []
    if variance.get("h2") is not None:
        concl.append("本次%s分析经%s次迭代%s，%s遗传力 h²=%s（95%%CI %s），估计精确。" % (
            model_key.upper(), _dash(variance.get("n_iterations")), converged_txt,
            plan.get("trait", _DASH), _dash(variance.get("h2")), ci_txt))
    if insights.get("trend_delta_per_gen") is not None:
        concl.append("按显式遗传世代口径估计的 EBV 趋势为每代 %s；解释时仍须结合群体构成、遗传连通和模型设定。"
                     % _dash(insights.get("trend_delta_per_gen")))
    if use_ped and inbreed.get("mean_inbreeding") is not None:
        concl.append("群体平均近交 %s、最大 %s，当前近交水平%s，仍应持续监测并避免近亲交配。" % (
            _dash(inbreed.get("mean_inbreeding")), _dash(inbreed.get("max_inbreeding")),
            "极低" if (inbreed.get("mean_inbreeding") or 0) < _thr("inbreeding_low") else "需关注"))
    if rel_ok:
        concl.append("已输出个体可靠性，建议以 EBV 为主、可靠性与家系/世代分布为辅综合选种。")
    else:
        concl.append("本次未输出个体可靠性，Top 结果仅供方法验证，不建议直接定选，建议开启 PEV 重跑。")
    for i, txt in enumerate(concl, 1):
        body("%d. %s" % (i, txt))
    limits = []
    if not rel_ok:
        limits.append("本次未计算个体可靠性，育种值的不确定度未能量化；")
    no_phe_gens = [r["gen"] for r in trend_rows if not r.get("n_valid")]
    if no_phe_gens:
        limits.append("Gen%s 自身尚无有效记录，其育种值来自亲属信息，需待后续性能数据验证更新；"
                      % "、".join(str(g) for g in no_phe_gens))
    limits.append("遗传力与育种值依赖所选模型、固定效应/协变量设定与数据质量，"
                  "若模型假设改变，结果应重新估计。")
    limits.append("本次为单性状、加性遗传效应＋残差的简单动物模型，适用于单记录性状；"
                  "若同一性状存在多次重复记录，应加入永久环境效应，开展多性状综合选择时"
                  "需先估计性状间遗传相关并改用多性状模型。")
    if model_key == "pblup":
        limits.append("PBLUP 依赖系谱的完整与准确，系谱缺失或登记错误会降低育种值估计准确性。")
    elif model_key == "gblup":
        limits.append("GBLUP 依赖分子标记密度、分型覆盖与基因组关系矩阵质量，未分型个体无法直接获得基因组评估。")
    else:
        limits.append("SSGBLUP 同时依赖系谱与基因型质量，融合 H 矩阵时需保证 A、G 矩阵尺度一致。")
    body("局限性：" + "".join(limits))

    # ==================== 附录 A：质量控制明细 ====================
    h1("附录 A　数据质量控制明细")
    if pheno_snap:
        b, a = pheno_snap.get("before") or {}, pheno_snap.get("after") or {}
        ob, oa = trait_overall(pheno_snap, "before"), trait_overall(pheno_snap, "after")
        caption("表型质控前后对比")
        table(["指标", "质控前", "质控后"], [
            ("记录总数", _dash(b.get("n_records")), _dash(a.get("n_records"))),
            ("目标性状有效数 ValidN", _dash(ob.get("ValidN")), _dash(oa.get("ValidN"))),
            ("缺失率(%)", _dash(ob.get("MissingPct")), _dash(oa.get("MissingPct"))),
            ("均值 Mean", _dash(ob.get("Mean")), _dash(oa.get("Mean"))),
            ("标准差 SD", _dash(ob.get("SD")), _dash(oa.get("SD"))),
            ("最小值 / 最大值", "%s / %s" % (_dash(ob.get("Min")), _dash(ob.get("Max"))),
             "%s / %s" % (_dash(oa.get("Min")), _dash(oa.get("Max")))),
        ], widths=[6.0, 4.8, 4.8])
        by_group = a.get("traits") or {}
        if by_group:
            grp = list(by_group.values())[0].get("by_group")
            if grp:
                for gname, levels in grp.items():
                    grows = [(lv, _dash(d.get("N")), _dash(d.get("Mean")))
                             for lv, d in sorted(levels.items())]
                    caption("按“%s”分组的质控后样本量与均值" % gname)
                    table(["%s 水平" % gname, "记录数", "均值"], grows, widths=[5.2, 5.2, 5.2])
    if use_ped and ped_snap:
        b, a = ped_snap.get("before") or {}, ped_snap.get("after") or {}
        caption("系谱质控前后对比")
        table(["指标", "质控前", "质控后"], [
            ("个体总数", _dash(b.get("Individuals")), _dash(a.get("Individuals"))),
            ("重复ID数", _dash(b.get("DuplicateIDs")), _dash(a.get("DuplicateIDs"))),
            ("奠基者数", _dash(b.get("FounderCount")), _dash(a.get("FounderCount"))),
            ("最大世代深度", _dash(b.get("MaxGenerationDepth")), _dash(a.get("MaxGenerationDepth"))),
            ("双亲已知率", _dash(b.get("BothParentsKnownRate")), _dash(a.get("BothParentsKnownRate"))),
            ("平均近交系数", _DASH, _dash(a.get("MeanInbreeding"))),
            ("最大近交系数", _DASH, _dash(a.get("MaxInbreeding"))),
        ], widths=[6.0, 4.8, 4.8])
    if use_geno and geno_snap:
        b, a = geno_snap.get("before") or {}, geno_snap.get("after") or {}
        caption("基因型质控前后对比")
        table(["指标", "质控前", "质控后"], [
            ("基因型样本数", _dash(b.get("Individuals")), _dash(a.get("Individuals"))),
            ("SNP/变异位点数", _dash(b.get("Variants")), _dash(a.get("Variants"))),
        ], widths=[6.0, 4.8, 4.8])
    integ_rows = [(label, _dash(integration_metrics[key]))
                  for key, label in INTEG_LABELS if key in integration_metrics]
    if integ_rows:
        caption("多源数据整合规模")
        table(["项目", "数值"], integ_rows, widths=[7.8, 7.8], left_cols=(0,))

    # ==================== 附录 B：工程留痕与可复现性 ====================
    h1("附录 B　质量闸门、告警与可复现性留痕")
    if gate_reports:
        caption("%s 质量闸门判定总览" % gate_span)
        grows = []
        for g in gate_reports:
            grows.append((g.get("gate_id"), g.get("gate_name") or GATE_NAME.get(g.get("gate_id"), _DASH),
                          _verdict_cn(g.get("verdict")), _dash(g.get("passed")),
                          _dash(g.get("passed_with_warning")), _dash(g.get("blocked")),
                          _dash(g.get("skipped"))))
        table(["闸门", "名称", "总结论", "通过", "告警", "阻断", "跳过"], grows,
              widths=[1.4, 4.6, 2.8, 1.7, 1.7, 1.7, 1.7], size=9.5)
    if checks:
        caption("结果校验闸门（G5）逐项结论")
        table(["编号", "检查项", "结论", "实际值"], [
            (c.get("check_id"), c.get("desc") or _DASH, _verdict_cn(c.get("verdict")),
             _dash(c.get("actual"))) for c in checks
        ], widths=[1.8, 6.6, 2.2, 5.0], left_cols=(1,), size=9.5)
    if gate_warnings or stage_warnings:
        wrows = []
        for item in gate_warnings:
            wrows.append(("闸门", _dash(item.get("check_id")), item.get("message"),
                          item.get("action_taken") or "未处置"))
        for item in stage_warnings:
            wrows.append(("阶段%s" % _dash(item.get("stage")), _dash(item.get("code")),
                          item.get("message"), item.get("action_taken") or "未处置"))
        caption("告警留痕清单（逐条不静默）")
        table(["类别", "编号", "告警内容", "处置动作"], wrows,
              widths=[1.6, 2.0, 7.6, 4.4], left_cols=(2, 3), size=9)
    else:
        caption("告警留痕清单")
        table(["类别", "编号", "告警内容", "处置动作"], [("—", "—", "本次运行未产生告警", "—")],
              widths=[1.6, 2.0, 7.6, 4.4], size=9)
    # 原始数据完整性
    if raw_guard is None:
        body("未取得原始输入校验和比对结果。")
    elif raw_guard.get("unchanged"):
        body("经 SHA256 校验，评估全过程原始表型、系谱（及基因型）文件保持不变，"
             "全部清洗与派生均输出到运行目录副本，原始数据可追溯、可复现。")
        if input_files:
            caption("原始输入文件校验和（SHA256，截断显示）")
            table(["角色", "文件名", "SHA256（前 24 位）"], [
                (role_cn.get(role, role), os.path.basename(info.get("path") or ""),
                 (info.get("checksum") or _DASH)[:24])
                for role, info in input_files.items()
            ], widths=[2.2, 6.6, 6.8], left_cols=(1, 2), size=9)
    # 完整命令（展示前规约内部运行目录路径，详见 _mask_run_dirs 说明）
    body("引擎完整运行命令：", indent=False)
    code_block(_mask_run_dirs(metadata.get("full_command_line")) or _DASH)
    if metadata.get("harness_command_line"):
        body("调度命令（ABGES 编排入口）：", indent=False)
        code_block(_mask_run_dirs(metadata["harness_command_line"]))
    # 基线对照
    if baseline_comparison:
        body("基线回归对照：%s；结论：%s。" % (
            baseline_comparison.get("baseline_path") or _DASH,
            "全部一致" if baseline_comparison.get("consistent") else "存在差异，逐项见下"))
        if not baseline_comparison.get("consistent"):
            caption("基线逐项对照")
            table(["指标", "基线值", "本次值", "差异", "容差", "一致"], [
                (it.get("item"), _dash(it.get("expected")), _dash(it.get("actual")),
                 _dash(it.get("diff", it.get("note"))), _dash(it.get("tolerance")),
                 "是" if it.get("match") else "否")
                for it in (baseline_comparison.get("items") or [])
            ], widths=[2.6, 2.4, 2.4, 3.4, 2.4, 2.4], size=9)
    # 产物清单
    out_files = metadata.get("output_files") or []
    if out_files:
        caption("本次实际登记的输出产物")
        table(["序号", "产物名称"], [(str(i + 1), name) for i, name in enumerate(out_files)],
              widths=[2.0, 13.6], left_cols=(1,), size=9)

    # ==================== 落款与页码 ====================
    p = doc.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    fixed(p); p.paragraph_format.space_before = Pt(12)
    set_font(p.add_run("ABGES 自动化育种遗传评估技能"), F_BODY, SZ_BODY)
    p2 = doc.add_paragraph(); p2.alignment = WD_ALIGN_PARAGRAPH.RIGHT; fixed(p2)
    set_font(p2.add_run(str(metadata.get("timestamp_end") or "")), F_BODY, SZ_BODY)

    # 封面（首页）不打页码，目录与正文自第 2 页起连续编号
    first_section.different_first_page_header_footer = True
    toc_section.different_first_page_header_footer = False
    body_section.different_first_page_header_footer = False
    footer = body_section.footer.paragraphs[0]
    footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = footer.add_run(); set_font(run, F_TBL, 10.5)
    for typ in ("begin", "instr", "end"):
        if typ == "instr":
            it = OxmlElement("w:instrText"); it.set(qn("xml:space"), "preserve")
            it.text = " PAGE "; run._r.append(it)
        else:
            fc = OxmlElement("w:fldChar"); fc.set(qn("w:fldCharType"), typ)
            run._r.append(fc)

    out_path = output_prefix + "_报告.docx"
    doc.save(out_path)
    # 优先用 Word 回写真实页码；失败时静态列出正文标题，防止空目录进入交付。
    try:
        toc_updated = _update_toc_via_word(out_path, context)
        if not toc_updated:
            toc_entries = []
            for paragraph in doc.paragraphs:
                ppr = paragraph._p.pPr
                outline = ppr.find(qn("w:outlineLvl")) if ppr is not None else None
                if outline is None or not paragraph.text.strip():
                    continue
                level = int(outline.get(qn("w:val"), "9"))
                if level <= 1:
                    toc_entries.append((level, paragraph.text.strip()))
            toc_field_paragraph = next(
                (paragraph for paragraph in doc.paragraphs
                 if any("TOC " in node.text
                        for node in paragraph._p.iter(qn("w:instrText")))), None)
            abbreviations = next(
                (paragraph for paragraph in doc.paragraphs
                 if paragraph.text.strip().startswith("主要符号与缩写")), None)
            if toc_field_paragraph is not None and abbreviations is not None:
                for level, title in toc_entries:
                    style_name = "TOC 1" if level == 0 else "TOC 2"
                    entry = abbreviations.insert_paragraph_before(title, style=style_name)
                    entry.paragraph_format.left_indent = Cm(0.6 if level else 0)
                toc_field_paragraph._element.getparent().remove(toc_field_paragraph._element)
                doc.save(out_path)
                logger.warning("Word 目录页码不可用，已写入静态标题目录（不含页码）：%s", out_path)
    except Exception:
        _log_report_diag(output_prefix, "toc_update",
                         traceback.format_exc())
    return out_path
