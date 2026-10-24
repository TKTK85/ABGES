"""ABGES MCP stdio server: authenticated gateway to the Harness."""
from __future__ import annotations
from mcp.server import MCPServer
from .service import check_environment, get_artifact, get_run_summary, list_runs
from .control_plane import (assert_run_access, authorize, get_job, issue_approval, submit, _can_read, _job_for_run)

mcp = MCPServer("ABGES", version="1.1.0")

@mcp.tool()
def abges_check_environment(identity: str, access_token: str, require_engines: bool = False) -> dict:
    """Read environment capability only for an authenticated caller."""
    authorize(identity, access_token, "read")
    return check_environment(require_engines)

@mcp.tool()
def abges_approve(identity: str, access_token: str, task_yaml: str, ttl_minutes: int = 30, resume_run: str = "") -> dict:
    """Issue a one-time approval bound to exactly this normalized task YAML."""
    return issue_approval(identity, access_token, task_yaml, ttl_minutes, resume_run or None)

@mcp.tool()
def abges_submit(identity: str, access_token: str, task_yaml: str, command: str, approved: bool = False, approval_id: str = "", approval_token: str = "") -> dict:
    """Queue validate/plan, or an approval-token-bound biological evaluation run."""
    return submit(identity, access_token, task_yaml, command, approved, approval_id=approval_id or None, approval_token=approval_token or None)

@mcp.tool()
def abges_resume(identity: str, access_token: str, task_yaml: str, resume_run: str, approval_id: str, approval_token: str) -> dict:
    return submit(identity, access_token, task_yaml, "run", True, resume_run, approval_id, approval_token)

@mcp.tool()
def abges_get_job(identity: str, access_token: str, job_id: str) -> dict:
    return get_job(identity, access_token, job_id)

@mcp.tool()
def abges_list_runs(identity: str, access_token: str, limit: int = 20) -> list[dict]:
    actor = authorize(identity, access_token, "read")
    from .control_plane import JOBS
    import json
    allowed = set()
    for path in JOBS.glob("job_*.json"):
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
            if _can_read(actor, job): allowed.add(job.get("run_id"))
        except (OSError, ValueError):
            continue
    return list_runs(limit=limit, allowed_ids=allowed)

@mcp.tool()
def abges_get_run_summary(identity: str, access_token: str, run_id: str) -> dict:
    assert_run_access(identity, access_token, run_id)
    result = get_run_summary(run_id)
    result["artifacts"] = {k: {a: b for a, b in v.items() if a != "path"} for k, v in result["artifacts"].items()}
    return result

@mcp.tool()
def abges_get_artifact(identity: str, access_token: str, run_id: str, artifact_name: str, max_chars: int = 12000) -> dict:
    assert_run_access(identity, access_token, run_id)
    result = get_artifact(run_id, artifact_name, max_chars)
    result.pop("path", None)
    return result

def main() -> None: mcp.run(transport="stdio")
if __name__ == "__main__": main()
