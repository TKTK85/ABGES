import sys, os
from pathlib import Path

ROOT = str(Path(__file__).resolve().parents[2])
sys.path.insert(0, ROOT)

from runners.result_evaluation import _restrict_to_ids


def test_restrict_to_ids_filters_every_individual_result_list():
    parsed = {
        "rand": [{"ID": "HB1", "GA": 1.0}, {"ID": "OTHER1", "GA": 9.0}],
        "pev": [{"ID": "HB1", "GA_PEV": 0.5}, {"ID": "OTHER1", "GA_PEV": 0.1}],
        "metadata": {"engine": "HIBLUP"},
    }
    result = _restrict_to_ids(parsed, ["HB1"])
    assert [row["ID"] for row in result["rand"]] == ["HB1"]
    assert [row["ID"] for row in result["pev"]] == ["HB1"]
    assert result["metadata"] == {"engine": "HIBLUP"}


def test_restrict_to_ids_preserves_default_behavior():
    parsed = {"rand": [{"ID": "HB1", "GA": 1.0}]}
    assert _restrict_to_ids(parsed, None) is parsed


class _Validation:
    checks = {}
    summary = {"heritability": 0.2}
    def suggest_status(self):
        return "completed"


class _ValidationTool:
    @staticmethod
    def summarize_breeding_values(parsed):
        return {"n": len(parsed.get("rand") or [])}
    @staticmethod
    def rank_top_individuals(parsed, top_n=20, genetic_variance=None):
        return [{"ID": row["ID"]} for row in (parsed.get("rand") or [])[:top_n]]
    @staticmethod
    def evaluate_result_quality(parsed, summary):
        individuals = [{"ID": row["ID"]} for row in parsed.get("rand") or []]
        return {"records": len(individuals),
                "ebv_reliability": {"records_valid": len(individuals),
                                    "individuals": individuals}}


class _State:
    class _Dirs(dict):
        pass
    dirs = {"reports": "reports"}


def test_build_artifacts_discloses_prediction_and_reporting_population(tmp_path):
    from runners.result_evaluation import build_artifacts
    captured = {}
    def write_json(path, payload):
        captured[Path(path).name] = payload
        return str(tmp_path / Path(path).name)
    result = build_artifacts(
        {"trait": "BW"}, _State(),
        {"rand": [{"ID": "P1", "GA": 1.0}, {"ID": "NO_PHENO", "GA": 9.0}]},
        _Validation(), _ValidationTool(), write_json, reporting_ids=["P1"],
        reporting_population={"basis": "phenotype_qc_ids", "analysis_individual_count": 1})
    population = captured["validation_report.json"]["reporting_population"]
    assert population["prediction_individual_count"] == 2
    assert population["reporting_individual_count"] == 1
    assert result["ebv_summary"]["n"] == 1
    reliability = captured["validation_report.json"]["result_evaluation"]["ebv_reliability"]
    assert reliability["records_valid"] == 1
    assert "individuals" not in reliability
    assert reliability["individual_detail_artifact"] == "delivery_ebv_workbook"
