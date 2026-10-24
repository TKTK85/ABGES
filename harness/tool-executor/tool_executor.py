# -*- coding: utf-8 -*-
"""
Tool Executor
=============
外部程序与内部工具函数的唯一执行边界。

铁律一：Agent 不得自行起子进程。所有 HIBLUP / PLINK 调用都必须经这里，
理由有三：
  1. 白名单校验，杜绝执行计划外的程序；
  2. 环境统一（TEMP/TMP 使用系统临时目录、线程数受控）；
  3. 每次调用的完整命令行、退出码、耗时都进审计，保证可复现。

内部 Python 工具函数不起子进程，但同样经 call_python 记录耗时与状态，
使调用链在审计中保持连续。
"""

import os
import time
import json
import hashlib
import logging
import subprocess

from _bootstrap import bootstrap

bootstrap()

import abges_core as core
import hiblup_tool
import plink_tool

logger = logging.getLogger(__name__)

# 引擎二进制完整性清单（相对项目根）；缺失时不阻断，仅告警。
ENGINE_MANIFEST_REL = "tools/ENGINE_MANIFEST.json"


class ExecResult(object):
    """一次外部调用的结果。"""

    def __init__(self, tool, command, exit_code, stdout, stderr, duration,
                 workdir=None):
        self.tool = tool
        self.command = command
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.duration = duration
        self.workdir = workdir

    @property
    def ok(self):
        return self.exit_code == 0

    @property
    def command_line(self):
        if isinstance(self.command, str):
            return self.command
        return " ".join(str(item) for item in self.command)

    def to_dict(self):
        return {
            "tool": self.tool,
            "command_line": self.command_line,
            "exit_code": self.exit_code,
            "duration_seconds": round(self.duration, 3),
            "workdir": self.workdir,
            "stdout_tail": (self.stdout or "")[-2000:],
            "stderr_tail": (self.stderr or "")[-2000:],
        }


class ToolExecutor(object):
    """统一工具执行器。"""

    def __init__(self, policy, audit):
        self.policy = policy
        self.audit = audit
        self._engine_manifest = None

    # -----------------------------------------------------------
    # 〇、引擎二进制完整性校验（S5）
    # -----------------------------------------------------------

    def _load_engine_manifest(self):
        path = core.resolve_project_path(*ENGINE_MANIFEST_REL.replace("\\", "/").split("/"))
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
            engines = data["engines"]
            if not isinstance(engines, list) or not engines:
                raise ValueError("empty engine manifest")
            return engines
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise core.AbgesError("E032", "Engine manifest missing or invalid; execution denied") from exc

    def _sha256(self, path):
        hasher = hashlib.sha256()
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                hasher.update(block)
        return hasher.hexdigest()

    def assert_engine_integrity(self, binary, label=None):
        """
        执行前强制校验引擎二进制 SHA256（对应 system-policies.yaml
        #execution_boundary / #data_protection.verify_before_delivery）。

        与 check_environment.py 的区别：那是旁路自检脚本（不带
        --require-engines 时 ok 恒 True），这是**执行链前置门禁**。

        清单缺失或无法归集时**只告警不阻断**（避免环境差异导致全局卡死）；
        一旦匹配到清单条目而哈希不符，直接抛错阻断并写审计。
        """
        name = os.path.basename(str(binary)).lower()
        manifest = self._load_engine_manifest()
        if not manifest:
            raise core.AbgesError("E032", "Engine manifest is empty")
        for engine in manifest:
            expected_path = str(engine.get("path", "")).replace("\\", "/")
            if os.path.basename(expected_path).lower() != name:
                continue
            expected = str(engine.get("sha256", "")).lower()
            if len(expected) != 64:
                raise core.AbgesError("E032", "Engine hash missing or invalid")
            if not os.path.isfile(binary):
                # 交给调用方原有的 not-found 逻辑处理
                raise core.AbgesError("E032", "Engine file missing")
            actual = self._sha256(binary).lower()
            if actual != expected:
                message = ("引擎二进制完整性校验失败：%s\n期望 sha256=%s\n实际 sha256=%s\n"
                           "该文件可能已被替换/降级/损坏，已阻断执行。"
                           % (binary, expected, actual))
                logger.error(message)
                if self.audit:
                    self.audit.event("engine_integrity_failed", engine=label or name,
                                     binary=str(binary), expected_sha256=expected,
                                     actual_sha256=actual)
                raise core.AbgesError("E032", message,
                                      {"engine": label or name, "binary": str(binary)})
            logger.info("引擎完整性校验通过：%s（%s）", label or name, actual[:16])
            return True
        raise core.AbgesError("E032", "Engine is not registered in manifest")

    # -----------------------------------------------------------
    # 一、通用外部程序
    # -----------------------------------------------------------
    def _prepare_env(self, threads=None):
        environment = os.environ.copy()
        environment.update(self.policy.env_overrides())
        thread_count = self.policy.thread_count(threads)
        environment["OMP_NUM_THREADS"] = str(thread_count)
        environment["MKL_NUM_THREADS"] = str(thread_count)
        return environment, thread_count

    def run_binary(self, tool_name, command, timeout=3600, workdir=None,
                   threads=None, note=None):
        """
        执行一个白名单内的外部程序。
        不做任何静默重试；失败原样返回，由 error-handler 决定后续。
        """
        if not command:
            raise core.AbgesError("E015", "命令为空，无法执行")
        self.policy.assert_binary_allowed(command[0])
        self.assert_engine_integrity(command[0], label="run_binary")
        environment, _ = self._prepare_env(threads)

        started = time.time()
        try:
            completed = subprocess.run(
                [str(item) for item in command],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                encoding="utf-8", errors="replace", cwd=workdir,
                env=environment, timeout=timeout)
            exit_code = completed.returncode
            stdout, stderr = completed.stdout, completed.stderr
        except FileNotFoundError:
            exit_code, stdout, stderr = -2, "", "BINARY_NOT_FOUND: %s" % command[0]
        except subprocess.TimeoutExpired:
            exit_code, stdout, stderr = -1, "", "TIMEOUT after %ss" % timeout
        duration = time.time() - started

        result = ExecResult(tool_name, command, exit_code, stdout, stderr,
                            duration, workdir)
        self.audit.tool_call(tool_name, result.command_line, exit_code,
                             duration, workdir, note)
        return result

    # -----------------------------------------------------------
    # 二、HIBLUP
    # -----------------------------------------------------------
    def run_hiblup(self, command, timeout=3600, workdir=None, threads=None,
                   note=None):
        """
        执行 HIBLUP。复用 hiblup_tool.run_hiblup（含进程树终止与心跳），
        在其外层补白名单校验与审计。
        """
        self.policy.assert_binary_allowed(command[0])
        # 【S5】执行前强制校验引擎二进制 SHA256，不匹配直接阻断
        self.assert_engine_integrity(command[0], label="HIBLUP")
        # hiblup_tool 内部会读 TEMP/TMP，先确保指向 D 盘
        for key, value in self.policy.env_overrides().items():
            os.environ[key] = value

        exit_code, stdout, stderr, duration = hiblup_tool.run_hiblup(
            command, workdir=workdir, timeout=timeout,
            threads=self.policy.thread_count(threads))

        result = ExecResult("HIBLUP", command, exit_code, stdout, stderr,
                            duration, workdir)
        self.audit.tool_call("HIBLUP", result.command_line, exit_code,
                             duration, workdir, note)
        return result

    def verify_hiblup(self, configured_path=None):
        """引擎可用性自检，返回 (是否可用, 版本或错误说明)。"""
        try:
            binary = hiblup_tool.resolve_hiblup_binary(configured_path)
        except core.AbgesError as error:
            return False, error.message
        self.policy.assert_binary_allowed(binary)
        self.assert_engine_integrity(binary, label="engine preflight")
        ok, info = hiblup_tool.verify_engine(binary)
        self.audit.event("engine_check", engine="HIBLUP", binary=binary,
                         available=bool(ok), info=info)
        return ok, info

    # -----------------------------------------------------------
    # 三、PLINK
    # -----------------------------------------------------------
    def run_plink_qc(self, input_path, output_dir, thresholds=None,
                     plink_exe=None, chr_set=None):
        """
        执行基因型质控全流程。plink_tool 内部按 mind→geno→maf→hwe 顺序调用，
        每步日志完整保留，这里负责白名单校验与整体审计。
        """
        binary = plink_tool.resolve_plink_binary(plink_exe)
        self.policy.assert_binary_allowed(binary)
        self.assert_engine_integrity(binary, label="engine preflight")
        # 【S5】执行前强制校验引擎二进制 SHA256，不匹配直接阻断
        self.assert_engine_integrity(binary, label="PLINK")
        started = time.time()
        summary = plink_tool.run_genotype_qc(
            input_path, output_dir, thresholds=thresholds, plink_exe=binary)
        duration = time.time() - started
        self.audit.tool_call("PLINK", "run_genotype_qc(%s)" % input_path,
                             0 if summary.get("success") else 1,
                             duration, output_dir,
                             note="基因型质控全流程")
        return summary

    def verify_plink(self, configured_path=None):
        try:
            binary = plink_tool.resolve_plink_binary(configured_path)
        except core.AbgesError as error:
            return False, error.message
        self.policy.assert_binary_allowed(binary)
        self.assert_engine_integrity(binary, label="engine preflight")
        ok, info = plink_tool.verify_plink(binary)
        self.audit.event("engine_check", engine="PLINK", binary=binary,
                         available=bool(ok), info=info)
        return ok, info

    # -----------------------------------------------------------
    # 四、内部 Python 工具
    # -----------------------------------------------------------
    def call_python(self, function_name, function, *args, **kwargs):
        """
        调用内部工具函数并记录审计。异常原样抛出，绝不吞掉。
        """
        started = time.time()
        try:
            value = function(*args, **kwargs)
        except Exception as error:
            self.audit.python_call(function_name, time.time() - started,
                                   "failed", {"error": str(error)})
            raise
        self.audit.python_call(function_name, time.time() - started, "ok")
        return value
