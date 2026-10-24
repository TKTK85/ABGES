# -*- coding: utf-8 -*-
"""
Audit Hub
=========
调用链、参数、闸门判定、数据增删改、覆盖与重试的统一落盘点。

底座复用 tools/Python/abges_core.py 的 AuditTrail（JSONL 追加写），
在其之上补两件事：
  1. 闸门判定同时写入独立的 gate_audit_{run_id}.jsonl，便于单独审阅；
  2. 提供 summary()，把审计流水折叠成交付报告可直接引用的统计。
"""

import io
import os
import json
import datetime

from _bootstrap import bootstrap

bootstrap()

import abges_core as core


class AuditHub(object):
    """审计中枢。所有写入都是追加，绝不改写既有记录。"""

    def __init__(self, log_dir, run_id):
        self.run_id = run_id
        self.log_dir = core.ensure_dir(log_dir)
        self.trail = core.AuditTrail(log_dir, run_id)
        self.gate_path = os.path.join(log_dir, "gate_audit_%s.jsonl" % run_id)
        self.gate_records = []

    # -----------------------------------------------------------
    # 一、通用事件
    # -----------------------------------------------------------
    def event(self, event_type, **payload):
        """记录一条自定义事件（阶段开始/结束、模型选定等）。"""
        record = {"event": event_type}
        record.update(payload)
        return self.trail._append(record)

    def stage_start(self, stage_id, agent_id=None, note=None):
        return self.event("stage_start", stage=stage_id, agent=agent_id, note=note)

    def stage_end(self, stage_id, status, duration_seconds=None, note=None):
        return self.event("stage_end", stage=stage_id, status=status,
                          duration_seconds=duration_seconds, note=note)

    # -----------------------------------------------------------
    # 二、工具调用
    # -----------------------------------------------------------
    def tool_call(self, tool, command, exit_code, duration_seconds,
                  workdir=None, note=None):
        return self.trail.record_tool_call(tool, command, exit_code,
                                           duration_seconds, workdir, note)

    def python_call(self, function_name, duration_seconds, status,
                    detail=None):
        """记录一次内部 Python 工具调用（不起子进程，但同样进审计）。"""
        return self.event("python_call", function=function_name,
                          duration_seconds=round(duration_seconds, 3),
                          status=status, detail=detail)

    # -----------------------------------------------------------
    # 三、闸门
    # -----------------------------------------------------------
    def gate(self, gate_id, check_id, verdict, actual=None, threshold=None,
             message=None, stage=None):
        """
        记录一次闸门判定。无论 pass 还是 block 都记录，
        同时写主审计流水与独立闸门审计文件。

        stage 为发起判定的阶段编号，用于区分同一 check_id 被多个阶段
        重复评估时的归属（G1/G2 → 01、02；G4 → 06、07；G5 → 07、08）。
        """
        record = self.trail.record_gate(gate_id, check_id, verdict,
                                        actual, threshold, message,
                                        stage=stage)
        gate_record = {
            "run_id": self.run_id,
            "gate_id": gate_id,
            "check_id": check_id,
            "stage": stage,
            "verdict": verdict,
            "actual_value": actual,
            "threshold": threshold,
            "message": message,
            "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
        }
        self.gate_records.append(gate_record)
        with io.open(self.gate_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(gate_record, ensure_ascii=False) + "\n")
        return record

    # -----------------------------------------------------------
    # 四、数据变更、覆盖与重试
    # -----------------------------------------------------------
    def data_change(self, stage, action, affected_count, reason, detail=None):
        return self.trail.record_data_change(stage, action, affected_count,
                                             reason, detail)

    def data_changes_batch(self, stage, change_log):
        """把 QC 结果里的 change_log 批量落审计。"""
        for entry in change_log or []:
            self.data_change(stage, entry.get("action", "CHANGE"),
                             entry.get("affected", 0),
                             "%s %s" % (entry.get("rule_id", ""),
                                        entry.get("reason", "")).strip(),
                             entry.get("detail"))
        return len(change_log or [])

    def override(self, item, original, new_value, reason):
        return self.trail.record_override(item, original, new_value, reason)

    def retry(self, stage, attempt, trigger, action):
        return self.trail.record_retry(stage, attempt, trigger, action)

    def human_decision(self, action, approved, reason=None, detail=None):
        return self.event("human_decision", action=action, approved=approved,
                          reason=reason, detail=detail)

    # -----------------------------------------------------------
    # 五、汇总
    # -----------------------------------------------------------
    def summary(self):
        """折叠审计流水，供最终报告与 result JSON 引用。"""
        counts = {}
        for entry in self.trail.entries:
            counts[entry["event"]] = counts.get(entry["event"], 0) + 1
        gate_counts = {}
        for record in self.gate_records:
            gate_counts[record["verdict"]] = gate_counts.get(record["verdict"], 0) + 1
        return {
            "audit_file": self.trail.path,
            "gate_audit_file": self.gate_path,
            "total_entries": len(self.trail.entries),
            "event_counts": counts,
            "gate_verdict_counts": gate_counts,
        }

    @property
    def path(self):
        return self.trail.path
