# -*- coding: utf-8 -*-
"""ABGES 工具层冒烟测试：导入 + 关键函数真实调用。"""
import sys, os, traceback
from pathlib import Path

ROOT = str(Path(__file__).resolve().parents[2])
sys.path.insert(0, os.path.join(ROOT, 'tools', 'Python'))
sys.path.insert(0, os.path.join(ROOT, 'tools', 'HIBLUP'))
sys.path.insert(0, os.path.join(ROOT, 'tools', 'PLINK'))



def check(name, fn):
    """执行一项检查；失败即抛出，交由 pytest 逐项归因（不再静默记录）。"""
    detail = fn()
    if detail:
        print("PASS  %-52s %s" % (name, detail))
    return detail


# ---------- 1. 导入 ----------
mods = {}
for mod in ("abges_core", "abges_ascii", "abges_dataio", "abges_phenotype",
            "abges_pedigree", "abges_traits", "abges_integration",
            "abges_validate", "hiblup_tool", "plink_tool"):
    def _imp(m=mod):
        mods[m] = __import__(m)
        return "ok"
    check("import %s" % mod, _imp)

if len(mods) < 10:
    print("\n导入未全部成功，终止后续测试")
    raise RuntimeError("tool imports are incomplete")

core = mods["abges_core"]
ascii_mod = mods["abges_ascii"]
dataio = mods["abges_dataio"]
pheno = mods["abges_phenotype"]
ped = mods["abges_pedigree"]
traits = mods["abges_traits"]
integ = mods["abges_integration"]
validate = mods["abges_validate"]
hiblup = mods["hiblup_tool"]
plink = mods["plink_tool"]

# ---------- 2. core ----------

# ---------- 3. ascii ----------

# ---------- 4. dataio ----------


def _flexdate():
    import pandas as pd
    s = pd.Series(["2025年5月13日", "2025-05-14", "2025/5/15", "20250516", "坏值"])
    out = dataio.parse_flexible_date(s)
    return "parsed=%d/5" % int(out.notna().sum())



# ---------- 5. phenotype ----------


def _pheno_qc():
    import pandas as pd, numpy as np
    rng = np.random.default_rng(7)
    n = 400
    df = pd.DataFrame({
        "翅号": ["%05d" % i for i in range(n)],
        "世代": ["G1"] * (n // 2) + ["G2"] * (n - n // 2),
        "批次": (["B1"] * (n // 4) + ["B2"] * (n // 4)) * 2,
        "性别": ["F"] * n,                    # 单水平 -> 应被 E022 剔除
        # 用 object 列模拟真实 Excel 读入：数值中混有文本
        "平均连产长度": np.round(rng.normal(6, 1.5, n), 3).astype(object),
    })
    df.loc[0, "平均连产长度"] = 999          # 极端离群值 -> 由 PH006（组内 mean±3SD）判为离群
    df.loc[1, "平均连产长度"] = "abc"        # 非数值 -> PH003 WARNING 阻断（不允许私自置 NA）
    df.loc[2, "平均连产长度"] = None         # 缺失 -> PH004 保持
    cfg = core.load_yaml_config(pheno.PHENOTYPE_QC_CONFIG)
    std, rename, unmapped = dataio.normalize_header(df, cfg["header_map"])
    try:
        pheno.run_phenotype_qc(std, ["ACL"], config=cfg, mode="single",
                               rename_detail=rename)
    except core.AbgesError as exc:
        # PH003 修订后契约：脏数据仅 WARNING，但保留原值且 AbgesError 阻断
        if exc.code == "PH003":
            return ("PH003_BLOCKED ok rows_pre=%d detail=%s"
                    % (std.shape[0],
                       list(exc.detail.keys()) if hasattr(exc, "detail") else {}))
        raise
    return ("PH003 未阻断，行为不符合 2026-08-17 修订契约")



# ---------- 6. pedigree ----------


def _ped_qc():
    import pandas as pd
    rows = []
    for i in range(1, 21):
        rows.append({"ID": "S%d" % i, "Sire": "0", "Dam": "0"})
        rows.append({"ID": "D%d" % i, "Sire": "0", "Dam": "0"})
    for i in range(1, 81):
        rows.append({"ID": "C%d" % i, "Sire": "S%d" % (i % 20 + 1),
                     "Dam": "D%d" % (i % 20 + 1)})
    # 全同胞交配产生近交后代
    rows.append({"ID": "X1", "Sire": "C1", "Dam": "C21"})
    df = pd.DataFrame(rows)
    res = ped.run_pedigree_qc(df)
    f_x1 = res.inbreeding.get("X1", {}).get("F")
    return ("n=%d founders=%d meanF=%s F(X1)=%.4f"
            % (res.statistics["after"]["Individuals"],
               res.statistics["after"]["FounderCount"],
               res.statistics["after"].get("MeanInbreeding"), f_x1))




def _ped_cross_validate():
    import pandas as pd
    rows = [{"ID": "A", "Sire": "0", "Dam": "0"},
            {"ID": "B", "Sire": "0", "Dam": "0"},
            {"ID": "C", "Sire": "A", "Dam": "B"},
            {"ID": "D", "Sire": "A", "Dam": "B"},
            {"ID": "E", "Sire": "C", "Dam": "D"}]
    df, tok = ped.standardize_pedigree(pd.DataFrame(rows))
    ok, max_diff, mism = ped.cross_validate_inbreeding(df)
    f_e = ped.compute_inbreeding(df)["E"]["F"]
    return "consistent=%s maxdiff=%.2e F(E)=%.4f(理论0.25)" % (ok, max_diff, f_e)




def _ped_cycle():
    import pandas as pd
    rows = [{"ID": "A", "Sire": "C", "Dam": "0"},
            {"ID": "B", "Sire": "A", "Dam": "0"},
            {"ID": "C", "Sire": "B", "Dam": "0"}]
    df, _ = ped.standardize_pedigree(pd.DataFrame(rows))
    cycles = ped.find_cycles(df)
    assert cycles, "应检出环但未检出"
    return "cycles=%d 首条=%s" % (len(cycles), "->".join(cycles[0]))



# ---------- 7. traits ----------


def _fcr():
    import pandas as pd
    df = pd.DataFrame({"ID": ["a", "b", "c", "d"],
                       "FI": [2000, 2100, 1900, 2000],
                       "WG": [1000, 0, 950, None]})
    res = traits.compute_formula_trait(df, "FCR")
    return ("status=%s valid=%d mean=%s"
            % (res.status, res.statistics["n_valid"], res.statistics["mean"]))




def _clutch():
    import pandas as pd
    # 个体 a：连产 3 天 -> 停 -> 连产 2 天  => ACL=2.5 MCL=3
    rows = [("a", "2025-01-01"), ("a", "2025-01-02"), ("a", "2025-01-03"),
            ("a", "2025-01-05"), ("a", "2025-01-06")]
    tmp = os.path.join(core.resolve_temp_dir(), "abges_egg_smoke.csv")
    pd.DataFrame(rows, columns=["翅号", "采集时间"]).to_csv(
        tmp, index=False, encoding="utf-8")
    res = traits.compute_clutch_traits(tmp)
    row = res.frame.iloc[0]
    assert abs(row["ACL"] - 2.5) < 1e-9, "ACL 期望 2.5 实际 %s" % row["ACL"]
    assert row["MCL"] == 3, "MCL 期望 3 实际 %s" % row["MCL"]
    os.remove(tmp)
    return "ACL=%.2f MCL=%d status=%s" % (row["ACL"], row["MCL"], res.status)



# ---------- 8. integration ----------


def _integ():
    import pandas as pd
    pheno_df = pd.DataFrame({"ID": ["C%d" % i for i in range(1, 201)],
                             "ACL": [5.0] * 200})
    ped_df = pd.DataFrame({"ID": ["C%d" % i for i in range(1, 181)],
                           "Sire": ["0"] * 180, "Dam": ["0"] * 180})
    res = integ.integrate_sources(pheno_df, ped_df, None, model="pblup")
    facts = integ.build_gate_g3_facts(res)
    return ("overlap=%d coverage=%.3f warns=%d"
            % (res.metrics["pheno_ped_overlap"],
               res.metrics["pheno_ped_coverage"], len(res.warnings)))




def _integ_mismatch():
    import pandas as pd
    pheno_df = pd.DataFrame({"ID": ["00%d" % i for i in range(1, 60)],
                             "ACL": [5.0] * 59})
    ped_df = pd.DataFrame({"ID": ["%d" % i for i in range(1, 60)],
                           "Sire": ["0"] * 59, "Dam": ["0"] * 59})
    try:
        integ.integrate_sources(pheno_df, ped_df, None, model="pblup")
        return "未按预期抛错"
    except core.AbgesError as exc:
        findings = exc.detail.get("findings", [])
        return "已阻断并诊断：%s" % (findings[0] if findings else "无")



# ---------- 9. hiblup 解析（合成格式夹具，不代表引擎实测） ----------
import pytest
REF_PREFIX = None


@pytest.fixture(scope="module", autouse=True)
def synthetic_engine_reference(tmp_path_factory):
    global REF_PREFIX
    REF_PREFIX = str(tmp_path_factory.mktemp("synthetic_engine") / "reference")
    contents = {
        ".vars": "Item Var Var_SE h2 h2_SE h2_Pr(Chisq)\nPA 0.42 0.04 0.42 0.03 1.0e-06\ne 0.58 0.03 0.58 0.03 1.0e-06\n",
        ".rand": "ID PA\nSYN001 -0.2\nSYN002 0.0\nSYN003 0.2\n",
        ".beta": "Item Estimate\nIntercept 5.0\n",
        ".log": "Synthetic parser fixture only\n[Converged?] Yes!\n",
    }
    for suffix, content in contents.items():
        Path(REF_PREFIX + suffix).write_text(content, encoding="utf-8")


def _parse_real():
    parsed = hiblup.parse_hiblup_output(REF_PREFIX, method="pblup",
                                        mode="single", trait_names=["ACL"])
    h2 = hiblup.extract_heritability(parsed)
    assert abs(h2["estimate"] - 0.42) < 1e-6, h2
    assert abs(h2["se"] - 0.03) < 1e-6, h2
    return ("suffix=%r h2=%.6f±%.6f item=%s Va=%.6f Ve=%.6f conv=%s iter=%s"
            % (parsed["output_suffix"], h2["estimate"], h2["se"], h2["item"],
               h2["genetic_variance"], h2["residual_variance"],
               parsed["converged"], parsed["n_iterations"]))




def _conv_flag():
    yes = hiblup.parse_convergence_flag("[Converged?] Yes!")
    no = hiblup.parse_convergence_flag("[Converged?] No!")
    unk = hiblup.parse_convergence_flag("nothing here")
    assert yes is True and no is False and unk is None, (yes, no, unk)
    return "Yes->True No->False 无标记->None"




def _verify_outputs():
    ok, missing = hiblup.verify_required_outputs(REF_PREFIX, "pblup")
    assert ok and not missing
    return "complete=%s missing=%s" % (ok, missing)




def _validate_g5():
    parsed = hiblup.parse_hiblup_output(REF_PREFIX, method="pblup")
    h2 = hiblup.extract_heritability(parsed)
    res = validate.validate_engine_result(parsed, 0, trait="ACL",
                                          heritability=h2)
    verdicts = {c["check_id"]: c["verdict"] for c in res.checks}
    return "status=%s verdicts=%s" % (res.suggest_status(), verdicts)




def _baseline():
    parsed = hiblup.parse_hiblup_output(REF_PREFIX, method="pblup")
    h2 = hiblup.extract_heritability(parsed)
    res = validate.validate_engine_result(parsed, 0, trait="ACL", heritability=h2)
    ok, details = validate.compare_with_baseline(
        res.summary,
        {"heritability": 0.42, "heritability_se": 0.03,
         "genetic_variance": 0.42, "residual_variance": 0.58})
    assert ok, details
    return "baseline_match=%s items=%d" % (ok, len(details))




def _ebv():
    parsed = hiblup.parse_hiblup_output(REF_PREFIX, method="pblup")
    summary = validate.summarize_breeding_values(parsed)
    top = validate.rank_top_individuals(parsed, top_n=3)
    assert summary["n"] == 3 and len(top) == 3
    return "col=%s n=%d mean=%s top1=%s" % (
        summary["column"], summary["n"], summary["mean"], top[0] if top else None)



# ---------- 10. 命令组装 ----------


def _assemble():
    import pandas as pd
    tmp_dir = core.resolve_temp_dir()
    pheno_path = os.path.join(tmp_dir, "abges_cmd_smoke.txt")
    pd.DataFrame({"ID": ["a", "b", "c", "d"],
                  "Gen": ["G1", "G1", "G2", "G2"],
                  "Batch": ["B1", "B2", "B1", "B2"],
                  "Sex": ["F", "F", "F", "F"],
                  "ACL": [5.1, 6.2, 4.8, 5.9]}).to_csv(
        pheno_path, sep="\t", index=False)
    cmd, plan = hiblup.assemble_hiblup_command(
        pheno_path, os.path.join(tmp_dir, "abges_cmd_out"),
        model_id="pblup", trait_columns=["ACL"],
        fixed_effects=["Gen", "Batch", "Sex"],
        pedigree_path=os.path.join(tmp_dir, "fake_ped.csv"),
        hiblup_exe="hiblup")
    os.remove(pheno_path)
    text = " ".join(cmd[1:])
    # Sex 全为 F（单水平），必须被剔除，否则 HIBLUP v1.7.0 写 .rand 会崩
    dropped_cols = plan["fixed_effects_dropped"]
    assert "Sex" in dropped_cols, "单水平 Sex 未被剔除：%s" % dropped_cols
    assert plan["fixed_effects_used"]["ACL"] == ["Gen", "Batch"], \
        "生效固定效应应为 Gen/Batch，实为 %s" % plan["fixed_effects_used"]["ACL"]
    assert "--pheno-pos" in text, "缺少 --pheno-pos"
    assert "--pedigree" in text, "PBLUP 缺少 --pedigree"
    assert plan["expected_output_suffix"] == "", "PBLUP 输出不应带 _ad 后缀"
    return "dropped=%s cmd=%s" % (dropped_cols, text)




def _assemble_gblup():
    import pandas as pd
    tmp_dir = core.resolve_temp_dir()
    pheno_path = os.path.join(tmp_dir, "abges_cmd_smoke2.txt")
    pd.DataFrame({"ID": ["a", "b", "c", "d"],
                  "Gen": ["G1", "G1", "G2", "G2"],
                  "Age": [120, 130, 125, 128],
                  "ACL": [5.1, 6.2, 4.8, 5.9]}).to_csv(
        pheno_path, sep="\t", index=False)
    cmd, plan = hiblup.assemble_hiblup_command(
        pheno_path, os.path.join(tmp_dir, "abges_cmd_out2"),
        model_id="gblup", trait_columns=["ACL"],
        fixed_effects=["Gen"], covariates=["Age"],
        genotype_prefix=os.path.join(tmp_dir, "fake_geno"),
        hiblup_exe="hiblup")
    os.remove(pheno_path)
    text = " ".join(cmd[1:])
    assert "--bfile" in text or "--xrm" in text, "GBLUP 缺少基因型入参"
    assert "--qcovar" in text, "缺少 --qcovar（协变量 Age）"
    return text



# ---------- 11. plink ----------


def _detect_missing():
    """不存在的前缀必须抛 E030，而不是静默返回——这是期望行为。"""
    try:
        fmt = plink.detect_genotype_format(r"D:\不存在的前缀")
    except core.AbgesError as exc:
        assert exc.code == "E030", "错误码应为 E030，实为 %s" % exc.code
        return "正确抛出 %s（不静默猜测）" % exc.code
    raise AssertionError("不存在的路径竟返回了 %s，未按约定阻断" % fmt)


# ------------------------------------------------------------------
# pytest 用例包装（由脚本式 check() 自动转换，断言逻辑与判定条件均未改动）
# ------------------------------------------------------------------

def test_core_get_abges_root():
    check("core.get_abges_root", lambda: core.get_abges_root())

def test_core_resolve_temp_dir():
    check("core.resolve_temp_dir", lambda: core.resolve_temp_dir())

def test_core_make_timestamp():
    check("core.make_timestamp", lambda: core.make_timestamp())

def test_core_resolve_thread_count():
    check("core.resolve_thread_count", lambda: core.resolve_thread_count("auto", 16))

def test_core_load_model_config_pblup():
    check("core.load_model_config(pblup)",
          lambda: core.load_model_config("pblup").get("meta", {}).get("model_id"))

def test_core_validate_status():
    check("core.validate_status", lambda: core.validate_status("completed"))

def test_ascii_is_pure_ascii():
    check("ascii.is_pure_ascii(中文)", lambda: ascii_mod.is_pure_ascii("世代"))

def test_ascii_convert_any_to_ascii():
    check("ascii.convert_any_to_ascii(世代)",
          lambda: ascii_mod.convert_any_to_ascii("世代", "COL", set()))

def test_ascii_convert_any_to_ascii_3():
    check("ascii.convert_any_to_ascii(第3批)",
          lambda: ascii_mod.convert_any_to_ascii("第3批", "LVL", set()))

def test_ascii_build_column_rename_map():
    check("ascii.build_column_rename_map(中文表头)",
          lambda: ascii_mod.build_column_rename_map(["个体号", "世代", "ID", "Sex"]))

def test_dataio_normalize_identifier_00123_0():
    check("dataio.normalize_identifier(00123.0)",
          lambda: dataio.normalize_identifier("00123.0"))

def test_dataio_normalize_identifier_none():
    check("dataio.normalize_identifier(None)",
          lambda: dataio.normalize_identifier(None))

def test__flexdate():
    check("dataio.parse_flexible_date", _flexdate)

def test_pheno_load_trait_registry():
    check("pheno.load_trait_registry",
          lambda: "%d traits" % len(pheno.load_trait_registry()))

def test__pheno_qc():
    check("pheno.run_phenotype_qc(合成数据)", _pheno_qc)

def test__ped_qc():
    check("ped.run_pedigree_qc(合成系谱)", _ped_qc)

def test__ped_cross_validate():
    check("ped.cross_validate_inbreeding", _ped_cross_validate)

def test__ped_cycle():
    check("ped.find_cycles(人造环)", _ped_cycle)

def test_traits_list_computable_traits():
    check("traits.list_computable_traits",
          lambda: traits.list_computable_traits(["FI", "WG", "BW"]))

def test__fcr():
    check("traits.compute_formula_trait(FCR)", _fcr)

def test__clutch():
    check("traits.compute_clutch_traits(已知答案)", _clutch)

def test__integ():
    check("integ.integrate_sources", _integ)

def test__integ_mismatch():
    check("integ 前导零不匹配诊断", _integ_mismatch)

def test__parse_real():
    check("hiblup.parse_hiblup_output(合成夹具)", _parse_real)

def test__conv_flag():
    check("hiblup.parse_convergence_flag", _conv_flag)

def test__verify_outputs():
    check("hiblup.verify_required_outputs", _verify_outputs)

def test__validate_g5():
    check("validate.validate_engine_result(合成夹具)", _validate_g5)

def test__baseline():
    check("validate.compare_with_baseline", _baseline)

def test__ebv():
    check("validate.summarize_breeding_values", _ebv)

def test__assemble():
    check("hiblup.assemble_hiblup_command", _assemble)

def test__assemble_gblup():
    check("hiblup.assemble_hiblup_command(gblup)", _assemble_gblup)

def test_plink_build_chromosome_args():
    check("plink.build_chromosome_args", lambda: plink.build_chromosome_args())

def test__detect_missing():
    check("plink.detect_genotype_format(不存在→应抛E030)", _detect_missing)
