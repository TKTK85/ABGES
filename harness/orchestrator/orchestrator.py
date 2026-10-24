# -*- coding: utf-8 -*-
"""
Orchestrator
============
阶段编排引擎。按 agent-registry#default_pipeline 顺序驱动各阶段，处理：

  - 并行组（01/02/03 在工作线程中并发执行，Harness 在汇合点单线程提交门禁/状态/审计）；
  - 跳过条件（skip_conditions / router.should_skip）；
  - 阶段闸门（调用 gatekeeper 对 agent.gates 逐条评估，缺依据一律 BLOCK）；
  - 错误处理（error_handler.classify_exception）与人工介入（hitl）；
  - 结果汇总成 result-schema 结构（status / gate_summary / warnings / errors / artifacts）。

本组件只做编排，不绑定具体工具逻辑；真实阶段执行由注入的 stage_runner 完成。
stage_runner 契约::

    stage_runner(agent_id, context, gatekeeper) -> dict
      dict 字段：
        placeholder       bool   若为 True，缺事实的闸门按 applicable=False(SKIP) 处理
        gate_facts        dict   {gate_id: {check_id: fact_dict}}
        artifacts         list   [(name, path), ...]  文件必须真实存在
        warnings          list   [str | {code,message,affected_count,action_taken,requires_review}]
        errors            list   [str | {code,message,blocking}]
        blackboard        dict   写回跨阶段共享内存（DataFrame 等不可序列化对象）
        availability_update dict 可选，合并进可用性探测结果

context 中的 harness 保留键（双下划线包裹，runner 只读取不覆写）::

    __task__            任务定义（task-schema）
    __availability__    数据可用性探测结果
    __model__           已选定模型 id
    __model_decision__  选型决策对象（含规则号与理由）
    __state__           StateManager 实例（run_dir / 统计 / 产物登记）
    __policy__          PolicyEngine（输出目录、线程、状态词校验）
    __audit__           AuditHub（工具调用与事件留痕）
    __tools__           ToolExecutor（外部引擎唯一调用入口）
"""

from _bootstrap import bootstrap, load_component

bootstrap()

import sys
import abges_core as core
from concurrent.futures import ThreadPoolExecutor, as_completed

_hitl_mod = load_component("human-in-the-loop", "hitl")
RecommendationDecision = _hitl_mod.RecommendationDecision

_gatekeeper_mod = load_component("gatekeeper", "gatekeeper")
GateBlocked = _gatekeeper_mod.GateBlocked
VERDICT_BLOCK = _gatekeeper_mod.VERDICT_BLOCK
VERDICT_HUMAN = _gatekeeper_mod.VERDICT_HUMAN
VERDICT_WARNING = _gatekeeper_mod.VERDICT_WARNING

_flow_mod = load_component("flow-enforcer", "flow_enforcer")
FlowIntegrityEnforcer = _flow_mod.FlowIntegrityEnforcer
_errors_mod = load_component("flow-enforcer", "errors")
FlowViolationError = _errors_mod.FlowViolationError

_state_mod = load_component("state-manager", "state_manager")
STAGE_SKIPPED = _state_mod.STAGE_SKIPPED
SKIP_NOT_APPLICABLE = _state_mod.SKIP_NOT_APPLICABLE
SKIP_UPSTREAM_FAILED = _state_mod.SKIP_UPSTREAM_FAILED

STATUS_COMPLETED = "completed"
STATUS_PARTIAL = "partial"
STATUS_FAILED = "failed"


class Orchestrator(object):
    """编排 default_pipeline 的全部阶段。"""

    def __init__(self, state, gatekeeper, router, policy, audit, error_handler,
                 hitl, tool_executor=None, context_manager=None):
        self.state = state
        self.gatekeeper = gatekeeper
        self.router = router
        self.policy = policy
        self.audit = audit
        self.error_handler = error_handler
        self.hitl = hitl
        self.tool_executor = tool_executor
        self.context_manager = context_manager

    # -----------------------------------------------------------
    def run(self, task, availability, model_decision, stage_runner=None,
            command="run", resume=False):
        try:
            return self._run_inner(task, availability, model_decision,
                                   stage_runner, command, resume)
        except FlowViolationError as fve:
            # 强制流程约束中断：任何步骤被跳过/失败/产物缺失即硬中断。
            self._emit_flow_banner(fve)
            self.state.set_status(STATUS_FAILED)
            self.state.add_error("orchestrator", fve.code, fve.message,
                                 blocking=True)
            return {
                "status": STATUS_FAILED,
                "status_basis": {"rule": "flow_integrity_halt",
                                 "diagnostic": fve.diagnostic},
                "run_id": getattr(self.state, "run_id", None),
                "model_id": model_decision.model_id,
                "errors": self.state.state.get("errors", []),
                "stages": self.state.state.get("stages", []),
                "blocked_by": "flow_enforcer",
                "flow_diagnostic": fve.diagnostic,
            }

    def _run_inner(self, task, availability, model_decision, stage_runner=None,
                   command="run", resume=False):
        if stage_runner is None:
            stage_runner = _placeholder_stage_runner

        pipeline = self.router.pipeline()
        parallel_groups = [set(group) for group in self.router.parallel_groups(availability)]
        # 强制流程约束守门员：两道断言（前置完成性 / 产物完整性）由它统一执行
        self.flow_enforcer = FlowIntegrityEnforcer(
            self.state, self.router, pipeline, parallel_groups)
        self.state.set_model(model_decision.model_id)
        self.audit.stage_start(
            "orchestrator",
            "pipeline=%s model=%s" % (pipeline, model_decision.model_id))

        # stage_runner 通过 context（黑板快照）获取任务定义与运行环境。
        # 以双下划线前缀标记 harness 保留键，避免与阶段间业务数据混淆。
        # policy / audit / tools 一并注入：真实 runner 调外部引擎必须经
        # tool-executor（执行边界与审计在此统一落实），不得自起子进程。
        self.state.put("__task__", task)
        self.state.put("__availability__", dict(availability or {}))
        self.state.put("__model__", model_decision.model_id)
        self.state.put("__model_decision__", model_decision)
        self.state.put("__state__", self.state)
        self.state.put("__policy__", self.policy)
        self.state.put("__audit__", self.audit)
        self.state.put("__tools__", self.tool_executor)
        self.state.put("__context_manager__", self.context_manager)

        working_availability = dict(availability or {})
        blocked = False

        # Branch work is concurrent; Harness is the sole state/gate writer.
        parallel_results = {}
        parallel_members = {}
        parallel_gate_facts = {}
        all_data_gate_facts = {}
        dispatched_parallel_groups = set()
        parallel_started = set()

        # 先生拍板 resume：若存在未决决策，模型定义相关阶段（00 构建 plan、
        # 01 表型 QC 产出 effective_covariates、06 组装 hiblup_command）必须重跑，
        # 才能把先生选择（剔除被拒效应）落实到最终模型。否则这些阶段在 pause 时
        # 已被判完成并跳过，会沿用含被拒效应的旧 plan / effective_covariates / hiblup_command。
        if resume and self.state.has_unresolved_recommendations():
            self._reset_model_defining_stages_for_resume()

        for agent_id in pipeline:
            if resume and self.state.is_checkpoint_complete(agent_id):
                self.audit.event("stage_resume_skip", agent=agent_id,
                                 reason="previous checkpoint completed or marked not applicable")
                continue
            # validate is a pure preflight command.  It may run only Python-side
            # task, phenotype and pedigree checks (00--02); it must never invoke
            # PLINK/HIBLUP or publish final result artifacts.
            if command == "validate" and str(agent_id) in {"03", "04", "05", "06", "07", "08", "09"}:
                reason = "command=validate: external-engine, integration, planning and delivery stages are not run"
                self.state.skip_stage(agent_id, reason, kind=SKIP_NOT_APPLICABLE)
                self.audit.event("stage_skip", agent=agent_id, reason=reason,
                                 skip_kind=SKIP_NOT_APPLICABLE)
                self._checkpoint_context(agent_id, {"artifacts": [], "warnings": [], "errors": []})
                continue

            if command == "plan" and str(agent_id) in {"07", "08", "09"}:
                reason = "command=plan: engine execution, result validation and delivery are not run"
                self.state.skip_stage(agent_id, reason, kind=SKIP_NOT_APPLICABLE)
                self.audit.event("stage_skip", agent=agent_id, reason=reason,
                                 skip_kind=SKIP_NOT_APPLICABLE)
                self._checkpoint_context(agent_id, {"artifacts": [], "warnings": [], "errors": []})
                continue

            if blocked:
                self.state.skip_stage(agent_id, "前置阶段未通过，流程终止",
                                      kind=SKIP_UPSTREAM_FAILED)
                self._checkpoint_context(agent_id, {"artifacts": [], "warnings": [], "errors": []})
                continue

            # ---- 方案决策前置拦截：resume 时若先生已注入选择，在此注入 hitl ----
            # （先生通过 --resume-run <run_id> 并在 task yaml 中传入 recommend_decisions 参数注入选择）
            if resume and self.state.has_unresolved_recommendations():
                self._inject_resolved_recommendations()

            should_skip, reason = self.router.should_skip(agent_id, working_availability)
            if should_skip:
                self.state.skip_stage(agent_id, reason,
                                      kind=SKIP_NOT_APPLICABLE)
                self.audit.event("stage_skip", agent=agent_id, reason=reason,
                                 skip_kind=SKIP_NOT_APPLICABLE)
                self._checkpoint_context(agent_id, {"artifacts": [], "warnings": [], "errors": []})
                continue

            group = self._parallel_group_of(agent_id, parallel_groups)
            group_key = tuple(sorted(group)) if group else None
            if group and group_key not in dispatched_parallel_groups:
                candidates = []
                for candidate in pipeline:
                    if candidate not in group:
                        continue
                    if resume and self.state.is_checkpoint_complete(candidate):
                        continue
                    if command == "validate" and str(candidate) in {"03", "04", "05", "06", "07", "08", "09"}:
                        continue
                    if command == "plan" and str(candidate) in {"07", "08", "09"}:
                        continue
                    skipped, _ = self.router.should_skip(candidate, working_availability)
                    if not skipped:
                        candidates.append(candidate)
                parallel_members[group_key] = candidates
                dispatched_parallel_groups.add(group_key)
                for candidate in candidates:
                    if resume:
                        self.state.reset_stage_for_resume(candidate)
                    self.state.start_stage(candidate, candidate)
                    self.audit.event("stage_start", agent=candidate,
                                     parallel_group=sorted(group))
                    parallel_started.add(candidate)
                if len(candidates) > 1:
                    self.audit.event("parallel_group_dispatched", agents=candidates)
                    with ThreadPoolExecutor(max_workers=len(candidates), thread_name_prefix="abges-data") as pool:
                        futures = {pool.submit(stage_runner, candidate, self._context_packet(candidate), self.gatekeeper): candidate for candidate in candidates}
                        for future in as_completed(futures):
                            candidate = futures[future]
                            try:
                                parallel_results[candidate] = {"result": future.result()}
                            except Exception as exc:
                                parallel_results[candidate] = {"exception": exc}
                elif candidates:
                    parallel_results[candidates[0]] = {"result": stage_runner(candidates[0], self._context_packet(candidates[0]), self.gatekeeper)}
            if agent_id not in parallel_started:
                if resume:
                    self.state.reset_stage_for_resume(agent_id)
                self.state.start_stage(agent_id, agent_id)
                self.audit.event("stage_start", agent=agent_id,
                                 parallel_group=sorted(group) if group else None)

            # ---- 强制流程约束：启动前校验前置阶段完成性（并行组成员跳过） ----
            self.flow_enforcer.assert_upstream_satisfied(agent_id)

            # ---- 执行阶段（真实逻辑由注入的 stage_runner 提供） ----
            try:
                prepared = parallel_results.pop(agent_id, None)
                if prepared is not None:
                    if "exception" in prepared:
                        raise prepared["exception"]
                    result = prepared["result"]
                else:
                    result = stage_runner(agent_id, self._context_packet(agent_id), self.gatekeeper)
            except Exception as exc:  # noqa: BLE001 - 兜底分类，不静默吞掉
                failure = self.error_handler.classify_exception(agent_id, exc)
                self.state.add_error(agent_id, failure.trigger, failure.message,
                                     blocking=True)
                self.state.finish_stage(agent_id, "failed",
                                        {"trigger": failure.trigger})
                self.audit.event("stage_failed", agent=agent_id,
                                 trigger=failure.trigger, message=failure.message)
                self._checkpoint_context(agent_id, {"artifacts": [], "warnings": [],
                                                    "errors": [{"code": failure.trigger}]})
                blocked = True
                continue

            # ---- 合并黑板与可用性更新 ----
            for key, value in (result.get("blackboard") or {}).items():
                self.state.put(key, value)
            if result.get("availability_update"):
                working_availability.update(result["availability_update"])

            # ---- 登记产物（文件必须真实存在，否则 put_artifact 报错） ----
            for item in (result.get("artifacts") or []):
                if isinstance(item, (list, tuple)) and len(item) == 2:
                    name, path = item
                    try:
                        self.state.put_artifact(name, path)
                    except core.AbgesError as ae:
                        self.state.add_warning(
                            agent_id, "ARTIFACT",
                            "产物未登记：%s (%s)" % (name, ae.message))
                else:
                    self.state.add_warning(agent_id, "ARTIFACT",
                                           "产物格式异常：%r" % (item,))

            # ---- 阶段级 warning / error ----
            for w in (result.get("warnings") or []):
                if isinstance(w, dict):
                    self.state.add_warning(agent_id, w.get("code", "W"),
                                           w.get("message", ""),
                                           w.get("affected_count"),
                                           w.get("action_taken"),
                                           w.get("requires_review", False))
                else:
                    self.state.add_warning(agent_id, "W", str(w))
            for e in (result.get("errors") or []):
                if isinstance(e, dict):
                    self.state.add_error(agent_id, e.get("code", "E"),
                                         e.get("message", ""), e.get("blocking", True))
                else:
                    self.state.add_error(agent_id, "E", str(e), True)

            # ---- 方案决策检查点：效应筛选检出 choose_one 等需先生拍板的情形 ----
            # 仅在效应筛选产出了 choose_one 建议时才触发
            unresolved = self._pause_for_recommendations(agent_id, result)
            if unresolved:
                # 写入 checkpoint 后返回，Stage 07 暂停
                self.state.checkpoint_agent_result(agent_id, result)
                self.state.set_status("partial")
                summary = self._summarize(model_decision, blocked)
                summary["pending_decisions"] = self.state.get_pending_recommendations()
                summary["status"] = "partial"
                summary["status_basis"] = {
                    "rule": "recommendation_pending",
                    "message": "方案决策待先生拍板，Stage 07 暂停",
                }
                self.audit.stage_end("orchestrator", summary["status"])
                self._checkpoint_context("final", gate_summary=summary.get("gate_summary"))
                return summary

            # ---- 阶段闸门 ----
            stage_blocked = False
            all_data_gate_facts[agent_id] = result.get("gate_facts") or {}
            gate_ids = self.router.gates_of(agent_id)
            if group:
                parallel_gate_facts[agent_id] = result.get("gate_facts") or {}
                members = parallel_members.get(group_key, [])
                if agent_id != (members[-1] if members else None):
                    gate_ids = []
                else:
                    merged_facts = {}
                    # Include 01 when derived processing made it run after 04 but
                    # before the 02/03 branch join.  This preserves one complete
                    # G1/G2 decision rather than treating 01 as a sibling of 04.
                    fact_members = list(members)
                    if "01" not in fact_members and "01" in all_data_gate_facts:
                        fact_members.append("01")
                    for member in fact_members:
                        for gate_id, facts in (all_data_gate_facts.get(member) or {}).items():
                            merged_facts.setdefault(gate_id, {}).update(facts or {})
                    # Each QC runner records its own before/after snapshot. A
                    # legacy serial runner made the final branch also inspect
                    # earlier snapshots; after concurrent execution Harness
                    # derives G2-04 from all completed branch facts here.
                    stat_values = {}
                    statistic_key_by_agent = {
                        "01": ("phenotype_statistics_complete", "phenotype"),
                        "02": ("pedigree_statistics_complete", "pedigree"),
                        "03": ("genotype_statistics_complete", "genotype"),
                    }
                    for member in members:
                        fact = ((parallel_gate_facts.get(member) or {}).get("G2") or {}).get("G2-04") or {}
                        actual = fact.get("actual") or {}
                        for key in statistic_key_by_agent.get(str(member), ()):
                            if key in actual:
                                stat_values[str(member)] = bool(actual[key])
                                break
                    if stat_values:
                        merged_facts.setdefault("G2", {})["G2-04"] = {
                            "passed": all(stat_values.values()),
                            "actual": stat_values,
                            "threshold": "all applicable data-agent before/after statistics complete",
                        }
                    result = dict(result)
                    result["gate_facts"] = merged_facts
                    gate_ids = sorted({gate_id for member in members for gate_id in self.router.gates_of(member)})
                    self.audit.event("parallel_group_joined", agents=members, gates=gate_ids)
            for gate_id in gate_ids:
                raw_facts = (result.get("gate_facts") or {}).get(gate_id, {}) or {}
                if result.get("placeholder") and not raw_facts:
                    # 占位模式：该阶段未真实执行，检查不适用 -> SKIP（不谎称通过）
                    raw_facts = {
                        check["id"]: {"applicable": False,
                                      "reason": "占位模式未执行该阶段，检查不适用"}
                        for check in self.gatekeeper.gate_checks(gate_id)
                    }
                report = self.gatekeeper.evaluate(gate_id, raw_facts,
                                                  stage=agent_id)
                if report.blocked:
                    stage_blocked = True
                    self.state.add_error(
                        agent_id, "GATE",
                        "闸门 %s 阻断：%s"
                        % (gate_id, "; ".join(d.get("message", "") for d in report.blocked)),
                        blocking=True)
                elif report.verdict == VERDICT_HUMAN:
                    decision = self.hitl.request("gate:%s" % gate_id)
                    if not decision.approved:
                        stage_blocked = True
                        self.state.add_error(
                            agent_id, "GATE",
                            "闸门 %s 需人工确认但未获批准" % gate_id, blocking=True)
                elif report.verdict == VERDICT_WARNING:
                    for wdec in report.warnings:
                        self.state.add_warning(agent_id, "GATE",
                                               wdec.get("message", ""))

            if stage_blocked:
                self.state.finish_stage(agent_id, "failed", {"gate_blocked": True})
                self.audit.event("stage_failed", agent=agent_id, reason="gate_blocked")
                self._checkpoint_context(agent_id, result)
                blocked = True
                continue

            # ---- 强制流程约束：本阶段登记产物必须存在且非空，否则立即中断 ----
            try:
                self.flow_enforcer.assert_outputs_satisfied(
                    agent_id, result.get("artifacts") or [])
            except FlowViolationError as fve:
                self.state.add_error(agent_id, fve.code, fve.message, blocking=True)
                self.state.finish_stage(agent_id, "failed", {"flow_violation": True})
                self.audit.event("stage_failed", agent=agent_id,
                                 reason="outputs_incomplete")
                self._checkpoint_context(agent_id, result)
                blocked = True
                continue

            execution = {"logical_agent": str(agent_id), "executor": "harness_stage_runner",
                         "isolation": "stage_context_packet", "authority": "harness_orchestrator"}
            self.state.finish_stage(agent_id, "completed", {"agent_execution": execution})
            self.audit.event("agent_execution_evidence", **execution)
            self.audit.event("stage_done", agent=agent_id)
            self._checkpoint_context(agent_id, result)

        summary = self._summarize(model_decision, blocked)
        self.state.set_status(summary["status"])
        self.audit.stage_end("orchestrator", summary["status"])
        self._checkpoint_context("final", gate_summary=summary.get("gate_summary"))
        return summary

    def _checkpoint_context(self, stage, result=None, gate_summary=None):
        """Write a bounded, platform-neutral handoff checkpoint."""
        self.state.checkpoint_agent_result(stage, result)
        if self.context_manager is None:
            return
        try:
            self.context_manager.checkpoint(stage, self.state, result, gate_summary)
        except Exception as exc:  # Context metadata must not alter evaluation.
            self.audit.event("context_checkpoint_warning", stage=stage, message=str(exc))

    def _emit_flow_banner(self, fve):
        """打印结构化「流程约束中断」横幅到 stderr，并留痕审计。"""
        d = getattr(fve, "diagnostic", None) or {}
        lines = [
            "",
            "═" * 56,
            " ABGES 流程约束中断（FlowIntegrityEnforcer） ",
            "═" * 56,
            "错误码   : %s" % getattr(fve, "code", "E_FLOW"),
            "失败阶段 : %s" % d.get("failed_stage"),
            "违规类型 : %s" % d.get("violation"),
        ]
        detail = d.get("detail")
        if detail:
            lines.append("明细     :")
            for item in detail:
                lines.append("   - %s" % (item,))
        lines.append("处置     : 修复上述步骤后重新运行，切勿跳过该阶段。")
        lines.append("═" * 56)
        try:
            sys.stderr.write("\n".join(lines) + "\n")
        except Exception:
            pass
        self.audit.event("flow_integrity_halt", stage=d.get("failed_stage"),
                         reason=d.get("violation"), detail=str(detail))


    # -----------------------------------------------------------
    def _parallel_group_of(self, agent_id, parallel_groups):
        for group in parallel_groups:
            if agent_id in group:
                return group
        return None

    # Each logical Agent receives only the data it needs. This stage allowlist
    # is the actual context-isolation boundary; it is not a full blackboard copy.
    _CONTEXT_KEYS = {
        "00": {"__task__", "__model__", "__policy__", "__state__", "task_root"},
        "04": {"plan", "__state__"},
        "01": {"plan", "__state__", "derived_phenotype_frame", "derived_trait_path"},
        "02": {"plan", "__state__", "phenotype_gate_facts"},
        "03": {"plan", "__state__", "phenotype_gate_facts"},
        "05": {"__state__", "phenotype_qc", "pedigree_qc", "genotype_prefix"},
        "06": {"plan", "__state__", "__policy__", "__tools__", "engine_phenotype",
               "engine_pedigree", "genotype_prefix", "genotype_scale_after", "phenotype_qc",
               "phenotype_metrics", "integration_metrics"},
        "07": {"plan", "__state__", "__policy__", "__tools__", "hiblup_command",
               "output_prefix", "g4_facts", "pedigree_individuals", "pedigree_ids",
               "genotype_individuals"},
        # 修复 F-3：阶段08 结果校验（_stage_validator）读取 heritability_by_trait /
        # genetic_correlations，原白名单缺键 → 多性状 h² 与遗传相关被静默丢弃；
        # 阶段09 报告（_stage_reporter）亦读取 genetic_correlations。
        "08": {"plan", "__state__", "parsed_result", "validation", "analysis_ids",
               "heritability_by_trait", "genetic_correlations"},
        # 【P2-1 补齐接线】阶段09 交付报告需要用本机 Word 排版引擎回写目录(TOC)。
        # 按铁律一，任何外部程序都必须经 ToolExecutor（唯一执行边界）拉起，
        # 故本阶段必须拿到 __tools__；此前白名单缺此键，
        # 导致 docx_report._update_toc_via_word 里 context.get("__tools__") 恒为 None，
        # ToolExecutor 分支成为死代码、目录静默不更新（run_008 实测回归）。
        "09": {"plan", "__state__", "__policy__", "__tools__", "output_prefix", "hiblup_plan",
               "engine_exec", "validation", "phenotype_qc", "phenotype_statistics_snapshot",
               "pedigree_statistics_snapshot", "genotype_statistics_snapshot", "engine_info",
               "phenotype_metrics", "integration_metrics", "ebv_summary", "baseline_comparison",
               "result_evaluation", "reporting_population", "top_individuals",
               # expert Word report (stage09, read-only): parsed engine results,
               # engine phenotype/pedigree (Top family analysis reads the QC pedigree)
               "parsed_result", "engine_phenotype", "engine_pedigree",
               "genetic_correlations"},
    }

    # 先生拍板会改变模型定义；以下阶段在 pause 时已判完成并会在 resume 被跳过，
    # 必须重置以便从修正后的 __task__ 重新推导 plan / effective_covariates / hiblup_command。
    _MODEL_DEFINING_STAGES = ("00", "01", "06")

    def _context_packet(self, agent_id):
        """Build a least-privilege context packet for the named logical Agent."""
        allowed = self._CONTEXT_KEYS.get(str(agent_id), set())
        packet = {key: self.state.blackboard[key] for key in allowed if key in self.state.blackboard}
        packet["__agent_id__"] = str(agent_id)
        packet["__context_scope__"] = sorted(key for key in packet if not key.startswith("__"))
        return packet

    def _inject_resolved_recommendations(self):
        """
        Resume 时：先生通过 task yaml 的 recommend_decisions 参数注入选择，
        将已选择的决策注入 hitl 并更新 state。

        先生的选择需要同时写入两个地方：
          1. hitl.recommendations（先生拍板后，hitl 的 choose_one 过滤器据此放行）
          2. state.pending_recommendations（状态持久化，WorkBuddy UI 据此展示已选项）
        """
        task = self.state.get("__task__") or {}
        decisions = task.get("recommend_decisions") or []
        if not decisions:
            return  # 无注入，继续等待

        injected = 0
        for item in decisions:
            decision_id = item.get("decision_id")
            chosen = item.get("chosen")
            if not decision_id or not chosen:
                continue

            # 情形：先生已通过 YAML 注入了选择（chosen 非空）
            # 1) 写入 hitl.recommendations（先生拍板的核心记录）
            #    resolve_recommendation 要求 decision_id 已在列表中才生效；
            #    因此先用 chosen 值直接构造并注入 RecommendationDecision 对象。
            context = {"injected_from": "recommend_decisions", "decision_id": decision_id}
            # 从 state.pending_recommendations 读取原始选项信息
            pending = self.state.get_pending_recommendations()
            orig_item = next((p for p in pending if p.get("decision_id") == decision_id), None)
            if orig_item:
                options = orig_item.get("options", [])
                context = orig_item.get("context") or context
            else:
                options = [{"label": chosen, "value": chosen, "description": "先生拍板: %s" % chosen}]
            rec = RecommendationDecision(decision_id, options, context)
            rec.approve(chosen)
            # 避免重复追加：先生已 resolved 的记录不再追加
            existing = [r for r in self.hitl.recommendations if r.decision_id == decision_id]
            if not existing:
                self.hitl.recommendations.append(rec)

            # 2) 写入 state.pending_recommendations（状态持久化）
            self.state.resolve_pending_recommendation(decision_id, chosen)
            # 3) choose_one：把先生选择落实到模型声明（剔除被拒效应）。
            #    真正的产物（plan / effective_covariates / hiblup_command）由
            #    _reset_model_defining_stages_for_resume 触发的 00/01/06 重跑重新生成。
            ctx = context
            cols = (ctx or {}).get("columns") or []
            if cols and chosen:
                rejected = [c for c in cols if c != chosen]
                if rejected:
                    self._filter_rejected_effects_from_model(rejected)
                    self.audit.event("choose_one_effect_filtered",
                                     decision_id=decision_id,
                                     rejected=rejected, chosen=chosen)
            injected += 1

        if injected:
            self.audit.event("recommendation_injected", count=injected)

    def _filter_rejected_effects_from_model(self, rejected):
        """把先生否决的效应从模型声明中剔除。

        仅修正模型声明（__task__ 与持久化 task）；真正的下游产物
        （plan / effective_covariates / hiblup_command）由
        _reset_model_defining_stages_for_resume 触发的 00/01/06 重跑重新生成，
        二者配合保证先生选择端到端生效。
        """
        rejected_set = set(rejected)
        # 1) 黑板 __task__（Stage 00 读取它构建 plan）
        current_task = self.state.get("__task__") or {}
        new_cov = [c for c in (current_task.get("covariates") or [])
                   if c not in rejected_set]
        new_fix = [f for f in (current_task.get("fixed_effects") or [])
                   if f not in rejected_set]
        if new_cov != current_task.get("covariates") \
                or new_fix != current_task.get("fixed_effects"):
            filtered = dict(current_task)
            filtered["covariates"] = new_cov
            filtered["fixed_effects"] = new_fix
            self.state.put("__task__", filtered)
        # 2) 持久化 task（state.state["task"]，供 diff / 未来 resume 使用）
        persisted = self.state.state.get("task") or {}
        if persisted:
            p_cov = [c for c in (persisted.get("covariates") or [])
                     if c not in rejected_set]
            p_fix = [f for f in (persisted.get("fixed_effects") or [])
                     if f not in rejected_set]
            if p_cov != persisted.get("covariates") \
                    or p_fix != persisted.get("fixed_effects"):
                p2 = dict(persisted)
                p2["covariates"] = p_cov
                p2["fixed_effects"] = p_fix
                self.state.bind_task(p2)

    def _reset_model_defining_stages_for_resume(self):
        """resume 且存在未决先生拍板决策时，重置模型定义相关阶段。

        使 00（构建 plan）、01（表型 QC 产出 effective_covariates）、
        06（组装 hiblup_command）从修正后的 __task__ 重新推导，确保被拒效应
        不会残留在最终模型里。
        """
        for sid in self._MODEL_DEFINING_STAGES:
            self.state.reset_stage_checkpoint(sid)
            self.audit.event("stage_checkpoint_reset_for_resume", stage=sid,
                             reason="model re-derivation after 先生拍板 injection")

    def _pause_for_recommendations(self, agent_id, result):
        """
        检测效应筛选产出的 choose_one 警告，触发先生拍板流程。

        触发条件（满足其一即检查）：
          - agent_id == "06" 且 effect_screening 产出了 choose_one 项目（首次触发）
          - agent_id == "07" 且 state.pending_recommendations 有未解决项（resume 拦截）

        返回 True 表示存在未解决的方案决策，Stage 07 应暂停。

        工作机制（先生拍板双通道）：
          - auto 模式：记录未决决策于 state.pending_recommendations，
            结果 JSON 携带 pending_decisions 供 WorkBuddy UI 呈现；
            先生下次运行时以 --resume-run <run_id> 续跑，并在 task yaml 中
            通过 recommend_decisions 参数注入选择。
          - interactive 模式：暂打印选项供选择（TODO）。
        """
        # ---- 情形A：Stage 06 首次触发 —— 从 effect_screening 构建选项 ----
        if str(agent_id) == "06":
            effect_screening = self.state.get("effect_screening") or {}
            choose_one_items = (
                effect_screening.get("Recommendations", {})
                .get("choose_one", [])
            )
            if not choose_one_items:
                return False

            # choose_one_items: list[str]（abges_effects 输出为列名字符串列表）
            cols = [c for c in choose_one_items if isinstance(c, str)]
            if not cols:
                return False

            # 先生已通过 resume 注入了选择？已选则放行，同时执行效应过滤
            if not self.state.has_unresolved_recommendations():
                # 从 pending_recommendations 取出被拒绝的效应（choose_one 中未被选中者）
                pending = self.state.get_pending_recommendations()
                rejected_effects = set()
                for item in pending:
                    if item.get("resolved") and item.get("chosen"):
                        chosen = item["chosen"]
                        all_cols = item.get("context", {}).get("columns", [])
                        for col in all_cols:
                            if col != chosen:
                                rejected_effects.add(col)
                # 将被拒绝的效应从黑板 __task__ 的 covariates 中移除
                if rejected_effects:
                    current_task = self.state.get("__task__") or {}
                    orig_cov = list(current_task.get("covariates") or [])
                    orig_fix = list(current_task.get("fixed_effects") or [])
                    new_cov = [c for c in orig_cov if c not in rejected_effects]
                    new_fix = [f for f in orig_fix if f not in rejected_effects]
                    filtered_task = dict(current_task)
                    filtered_task["covariates"] = new_cov
                    filtered_task["fixed_effects"] = new_fix
                    self.state.put("__task__", filtered_task)
                    self.audit.event("choose_one_effect_filter_applied",
                                  rejected=list(rejected_effects),
                                  covariates_before=orig_cov, covariates_after=new_cov,
                                  fixed_effects_before=orig_fix, fixed_effects_after=new_fix)
                self.audit.event("recommendation_already_resolved", agent=agent_id)
                return False

            decision_id = "choose_one_gen_vs_hatchdays"
            context = {
                "type": "choose_one",
                "columns": cols,
                "detail": "Gen 与 HatchDays 相关系数 |r|=0.9996（近乎恒等共线），两者不可同时入模",
            }
            options = []
            for col in cols:
                desc = (
                    "保留 Gen（世代效应，生物学层级清晰，更符合遗传评估惯例）"
                    if col == "Gen"
                    else "保留 HatchDays（连续变量，精确捕获出雏时间信息）"
                )
                options.append({
                    "label": col,
                    "value": col,
                    "description": desc,
                })
            rec = RecommendationDecision(decision_id, options, context)
            recommendations = [rec]

            unresolved, rec_dicts = self.hitl.request_recommendation(recommendations)
            if unresolved:
                self.state.set_pending_recommendations(rec_dicts)
            # 先生选择 resolve 后，state.save() 必须立即调用以确保落盘；
            # 否则 resume 时 state.json 仍为 resolved=false，会再次触发未决警告。
            self.state.save()
            return unresolved

        # ---- 情形B：Stage 07 进入前拦截 —— resume 时检查先生是否已选择 ----
        # 先生已选（resolved=True）：注入 hitl，直接放行 Stage 07
        # 先生未选（resolved=False）：先生拍板未决，暂停
        if str(agent_id) == "07" and self.state.has_unresolved_recommendations():
            pending = self.state.get_pending_recommendations()
            # 先生已通过 resume 注入了选择？
            has_resolved = any(item.get("resolved") for item in pending)
            if has_resolved:
                # 先生已选：从 resolved 项注入 hitl.recommendations（先生拍板已解决）
                for item in pending:
                    if item.get("resolved"):
                        decision_id = item.get("decision_id")
                        chosen = item.get("chosen")
                        options = item.get("options", [])
                        context = item.get("context") or {}
                        rec = RecommendationDecision(decision_id, options, context)
                        rec.approve(chosen)
                        existing = [r for r in self.hitl.recommendations
                                    if r.decision_id == decision_id]
                        if not existing:
                            self.hitl.recommendations.append(rec)
                        self.audit.event("recommendation_resolved_in_resume",
                                        decision_id=decision_id, chosen=chosen)
                return False  # 先生已选，放行 Stage 07
            # 先生未选：先生拍板未决，触发暂停流程
            if not self.hitl.has_unresolved_recommendations():
                pending_items = [item for item in pending if not item.get("resolved")]
                rec_objs = []
                for item in pending_items:
                    rec = RecommendationDecision(
                        item["decision_id"], item["options"], item.get("context"))
                    rec_objs.append(rec)
                if rec_objs:
                    self.hitl.request_recommendation(rec_objs)
            return self.hitl.has_unresolved_recommendations()

        return False

    def _summarize(self, model_decision, blocked):
        state = self.state.state
        errors = state.get("errors", [])
        warnings = state.get("warnings", [])
        stages = state.get("stages", [])
        gate_summary = self.gatekeeper.overall_summary()

        status, status_basis = self._decide_status(
            blocked, errors, warnings, stages, gate_summary)
        # 诚实交付铁律：状态词必须合法（completed/partial/failed）
        self.policy.assert_status_allowed(status)
        self.audit.event("status_decided", status=status, basis=status_basis)
        return {
            "status": status,
            "status_basis": status_basis,
            "run_id": self.state.run_id,
            "model_id": model_decision.model_id,
            "model_rule": model_decision.rule_id,
            "model_reason": model_decision.reason,
            "gate_summary": gate_summary,
            # 依 result-schema：warnings/errors 均为对象列表，
            # 字段分别为 [stage, code, message, affected_count, action_taken]
            # 与 [stage, code, message, blocking]，不可退化为字符串。
            "warnings": warnings,
            "errors": errors,
            # 【2026-10-19】结果展示剔除 reports/input/output 下的 .json 内部文件
            # 【2026-10-20】改走中央判定：root=run_dir 使路径规约为 reports/x.json 形式，
            # 层级可判（.json 仅在受限目录被剔除），同时排除 _runs；与交付物固化同口径。
            "artifacts": core.exclude_json_artifacts(
                state.get("artifacts", {}), root=getattr(self.state, "run_dir", None)),
            "stages": stages,
            # 方案决策（先生拍板）状态：供 WorkBuddy UI 呈现选项
            "pending_decisions": state.get("pending_recommendations", []),
        }

    # -----------------------------------------------------------
    def _decide_status(self, blocked, errors, warnings, stages, gate_summary):
        """
        严格依 result-schema#status_rules 与 system-policies#honesty 判定总体状态。
        返回 (状态, 判定依据说明)，依据一并写入结果，便于复核为何是该状态。

        failed    闸门阻断 / 存在错误 / 因上游失败而中断的阶段
        partial   存在 pass_with_warning 闸门项、未处置告警或需人工复核项，
                  或 G5 未全部通过
        completed 以上皆无

        注意："设计内不适用"的阶段跳过不构成降级依据。status_rules 中并无
        此条；PBLUP 任务本就不需要基因型阶段，据此降级会把正常完成的评估
        误报为 partial，反而失真。仅"因上游失败而跳过"才计入 failed。
        """
        aborted = [s.get("stage") for s in stages
                   if s.get("status") == STAGE_SKIPPED
                   and s.get("skip_kind") != SKIP_NOT_APPLICABLE]
        if blocked or errors or gate_summary.get("blocked") or aborted:
            return STATUS_FAILED, {
                "rule": "status_rules#failed",
                "gate_blocked": gate_summary.get("blocked", 0),
                "error_count": len(errors),
                "aborted_stages": aborted,
            }

        # 诚实铁律：存在未处置告警不得声称 completed。
        # 需人工复核的项即使已执行了安全处置，也不能宣称完整交付。
        unhandled = [{"stage": w.get("stage"), "code": w.get("code"),
                      "message": w.get("message"),
                      "requires_review": bool(w.get("requires_review"))}
                     for w in warnings if (not w.get("action_taken")
                                            or w.get("requires_review"))]
        gate_warned = gate_summary.get("passed_with_warning", 0)
        g5_passed, g5_reason = self._g5_fully_passed()

        if gate_warned or unhandled or not g5_passed:
            return STATUS_PARTIAL, {
                "rule": "status_rules#partial",
                "gate_passed_with_warning": gate_warned,
                "unhandled_warnings": unhandled,
                "g5_fully_passed": g5_passed,
                "g5_reason": g5_reason,
            }

        na_skipped = [s.get("stage") for s in stages
                      if s.get("status") == STAGE_SKIPPED]
        return STATUS_COMPLETED, {
            "rule": "status_rules#completed",
            "g5_fully_passed": True,
            "handled_warnings": len(warnings),
            "not_applicable_stages": na_skipped,
        }

    def _g5_fully_passed(self):
        """
        G6-05 铁律：声称 completed 时 G5（结果校验）必须全部通过。
        G5 从未被评估同样不满足——没验过就不能说验过。
        返回 (是否通过, 原因说明)。
        """
        report = self.gatekeeper.reports.get("G5")
        if report is None:
            return False, "G5 未被评估，无法声称结果已校验"
        if report.blocked:
            return False, "G5 存在阻断项 %d 条" % len(report.blocked)
        if report.human_required:
            return False, "G5 存在待人工确认项 %d 条" % len(report.human_required)
        if report.warnings:
            return False, "G5 存在告警通过项 %d 条" % len(report.warnings)
        if not report.passed:
            return False, "G5 无任何通过项（全部跳过或未执行）"
        return True, "G5 全部 %d 项通过" % len(report.passed)


def _placeholder_stage_runner(agent_id, context, gatekeeper):
    """
    占位执行器：不调用任何真实工具，仅保证 harness 框架可完整走通流程。
    返回 placeholder=True，使 orchestrator 对缺事实的闸门按 applicable=False 处理（SKIP），
    最终状态为 partial（诚实，不谎称已完成）。

    真实端到端执行应由注入的 stage_runner 提供（见 Task #6 PBLUP 验收）。
    """
    return {
        "placeholder": True,
        "gate_facts": {},
        "artifacts": [],
        "warnings": [{
            "code": "PLACEHOLDER",
            "message": "阶段 %s 使用占位执行器，未做真实数据处理（框架验证模式）" % agent_id,
        }],
        "errors": [],
        "blackboard": {},
    }
