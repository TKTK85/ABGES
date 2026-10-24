# -*- coding: utf-8 -*-
"""
Policy Engine
=============
系统规则的唯一权威执行点，全部规则来自 configs/policies/system-policies.yaml，
不在代码中硬编码第二套标准。

承担：
  - 执行边界（外部二进制白名单）
  - 原始数据只读（读入登记校验和，交付前复核）
  - 输出永不覆盖（前缀命名与时间戳）
  - 状态词表与禁用措辞（诚实交付）
  - 超时、重试、线程与内存约束
  - 人工确认清单查询
"""

import os

from _bootstrap import bootstrap

bootstrap()

import abges_core as core


class PolicyViolation(core.AbgesError):
    """策略违规。区别于业务错误，一律阻断且必须写审计。"""

    def __init__(self, code, message, detail=None):
        super(PolicyViolation, self).__init__(code, message, detail)


class RawDataGuard(object):
    """
    原始数据只读守卫。
    读入即记录校验和，交付前复核，不一致直接判违规（对应 G6-06）。
    """

    def __init__(self):
        self.registry = {}

    def register(self, role, file_path):
        """登记一个原始输入文件，返回其描述（含校验和）。"""
        if not file_path:
            return None
        description = core.describe_input_file(file_path)
        description["role"] = role
        self.registry[os.path.abspath(file_path)] = description
        return description

    def verify_unchanged(self):
        """
        复核全部已登记原始文件。
        返回 (是否全部未变, 违规明细列表)。
        """
        violations = []
        for path, description in self.registry.items():
            if not os.path.exists(path):
                violations.append({"path": path, "reason": "文件已消失"})
                continue
            current = core.compute_checksum(path)
            if current != description["checksum"]:
                violations.append({
                    "path": path,
                    "reason": "校验和不一致",
                    "checksum_on_read": description["checksum"],
                    "checksum_now": current,
                })
        return (not violations), violations

    def to_dict(self):
        return {"files": list(self.registry.values())}


class PolicyEngine(object):
    """系统策略执行器。"""

    CONFIG_PATH = "configs/policies/system-policies.yaml"

    def __init__(self, config=None):
        self.config = config or core.load_yaml_config(self.CONFIG_PATH)
        self.raw_guard = RawDataGuard()
        self._binary_whitelist = self._build_binary_whitelist()

    # -----------------------------------------------------------
    # 一、执行边界
    # -----------------------------------------------------------
    def _build_binary_whitelist(self):
        """
        由配置构造允许调用的可执行文件名集合。
        比对用文件名而非完整路径：引擎实际安装位置因机器而异，
        但"只允许 HIBLUP 与 PLINK"这条边界不变。
        """
        entries = self.config.get("execution_boundary", {}) \
                             .get("allowed_external_binaries", []) or []
        names = set()
        for entry in entries:
            names.add(os.path.basename(str(entry)).lower())
        # PLINK 在不同发行版中可执行名可能为 plink.exe 或 plink2.exe
        if "plink2.exe" in names:
            names.add("plink.exe")
        if "hiblup.exe" in names:
            names.add("hiblup")
        return names

    def assert_binary_allowed(self, binary_path):
        """
        校验外部程序是否在白名单内。不在则拒绝（配置：reject_and_audit）。
        """
        name = os.path.basename(str(binary_path)).lower()
        if name not in self._binary_whitelist:
            raise PolicyViolation(
                "P001", "调用了白名单之外的外部程序：%s" % binary_path,
                {"allowed": sorted(self._binary_whitelist)})
        return True

    def forbids_direct_subprocess(self):
        return bool(self.config.get("execution_boundary", {})
                    .get("forbid_direct_subprocess", True))

    # -----------------------------------------------------------
    # 二、资源与路径
    # -----------------------------------------------------------
    def temp_dir(self):
        """返回可写临时目录（D 盘优先，不可用时退回项目目录）。"""
        return core.resolve_temp_dir()

    def env_overrides(self):
        """返回需要注入子进程的环境变量。"""
        overrides = dict(self.config.get("resources", {})
                         .get("env_overrides", {}) or {})
        actual_temp = self.temp_dir()
        for key in ("TEMP", "TMP"):
            overrides[key] = actual_temp
        return overrides

    # -----------------------------------------------------------
    # 二·补充：输出目录守卫（S1）
    # -----------------------------------------------------------

    def _output_dir_policy(self):
        return self.config.get("resources", {}).get("output_dir_policy", {}) or {}

    def _expand_roots(self, key):
        """展开白/黑名单根：{ABGES_ROOT} 占位 + realpath 归一化 + normcase。

        realpath 会解析 `..` 与 junction/符号链接，normcase 消除盘符与大小写差异，
       二者共同构成穿越防护的基础。
        """
        policy = self._output_dir_policy()
        items = [str(v) for v in (policy.get(key) or [])]
        if key == "allowed_roots":
            env_key = policy.get("allowed_roots_env")
            if env_key:
                items += [p.strip() for p in os.environ.get(env_key, "").split(os.pathsep)
                          if p.strip()]
        roots = []
        for item in items:
            item = item.replace("{ABGES_ROOT}", core.get_abges_root())
            roots.append(os.path.normcase(os.path.realpath(os.path.abspath(item))))
        return roots

    def assert_output_dir_allowed(self, output_dir):
        """
        输出目录校验（S1 加固版）。

        校验链：非空 → 禁 C 盘（既有）→ 禁盘符根 → 命中黑名单则拒 →
                未命中白名单则拒 → 可创建/可写。

        返回值仍为 os.path.abspath(output_dir)（不改成 realpath），
        以免改变下游字符串语义与输出路径外观。
        """
        if not output_dir:
            raise PolicyViolation("P002", "未指定输出目录")
        absolute = os.path.abspath(output_dir)
        real = os.path.realpath(absolute)        # 归一化：解析 .. 与链接
        norm = os.path.normcase(real)

        forbid_c = bool(self.config.get("resources", {})
                        .get("forbid_c_drive_write", True))
        if forbid_c and os.name == "nt" and absolute[:2].upper() == "C:":
            raise PolicyViolation(
                "P002", "本机禁止把任务数据写入 C 盘：%s" % absolute,
                {"suggestion": "改用 D 盘目录，例如 D:/abges_output"})

        policy = self._output_dir_policy()
        if policy.get("enabled", True):
            if policy.get("forbid_drive_root", True) and os.path.dirname(real) == real:
                raise PolicyViolation("P002", "禁止以盘符根目录作为输出目录：%s" % real)
            if policy.get("forbid_user_home_root", True):
                home = os.path.normcase(os.path.realpath(
                    os.path.expanduser("~")))
                if norm == home or norm == os.path.dirname(home):
                    raise PolicyViolation(
                        "P002", "禁止以用户主目录（或其上一级）作为输出目录：%s" % real)
            # 黑名单：相等或位于其中（按路径分段判定，不做字符串前缀匹配）
            for bad in self._expand_roots("forbidden_roots"):
                if norm == bad or core.is_relative_inside(real, bad):
                    raise PolicyViolation(
                        "P002", "输出目录位于受保护目录内：%s" % real,
                        {"forbidden_root": bad})
            # 白名单：必须命中其一
            allowed = self._expand_roots("allowed_roots")
            if allowed and not any(
                    norm == a or core.is_relative_inside(real, a) for a in allowed):
                raise PolicyViolation(
                    "P002", "输出目录不在允许的输出根白名单内：%s" % real,
                    {"allowed_roots": allowed,
                     "suggestion": "把该目录加入 system-policies.yaml"
                                   "#resources.output_dir_policy.allowed_roots，"
                                   "或设置环境变量 ABGES_OUTPUT_ROOTS"})

        core.ensure_dir(real)
        if not os.access(real, os.W_OK):
            raise PolicyViolation("P002", "输出目录不可写：%s" % real)
        return absolute

    def thread_count(self, requested=None):
        resources = self.config.get("resources", {})
        cap = int(resources.get("threads_cap", 16))
        if requested is None:
            requested = resources.get("threads_default", "auto")
        return core.resolve_thread_count(requested, cap)

    # -----------------------------------------------------------
    # 三、输出命名（永不覆盖）
    # -----------------------------------------------------------
    def build_output_prefix(self, output_dir, task, trait, model, timestamp=None):
        """生成不冲突的输出前缀，遵循永不覆盖约定。

        实际前缀格式为 {trait}_{timestamp}（由 core.build_output_prefix 实现；
        目录本身已含 task_id，故前缀不再重复 task/model，仅含 trait 与运行戳）。
        """
        self.assert_output_dir_allowed(output_dir)
        return core.build_output_prefix(output_dir, task, trait, model, timestamp)

    def never_overwrite(self):
        return bool(self.config.get("output_policy", {})
                    .get("never_overwrite", True))

    # -----------------------------------------------------------
    # 四、超时与重试
    # -----------------------------------------------------------
    def timeout_for(self, model_id):
        timeouts = self.config.get("timeouts", {}) or {}
        key = str(model_id).lower()
        if key == "ssgblup":
            key = "ssblup"
        return int(timeouts.get(key, timeouts.get("default_tool_timeout", 3600)))

    def max_retry_attempts(self):
        return int(self.config.get("retry", {}).get("max_attempts", 2))

    def is_retryable(self, trigger):
        """判定某触发原因是否允许重试。未列入者一律不重试。"""
        retry = self.config.get("retry", {}) or {}
        if trigger in (retry.get("non_retryable") or []):
            return False
        return trigger in (retry.get("retryable") or [])

    def forbids_silent_retry(self):
        return bool(self.config.get("retry", {}).get("forbid_silent_retry", True))

    # -----------------------------------------------------------
    # 五、人工确认
    # -----------------------------------------------------------
    def requires_confirmation(self, action):
        """动作是否需要人工确认。"""
        items = self.config.get("human_in_the_loop", {}) \
                           .get("require_confirmation", []) or []
        return any(str(item) in str(action) or str(action) in str(item)
                   for item in items)

    def auto_proceed_allowed(self, action):
        items = self.config.get("human_in_the_loop", {}) \
                           .get("auto_proceed", []) or []
        return any(str(item) in str(action) or str(action) in str(item)
                   for item in items)

    # -----------------------------------------------------------
    # 六、诚实交付
    # -----------------------------------------------------------
    def assert_status_allowed(self, status):
        allowed = self.config.get("honesty", {}).get("allowed_status") \
                  or list(core.ALLOWED_STATUS)
        if status not in allowed:
            raise PolicyViolation("P003", "非法交付状态：%r" % status,
                                  {"allowed": allowed})
        return status

    def find_forbidden_phrases(self, text):
        """
        扫描文本中的禁用措辞，返回命中列表。
        用于报告交付前自检，避免"基本完成""应该没问题"这类模糊表述。
        """
        phrases = self.config.get("honesty", {}).get("forbidden_phrases", []) or []
        content = str(text or "")
        return [phrase for phrase in phrases if str(phrase) in content]

    # -----------------------------------------------------------
    # 七、统计与元数据强制项
    # -----------------------------------------------------------
    def statistics_required(self):
        return bool(self.config.get("statistics_policy", {})
                    .get("mandatory", True))

    def metadata_required_keys(self):
        return list(self.config.get("metadata", {}).get("must_contain", []) or [])

    def audit_output_template(self):
        return self.config.get("audit", {}).get(
            "output", "workspace/logs/audit_{run_id}.jsonl")

    def directories(self):
        return dict(self.config.get("output_policy", {}).get("directories", {}) or {})

    def run_dir_template(self):
        return self.config.get("output_policy", {}).get(
            "run_dir_template", "workspace/{run_id}")
