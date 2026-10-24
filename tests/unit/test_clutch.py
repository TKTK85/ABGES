# -*- coding: utf-8 -*-
"""
test_clutch.py — 连产性状算法 v2 单元测试
==============================================
用例：

1. test_double_egg_day_counts_as_one     — 双产日按1枚计入连产
2. test_forward_gap_search              — 前向搜索找最近空档
3. test_two_records_prev_has_record     — 前一天有→保留第一条，删第二条
4. test_two_records_prev_no_record      — 前一天无→平移+双产标记
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from datetime import date, timedelta
import pandas as pd


# ------------------------------------------------------------------
# 辅助：构造原始捡蛋记录 DataFrame
# ------------------------------------------------------------------
def make_egg_frame(records):
    """
    records: list of (翅号str, date_str "YYYY-MM-DD")
    """
    rows = [{"ID": ch, "date": pd.Timestamp(d)} for ch, d in records]
    return pd.DataFrame(rows)


# ------------------------------------------------------------------
# 辅助：直接调用 correct_daily_records（从 abges_traits 导入）
# ------------------------------------------------------------------
def run_correction(frame):
    # 【P2-2 消除双源】算法实现的唯一真相是 tools/Python/abges_traits.py。
    # 此前 skills/04-derived-trait-calculation/scripts/ 下有一份字节相同的副本，
    # 因目录名以数字开头且含连字符（非合法标识符）只能经 importlib 动态加载，
    # 现改为直接 import 正式模块，副本已删除。
    import abges_traits as traits_mod
    corrected, stats, double_records = traits_mod.correct_daily_records(frame)
    return corrected, stats, double_records


# ------------------------------------------------------------------
# 用例1：双产日按1枚计入
# ------------------------------------------------------------------
def test_double_egg_day_counts_as_one():
    """
    场景：鸡A，5月1日有1条，5月3日有2条（同日两条）
    前一天5月2日无记录 → 保留第一条在5月3日，第二条前向搜索最近空档5月4日
    期望：最终日期 = [5/1, 5/3, 5/4]，平移 1 条，当天连产按 1 枚计入。

    注：本用例与用例4 场景相同，原期望“仅 2 个日期”是笔误，已与手册对齐修正。
    """
    frame = make_egg_frame([
        ("A", "2023-05-01"),
        ("A", "2023-05-03"),
        ("A", "2023-05-03"),   # 同日两条
    ])
    corrected, stats, double_records = run_correction(frame)

    dates = sorted(corrected["date"].dt.date.tolist())
    assert dates == [date(2023, 5, 1), date(2023, 5, 3), date(2023, 5, 4)], \
        f"期望[5/1, 5/3, 5/4]，实际{dates}"
    assert stats["shifted"] == 1, f"应平移 1 条，实际{stats['shifted']}"
    print("  [PASS] test_double_egg_day_counts_as_one")


# ------------------------------------------------------------------
# 用例2：前向搜索最近空档
# ------------------------------------------------------------------
def test_forward_gap_search():
    """
    场景：鸡B，5月1日有1条，5月3日有2条（双产）
    5月4日已被占用 → 前向搜索到5月5日
    期望：5月1日、5月3日（保留第一条）、5月5日（平移）
    """
    frame = make_egg_frame([
        ("B", "2023-05-01"),
        ("B", "2023-05-03"),
        ("B", "2023-05-03"),   # 双产，搜索目标5月4日
        ("B", "2023-05-04"),   # 5月4日已被占用 → 搜到5月5日
    ])
    corrected, stats, _ = run_correction(frame)

    dates = sorted(corrected["date"].dt.date.tolist())
    # 5月1日、5月3日（仅1条）、5月4日（原有）、5月5日（平移）
    assert date(2023, 5, 1) in dates
    assert date(2023, 5, 3) in dates
    assert date(2023, 5, 4) in dates  # 原有
    assert date(2023, 5, 5) in dates   # 平移
    print("  [PASS] test_forward_gap_search")


# ------------------------------------------------------------------
# 用例3：前一天有记录 → 保留第一条，删第二条
# ------------------------------------------------------------------
def test_two_records_prev_has_record():
    """
    场景：鸡C，5月2日有1条（5月1日无记录）
    5月3日有2条 → 前一天5月2日有记录 → 保留5月3日第一条，删第二条
    期望：5月2日、5月3日（仅1条），丢弃1条
    """
    frame = make_egg_frame([
        ("C", "2023-05-02"),
        ("C", "2023-05-03"),
        ("C", "2023-05-03"),   # 前一天有记录 → 删
    ])
    corrected, stats, _ = run_correction(frame)

    dates = sorted(corrected["date"].dt.date.tolist())
    assert dates == [date(2023, 5, 2), date(2023, 5, 3)], \
        f"期望[5/2, 5/3]，实际{dates}"
    assert stats["discarded"] >= 1, f"应有丢弃，实际{stats['discarded']}"
    print("  [PASS] test_two_records_prev_has_record")


# ------------------------------------------------------------------
# 用例4：前一天无记录 → 平移 + 双产标记
# ------------------------------------------------------------------
def test_two_records_prev_no_record():
    """
    场景：鸡D，5月1日有1条，5月3日有2条
    5月2日无记录，5月4日空 → 前向搜索到5月4日平移
    期望：5月1日、5月3日、5月4日（平移），丢弃1条
    """
    frame = make_egg_frame([
        ("D", "2023-05-01"),
        ("D", "2023-05-03"),
        ("D", "2023-05-03"),   # 前一天无记录 → 前向搜索平移
    ])
    corrected, stats, double_records = run_correction(frame)

    dates = sorted(corrected["date"].dt.date.tolist())
    assert dates == [date(2023, 5, 1), date(2023, 5, 3), date(2023, 5, 4)], \
        f"期望[5/1, 5/3, 5/4(平移)]，实际{dates}"
    assert stats["shifted"] >= 1, f"应有平移，实际{stats['shifted']}"
    print("  [PASS] test_two_records_prev_no_record")


# ------------------------------------------------------------------
# 用例5：T+1~T+7 全被占用 → 记为双产日（连产按 1 枚计）
# ------------------------------------------------------------------
def test_double_egg_day_when_no_gap():
    """
    场景：鸡E，5/1 有1条、5/2 无记录、5/3 有2条、5/4~5/10 每天1条
    因 5/2 无记录 → 走"前向平移"分支；第二条搜索 5/4~5/10 全被占用 → 无处可移
    期望：记为双产日（连产按 1 枚计），日期为 5/1、5/3~5/10 共 9 天，
          不凭空产生 5/11，double_egg_days = 1
    """
    days = [1] + list(range(3, 11))          # 故意跳过 5/2
    frame = make_egg_frame([("E", "2023-05-%02d" % d) for d in days])
    frame = pd.concat([frame, make_egg_frame([("E", "2023-05-03")])],
                      ignore_index=True)
    corrected, stats, double_records = run_correction(frame)

    dates = sorted(corrected["date"].dt.date.tolist())
    assert date(2023, 5, 2) not in dates, "5/2 本就无记录，不应凭空出现"
    assert date(2023, 5, 11) not in dates, "不应凭空产生 5/11"
    assert len(dates) == 9, f"期望9天，实际{len(dates)}: {dates}"
    assert stats["double_egg_days"] == 1, \
        f"应记 1 个双产日，实际{stats['double_egg_days']}"
    assert len(double_records) == 1, f"应返回 1 条双产记录，实际{double_records}"
    print("  [PASS] test_double_egg_day_when_no_gap")


# ------------------------------------------------------------------
# 用例6：平移判定必须逐个体隔离（回归旧缺陷：跨个体读到别人的日期）
# ------------------------------------------------------------------
def test_shift_isolated_between_individuals():
    """
    场景：鸡A 仅在 5/4 有 1 条；鸡Z 在 5/3 有 2 条（前一日 5/2 无记录）
    Z 的第二条应前向平移到 5/4——5/4 属于 A，与 Z 无关，不构成冲突
    期望：Z 的日期含 5/4，且不含 5/5（5/5 只在被误判“5/4已占用”时才会出现）
    """
    frame = make_egg_frame([
        ("A", "2023-05-04"),
        ("Z", "2023-05-03"),
        ("Z", "2023-05-03"),
    ])
    corrected, stats, _ = run_correction(frame)

    z_dates = sorted(corrected.loc[corrected["ID"] == "Z", "date"].dt.date.tolist())
    assert z_dates == [date(2023, 5, 3), date(2023, 5, 4)], \
        f"Z 期望[5/3, 5/4]，实际{z_dates}"
    a_dates = sorted(corrected.loc[corrected["ID"] == "A", "date"].dt.date.tolist())
    assert a_dates == [date(2023, 5, 4)], f"A 应仅 5/4，实际{a_dates}"
    print("  [PASS] test_shift_isolated_between_individuals")


# ------------------------------------------------------------------
def main():
    print("=" * 50)
    print("连产性状算法 v2 单元测试")
    print("=" * 50)
    test_double_egg_day_counts_as_one()
    test_forward_gap_search()
    test_two_records_prev_has_record()
    test_two_records_prev_no_record()
    test_double_egg_day_when_no_gap()
    test_shift_isolated_between_individuals()
    print("=" * 50)
    print("全部通过！")


if __name__ == "__main__":
    main()
