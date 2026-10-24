# -*- coding: utf-8 -*-
"""
Human In The Loop
=================
关键节点的人工确认通道。

分工来自 system-policies#human_in_the_loop：
  auto_proceed          —— 读取、分析、生成新文件、自动剔除单水平固定效应、
                            非 ASCII 自动转码等，直接放行但写审计；
  require_confirmation  —— 覆盖既有正式结果、放宽闸门阈值、删除正式产出、
                            修改原始输入、对外提交，必须人工确认。

运行模式：
  auto        非交互（默认）。需确认的动作一律挂起，不擅自代先生决定。
  interactive 命令行询问，用于人工在场时的一次性放行。
  deny        一律拒绝，用于自动化回归测试。
"""

import sys

from _bootstrap import bootstrap

bootstrap()

import abges_core as core

MODE_AUTO = "auto"
MODE_INTERACTIVE = "interactive"
MODE_DENY = "deny"


class HumanConfirmationRequired(core.AbgesError):
    """需要人工确认而当前无法取得确认，流程在此暂停。"""

    def __init__(self, action, detail=None):
        super(HumanConfirmationRequired, self).__init__(
            "HITL_PENDING", "该动作需人工确认后方可继续：%s" % action, detail)
        self.action = action


class Decision(object):
    """一次人工确认的结论。"""

    def __init__(self, action, approved, reason=None, source="auto"):
        self.action = action
        self.approved = approved
        self.reason = reason
        self.source = source

    def to_dict(self):
        return {"action": self.action, "approved": self.approved,
                "reason": self.reason, "source": self.source}


class RecommendationDecision(object):
    """效应筛选 / 共线性等分析后，一次需要先生拍板的方案决策。"""

    def __init__(self, decision_id, options, context=None):
        self.decision_id = decision_id
        self.options = options          # list of {label, value, description}
        self.context = context or {}    # 原始分析信息（相关系数、效应值等）
        self.chosen = None             # 用户选择的 option value

    def approve(self, value):
        """先生选择一个选项，标记为已批准。"""
        self.chosen = value
        return self

    def to_dict(self):
        return {
            "decision_id": self.decision_id,
            "options": self.options,
            "context": self.context,
            "chosen": self.chosen,
            "resolved": self.chosen is not None,
        }


class HumanInTheLoop(object):
    """人工确认通道。"""

    def __init__(self, policy, audit, mode=MODE_AUTO):
        self.policy = policy
        self.audit = audit
        self.mode = mode
        self.pending = []
        self.decisions = []
        self.recommendations = []   # 方案决策列表（RecommendationDecision）

    # -----------------------------------------------------------
    def request(self, action, detail=None, default_reason=None):
        """
        请求执行某动作。

        返回 Decision。需人工确认但当前取不到确认时，
        动作进入 pending 并返回未批准，由调用方决定是挂起还是走无害分支。
        """
        detail = detail or {}

        # 明确列入自动放行的动作：直接通过，但留痕
        if self.policy.auto_proceed_allowed(action) and \
                not self.policy.requires_confirmation(action):
            decision = Decision(action, True,
                                default_reason or "策略允许自动执行", "policy")
            return self._record(decision, detail)

        if not self.policy.requires_confirmation(action):
            # 既不在必须确认清单，也不在自动清单：按最小风险原则自动放行并留痕
            decision = Decision(action, True,
                                "未列入必须确认清单，按只读/新建类动作放行",
                                "policy_default")
            return self._record(decision, detail)

        # 需要人工确认
        if self.mode == MODE_DENY:
            decision = Decision(action, False, "当前为 deny 模式，一律不批准",
                                "deny")
            self.pending.append({"action": action, "detail": detail})
            return self._record(decision, detail)

        if self.mode == MODE_INTERACTIVE:
            approved, reason = self._ask(action, detail)
            decision = Decision(action, approved, reason, "interactive")
            if not approved:
                self.pending.append({"action": action, "detail": detail})
            return self._record(decision, detail)

        # auto 模式：不代先生决定，挂起
        self.pending.append({"action": action, "detail": detail})
        decision = Decision(action, False,
                            "非交互模式下不代为决定，已挂起等待人工确认", "auto")
        return self._record(decision, detail)

    def require(self, action, detail=None):
        """
        与 request 相同，但未获批准时直接抛 HumanConfirmationRequired，
        用于"没有确认就绝不能继续"的场景。
        """
        decision = self.request(action, detail)
        if not decision.approved:
            raise HumanConfirmationRequired(action, detail)
        return decision

    # -----------------------------------------------------------
    # 三、方案推荐（先生拍板）
    # -----------------------------------------------------------

    def request_recommendation(self, recommendations):
        """
        提交一个或多个方案决策请求。

        触发时机：Stage 06 完成效应筛选后，若检出 choose_one / 共线性等
        需要先生拍板的情形，由调用方传入 RecommendationDecision 列表。

        行为：
          auto      —— 记录决策请求于 self.recommendations，返回 unresolved=True；
                      结果 JSON 的 pending_decisions 字段携带完整的选项信息；
                      下次运行时由外部（WorkBuddy UI / 续跑脚本）注入先生的选择。
          interactive —— 在终端打印选项供先生选择（暂不支持，跳转为 auto）。
          deny      —— 标记全部为 denied，返回 unresolved=True。

        参数：
          recommendations: list[RecommendationDecision]

        返回：
          unresolved: bool  —— True 表示需等待先生选择，Stage 07 应暂停；
                              False 表示所有决策均已解决（interactive 直接完成）。
          decisions: list[dict]  —— 每个决策的 to_dict() 快照。
        """
        if not recommendations:
            return False, []

        for rec in recommendations:
            if self.mode == MODE_DENY:
                rec.chosen = None  # deny 模式下不做任何选择
                self.decisions.append(rec.to_dict())
                self.audit.human_decision(
                    "recommendation:%s" % rec.decision_id,
                    False, "deny 模式拒绝方案决策", rec.to_dict())
            else:
                # auto / interactive：记录请求，不代先生选
                self.recommendations.append(rec)
                self.audit.human_decision(
                    "recommendation:%s" % rec.decision_id,
                    None, "方案决策已登记，等待先生选择", rec.to_dict())

        return True, [rec.to_dict() for rec in recommendations]

    def resolve_recommendation(self, decision_id, chosen_value):
        """
        由外部（WorkBuddy UI / resume 流程）注入先生的选择。

        参数：
          decision_id: str   —— 与 request_recommendation 传入的 decision_id 对应
          chosen_value: str  —— 先生选择的 option value

        返回：
          bool —— 是否成功找到并解决了该决策
        """
        for rec in self.recommendations:
            if rec.decision_id == decision_id:
                rec.approve(chosen_value)
                self.audit.human_decision(
                    "recommendation:%s" % decision_id,
                    True, "先生选择: %s" % chosen_value, rec.to_dict())
                return True
        return False

    def pending_recommendations(self):
        """返回所有尚未解决的方案决策。"""
        return [rec.to_dict() for rec in self.recommendations if not rec.resolved]

    def has_unresolved_recommendations(self):
        return any(not rec.resolved for rec in self.recommendations)

    # -----------------------------------------------------------
    def _ask(self, action, detail):
        prompt = "\n[需要确认] %s\n细节：%s\n批准执行？(y/N) " % (action, detail)
        try:
            answer = input(prompt)
        except (EOFError, KeyboardInterrupt):
            return False, "无法取得交互输入"
        approved = str(answer).strip().lower() in ("y", "yes", "是")
        return approved, "人工%s" % ("批准" if approved else "拒绝")

    def _record(self, decision, detail):
        self.decisions.append(decision)
        self.audit.human_decision(decision.action, decision.approved,
                                  decision.reason, detail)
        return decision

    # -----------------------------------------------------------
    def has_pending(self):
        return bool(self.pending)

    def pending_summary(self):
        return list(self.pending)

    def to_list(self):
        return [item.to_dict() for item in self.decisions]
