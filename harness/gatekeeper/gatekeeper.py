# -*- coding: utf-8 -*-
"""
Gatekeeper
==========
阶段准入判定的唯一执行点。规则全部来自 configs/gates/gate-rules.yaml，
Agent 无权跳过、降级或自定义闸门。

统一门控状态（设计方案 5.2）：
    PASS
    WARNING                        -> 允许继续，但告警必须进最终交付
    HUMAN_CONFIRMATION_REQUIRED    -> 暂停等待人工确认
    BLOCK / FAIL                   -> 禁止继续

判定依据以"事实字典"形式传入，格式：
    {"G2-02": {"passed": True, "actual": 48, "threshold": 100},
     "G2-06": {"applicable": False, "reason": "本次未提供基因型"}}

缺少某项适用检查的事实，一律判 BLOCK。这是刻意的：
拿不出判定依据就不允许放行，比默认放过更安全。
"""

from _bootstrap import bootstrap

bootstrap()

import abges_core as core

VERDICT_PASS = "pass"
VERDICT_WARNING = "pass_with_warning"
VERDICT_HUMAN = "human_confirmation_required"
VERDICT_BLOCK = "block"
VERDICT_SKIP = "skip"


class GateBlocked(core.AbgesError):
    """闸门阻断。上层据此终止流程并如实报告原因。"""

    def __init__(self, gate_id, blocked_checks):
        message = "闸门 %s 阻断：%s" % (
            gate_id,
            "；".join("%s %s" % (item["check_id"], item.get("message") or item["desc"])
                     for item in blocked_checks))
        super(GateBlocked, self).__init__("GATE_BLOCK", message,
                                          {"gate_id": gate_id,
                                           "blocked": blocked_checks})
        self.gate_id = gate_id
        self.blocked_checks = blocked_checks


class GateReport(object):
    """单个闸门的完整判定结果。"""

    def __init__(self, gate_id, gate_name):
        self.gate_id = gate_id
        self.gate_name = gate_name
        self.decisions = []

    def add(self, decision):
        self.decisions.append(decision)
        return decision

    def _by_verdict(self, verdict):
        return [item for item in self.decisions if item["verdict"] == verdict]

    @property
    def blocked(self):
        return self._by_verdict(VERDICT_BLOCK)

    @property
    def warnings(self):
        return self._by_verdict(VERDICT_WARNING)

    @property
    def human_required(self):
        return self._by_verdict(VERDICT_HUMAN)

    @property
    def passed(self):
        return self._by_verdict(VERDICT_PASS)

    @property
    def skipped(self):
        return self._by_verdict(VERDICT_SKIP)

    @property
    def verdict(self):
        if self.blocked:
            return VERDICT_BLOCK
        if self.human_required:
            return VERDICT_HUMAN
        if self.warnings:
            return VERDICT_WARNING
        return VERDICT_PASS

    @property
    def allowed_to_proceed(self):
        return not self.blocked and not self.human_required

    def to_dict(self):
        return {
            "gate_id": self.gate_id,
            "gate_name": self.gate_name,
            "verdict": self.verdict,
            "total_checks": len(self.decisions),
            "passed": len(self.passed),
            "passed_with_warning": len(self.warnings),
            "blocked": len(self.blocked),
            "skipped": len(self.skipped),
            "decisions": self.decisions,
        }


class Gatekeeper(object):
    """闸门执行器。"""

    CONFIG_PATH = "configs/gates/gate-rules.yaml"

    def __init__(self, audit=None, config=None):
        self.config = config or core.load_yaml_config(self.CONFIG_PATH)
        self.audit = audit
        self.gates = {entry["id"]: entry
                      for entry in self.config.get("gates", []) or []}
        override_policy = self.config.get("override_policy", {}) or {}
        self.never_overridable = set(override_policy.get("never_overridable", []) or [])
        self.override_requires_reason = bool(
            override_policy.get("requires_reason", True))
        self.reports = {}

    # -----------------------------------------------------------
    def gate_checks(self, gate_id):
        gate = self.gates.get(gate_id)
        if gate is None:
            raise core.AbgesError("E016", "未定义的闸门：%s" % gate_id,
                                  {"known": sorted(self.gates.keys())})
        return gate.get("checks", []) or []

    def evaluate(self, gate_id, facts, overrides=None, stage=None):
        """
        评估一个闸门。

        参数
        ----
        facts     : dict  {check_id: {passed/applicable/actual/threshold/message}}
        overrides : dict  {check_id: {"reason": str, "approved_by": str}}
                          仅人工可授权，且 never_overridable 永不接受
        stage     : str   发起本次判定的阶段编号，仅用于审计归属

        返回 GateReport；不抛异常，是否终止由调用方依 allowed_to_proceed 决定。
        """
        gate = self.gates.get(gate_id)
        if gate is None:
            raise core.AbgesError("E016", "未定义的闸门：%s" % gate_id,
                                  {"known": sorted(self.gates.keys())})
        overrides = overrides or {}
        report = GateReport(gate_id, gate.get("name", gate_id))

        for check in gate.get("checks", []) or []:
            check_id = check.get("id")
            fact = (facts or {}).get(check_id)
            decision = self._decide(gate_id, check, fact)

            if decision["verdict"] == VERDICT_BLOCK and check_id in overrides:
                decision = self._apply_override(decision, overrides[check_id])

            report.add(decision)
            if self.audit is not None:
                self.audit.gate(gate_id, check_id, decision["verdict"],
                                actual=decision.get("actual"),
                                threshold=decision.get("threshold"),
                                message=decision.get("message"),
                                stage=stage)

        self.reports[gate_id] = report
        return report

    # -----------------------------------------------------------
    def _decide(self, gate_id, check, fact):
        """对单条检查作判定。"""
        check_id = check.get("id")
        description = check.get("desc", "")
        on_fail = check.get("on_fail", "block")
        base = {
            "gate_id": gate_id,
            "check_id": check_id,
            "desc": description,
            "actual": None,
            "threshold": None,
            "message": None,
            "overridden": False,
        }

        if fact is None:
            base.update({
                "verdict": VERDICT_BLOCK,
                "message": "未提供判定依据，按不可放行处理",
            })
            return base

        base["actual"] = fact.get("actual")
        base["threshold"] = fact.get("threshold")

        if fact.get("applicable") is False:
            base.update({
                "verdict": VERDICT_SKIP,
                "message": fact.get("reason") or "本次流程不适用",
            })
            return base

        if fact.get("requires_human"):
            base.update({
                "verdict": VERDICT_HUMAN,
                "message": fact.get("message") or check.get("message")
                           or "需人工确认后方可继续",
            })
            return base

        passed = fact.get("passed")
        if passed is True:
            base.update({"verdict": VERDICT_PASS,
                         "message": fact.get("message")})
            return base
        if passed is False:
            verdict = VERDICT_WARNING if on_fail == "pass_with_warning" else VERDICT_BLOCK
            base.update({
                "verdict": verdict,
                "message": fact.get("message") or check.get("message") or description,
            })
            if check.get("action"):
                base["action"] = check["action"]
            return base

        base.update({
            "verdict": VERDICT_BLOCK,
            "message": "判定结果不确定（passed=%r），按不可放行处理" % passed,
        })
        return base

    def _apply_override(self, decision, override):
        """
        应用人工授权的阈值放宽。
        never_overridable 项一律拒绝，理由缺失一律拒绝。
        """
        check_id = decision["check_id"]
        if check_id in self.never_overridable:
            decision["message"] = "%s（该项属诚实交付/只读铁律，永不可放宽）" % (
                decision.get("message") or "")
            return decision
        reason = (override or {}).get("reason")
        if self.override_requires_reason and not reason:
            decision["message"] = "%s（放宽请求缺少理由，未予采纳）" % (
                decision.get("message") or "")
            return decision

        original_verdict = decision["verdict"]
        decision["verdict"] = VERDICT_WARNING
        decision["overridden"] = True
        decision["override_reason"] = reason
        decision["override_approved_by"] = (override or {}).get("approved_by", "human")
        decision["message"] = "%s（已由人工放宽：%s）" % (
            decision.get("message") or "", reason)
        if self.audit is not None:
            self.audit.override("gate:%s" % check_id, original_verdict,
                                VERDICT_WARNING, reason)
        return decision

    # -----------------------------------------------------------
    def require_pass(self, report):
        """闸门不通过时抛 GateBlocked，供 orchestrator 直接终止流程。"""
        if report.blocked:
            raise GateBlocked(report.gate_id, report.blocked)
        return report

    def overall_summary(self):
        """汇总全部已评估闸门，对应 result-schema 的 gate_summary。"""
        total = passed = warning = blocked = skipped = 0
        blocked_details = []
        for report in self.reports.values():
            total += len(report.decisions)
            passed += len(report.passed)
            warning += len(report.warnings)
            blocked += len(report.blocked)
            skipped += len(report.skipped)
            blocked_details.extend(report.blocked)
        return {
            "total_checks": total,
            "passed": passed,
            "passed_with_warning": warning,
            "blocked": blocked,
            "skipped": skipped,
            "blocked_details": blocked_details,
        }

    def all_warnings(self):
        """收集全部 pass_with_warning 项，G6-03 要求它们必须进报告。"""
        collected = []
        for report in self.reports.values():
            collected.extend(report.warnings)
        return collected
