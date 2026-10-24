import pytest

from abges_mcp.service import GatewayError, _compact_result, _load_task, _runner_for, get_artifact


def test_mcp_rejects_implicit_or_unknown_model():
    with pytest.raises(GatewayError):
        _runner_for({"model": "auto"})
    assert _runner_for({"model": "pblup"}).endswith(":run")


def test_mcp_task_must_be_a_mapping():
    with pytest.raises(GatewayError):
        _load_task("- not\n- a task")


def test_mcp_result_excludes_engine_logs_raw_and_json_artifacts():
    compact = _compact_result({
        "status": "completed", "run_id": "r1", "artifacts": {
            "validation_report": {"path": "report.json"},
            "ebv_top20": {"path": "ebv_top20.json"},
            "delivery_report": {"path": "report.docx"},
            "hiblup_stdout_log": {"path": "engine.log"},
            "engine_phenotype": {"path": "raw.txt"},
        },
    })
    # 引擎日志/原始表型（非公开）与内部 JSON 交付件均不进入展示；
    # 仅保留公开且非 JSON 的交付件（如 docx 报告）。
    assert set(compact["artifacts"]) == {"delivery_report"}


def test_mcp_artifact_rejects_non_public_names():
    with pytest.raises(GatewayError):
        get_artifact("20260811_demo", "hiblup_stdout_log")


def test_binary_artifact_is_never_decoded_as_context(tmp_path, monkeypatch):
    import json
    from abges_mcp import service
    run = tmp_path / "run"; run.mkdir(); (run / "state.json").write_text(json.dumps({"run_id":"run", "artifacts":{"ebv_top20":{"path":str(run / "ebv.xlsx")}}}), encoding="utf-8")
    (run / "ebv.xlsx").write_bytes(b"PK\x03\x04binary")
    monkeypatch.setattr(service, "RUN_ROOT", tmp_path)
    result = service.get_artifact("run", "ebv_top20")
    assert result["binary"] and result["content"] is None
    (run / "report.docx").write_bytes(b"PK\x03\x04binary")
    state = json.loads((run / "state.json").read_text(encoding="utf-8")); state["artifacts"]["delivery_report"] = {"path": str(run / "report.docx")}; (run / "state.json").write_text(json.dumps(state), encoding="utf-8")
    assert service.get_artifact("run", "delivery_report")["binary"]


def test_router_rejects_duplicate_pipeline_agents():
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]; harness = str(root / "harness")
    if harness not in sys.path: sys.path.insert(0, harness)
    from _bootstrap import load_component
    Router = load_component("router", "router").Router
    registry = {"agents": [{"id": "00"}], "default_pipeline": ["00", "00"]}
    with pytest.raises(Exception): Router(agent_registry=registry).pipeline()
