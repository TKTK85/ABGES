# -*- coding: utf-8 -*-
"""
流程约束违例异常。

由 FlowIntegrityEnforcer 在「前置未完成 / 产物缺失或为空」时抛出。
Orchestrator.run 顶层捕获后，打印结构化中断横幅并令整个运行以
非零状态退出——绝不降级为 partial 后继续推进下游阶段。
"""


class FlowViolationError(Exception):
    """流程完整性违例：任一预定义步骤被跳过/失败，或其产物缺失/为空。"""

    def __init__(self, code="E_FLOW", message="", diagnostic=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.diagnostic = diagnostic or {}

    def __str__(self):
        return "[%s] %s" % (self.code, self.message)
