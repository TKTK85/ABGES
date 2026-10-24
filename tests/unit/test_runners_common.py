# -*- coding: utf-8 -*-
"""runners/common.py 特征化测试。

common.py 是三份 stage_runner 的公共helper层，此前覆盖率为 0%。
本文件把它每一个 helper 的现行行为锁死，为抽取 base_stage_runner 铺路。

命名说明：这里测的是「现状」而非「应然」。若日后有意改变某个行为，
应连同本文件一起改，并说明是有意变更还是修复。
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

from runners import common  # noqa: E402


# ----------------------------------------------------------------------
# require_context：缺失即 E019，存在即原值返回
# ----------------------------------------------------------------------
def test_require_context_returns_existing_value():
    assert common.require_context({"a": 42}, "a", "01") == 42


def test_require_context_missing_key_raises_e019():
    with pytest.raises(Exception) as exc:
        common.require_context({"a": 42}, "plan", "01")
    assert "E019" in str(getattr(exc.value, "code", "") or exc.value)


def test_require_context_none_value_raises_e019():
    """None 视为缺失——防止上游把 None 当有效值静默透传。"""
    with pytest.raises(Exception):
        common.require_context({"plan": None}, "plan", "01")


def test_require_context_error_lists_available_keys():
    """错误信息里应给出可用键，便于定位是哪一环没产出。"""
    try:
        common.require_context({"alpha": 1}, "plan", "01")
    except Exception as exc:
        assert getattr(exc, "code", "") == "E019"
        assert "alpha" in (getattr(exc, "detail", {}) or {}).get("available", [])
    else:
        raise AssertionError("应抛出 E019")


# ----------------------------------------------------------------------
# is_pure_ascii
# ----------------------------------------------------------------------
@pytest.mark.parametrize("value,expected", [
    ("abc", True), ("123", True), ("Gen", True),
    ("世代", False), ("产蛋数", False), ("café", False),
    ("", True), (None, True), (42, True),
])
def test_is_pure_ascii(value, expected):
    assert common.is_pure_ascii(value) is expected


# ----------------------------------------------------------------------
# empty_result：默认骨架
# ----------------------------------------------------------------------
def test_empty_result_defaults():
    result = common.empty_result()
    assert result["placeholder"] is False
    assert result["gate_facts"] == {}
    assert result["artifacts"] == []
    assert result["warnings"] == []
    assert result["errors"] == []
    assert result["blackboard"] == {}


def test_empty_result_overrides():
    result = common.empty_result(blackboard={"plan": {"trait": "EN"}})
    assert result["blackboard"]["plan"]["trait"] == "EN"


# ----------------------------------------------------------------------
# collect_fixed_effect_levels
# ----------------------------------------------------------------------
def test_collect_fixed_effect_levels_skips_missing_columns():
    import pandas as pd
    frame = pd.DataFrame({"Gen": ["G1", "G2", "G1"], "Batch": ["b2", "b1", "b1"]})
    levels = common.collect_fixed_effect_levels(frame, ["Gen", "Batch", "Nope"])
    assert levels["Gen"] == ["G1", "G2"]
    assert levels["Batch"] == ["b1", "b2"]
    assert "Nope" not in levels


def test_collect_fixed_effect_levels_drops_na_and_sorts():
    import pandas as pd
    frame = pd.DataFrame({"Gen": ["G3", None, "G1", "G2"]})
    assert common.collect_fixed_effect_levels(frame, ["Gen"])["Gen"] == \
        ["G1", "G2", "G3"]


# ----------------------------------------------------------------------
# convert_tool_warnings
# ----------------------------------------------------------------------
def test_convert_tool_warnings_prefers_rule_id():
    out = common.convert_tool_warnings(
        [{"rule_id": "PH001", "code": "IGNORED", "message": "msg",
          "affected_count": 5, "action_taken": "done"}], "QC")
    assert out[0]["code"] == "PH001"
    assert out[0]["message"] == "msg"
    assert out[0]["affected_count"] == 5
    assert out[0]["action_taken"] == "done"


def test_convert_tool_warnings_falls_back_to_code_then_default():
    assert common.convert_tool_warnings([{"code": "PH002"}], "QC")[0]["code"] \
        == "PH002"
    assert common.convert_tool_warnings([{}], "QC")[0]["code"] == "QC"


def test_convert_tool_warnings_handles_bare_string():
    """非 dict 告警（裸字符串）也要转成标准结构，不得抛异常。"""
    out = common.convert_tool_warnings(["裸字符串告警"], "QC")
    assert out == [{"code": "QC", "message": "裸字符串告警",
                    "affected_count": None, "action_taken": None}]


def test_convert_tool_warnings_empty_input():
    assert common.convert_tool_warnings(None, "QC") == []
    assert common.convert_tool_warnings([], "QC") == []


# ----------------------------------------------------------------------
# validation_checks_to_gate_facts
# ----------------------------------------------------------------------
def test_validation_checks_to_gate_facts_maps_verdict_to_passed():
    checks = [
        {"check_id": "VA-01", "verdict": "pass", "actual": 1,
         "threshold": 2, "message": "ok"},
        {"check_id": "VA-02", "verdict": "block", "actual": 3,
         "threshold": 2, "message": "bad"},
    ]
    facts = common.validation_checks_to_gate_facts(checks)
    assert facts["VA-01"]["passed"] is True
    assert facts["VA-02"]["passed"] is False
    assert facts["VA-02"]["message"] == "bad"


def test_validation_checks_to_gate_facts_empty():
    assert common.validation_checks_to_gate_facts(None) == {}
