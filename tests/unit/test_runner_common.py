import sys, os
from pathlib import Path

ROOT = str(Path(__file__).resolve().parents[2])
sys.path.insert(0, ROOT)

from runners.common import (collect_fixed_effect_levels, convert_tool_warnings,
                            empty_result, is_pure_ascii, validation_checks_to_gate_facts)


def test_shared_runner_helpers_keep_contracts():
    class Frame:
        columns = ["Sex"]
        def __getitem__(self, key):
            class Series:
                def dropna(self): return self
                def astype(self, _): return self
                def unique(self):
                    class Values:
                        def tolist(self): return ["F", "M"]
                    return Values()
            return Series()
    assert collect_fixed_effect_levels(Frame(), ["Sex", "Missing"]) == {"Sex": ["F", "M"]}
    assert is_pure_ascii("ABC")
    assert not is_pure_ascii("\u6bcd")
    assert convert_tool_warnings([{"rule_id": "PH001", "message": "x"}])[0]["code"] == "PH001"
    assert validation_checks_to_gate_facts([{"check_id": "G1", "verdict": "pass"}])["G1"]["passed"]
    assert empty_result()["placeholder"] is False
