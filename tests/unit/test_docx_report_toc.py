# -*- coding: utf-8 -*-
"""docx_report._update_toc_via_word 的回归测试（P2-1 补测 + P2-3 锁死）。

背景（实测发现的真实缺陷，此处把它钉死，防止再犯）：
  1. 原实现直接 subprocess.run 拉起 powershell，绕过 tool-executor，
     违反铁律一；且 stdout/stderr 全丢 DEVNULL、check=False，
     **失败完全静默**——目录有没有更新无从得知。
  2. P2-1 改走 ToolExecutor 后，因阶段09 上下文白名单不含 __tools__，
     context.get("__tools__") 恒为 None，新分支成了**死代码**。
     （该问题已修：orchestrator._CONTEXT_KEYS["09"] 补入 __tools__。）
  3. 成功判定不能用退出码：COM 的 Quit() 抛「RPC 服务器不可用」等
     **已捕获**异常也会让 powershell 返回 exit_code=1，
     实测脚本 Open/Update/Save 全部成功仍报 1。故改由脚本输出
     哨兵 ABGES_TOC_OK，Python 侧解析哨兵判定成败。
"""
import sys
import os
from pathlib import Path

ROOT = str(Path(__file__).resolve().parents[2])
for _p in (ROOT, os.path.join(ROOT, 'tools', 'Python'),
           os.path.join(ROOT, 'harness')):
    if _p not in sys.path:
        sys.path.insert(0, _p)
os.chdir(ROOT)

import pytest  # noqa: E402

from runners import docx_report as dr  # noqa: E402


class _FakeResult(object):
    def __init__(self, ok, exit_code, stdout, stderr):
        self.ok = ok
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr


class _FakeTools(object):
    """最小替身：只记录调用，不真的拉起 Word。"""

    def __init__(self, result):
        self.result = result
        self.calls = []

    def run_binary(self, tool_name, command, timeout=None, note=None):
        self.calls.append({"tool_name": tool_name, "command": command,
                           "timeout": timeout, "note": note})
        return self.result


def test_returns_false_without_tool_executor(caplog):
    """未提供执行器：按铁律一不得私起子进程，如实留痕后降级。"""
    assert dr._update_toc_via_word("x_报告.docx", context={}) is False


def test_success_is_decided_by_sentinel_not_exit_code(tmp_path, caplog):
    """exit_code=1 但哨兵为 OK —— 必须判成功（真实环境就是这种组合）。"""
    docx_path = tmp_path / "a_报告.docx"
    docx_path.write_bytes(b"")
    tools = _FakeTools(_FakeResult(False, 1,
                                   "ABGES_TOC_OK updated=1\n", "CLIXML noise"))
    assert dr._update_toc_via_word(str(docx_path),
                                   context={"__tools__": tools}) is True
    assert len(tools.calls) == 1
    assert tools.calls[0]["tool_name"] == "powershell"
    assert os.path.basename(tools.calls[0]["command"][0]).lower() == "powershell.exe"
    assert "-EncodedCommand" in tools.calls[0]["command"]


def test_failure_writes_diagnostic_log(tmp_path):
    """失败必须留痕（不再 DEVNULL 静默），且返回 False。"""
    docx_path = tmp_path / "b_报告.docx"
    docx_path.write_bytes(b"")
    tools = _FakeTools(_FakeResult(False, 1, "", "ABGES_TOC_FAIL 无法打开文档"))
    assert dr._update_toc_via_word(str(docx_path),
                                   context={"__tools__": tools}) is False
    log_path = tmp_path / "b_报告诊断.log"
    assert log_path.is_file()
    body = log_path.read_text(encoding="utf-8")
    assert "toc_update" in body
    assert "ABGES_TOC_FAIL" in body or "exit_code=1" in body


def test_powershell_script_contains_sentinel_and_exit_zero():
    """脚本必须显式输出哨兵并 exit 0；否则退出码噪声会淹没真实成败。"""
    src = Path(dr.__file__).read_text(encoding="utf-8")
    assert "ABGES_TOC_OK" in src
    assert "ABGES_TOC_FAIL" in src
    assert "exit 0" in src


def test_stage09_context_allowlist_includes_tools():
    """阶段09 必须能拿到 __tools__，否则 ToolExecutor 分支是死代码。

    用 AST 直接取 _CONTEXT_KEYS 的字面量取值，避免文本切片被嵌套花括号
    或注释里的 "09" 误伤。
    """
    import ast
    orch = Path(ROOT) / "harness" / "orchestrator" / "orchestrator.py"
    tree = ast.parse(orch.read_text(encoding="utf-8"))
    found = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "_CONTEXT_KEYS":
                    found = node.value
    assert found is not None, "未找到 _CONTEXT_KEYS 定义"
    assert isinstance(found, ast.Dict)
    mapping = {}
    for key_node, value_node in zip(found.keys, found.values):
        key = ast.literal_eval(key_node)
        mapping[key] = {ast.literal_eval(item)
                        for item in getattr(value_node, "elts", [])}
    assert "09" in mapping, "未找到阶段09 的上下文白名单"
    assert "__tools__" in mapping["09"], (
        "阶段09 白名单缺 __tools__，Word 目录回写会静默失效（历史回归）")
