# -*- coding: utf-8 -*-
"""
State Manager
=============
任务状态与中间结果的持有者。

职责：
  1. 为每次运行建立独立目录（workspace/{run_id}），永不复用；
  2. 维护 state.json，记录任务、各阶段状态、产物与告警，支持 status 查询；
  3. 提供黑板（blackboard）在阶段之间传递 DataFrame 等不可序列化对象，
     黑板只在内存，落盘的一律是可读的 JSON 与数据文件。
"""

import os
import time
import datetime
import hashlib
import json
import uuid
from abges_runtime.storage import identifier, inside, resolve_run, atomic_json
from abges_runtime.checkpoints import encode, decode
from abges_runtime.tasks import input_manifest
import threading

from _bootstrap import bootstrap

bootstrap()

import abges_core as core

STAGE_PENDING = "pending"
STAGE_RUNNING = "running"
STAGE_COMPLETED = "completed"
STAGE_SKIPPED = "skipped"
STAGE_FAILED = "failed"

# 跳过的两种性质，必须分开记录：
#   not_applicable  设计内不适用（如 PBLUP 任务无需基因型阶段），属正常流程
#   upstream_failed 因上游失败或闸门阻断而未能执行，属异常中断
# 二者在 result-schema#status_rules 中的含义完全不同，不可混为一谈。
SKIP_NOT_APPLICABLE = "not_applicable"
SKIP_UPSTREAM_FAILED = "upstream_failed"


class StateManager(object):
    """运行状态管理器。"""

    def __init__(self, run_id, run_root=None):
        identifier(run_id)
        self.run_id = run_id
        root = run_root or core.resolve_project_path("workspace")
        if run_root and os.path.basename(os.path.normpath(run_root)) == "_runs":
            task_root = os.path.dirname(os.path.abspath(run_root))
            for name in ("input", "output", "reports"):
                core.ensure_dir(os.path.join(task_root, name))
        self.run_dir = str(inside(root, os.path.join(root, run_id)))
        os.makedirs(self.run_dir, exist_ok=False)
        # 交付目录：input / output / reports 三件套（对外交付物）
        # logs / intermediate / change_logs 为 runner 侧依赖目录，2026-10-16 重构
        # 精简时漏加，导致多处 state.dirs[...] 引用 KeyError，此处一并补回
        self.dirs = {
            "input": core.ensure_dir(os.path.join(self.run_dir, "input")),
            "output": core.ensure_dir(os.path.join(self.run_dir, "output")),
            "reports": core.ensure_dir(os.path.join(self.run_dir, "reports")),
            "logs": core.ensure_dir(os.path.join(self.run_dir, "logs")),
            "intermediate": core.ensure_dir(os.path.join(self.run_dir, "intermediate")),
            "change_logs": core.ensure_dir(os.path.join(self.run_dir, "change_logs")),
        }
        self.state_path = os.path.join(self.run_dir, "state.json")
        self.matrix_path = os.path.join(self.run_dir, "agent_result_matrix.json")
        self.blackboard_path = os.path.join(self.run_dir, "checkpoint_blackboard.json")
        self.state = {
            "run_id": run_id,
            "abges_version": core.ABGES_VERSION,
            "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "updated_at": None,
            "status": "initialized",
            "task": {},
            "model_id": None,
            "stages": [],
            "artifacts": {},
            "warnings": [],
            "errors": [],
            "statistics": {},
            "pending_recommendations": [],   # 方案决策（效应筛选choose_one等，先生拍板）
        }
        self.blackboard = {}
        self._lock = threading.RLock()
        self._stage_clock = {}
        self.resumed = False
        self.save()

    # -----------------------------------------------------------
    # 一、任务与总体状态
    # -----------------------------------------------------------
    def __getstate__(self):
        """Exclude the runtime lock from persisted blackboard checkpoints."""
        payload = dict(self.__dict__)
        payload.pop("_lock", None)
        return payload

    def __setstate__(self, payload):
        self.__dict__.update(payload)
        self._lock = threading.RLock()

    @classmethod
    def resume(cls, run_id, run_root=None, expected_task=None):
        """Restore a checksum-verified blackboard checkpoint in place."""
        root = run_root or core.resolve_project_path("workspace")
        run_dir = str(resolve_run(root, run_id))
        state_path = os.path.join(run_dir, "state.json")
        core.require_readable_file(state_path, "E017")
        instance = cls.__new__(cls)
        instance.run_id, instance.run_dir, instance.state_path = run_id, run_dir, state_path
        # 交付目录 + 工作目录（与 __init__ 保持一致）
        instance.dirs = {name: os.path.join(run_dir, name) for name in ("input", "output", "reports", "logs", "intermediate", "change_logs")}
        # 【S6】resume 路径下目录可能已被外部清理，而 setup_logger 需要 logs 目录存在，
        # 与 __init__ 对齐：进入时确保目录已建立。
        try:
            core.ensure_dir(instance.dirs["logs"])
        except Exception:
            pass
        instance.matrix_path, instance.blackboard_path = os.path.join(run_dir, "agent_result_matrix.json"), os.path.join(run_dir, "checkpoint_blackboard.json")
        with open(state_path, "r", encoding="utf-8") as handle: instance.state = json.load(handle)
        if expected_task is not None:
            previous, requested = dict(instance.state.get("task") or {}), dict(expected_task)
            for item in (previous, requested):
                for key in ("command", "gateway_context", "recommend_decisions"):
                    item.pop(key, None)
            if previous != requested:
                raise core.AbgesError("E017", "resume task differs from original task")
            frozen = instance.state.get("input_manifest")
            if frozen is None or frozen != input_manifest(expected_task.get("inputs", {})):
                raise core.AbgesError("E017", "resume inputs changed or original fingerprints missing")
        instance._lock, instance._stage_clock, instance.resumed = threading.RLock(), {}, True
        # partial 状态在先生拍板暂停时出现（pending_recommendations），属可恢复的中间态
        if instance.state.get("status") == "completed":
            raise core.AbgesError("E017", "only interrupted or failed runs may be resumed")
        if instance.state.get("status") == "partial" and not instance.has_unresolved_recommendations():
            raise core.AbgesError("E017", "partial run has no unresolved recommendations, nothing to resume")
        checkpoint = instance.state.get("resume_checkpoint") or {}
        if checkpoint.get("format") != "data-json-v1":
            raise core.AbgesError("E017", "Legacy pickle checkpoint is disabled; start a fresh run")
        name = identifier(checkpoint.get("file"))
        instance.blackboard_path = str(inside(run_dir, os.path.join(run_dir, name)))
        expected = checkpoint.get("blackboard_sha256")
        if not expected or not os.path.isfile(instance.blackboard_path): raise core.AbgesError("E017", "resume checkpoint missing")
        actual = instance._sha256(instance.blackboard_path)
        if actual != expected: raise core.AbgesError("E017", "resume checkpoint hash mismatch", {"expected": expected, "actual": actual})
        with open(instance.blackboard_path, encoding="utf-8") as handle: instance.blackboard = decode(json.load(handle))
        if not isinstance(instance.blackboard, dict): raise core.AbgesError("E017", "resume checkpoint malformed")
        instance.state["status"] = "resuming"; instance.save(); return instance

    def bind_task(self, task):
        self.state["task"] = task
        self.state.setdefault("input_manifest", input_manifest(task.get("inputs", {})))
        self.state["model_id"] = task.get("model")
        return self.save()

    def set_model(self, model_id):
        self.state["model_id"] = model_id
        return self.save()

    def set_status(self, status):
        self.state["status"] = status
        return self.save()

    # -----------------------------------------------------------
    # 二、阶段
    # -----------------------------------------------------------
    def _find_stage(self, stage_id):
        for entry in self.state["stages"]:
            if entry["stage"] == stage_id:
                return entry
        return None

    def start_stage(self, stage_id, agent_id=None):
        entry = self._find_stage(stage_id)
        if entry is None:
            entry = {"stage": stage_id, "agent": agent_id}
            self.state["stages"].append(entry)
        entry.update({
            "status": STAGE_RUNNING,
            "started_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "finished_at": None,
            "duration_seconds": None,
        })
        self._stage_clock[stage_id] = time.time()
        self.save()
        return entry

    def finish_stage(self, stage_id, status=STAGE_COMPLETED, summary=None):
        entry = self._find_stage(stage_id) or {"stage": stage_id}
        if entry not in self.state["stages"]:
            self.state["stages"].append(entry)
        started = self._stage_clock.get(stage_id)
        entry.update({
            "status": status,
            "finished_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "duration_seconds": round(time.time() - started, 3) if started else None,
            "summary": summary or {},
        })
        self.save()
        return entry

    def reset_stage_for_resume(self, stage_id):
        self.state["warnings"] = [x for x in self.state["warnings"] if x.get("stage") != stage_id]
        self.state["errors"] = [x for x in self.state["errors"] if x.get("stage") != stage_id]
        self.save()

    @staticmethod
    def _sha256(path):
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(1048576), b""): digest.update(block)
        return digest.hexdigest()

    def checkpoint_agent_result(self, agent_id, result=None):
        # Runtime services are re-injected by Orchestrator; never persist them.
        data = {k: v for k, v in self.blackboard.items() if not k.startswith("__")}
        # One atomic snapshot per run: avoid writing a full blackboard copy at
        # every stage and growing dozens of near-duplicate checkpoint JSONs.
        self.blackboard_path = os.path.join(self.run_dir, "checkpoint_blackboard.json")
        atomic_json(self.blackboard_path, encode(data))
        checkpoint = {"format": "data-json-v1", "file": os.path.basename(self.blackboard_path),
                      "agent": str(agent_id), "blackboard_sha256": self._sha256(self.blackboard_path),
                      "created_at": datetime.datetime.now().isoformat(timespec="seconds")}
        self.state["resume_checkpoint"] = checkpoint; result = result or {}
        current_artifacts = [x[0] for x in result.get("artifacts", []) if isinstance(x, (list, tuple)) and len(x) == 2]
        matrix = []
        for stage in self.state.get("stages", []):
            stage_id = str(stage.get("stage"))
            status, skip_kind = stage.get("status"), stage.get("skip_kind")
            if status == STAGE_COMPLETED:
                resume_action = "reuse"
            elif status == STAGE_SKIPPED and skip_kind == SKIP_NOT_APPLICABLE:
                resume_action = "skip"
            else:
                resume_action = "rerun"
            matrix.append({
                "agent": stage_id, "status": status, "skip_kind": skip_kind,
                "finished_at": stage.get("finished_at"),
                "resume_action": resume_action,
                "resume_ready": resume_action != "rerun",
                "artifacts": current_artifacts if stage_id == str(agent_id) else [],
                "warning_codes": [x.get("code") for x in self.state.get("warnings", []) if str(x.get("stage")) == stage_id],
                "error_codes": [x.get("code") for x in self.state.get("errors", []) if str(x.get("stage")) == stage_id],
            })
        core.write_json(self.matrix_path, {"run_id": self.run_id, "checkpoint": checkpoint,
                                           "agents": matrix})
        self.save()
        return checkpoint

    def skip_stage(self, stage_id, reason, agent_id=None,
                   kind=SKIP_NOT_APPLICABLE):
        """
        记录阶段跳过。kind 必须如实反映跳过性质：
        设计内不适用用 SKIP_NOT_APPLICABLE，上游失败导致的中断用
        SKIP_UPSTREAM_FAILED。总体状态判定依赖此字段。
        """
        if kind not in (SKIP_NOT_APPLICABLE, SKIP_UPSTREAM_FAILED):
            raise core.AbgesError("E018", "未知的跳过性质：%r" % (kind,),
                                  {"allowed": [SKIP_NOT_APPLICABLE,
                                               SKIP_UPSTREAM_FAILED]})
        entry = self._find_stage(stage_id)
        if entry is None:
            entry = {"stage": stage_id, "agent": agent_id}
            self.state["stages"].append(entry)
        entry.update({
            "status": STAGE_SKIPPED,
            "skip_kind": kind,
            "reason": reason,
            "finished_at": datetime.datetime.now().isoformat(timespec="seconds"),
        })
        self.save()
        return entry

    def stage_status(self, stage_id):
        entry = self._find_stage(stage_id)
        return entry.get("status") if entry else STAGE_PENDING

    def is_legit_skip(self, stage_id):
        """判断阶段是否为设计内合法跳过（skip_kind=not_applicable）。

        强制流程约束（FlowIntegrityEnforcer）使用：合法跳过视为前置已满足，
        不应触发中断；而因上游失败导致的 skipped（skip_kind=upstream_failed）
        仍属未达标，须阻断下游。
        """
        entry = self._find_stage(stage_id)
        return bool(entry) and entry.get("status") == STAGE_SKIPPED \
            and entry.get("skip_kind") == SKIP_NOT_APPLICABLE

    # -----------------------------------------------------------
    # 四、方案决策（先生拍板）
    # -----------------------------------------------------------

    def set_pending_recommendations(self, decisions):
        """
        存储待先生拍板的方案决策列表。

        在 Stage 06 完成后调用，将方案决策写入 state；
        下次 resume 时可取出，先生选择后由 resolve_recommendation 注入结果。
        """
        self.state["pending_recommendations"] = decisions
        self.save()

    def get_pending_recommendations(self):
        """返回当前所有未解决的方案决策。"""
        return self.state.get("pending_recommendations", [])

    def resolve_pending_recommendation(self, decision_id, chosen_value):
        """
        注入先生对某个决策的选择。

        先生回复"方案A"后，由外部调用本方法更新 state，
        并将结果写入 hitl 的 recommendations 列表供下游使用。
        """
        pending = self.state.get("pending_recommendations", [])
        for item in pending:
            if item.get("decision_id") == decision_id:
                item["chosen"] = chosen_value
                item["resolved"] = True
                self.save()
                return True
        return False

    def has_unresolved_recommendations(self):
        """检查是否有未解决的方案决策。"""
        return any(
            not item.get("resolved", False)
            for item in self.state.get("pending_recommendations", [])
        )

    def reset_stage_checkpoint(self, stage_id):
        """强制某阶段在 resume 时重新执行。

        先生拍板注入会改变模型定义（剔除被拒效应），而 pause 时
        00/01/06 等"模型定义快照"阶段已被判完成并会在 resume 被跳过；
        若不重置，这些阶段会复用含被拒效应的旧产物（plan /
        effective_covariates / hiblup_command），导致先生选择未落实到
        最终模型。本方法仅将阶段状态置为 pending，使其越过
        is_checkpoint_complete 的跳过判断，由流水线重新执行并重新落盘。
        """
        entry = self._find_stage(stage_id)
        if entry is None:
            entry = {"stage": stage_id, "agent": stage_id}
            self.state["stages"].append(entry)
        entry["status"] = "pending"
        entry.pop("finished_at", None)
        entry.pop("checkpoint", None)
        return self.save()

    def is_checkpoint_complete(self, stage_id):
        """True only when a stage can be safely reused during resume."""
        entry = self._find_stage(stage_id)
        if not entry:
            return False
        return entry.get("status") == STAGE_COMPLETED or (
            entry.get("status") == STAGE_SKIPPED
            and entry.get("skip_kind") == SKIP_NOT_APPLICABLE
        )

    # -----------------------------------------------------------
    # 三、产物、告警与统计
    # -----------------------------------------------------------
    def put_artifact(self, key, path, desc=None):
        """
        登记产物。文件必须真实存在，否则直接报错，
        堵死"未生成却声称已生成"。
        """
        description = core.describe_output_file(path, desc=desc)
        self.state["artifacts"][key] = description
        self.save()
        return description

    def get_artifact(self, key):
        entry = self.state["artifacts"].get(key)
        return entry.get("path") if entry else None

    def add_warning(self, stage, code, message, affected_count=None,
                    action_taken=None, requires_review=False):
        record = {
            "stage": stage,
            "code": code,
            "message": message,
            "affected_count": affected_count,
            "action_taken": action_taken,
            "requires_review": bool(requires_review),
        }
        self.state["warnings"].append(record)
        self.save()
        return record

    def add_error(self, stage, code, message, blocking=True):
        record = {"stage": stage, "code": code, "message": message,
                  "blocking": blocking}
        self.state["errors"].append(record)
        self.save()
        return record

    def record_statistics(self, stage, snapshot):
        """
        登记某阶段的处理前后统计。
        snapshot 可为 StatisticsSnapshot 或 {"before":..., "after":...} 字典。
        """
        if isinstance(snapshot, core.StatisticsSnapshot):
            payload = snapshot.to_dict()
        else:
            payload = dict(snapshot or {})
        with self._lock:
            self.state["statistics"][stage] = payload
            self.save()
        return payload

    def statistics_complete(self, stage):
        """G2-04 判定依据：该阶段 before/after 是否均非空。"""
        payload = self.state["statistics"].get(stage) or {}
        return bool(payload.get("before")) and bool(payload.get("after"))

    # -----------------------------------------------------------
    # 四、黑板（内存态，跨阶段传递 DataFrame 等对象）
    # -----------------------------------------------------------
    def put(self, key, value):
        self.blackboard[key] = value
        return value

    def get(self, key, default=None):
        return self.blackboard.get(key, default)

    def has(self, key):
        return key in self.blackboard

    # -----------------------------------------------------------
    # 五、落盘与查询
    # -----------------------------------------------------------
    def save(self):
        with self._lock:
            self.state["updated_at"] = datetime.datetime.now().isoformat(timespec="seconds")
            return core.write_json(self.state_path, self.state)

    def snapshot(self):
        return dict(self.state)

    @staticmethod
    def load_state(run_id, run_root=None):
        """按 run_id 读取既有状态，供 status 命令查询。"""
        import json
        root = run_root or core.resolve_project_path("workspace")
        path = os.path.join(root, run_id, "state.json")
        core.require_readable_file(path, "E017")
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)

    @staticmethod
    def list_runs(run_root=None):
        """列出全部历史运行，按时间倒序。"""
        root = run_root or core.resolve_project_path("workspace")
        if not os.path.isdir(root):
            return []
        entries = [name for name in os.listdir(root)
                   if os.path.isdir(os.path.join(root, name))]
        return sorted(entries, reverse=True)
