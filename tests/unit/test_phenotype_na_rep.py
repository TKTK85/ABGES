# -*- coding: utf-8 -*-
"""ABGES 回归测试：表型缺失值写出必须为 'NA'，不得为空串。

背景：M3 验证发现管线写出引擎表型时缺失值曾用空字符串，HIBLUP 按空白符
坍缩列、报 "line N does not have M elements"。本测试锁定两类写出路径：
  1. dataio.write_engine_table（主路径，默认 na_rep="NA"）
  2. abges_ascii.sanitize_phenotype_for_engine（ASCII 转码路径，B1 修复点）
均不得产出连续制表符（空字段），缺失必须写成 "NA"。
"""
import sys, os, tempfile
from pathlib import Path

ROOT = str(Path(__file__).resolve().parents[2])
sys.path.insert(0, os.path.join(ROOT, 'tools', 'Python'))

PASS, FAIL = [], []


def check(name, fn):
    try:
        detail = fn()
        PASS.append((name, detail))
        print("PASS  %-46s %s" % (name, detail if detail else ""))
    except Exception as exc:
        FAIL.append((name, repr(exc)))
        print("FAIL  %-46s %r" % (name, exc))


import pandas as pd
import abges_ascii as ascii_tool
import abges_dataio as dataio

TMP = os.path.join(tempfile.gettempdir(), 'abges_na_test')


def _write_src(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # 含中文列名/水平 + 缺失值（缺失用 "NA" 与空串两种形态）
    with open(path, 'w', encoding='utf-8') as handle:
        handle.write("ID\t世代\t性别\t体重\n")
        handle.write("A1\t1\t公\tNA\n")        # 体重缺失（NA 记号）
        handle.write("A2\t2\t母\t3500\n")
        handle.write("A3\t1\t\t2800\n")        # 性别缺失（空串形态）
    return path


def test_write_engine_table_na():
    out = os.path.join(TMP, 'eng.txt')
    df = pd.DataFrame({'ID': ['A1', 'A2'],
                       'BW': [float('nan'), 3500.0]})
    dataio.write_engine_table(df, out)
    with open(out, encoding='utf-8') as handle:
        content = handle.read()
    assert '\t\t' not in content, "出现空字段(连续制表符)"
    assert 'NA' in content, "缺失未写成 NA"
    return None


def test_sanitize_na_rep():
    src = os.path.join(TMP, 'src.txt')
    _write_src(src)
    safe_path, _mapping, _changed = ascii_tool.sanitize_phenotype_for_engine(
        src, TMP)
    with open(safe_path, encoding='utf-8') as handle:
        lines = handle.readlines()
    ncols = len(lines[0].rstrip('\n').split('\t'))
    for line in lines:
        fields = line.rstrip('\n').split('\t')
        assert len(fields) == ncols, "列数不一致(空串坍缩): %r" % line
        assert '' not in fields, "出现空字段(缺失未写成NA): %r" % line
    assert 'NA' in ''.join(lines), "缺失未写成 NA"
    return None


check("write_engine_table 缺失写 NA", test_write_engine_table_na)
check("sanitize_phenotype_for_engine 缺失写 NA", test_sanitize_na_rep)

print("\n=== 结果 ===")
print("PASS %d  FAIL %d" % (len(PASS), len(FAIL)))
if FAIL:
    print("FAILURES:")
    for name, err in FAIL:
        print("  %s: %s" % (name, err))
    raise AssertionError("legacy module checks failed")
print("核心指标测试全部通过")
