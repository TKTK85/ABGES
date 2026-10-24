# -*- coding: utf-8 -*-
"""
FlowIntegrityEnforcer —— 强制流程约束守门员
===========================================

在 Orchestrator 的两处统一执行断言，确保 ABGES 每次运行严格按
agent-registry#default_pipeline 的完整顺序执行，逐步校验完成状态，并在
任何步骤被跳过或失败时立即中断运行：

  1) assert_upstream_satisfied(agent_id)
     阶段启动前调用。校验 pipeline 中该阶段的所有前置阶段：
       - 声明式合法跳过 (SKIP_NOT_APPLICABLE) → 视为完成，放行；
       - 同并行组的成员视为并发，不相互前置，跳过；
       - 其余前置必须 stage_status == "completed"（且产物已在完成时
         经 assert_outputs_satisfied 校验非空）。
     任一前置未达标 → 抛 FlowViolationError（立即中断）。

  2) assert_outputs_satisfied(agent_id, artifacts)
     阶段登记产物后调用。校验本阶段登记的全部产物文件：
       - 必须 os.path.exists 且 os.path.getsize > 0（非空）。
     任一产物缺失/为空 → 抛 FlowViolationError（立即中断）。

任何 FlowViolationError 由 Orchestrator.run 顶层捕获，打印结构化中断
横幅并令整个运行以非零状态退出（绝不降级为 partial 后继续）。
"""

import os

# 与 state_manager / orchestrator 保持一致的常量
STATUS_COMPLETED = "completed"
SKIP_NOT_APPLICABLE = "not_applicable"

# 动态导入同目录errors模块：load_component以"abges_harness_<name>"前缀注册组件，
# 裸"from errors import"在exec_module包上下文中无法解析；
# errors.py被导入时以"errors"（非"abges_harness_errors"）为键注册，
# 因此同时查 sys.modules['abges_harness_errors'] 和 'errors' 两个可能键。
import sys as _sys
_flow_enf_mod = _sys.modules.get(__spec__.name)
_errors_candidates = [
    __spec__.name.rsplit('.', 1)[0] + '.errors',  # abges_harness_errors
    'errors',                                       # 裸导入时的键
]
for _cand in _errors_candidates:
    _err_mod = _sys.modules.get(_cand)
    if _err_mod is not None and hasattr(_err_mod, 'FlowViolationError'):
        FlowViolationError = _err_mod.FlowViolationError
        break
else:
    raise ImportError(
        f"无法找到FlowViolationError：已检查 {__spec__.name} 同目录的 errors 模块，"
        f"候选键={_errors_candidates}，实际sys.modules键="
        f"{[k for k in _sys.modules if 'error' in k.lower()]}"
    )


class FlowIntegrityEnforcer(object):
    """强制约束守门员：两道断言 + 中断诊断。"""

    def __init__(self, state_manager, router, pipeline, parallel_groups,
                 status_completed=STATUS_COMPLETED,
                 skip_not_applicable=SKIP_NOT_APPLICABLE):
        self.state = state_manager
        self.router = router
        self.pipeline = list(pipeline)
        self.parallel_groups = [set(g) for g in (parallel_groups or [])]
        self.status_completed = status_completed
        self.skip_not_applicable = skip_not_applicable

    # -----------------------------------------------------------
    def _parallel_group_of(self, agent_id):
        for g in self.parallel_groups:
            if agent_id in g:
                return g
        return None

    def _stage_status(self, agent_id):
        try:
            return self.state.stage_status(agent_id)
        except Exception:
            return None

    # -----------------------------------------------------------
    def assert_upstream_satisfied(self, agent_id):
        """前置阶段必须已完成（或合法跳过）；否则立即中断。"""
        if agent_id not in self.pipeline:
            return
        idx = self.pipeline.index(agent_id)
        my_group = self._parallel_group_of(agent_id)
        blocked = []
        for p in self.pipeline[:idx]:
            if my_group is not None and p in my_group:
                continue  # 同并行组并发，不互为前置
            # 设计内合法跳过（skip_kind=not_applicable）视为已完成，
            # 不得因此触发硬中断；因上游失败而跳过的（upstream_failed）
            # 仍视为未完成，须阻断。
            if self.state.is_legit_skip(p):
                continue
            p_status = self._stage_status(p)
            if p_status != self.status_completed:
                blocked.append((p, p_status))
        if blocked:
            self._halt(agent_id, "upstream_incomplete", blocked)

    def assert_outputs_satisfied(self, agent_id, artifacts):
        """本阶段登记的全部产物必须存在且非空；否则立即中断。"""
        missing = []
        for item in (artifacts or []):
            if isinstance(item, (list, tuple)) and len(item) == 2:
                name, path = item
            elif isinstance(item, dict) and "path" in item:
                name, path = item.get("name"), item["path"]
            else:
                continue
            if not path or not os.path.exists(path):
                missing.append((name, path, "absent"))
            elif os.path.getsize(path) == 0:
                missing.append((name, path, "empty"))
        if missing:
            self._halt(agent_id, "outputs_incomplete", missing)

    # -----------------------------------------------------------
    def _halt(self, agent_id, reason, detail):
        diag = {
            "violation": reason,
            "failed_stage": agent_id,
            "pipeline": self.pipeline,
            "detail": detail,
        }
        raise FlowViolationError(
            code="E_FLOW",
            message="流程约束中断：阶段 %s 的前置/产物校验未通过（%s）"
                    % (agent_id, reason),
            diagnostic=diag)
