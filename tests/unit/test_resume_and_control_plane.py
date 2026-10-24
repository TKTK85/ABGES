import hashlib
import json
from pathlib import Path

import pytest

from abges_mcp import control_plane
from abges_mcp.service import GatewayError


def test_authorize_enforces_role_and_hash(monkeypatch):
    token = "test-token"
    monkeypatch.setenv("ABGES_MCP_ACCESS_JSON", json.dumps({"operator": {"role": "operator", "token_sha256": hashlib.sha256(token.encode()).hexdigest()}}))
    assert control_plane.authorize("operator", token, "run")["role"] == "operator"
    with pytest.raises(GatewayError):
        control_plane.authorize("operator", token, "admin")


def test_agent_result_checkpoint_restores_blackboard(tmp_path):
    import sys
    root = Path(__file__).resolve().parents[2]
    harness = str(root / "harness")
    if harness not in sys.path: sys.path.insert(0, harness)
    from _bootstrap import load_component
    StateManager = load_component("state-manager", "state_manager").StateManager
    state = StateManager("resume_test", run_root=str(tmp_path))
    state.put("verified", {"value": 7})
    state.start_stage("01", "01"); state.finish_stage("01", "completed")
    state.checkpoint_agent_result("01", {"artifacts": []})
    assert (tmp_path / "resume_test" / "agent_result_matrix.json").is_file()
    restored = StateManager.resume("resume_test", run_root=str(tmp_path))
    assert restored.get("verified")["value"] == 7


def test_credibility_refuses_partial_or_missing_evidence():
    from abges_mcp.service import _credibility
    assert not _credibility({"status": "partial", "artifacts": {}})["decision_ready"]
    assert _credibility({"status": "completed", "artifacts": {"validation_report": {}, "run_metadata": {}, "run_statistics": {}}})["decision_ready"]


def test_resume_reuses_only_completed_or_not_applicable_stages(tmp_path):
    import sys
    root = Path(__file__).resolve().parents[2]
    harness = str(root / "harness")
    if harness not in sys.path: sys.path.insert(0, harness)
    from _bootstrap import load_component
    module = load_component("state-manager", "state_manager")
    StateManager = module.StateManager
    state = StateManager("matrix_semantics", run_root=str(tmp_path))
    state.start_stage("01", "01"); state.finish_stage("01", "completed")
    state.skip_stage("02", "not needed", kind=module.SKIP_NOT_APPLICABLE)
    state.skip_stage("03", "upstream failed", kind=module.SKIP_UPSTREAM_FAILED)
    assert state.is_checkpoint_complete("01")
    assert state.is_checkpoint_complete("02")
    assert not state.is_checkpoint_complete("03")
    assert not state.is_checkpoint_complete("04")


def test_agent_matrix_marks_failed_and_upstream_stages_for_rerun(tmp_path):
    import sys
    root = Path(__file__).resolve().parents[2]
    harness = str(root / "harness")
    if harness not in sys.path: sys.path.insert(0, harness)
    from _bootstrap import load_component
    module = load_component("state-manager", "state_manager")
    state = module.StateManager("matrix_actions", run_root=str(tmp_path))
    state.start_stage("01", "01"); state.finish_stage("01", "completed")
    state.start_stage("02", "02"); state.finish_stage("02", "failed")
    state.add_error("02", "E_TEST", "intentional failure")
    state.skip_stage("03", "upstream failure", kind=module.SKIP_UPSTREAM_FAILED)
    state.checkpoint_agent_result("03", {"artifacts": []})
    matrix = json.loads((tmp_path / "matrix_actions" / "agent_result_matrix.json").read_text(encoding="utf-8"))
    actions = {row["agent"]: row["resume_action"] for row in matrix["agents"]}
    assert actions == {"01": "reuse", "02": "rerun", "03": "rerun"}
    assert next(row for row in matrix["agents"] if row["agent"] == "02")["error_codes"] == ["E_TEST"]


def test_atomic_claim_allows_only_one_worker(tmp_path, monkeypatch):
    monkeypatch.setattr(control_plane, "JOBS", tmp_path / "jobs")
    monkeypatch.setattr(control_plane, "CONTROL", tmp_path)
    job = {"job_id": "job_atomic", "status": "queued", "actor": {"identity": "x"}}
    control_plane._write(control_plane._job_path("job_atomic"), job)
    first = control_plane._claim_next_job()
    second = control_plane._claim_next_job()
    assert first is not None
    assert second is None
    assert first[0].name == "job_atomic.running.json"


def test_approval_is_single_use_and_bound_to_task(tmp_path, monkeypatch):
    token = "operator-token"
    approver_token = "approver-token"
    monkeypatch.setenv("ABGES_MCP_ACCESS_JSON", json.dumps({
        "operator": {"role": "operator", "token_sha256": hashlib.sha256(token.encode()).hexdigest()},
        "approver": {"role": "approver", "token_sha256": hashlib.sha256(approver_token.encode()).hexdigest()},
    }))
    monkeypatch.setattr(control_plane, "CONTROL", tmp_path)
    monkeypatch.setattr(control_plane, "JOBS", tmp_path / "jobs")
    monkeypatch.setattr(control_plane, "APPROVALS", tmp_path / "approvals")
    task = ("task_id: bound\nmodel: pblup\ncommand: run\ntraits: [EN]\n"
            "inputs:\n  phenotype: D:/data/sample.tsv\n")
    approval = control_plane.issue_approval("approver", approver_token, task)
    job = control_plane.submit("operator", token, task, "run", True, approval_id=approval["approval_id"], approval_token=approval["approval_token"])
    assert job["task_sha256"] == approval["task_sha256"]
    with pytest.raises(GatewayError):
        control_plane.submit("operator", token, task, "run", True, approval_id=approval["approval_id"], approval_token=approval["approval_token"])


def test_job_read_is_isolated_to_owner(tmp_path, monkeypatch):
    owner, other = "owner-token", "other-token"
    monkeypatch.setenv("ABGES_MCP_ACCESS_JSON", json.dumps({
        "owner": {"role": "viewer", "token_sha256": hashlib.sha256(owner.encode()).hexdigest()},
        "other": {"role": "viewer", "token_sha256": hashlib.sha256(other.encode()).hexdigest()},
    }))
    monkeypatch.setattr(control_plane, "CONTROL", tmp_path); monkeypatch.setattr(control_plane, "JOBS", tmp_path / "jobs")
    control_plane._write(control_plane._job_path("job_owned"), {"job_id": "job_owned", "status": "queued", "actor": {"identity": "owner"}})
    assert control_plane.get_job("owner", owner, "job_owned")["job_id"] == "job_owned"
    with pytest.raises(GatewayError): control_plane.get_job("other", other, "job_owned")


def test_submit_rejects_command_different_from_approved_task(tmp_path, monkeypatch):
    token = "operator-token"
    monkeypatch.setenv("ABGES_MCP_ACCESS_JSON", json.dumps({"operator": {"role": "operator", "token_sha256": hashlib.sha256(token.encode()).hexdigest()}}))
    monkeypatch.setattr(control_plane, "CONTROL", tmp_path); monkeypatch.setattr(control_plane, "JOBS", tmp_path / "jobs")
    with pytest.raises(GatewayError):
        control_plane.submit(
            "operator", token,
            "task_id: sample\ncommand: plan\nmodel: pblup\ntraits: [EN]\n"
            "inputs:\n  phenotype: D:/data/sample.tsv\n",
            "run",
        )


def test_expired_running_job_is_requeued(tmp_path, monkeypatch):
    monkeypatch.setattr(control_plane, "CONTROL", tmp_path); monkeypatch.setattr(control_plane, "JOBS", tmp_path / "jobs")
    control_plane._write(control_plane._running_path("job_stale"), {"job_id": "job_stale", "status": "running", "actor": {"identity": "x"}, "attempt": 1, "lease_expires_at": "2000-01-01T00:00:00"})
    assert control_plane.recover_stale_jobs() == ["job_stale"]
    assert json.loads(control_plane._job_path("job_stale").read_text(encoding="utf-8"))["status"] == "queued"


def test_context_packet_excludes_unrelated_blackboard(tmp_path):
    import sys
    root = Path(__file__).resolve().parents[2]; harness = str(root / "harness")
    if harness not in sys.path: sys.path.insert(0, harness)
    from _bootstrap import load_component
    Orchestrator = load_component("orchestrator", "orchestrator").Orchestrator
    state = load_component("state-manager", "state_manager").StateManager("packet", run_root=str(tmp_path))
    state.put("plan", {"trait": "BW"}); state.put("secret_other_stage", "do-not-leak")
    orchestrator = object.__new__(Orchestrator); orchestrator.state = state
    packet = orchestrator._context_packet("01")
    assert packet["plan"]["trait"] == "BW" and "secret_other_stage" not in packet
