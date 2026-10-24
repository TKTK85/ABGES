"""Safe, platform-neutral service layer behind the ABGES MCP server.

The service deliberately delegates all evaluation work to the existing Harness
CLI.  It does not construct HIBLUP or PLINK commands and never returns raw
input data or unbounded logs to an agent context.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import yaml
from abges_runtime.storage import identifier, inside, resolve_run, run_directories
from abges_runtime.tasks import validate_task
from abges_runtime.processes import run_process


ROOT = Path(__file__).resolve().parents[1]
RUN_ROOT = ROOT / "workspace"
MCP_TASK_ROOT = ROOT / "workspace" / "mcp_tasks"
RUNNERS = {
    "pblup": "runners.pblup_stage_runner:run",
    "gblup": "runners.gblup_stage_runner:run",
    "ssgblup": "runners.ssgblup_stage_runner:run",
}
PUBLIC_ARTIFACTS = {
    "result_json", "validation_report", "ebv_top20", "phenotype_qc_report",
    "pedigree_qc_report", "integration_report", "execution_plan",
    "run_metadata", "run_statistics", "delivery_report", "agent_result_matrix",
    "derived_trait_report", "genotype_qc_report", "gate_audit",
}


class GatewayError(ValueError):
    """An input or authorization error that an MCP client can correct."""


def _load_task(task_yaml: str) -> dict[str, Any]:
    try:
        task = yaml.safe_load(task_yaml)
    except yaml.YAMLError as exc:
        raise GatewayError("task_yaml is not valid YAML") from exc
    if not isinstance(task, dict):
        raise GatewayError("task_yaml must contain one mapping")
    try:
        return validate_task(task)
    except ValueError as exc:
        raise GatewayError(str(exc)) from exc


def _runner_for(task: dict[str, Any]) -> str:
    model = str(task.get("model", "")).lower()
    if model not in RUNNERS:
        raise GatewayError("MCP requires an explicit model: pblup, gblup, or ssgblup")
    return RUNNERS[model]


def _json_from_stdout(stdout: str) -> dict[str, Any]:
    """Extract the Harness' JSON payload without treating logs as a result."""
    decoder = json.JSONDecoder()
    for index, char in enumerate(stdout):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(stdout[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and "status" in value:
            return value
    raise GatewayError("Harness returned no machine-readable result")


def _compact_result(result: dict[str, Any]) -> dict[str, Any]:
    artifacts = result.get("artifacts") or {}
    # 【2026-10-19】结果展示剔除 reports/input/output 下的 .json 内部文件
    return {
        "status": result.get("status"),
        "run_id": result.get("run_id"),
        "model_id": result.get("model_id"),
        "status_basis": result.get("status_basis"),
        "gate_summary": result.get("gate_summary"),
        "warnings": result.get("warnings", []),
        "errors": result.get("errors", []),
        "stages": result.get("stages", []),
        "gateway": result.get("gateway"),
        "artifacts": {name: value for name, value in artifacts.items()
                      if name in PUBLIC_ARTIFACTS
                      and not str((value.get("path") if isinstance(value, dict) else value) or "").lower().endswith(".json")},
    }


def execute_task(task_yaml: str, command: str, approved: bool = False, resume_run: str | None = None, gateway_context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run one Harness command with an allowlisted runner and auditable task copy."""
    if command not in {"validate", "plan", "run"}:
        raise GatewayError("command must be validate, plan, or run")
    if command == "run" and (not approved or not gateway_context or not gateway_context.get("approval")):
        raise GatewayError("run must be dispatched through an approved control-plane job")
    task = _load_task(task_yaml)
    if str(task.get("command", "run")) != command:
        raise GatewayError("requested command must match task_yaml command")
    # The authenticated MCP action is authoritative. Existing human task files
    # commonly default to command: run, so preserve the requested source copy
    # only after recording the explicit gateway command into the audit copy.
    task["command"] = command
    # This contains no access token. Harness persists it and writes it into its
    # own audit stream, binding MCP job, task digest and final run_id.
    task["gateway_context"] = dict(gateway_context or {})
    runner = _runner_for(task)
    MCP_TASK_ROOT.mkdir(parents=True, exist_ok=True)
    task_path = inside(MCP_TASK_ROOT, MCP_TASK_ROOT / ("task_" + uuid.uuid4().hex + ".yaml"))
    task_path.write_text(yaml.safe_dump(task, allow_unicode=True, sort_keys=False), encoding="utf-8")
    environment = dict(os.environ)
    environment["PYTHONIOENCODING"] = "utf-8"
    completed = run_process(
        [sys.executable, "harness/entrypoint/main.py", str(task_path),
         "--runner", runner, "--mode", "deny"] + (["--resume-run", resume_run] if resume_run else []),
        cwd=ROOT, env=environment, timeout=7200,
    )
    try:
        result = _json_from_stdout(completed.stdout)
    except GatewayError:
        raise GatewayError("Harness did not return JSON; inspect the server audit log")
    result["gateway"] = {
        "command": command,
        "task_copy": str(task_path.relative_to(ROOT)),
        "process_exit_code": completed.returncode,
        "resume_run": resume_run,
    }
    return _compact_result(result)


def check_environment(require_engines: bool = False) -> dict[str, Any]:
    args = [sys.executable, "scripts/check_environment.py", "--json"]
    if require_engines:
        args.append("--require-engines")
    completed = subprocess.run(args, cwd=ROOT, text=True, encoding="utf-8",
                               errors="replace", capture_output=True, check=False)
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise GatewayError("environment check returned invalid JSON") from exc


def list_runs(limit: int = 20, allowed_ids=None) -> list[dict[str, Any]]:
    if not 1 <= limit <= 100:
        raise GatewayError("limit must be between 1 and 100")
    result = []
    for directory in sorted(run_directories(RUN_ROOT), key=lambda p: p.name, reverse=True):
        if allowed_ids is not None and directory.name not in allowed_ids:
            continue
        state_path = directory / "state.json"
        if not state_path.is_file():
            continue
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        result.append({key: state.get(key) for key in
                       ("run_id", "created_at", "updated_at", "status", "model_id")})
        if len(result) >= limit:
            break
    return result


def _credibility(state: dict[str, Any]) -> dict[str, Any]:
    artifacts = state.get("artifacts") or {}
    required = ("validation_report", "run_metadata", "run_statistics")
    missing = [name for name in required if name not in artifacts]
    completed = state.get("status") == "completed"
    return {"decision_ready": completed and not missing,
            "status": state.get("status"),
            "missing_evidence": missing,
            "engine_outputs_present": bool(artifacts.get("hiblup_vars") and artifacts.get("hiblup_rand")),
            "resume_checkpoint": state.get("resume_checkpoint"),
            "rule": "completed + validation/report/metadata/statistics artifacts"}


def get_run_summary(run_id: str) -> dict[str, Any]:
    _validate_run_id(run_id)
    try:
        directory = resolve_run(RUN_ROOT, run_id)
    except ValueError as exc:
        raise GatewayError(str(exc)) from exc
    path = directory / "state.json"
    if not path.is_file():
        raise GatewayError("run_id was not found")
    state = json.loads(path.read_text(encoding="utf-8"))
    artifacts = {name: value for name, value in (state.get("artifacts") or {}).items()
                 if name in PUBLIC_ARTIFACTS}
    matrix = directory / "agent_result_matrix.json"
    if matrix.is_file():
        artifacts["agent_result_matrix"] = {"path": str(matrix), "type": "json", "desc": "resumable agent-result matrix"}
    return {"run_id": state.get("run_id"), "created_at": state.get("created_at"),
            "updated_at": state.get("updated_at"), "status": state.get("status"),
            "model_id": state.get("model_id"), "stages": state.get("stages", []),
            "warnings": state.get("warnings", []), "errors": state.get("errors", []),
            "artifacts": artifacts, "credibility": _credibility(state)}


def get_artifact(run_id: str, artifact_name: str, max_chars: int = 12000) -> dict[str, Any]:
    _validate_run_id(run_id)
    if artifact_name not in PUBLIC_ARTIFACTS:
        raise GatewayError("artifact_name is not allowed by the MCP context policy")
    if not 1 <= max_chars <= 12000:
        raise GatewayError("max_chars must be between 1 and 12000")
    summary = get_run_summary(run_id)
    artifact = (summary.get("artifacts") or {}).get(artifact_name)
    if not artifact:
        raise GatewayError("requested public artifact is not present for this run")
    path = inside(resolve_run(RUN_ROOT, run_id), artifact["path"])
    if not path.is_file():
        raise GatewayError("registered artifact file is missing")
    suffix = path.suffix.lower()
    # Context injection is text-only: unknown extensions are not decoded.
    text_suffixes = {".json", ".md", ".txt", ".csv", ".tsv", ".yaml", ".yml"}
    if suffix not in text_suffixes:
        return {"run_id": run_id, "artifact_name": artifact_name, "path": str(path),
                "binary": True, "content": None, "truncated": False,
                "size_bytes": path.stat().st_size, "suffix": suffix}
    with path.open(encoding="utf-8", errors="replace") as stream:
        content = stream.read(max_chars + 1)
    return {"run_id": run_id, "artifact_name": artifact_name, "path": str(path),
            "binary": False, "content": content[:max_chars], "truncated": len(content) > max_chars,
            "size_bytes": path.stat().st_size, "suffix": suffix}


def _validate_run_id(run_id: str) -> None:
    try:
        identifier(run_id)
    except ValueError as exc:
        raise GatewayError(str(exc)) from exc
