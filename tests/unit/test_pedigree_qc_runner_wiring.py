# -*- coding: utf-8 -*-
"""ABGES 系谱 QC runner 链路端到端验证：可选列透传。

验证目标：runner 实际调用路径 `load_pedigree(path) -> run_pedigree_qc(frame)`
（不传 config 覆盖、使用默认配置）是否会保留 Gen/BirthDate/Wing 可选列，
并触发 PD014/015/018/019/020。

为何需要此测试：早期曾误判"runner 预先 standardize 成三列导致可选列丢失"，
实测证明 load_pedigree 仅重命名映射列、保留未识别列，run_pedigree_qc 内部
standardize_pedigree 会透传 optional_columns。本测试锁定该行为，防止日后回归。

运行：python -S tests/unit/test_pedigree_qc_runner_wiring.py
"""
import sys, os, traceback, tempfile
from pathlib import Path

ROOT = str(Path(__file__).resolve().parents[2])
sys.path.insert(0, os.path.join(ROOT, 'tools', 'Python'))

import pandas as pd  # noqa: E402
import abges_pedigree as ped  # noqa: E402



def check(name, fn):
    """执行一项检查；失败即抛出，交由 pytest 逐项归因（不再静默记录）。"""
    detail = fn()
    if detail:
        print("PASS  %-52s %s" % (name, detail))
    return detail


def _build_csv():
    """构造含 Gen/BirthDate/Wing 的系谱 CSV（>=60 行以越过默认 min_individuals=50）。"""
    tmp = tempfile.gettempdir()
    path = os.path.join(tmp, "runner_wiring_ped.csv")
    rows = []
    for i in range(5):
        for prefix in ("S", "D"):
            rows.append({"ID": "%s%d" % (prefix, i), "Sire": "0", "Dam": "0",
                         "Gen": 0, "BirthDate": "2018/01/0%d" % (i + 1),
                         "Wing": "%s00%d" % (prefix, i)})
    sire_founders = ["S0", "S1", "S2", "S3", "S4"]
    dam_founders = ["D0", "D1", "D2", "D3", "D4"]
    for k in range(60):
        sire = sire_founders[k % 5]
        dam = dam_founders[(k + 1) % 5]
        wing = "W1%02d" % k
        bd = "2019/%02d/15" % ((k % 12) + 1)
        gen = 1
        if k == 10:
            wing = "WX12"          # PD003 同世代编码格式不一致（WA## vs W###）
        if k == 20:
            gen = 9               # PD014 显式 9 vs 拓扑 1 -> 冲突
        if k == 30:
            bd = "1899-05-05"     # PD008 年份越界
        if k == 31:
            bd = "garbage_date"   # PD008 无法解析
        if k == 40:
            sire = "ZZZ"          # PD015 父本不在系谱集合 -> 断链（PD005 补建前）
        rows.append({"ID": "O%d" % k, "Sire": sire, "Dam": dam,
                     "Gen": gen, "BirthDate": bd, "Wing": wing})
    rows.append({"ID": "LATE", "Sire": "S0", "Dam": "0",
                 "Gen": 4, "BirthDate": "2020/06/01", "Wing": "WL01"})  # PD010 晚世代未知亲本
    pd.DataFrame(rows).to_csv(path, index=False, encoding="utf-8")
    return path


def t_wiring():
    path = _build_csv()
    # 复刻 runner：load_pedigree -> run_pedigree_qc（默认配置，无覆盖）
    frame, _info, rename = ped.load_pedigree(path)
    assert "Gen" in frame.columns and "BirthDate" in frame.columns \
        and "Wing" in frame.columns, list(frame.columns)
    qc = ped.run_pedigree_qc(frame)  # 关键：不传 config，走默认（min_individuals=50）

    removed = {item["ID"]: item.get("rule_id") for item in qc.exclusions}
    assert removed.get("O30") == "PD008" and removed.get("O31") == "PD008", removed
    assert removed.get("LATE") == "PD010", removed
    assert removed.get("O40") == "PD005", removed
    assert any(note.get("rule_id") == "PD008" for note in qc.notes), qc.notes
    return "method=%s excluded=%d" % (qc.generation.get("method"), len(qc.exclusions))


# ------------------------------------------------------------------
# pytest 用例包装（由脚本式 check() 自动转换，断言逻辑与判定条件均未改动）
# ------------------------------------------------------------------

def test_wiring():
    check("runner 链路透传可选列并触发 PD014/015/018/019/020", t_wiring)
