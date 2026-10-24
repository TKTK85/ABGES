"""Persistent local control plane: authenticated jobs, approvals, worker and access isolation."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .service import GatewayError, execute_task
from abges_runtime.storage import atomic_json, identifier, file_lock
from abges_runtime.tasks import validate_task
from functools import wraps

ROOT = Path(__file__).resolve().parents[1]
CONTROL = ROOT / "workspace" / "control_plane"
JOBS = CONTROL / "jobs"
APPROVALS = CONTROL / "approvals"
AUDIT = CONTROL / "audit.jsonl"
LEASE_SECONDS = int(os.environ.get("ABGES_MCP_LEASE_SECONDS", "7500"))
MAX_ATTEMPTS = int(os.environ.get("ABGES_MCP_MAX_ATTEMPTS", "2"))
HEARTBEAT_SECONDS = float(os.environ.get("ABGES_MCP_HEARTBEAT_SECONDS", "30"))

ROLE_CAPABILITIES = {
    "viewer": {"read"},
    "planner": {"read", "validate", "plan"},
    "operator": {"read", "validate", "plan", "run", "resume"},
    "approver": {"read", "approve"},
    "admin": {"read", "validate", "plan", "run", "resume", "approve", "admin"},
}


def _now(): return datetime.now().isoformat(timespec="seconds")
def _sha(text): return hashlib.sha256(text.encode("utf-8")).hexdigest()
def _normalized_task(task_yaml):
    try:
        task = __import__("yaml").safe_load(task_yaml)
    except Exception as exc:
        raise GatewayError("task_yaml is not valid YAML") from exc
    if not isinstance(task, dict):
        raise GatewayError("task_yaml must contain one mapping")
    return validate_task(task)

def task_digest(task_yaml):
    """Stable digest binds an approval to precisely the submitted task content."""
    return _sha(json.dumps(_normalized_task(task_yaml), sort_keys=True, ensure_ascii=False, separators=(",", ":")))

def _assert_command_matches_task(task_yaml, command):
    task_command = str(_normalized_task(task_yaml).get("command", "run"))
    if task_command != command:
        raise GatewayError("requested command must match task_yaml command")

def _serialized(fn):
    @wraps(fn)
    def transaction(*args, **kwargs):
        with file_lock(CONTROL / ".transaction.lock"):
            return fn(*args, **kwargs)
    return transaction


def _write(path, payload):
    atomic_json(path, payload)


def _event(event, **fields):
    CONTROL.mkdir(parents=True, exist_ok=True)
    audit_path = CONTROL / "audit.jsonl"
    with audit_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"at": _now(), "event": event, **fields}, ensure_ascii=False) + "\n")

def _job_path(job_id): return JOBS / (identifier(job_id) + ".json")
def _lease_until(): return (datetime.now() + timedelta(seconds=LEASE_SECONDS)).isoformat(timespec="seconds")
def _running_path(job_id): return JOBS / (identifier(job_id) + ".running.json")
def _approval_path(approval_id): return APPROVALS / (identifier(approval_id) + ".json")

def _find_job(job_id):
    for path in (_job_path(job_id), _running_path(job_id)):
        if path.is_file(): return path
    return None

def authorize(identity, access_token, capability):
    try: registry = json.loads(os.environ.get("ABGES_MCP_ACCESS_JSON", "{}"))
    except json.JSONDecodeError as exc: raise GatewayError("ABGES_MCP_ACCESS_JSON is invalid") from exc
    record = registry.get(identity)
    if not identity or not access_token or not isinstance(record, dict): raise GatewayError("authentication failed")
    if not secrets.compare_digest(str(record.get("token_sha256", "")), _sha(access_token)): raise GatewayError("authentication failed")
    role = str(record.get("role", ""))
    if capability not in ROLE_CAPABILITIES.get(role, set()): raise GatewayError("authorization denied for capability: " + capability)
    return {"identity": identity, "role": role}

def issue_approval(identity, access_token, task_yaml, ttl_minutes=30, resume_run=None):
    actor = authorize(identity, access_token, "approve")
    if not 1 <= int(ttl_minutes) <= 1440: raise GatewayError("ttl_minutes must be between 1 and 1440")
    if resume_run:
        identifier(resume_run)
    approval_id, token = "apr_" + secrets.token_hex(10), secrets.token_urlsafe(32)
    record = {"approval_id": approval_id, "status": "active", "created_at": _now(),
              "expires_at": (datetime.now() + timedelta(minutes=int(ttl_minutes))).isoformat(timespec="seconds"),
              "approver": actor, "task_sha256": task_digest(task_yaml), "token_sha256": _sha(token), "used_by": None, "resume_run": resume_run}
    _write(_approval_path(approval_id), record)
    _event("approval_issued", approval_id=approval_id, approver=actor, task_sha256=record["task_sha256"])
    return {"approval_id": approval_id, "approval_token": token, "expires_at": record["expires_at"], "task_sha256": record["task_sha256"]}

def _consume_approval(approval_id, approval_token, task_yaml, actor, resume_run=None):
    path = _approval_path(approval_id)
    if not approval_id or not approval_token or not path.is_file(): raise GatewayError("a valid approval is required for run")
    claimed = path.with_suffix(".consuming.json")
    try: os.replace(path, claimed)
    except FileNotFoundError: raise GatewayError("approval is expired or already used")
    record = json.loads(claimed.read_text(encoding="utf-8"))
    if record.get("status") != "active" or datetime.fromisoformat(record["expires_at"]) <= datetime.now():
        os.replace(claimed, path); raise GatewayError("approval is expired or already used")
    if not secrets.compare_digest(record.get("token_sha256", ""), _sha(approval_token)):
        os.replace(claimed, path); raise GatewayError("approval token is invalid")
    digest = task_digest(task_yaml)
    if not secrets.compare_digest(record.get("task_sha256", ""), digest):
        os.replace(claimed, path); raise GatewayError("approval does not match this task")
    if record.get("resume_run") != resume_run:
        os.replace(claimed, path); raise GatewayError("approval does not match resume target")
    record.update({"status": "consumed", "used_at": _now(), "used_by": actor})
    _write(claimed, record); os.replace(claimed, path)
    _event("approval_consumed", approval_id=approval_id, actor=actor, task_sha256=digest)
    return {"approval_id": approval_id, "task_sha256": digest, "approver": record["approver"]}

@_serialized
def submit(identity, access_token, task_yaml, command, approved=False, resume_run=None, approval_id=None, approval_token=None):
    capability = "resume" if resume_run else command
    actor = authorize(identity, access_token, capability)
    if command not in {"validate", "plan", "run"}: raise GatewayError("command must be validate, plan, or run")
    _assert_command_matches_task(task_yaml, command)
    if resume_run:
        if command != "run": raise GatewayError("resume requires run command")
        identifier(resume_run)
        assert_run_access(identity, access_token, resume_run)
    approval = None
    if command == "run":
        # Legacy boolean cannot authorize a biological evaluation.
        if not approved: raise GatewayError("run requires an approval token")
        approval = _consume_approval(approval_id, approval_token, task_yaml, actor, resume_run)
    job_id = "job_" + secrets.token_hex(10)
    job = {"job_id": job_id, "status": "queued", "created_at": _now(), "updated_at": _now(), "actor": actor,
           "command": command, "approval": approval, "resume_run": resume_run, "task_sha256": task_digest(task_yaml), "task_yaml": task_yaml}
    _write(_job_path(job_id), job)
    _event("job_queued", job_id=job_id, actor=actor, command=command, task_sha256=job["task_sha256"], approval_id=(approval or {}).get("approval_id"))
    return {key: job[key] for key in ("job_id", "status", "created_at", "actor", "command", "resume_run", "task_sha256")}

def _can_read(actor, job): return actor.get("role") == "admin" or job.get("actor", {}).get("identity") == actor.get("identity")
def get_job(identity, access_token, job_id):
    actor = authorize(identity, access_token, "read"); path = _find_job(job_id)
    if path is None: raise GatewayError("job_id was not found")
    job = json.loads(path.read_text(encoding="utf-8"))
    if not _can_read(actor, job): raise GatewayError("job access denied")
    return {key: value for key, value in job.items() if key != "task_yaml"}

def _recover_stale_jobs_unlocked():
    """Requeue expired claims; a live worker extends its lease by heartbeat."""
    JOBS.mkdir(parents=True, exist_ok=True)
    recovered = []
    for path in JOBS.glob("job_*.running.json"):
        try: job = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError): continue
        expires = job.get("lease_expires_at")
        if not expires or datetime.fromisoformat(expires) > datetime.now(): continue
        # A late heartbeat is not proof that the owner died. Never dispatch a
        # second evaluation while its original worker is still alive.
        if _process_alive(job.get("worker_pid")):
            continue
        attempts = int(job.get("attempt", 1))
        job.update({"updated_at": _now(), "recovered_at": _now(), "recovery_reason": "lease_expired"})
        if attempts >= MAX_ATTEMPTS:
            job.update({"status": "failed", "error": "worker lease expired after maximum attempts"})
        else:
            job.update({"status": "queued", "attempt": attempts})
        destination = _job_path(job["job_id"])
        _write(path, job); os.replace(path, destination)
        _event("job_recovered", job_id=job["job_id"], status=job["status"], attempt=job.get("attempt"))
        recovered.append(job["job_id"])
    return recovered


def _process_alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            # Access denied is not proof of death; preserve the lease owner.
            return ctypes.get_last_error() == 5
        try:
            code = ctypes.c_ulong()
            return not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True

@_serialized
def recover_stale_jobs():
    return _recover_stale_jobs_unlocked()


@_serialized
def _renew_lease(path, token):
    if not path.is_file(): return False
    current = json.loads(path.read_text(encoding="utf-8"))
    if current.get("claim_token") != token: return False
    current.update({"heartbeat_at": _now(), "lease_expires_at": _lease_until(), "updated_at": _now()})
    _write(path, current)
    return True


def _heartbeat(path, job, stop_event):
    while not stop_event.wait(HEARTBEAT_SECONDS):
        if not _renew_lease(path, job.get("claim_token")):
            return


@_serialized
def _finish_job(path, job):
    if not path.is_file(): return False
    current = json.loads(path.read_text(encoding="utf-8"))
    if current.get("claim_token") != job.get("claim_token"): return False
    job.pop("lease_expires_at", None)
    job["updated_at"] = _now()
    _write(path, job)
    os.replace(path, _job_path(job["job_id"]))
    _event("job_finished", job_id=job["job_id"], status=job["status"], run_id=job.get("run_id"))
    return True


@_serialized
def _claim_next_job():
    _recover_stale_jobs_unlocked()
    JOBS.mkdir(parents=True, exist_ok=True)
    for path in sorted(JOBS.glob("job_*.json")):
        if path.name.endswith(".running.json"): continue
        try: job = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError): continue
        if job.get("status") != "queued": continue
        claimed = _running_path(job["job_id"])
        try: os.replace(path, claimed)
        except FileNotFoundError: continue
        job.update({"status": "running", "updated_at": _now(), "worker_pid": os.getpid(), "claim_token": secrets.token_hex(16),
                    "attempt": int(job.get("attempt", 0)) + 1, "heartbeat_at": _now(),
                    "lease_expires_at": _lease_until()}); _write(claimed, job)
        _event("job_started", job_id=job["job_id"], actor=job["actor"], task_sha256=job.get("task_sha256", "legacy_unbound")); return claimed, job
    return None

def run_one_worker():
    claimed = _claim_next_job()
    if claimed is None: return {"status": "idle"}
    path, job = claimed
    stop_heartbeat = threading.Event()
    heartbeat = threading.Thread(target=_heartbeat, args=(path, job, stop_heartbeat), daemon=True)
    heartbeat.start()
    try:
        result = execute_task(job["task_yaml"], job["command"], approved=bool(job.get("approval")), resume_run=job.get("resume_run"), gateway_context={"job_id": job["job_id"], "actor": job["actor"], "task_sha256": job["task_sha256"], "approval": job.get("approval")})
        job.update({"status": result.get("status", "failed"), "result": result, "run_id": result.get("run_id"), "updated_at": _now()})
    except Exception as exc: job.update({"status": "failed", "error": str(exc), "updated_at": _now()})
    finally:
        stop_heartbeat.set(); heartbeat.join(timeout=HEARTBEAT_SECONDS + 1)
    job.pop("lease_expires_at", None); job.update({"heartbeat_at": _now(), "updated_at": _now()})
    if not _finish_job(path, job):
        return {"status": "failed", "error": "worker lease lost; result was not published"}
    return {key: value for key, value in job.items() if key != "task_yaml"}

def run_worker_forever(poll_seconds=1.0):
    if poll_seconds <= 0: raise ValueError("poll_seconds must be positive")
    recover_stale_jobs()
    _event("worker_started", worker_pid=os.getpid())
    try:
        while True:
            if run_one_worker().get("status") == "idle": time.sleep(poll_seconds)
    except KeyboardInterrupt: _event("worker_stopped", worker_pid=os.getpid(), reason="keyboard_interrupt")

def _job_for_run(run_id):
    for path in JOBS.glob("job_*.json"):
        try:
            job=json.loads(path.read_text(encoding="utf-8"))
            if job.get("run_id") == run_id: return job
        except (OSError, json.JSONDecodeError): pass
    return None

def assert_run_access(identity, access_token, run_id):
    actor=authorize(identity, access_token, "read"); job=_job_for_run(run_id)
    if job is None or not _can_read(actor, job): raise GatewayError("run access denied")
    return actor
