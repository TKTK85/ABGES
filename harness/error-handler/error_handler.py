# -*- coding: utf-8 -*-
"""
Error Handler
=============
错误分类、重试决策、回退策略与终止。

原则：
  - 只对"确定性可恢复"的失败重试，且每次重试必须写审计（禁止静默重试）；
  - 引擎非零退出、前置校验失败、闸门阻断一律不重试；
  - 未收敛不是崩溃，按可恢复处理：AI 换 EM 再算一次；
  - 任何被吞掉的异常都视为缺陷，这里只做分类与建议，不掩盖失败。
"""

from _bootstrap import bootstrap

bootstrap()

import abges_core as core
import hiblup_tool

# 触发原因常量，与 system-policies#retry 的取值一一对应
TRIGGER_NOT_CONVERGED = "vc_not_converged"
TRIGGER_TRANSIENT_IO = "transient_io_error"
TRIGGER_ENGINE_NONZERO = "engine_nonzero_exit"
TRIGGER_PREFLIGHT = "preflight_failed"
TRIGGER_GATE_BLOCKED = "gate_blocked"
TRIGGER_TIMEOUT = "engine_timeout"
TRIGGER_BINARY_MISSING = "engine_not_found"
TRIGGER_UNKNOWN = "unknown_error"


class StageFailure(object):
    """一次阶段失败的结构化描述。"""

    def __init__(self, stage, code, message, trigger, retryable,
                 detail=None, blocking=True):
        self.stage = stage
        self.code = code
        self.message = message
        self.trigger = trigger
        self.retryable = retryable
        self.detail = detail or {}
        self.blocking = blocking

    def to_dict(self):
        return {
            "stage": self.stage,
            "code": self.code,
            "message": self.message,
            "trigger": self.trigger,
            "retryable": self.retryable,
            "blocking": self.blocking,
            "detail": self.detail,
        }


class ErrorHandler(object):
    """错误处理器。"""

    def __init__(self, policy, audit):
        self.policy = policy
        self.audit = audit
        self.failures = []

    # -----------------------------------------------------------
    # 一、分类
    # -----------------------------------------------------------
    def classify_exception(self, stage, error):
        """把异常归类为 StageFailure。"""
        code = getattr(error, "code", None) or error.__class__.__name__
        message = getattr(error, "message", None) or str(error)
        detail = getattr(error, "detail", None) or {}

        if code == "GATE_BLOCK":
            trigger = TRIGGER_GATE_BLOCKED
        elif isinstance(error, (IOError, OSError)):
            trigger = TRIGGER_TRANSIENT_IO
        elif str(code).startswith(("E01", "E02", "PH", "PD", "GT", "G1", "G3")):
            trigger = TRIGGER_PREFLIGHT
        else:
            trigger = TRIGGER_UNKNOWN

        failure = StageFailure(stage, code, message, trigger,
                               self.policy.is_retryable(trigger), detail)
        self.failures.append(failure)
        self.audit.event("stage_error", stage=stage, code=code,
                         message=message, trigger=trigger,
                         retryable=failure.retryable)
        return failure

    def classify_engine_result(self, stage, exec_result, parsed=None):
        """
        依据引擎退出码与解析结果归类。
        返回 StageFailure 或 None（表示无需处置）。
        """
        exit_code = exec_result.exit_code
        if exit_code == hiblup_tool.EXIT_TIMEOUT:
            trigger, message = TRIGGER_TIMEOUT, "引擎执行超时，已终止进程树"
        elif exit_code == hiblup_tool.EXIT_NOT_FOUND:
            trigger, message = TRIGGER_BINARY_MISSING, "找不到引擎可执行文件"
        elif exit_code != 0:
            trigger = TRIGGER_ENGINE_NONZERO
            message = "引擎非零退出（%d），结果不可信" % exit_code
        elif parsed is not None and parsed.get("converged") is False:
            trigger, message = TRIGGER_NOT_CONVERGED, "方差组分未收敛"
        else:
            return None

        failure = StageFailure(
            stage, "ENGINE", message, trigger,
            self.policy.is_retryable(trigger),
            {"exit_code": exit_code,
             "stderr_tail": (exec_result.stderr or "")[-1000:]},
            blocking=(trigger != TRIGGER_NOT_CONVERGED))
        self.failures.append(failure)
        self.audit.event("engine_failure", stage=stage, trigger=trigger,
                         exit_code=exit_code, message=message,
                         retryable=failure.retryable)
        return failure

    # -----------------------------------------------------------
    # 二、重试决策
    # -----------------------------------------------------------
    def plan_retry(self, failure, attempt, current_params=None):
        """
        给出重试方案。返回 None 表示不重试。

        目前唯一的自动回退是未收敛时 AI -> EM：
        这是有明确依据的确定性调整，且必然写审计。
        """
        if failure is None or not failure.retryable:
            return None
        if attempt >= self.policy.max_retry_attempts():
            self.audit.event("retry_exhausted", stage=failure.stage,
                             attempt=attempt, trigger=failure.trigger)
            return None

        params = dict(current_params or {})
        if failure.trigger == TRIGGER_NOT_CONVERGED:
            original_method = str(params.get("vc_method", "AI")).upper()
            if original_method == "EM":
                return None      # 已经是 EM，无更稳妥的回退，如实报未收敛
            params["vc_method"] = "EM"
            action = "vc_method 由 %s 回退为 EM 重算" % original_method
        elif failure.trigger == TRIGGER_TRANSIENT_IO:
            action = "重试一次 I/O 操作"
        else:
            return None

        self.audit.retry(failure.stage, attempt + 1, failure.trigger, action)
        return {"params": params, "action": action, "attempt": attempt + 1}

    # -----------------------------------------------------------
    # 三、终止
    # -----------------------------------------------------------
    def terminal_status(self):
        """
        依据已记录的失败判定整体状态。
        有阻断性失败 -> failed；只有非阻断（如未收敛）-> partial；无 -> completed。
        """
        if any(item.blocking for item in self.failures):
            return core.STATUS_FAILED
        if self.failures:
            return core.STATUS_PARTIAL
        return core.STATUS_COMPLETED

    def to_list(self):
        return [item.to_dict() for item in self.failures]
