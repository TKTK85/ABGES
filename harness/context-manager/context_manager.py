# -*- coding: utf-8 -*-
"""Platform-neutral, privacy-preserving run-context checkpoints."""
import datetime
import json
import os


class ContextManager(object):
    """Persist minimal handoff state without serialising raw analysis objects."""

    def __init__(self, run_dir, run_id, task):
        self.run_dir = run_dir
        self.run_id = run_id
        self.context_dir = os.path.join(run_dir, "context")
        os.makedirs(self.context_dir, exist_ok=True)
        self.state_path = os.path.join(self.context_dir, "current_state.json")
        self.summary_path = os.path.join(self.context_dir, "context_summary.md")
        self.log_path = os.path.join(self.context_dir, "decision_log.jsonl")
        self.objective = str((task or {}).get("task_id") or "ABGES evaluation")
        # 通用上下文存储（供外部注入如 task_root 等信息）
        self.extra = {}
        self._write("initialized", None, {}, [], [], "Select the applicable stage runner.")

    @staticmethod
    def _now():
        return datetime.datetime.now().isoformat(timespec="seconds")

    @staticmethod
    def _warning_items(items):
        return [{"code": item.get("code"), "stage": item.get("stage"),
                 "message": str(item.get("message", ""))[:500]}
                for item in (items or []) if isinstance(item, dict)]

    def put(self, key, value):
        """写入通用上下文，供后续阶段读取（内存态，不落盘）。"""
        self.extra[key] = value
        return value

    def get(self, key, default=None):
        """读取通用上下文。"""
        return self.extra.get(key, default)

    def checkpoint(self, stage, state, result=None, gate_summary=None):
        result = result or {}
        artifacts = [{"name": name, "path": value.get("path")}
                     for name, value in (state.state.get("artifacts") or {}).items()
                     if isinstance(value, dict)
                     and not (isinstance(value.get("path"), str)
                              and value.get("path").lower().endswith(".json"))]
        risks = self._warning_items(state.state.get("warnings"))
        risks.extend(self._warning_items(state.state.get("errors")))
        next_action = "Review applicable gates and continue to the next stage."
        if state.state.get("errors"):
            next_action = "Resolve blocking errors before continuing."
        checkpoint_status = (state.state.get("status", "running")
                             if str(stage) == "final" else state.stage_status(stage))
        self._write(checkpoint_status, str(stage),
                    gate_summary or {}, artifacts, risks, next_action)

    def _write(self, status, stage, gates, artifacts, risks, next_action):
        payload = {
            "run_id": self.run_id,
            "updated_at": self._now(),
            "objective": self.objective,
            "current_stage": stage or "not_started",
            "status": status,
            "model_id": None,
            "constraints": ["raw inputs are read-only", "use explicit runner", "externalize large artifacts"],
            "decisions": [],
            "gates": gates,
            "artifacts": artifacts[:200],
            "risks": risks[:100],
            "next_action": next_action,
        }
        with open(self.state_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        with open(self.log_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"timestamp": payload["updated_at"], "stage": payload["current_stage"], "status": status, "next_action": next_action}, ensure_ascii=False) + "\n")
        with open(self.summary_path, "w", encoding="utf-8") as handle:
            handle.write("# ABGES context handoff\n\n")
            handle.write("- Run: `%s`\n- Stage: `%s`\n- Status: `%s`\n- Next action: %s\n" % (self.run_id, payload["current_stage"], status, next_action))
        return payload