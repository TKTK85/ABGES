# -*- coding: utf-8 -*-
"""
ABGES Entrypoint
================
唯一入口：解析 task 实例（task-schema），校验输入，生成 run_id，初始化全部 harness
组件，驱动 Orchestrator 完成阶段编排，写运行元数据并输出 result-schema。

典型用法::

    python harness/entrypoint/main.py <task.yaml> [--runner module:func] \\
        [--mode auto|interactive|deny]

  <task.yaml>   一个符合 task-schema 的任务实例（含 command/inputs/traits/model 等）
  --runner      真实阶段执行器，格式 module:func；缺省使用占位执行器（仅验证框架）
  --mode        人工介入模式：auto（留痕放行）/ interactive（命令行询问）/ deny（全拒）
"""
import os
import sys
import re
import json
import logging
import datetime
import argparse
import shutil
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

# 把 harness/ 根目录加入 sys.path，使 `from _bootstrap import ...` 可解析
_HARNESS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _HARNESS_DIR not in sys.path:
    sys.path.insert(0, _HARNESS_DIR)

from _bootstrap import bootstrap, load_component  # noqa: E402

bootstrap()

from abges_runtime.storage import identifier, inside
from abges_runtime.tasks import validate_task, physical_inputs

import yaml  # noqa: E402
import abges_core as core  # noqa: E402
import abges_phenotype as phenotype_tool  # noqa: E402

# 装配 harness 组件（连字符目录须经 load_component 按路径加载）
_orchestrator_mod = load_component("orchestrator", "orchestrator")
Orchestrator = _orchestrator_mod.Orchestrator
_placeholder_runner = _orchestrator_mod._placeholder_stage_runner

_gatekeeper_mod = load_component("gatekeeper", "gatekeeper")
_policy_mod = load_component("policy-engine", "policy_engine")
_state_mod = load_component("state-manager", "state_manager")
_audit_mod = load_component("audit", "audit_hub")
_router_mod = load_component("router", "router")
_tool_mod = load_component("tool-executor", "tool_executor")
_error_mod = load_component("error-handler", "error_handler")
_hitl_mod = load_component("human-in-the-loop", "hitl")
_context_mod = load_component("context-manager", "context_manager")


def _load_task(task_path):
    core.require_readable_file(task_path, "E002")
    with open(task_path, "r", encoding="utf-8") as handle:
        return validate_task(yaml.safe_load(handle))


def _plink_prefix_exists(prefix):
    """判定 plink 前缀是否指向可用的基因型数据。

    支持两种形态：
    - 前缀本身（无扩展名），对应 <prefix>.bed/.bim/.fam
    - 目录，目录内含任意一组 *.bed/*.bim/*.fam 三件套（同前缀）
    """
    if not prefix:
        return False
    if os.path.isfile(prefix + ".bed") and os.path.isfile(prefix + ".bim") and os.path.isfile(prefix + ".fam"):
        return True
    if os.path.isdir(prefix):
        try:
            names = os.listdir(prefix)
        except OSError:
            return False
        bases = {os.path.splitext(n)[0] for n in names}
        exts = {os.path.splitext(n)[1].lower() for n in names}
        if {".bed", ".bim", ".fam"}.issubset(exts):
            for b in bases:
                if all(os.path.isfile(os.path.join(prefix, b + e)) for e in (".bed", ".bim", ".fam")):
                    return True
    return False


def _detect_availability(task):
    """基于输入文件存在性探测数据可用性，供 router 选型与跳过判定。"""
    inputs = task.get("inputs", {}) or {}
    availability = {
        "has_phenotype": False,
        "has_pedigree": False,
        "has_genotype": False,
        "genotyped_ratio": None,
        "model_hint": task.get("model", "auto"),
        "traits_missing_in_phenotype": [],
    }
    for role in ("phenotype", "pedigree", "genotype"):
        path = inputs.get(role)
        if role == "genotype":
            availability["has_genotype"] = _plink_prefix_exists(path)
        elif path and os.path.isfile(path):
            availability["has_" + role] = True
    egg_record = inputs.get("egg_record")
    availability["has_egg_record"] = bool(egg_record and os.path.exists(egg_record))
    if availability["has_phenotype"]:
        # Use the same header normalization as Agent 01; this is only a
        # routing probe and never writes the raw input.
        frame, _, _, _ = phenotype_tool.load_phenotype(inputs["phenotype"])
        availability["traits_missing_in_phenotype"] = [
            trait for trait in (task.get("traits") or []) if trait not in frame.columns
        ]
    return availability


def _validate_task(task, policy):
    """task-schema#validation 的强制校验，不通过直接 failed。"""
    validate_task(task)
    command = task.get("command", "run")
    if command not in ("validate", "plan", "run", "status"):
        raise core.AbgesError("E002", "非法 command：%r" % command,
                              {"allowed": ["validate", "plan", "run", "status"]})
    output_policy = (policy.config or {}).get("output_policy", {})
    if output_policy.get("task_directory_layout", True):
        # Publish every task to its stable workspace task root. An explicitly
        # configured output_dir remains supported only when it is that same
        # task root; legacy external/RunNN paths are normalized to this layout.
        task_key = task.get("task_name") or task.get("task_id")
        if not task_key:
            raise core.AbgesError("E002", "任务目录输出策略要求 task_id 或 task_name")
        output_dir = core.resolve_project_path("workspace", identifier(task_key))
        if not os.path.isabs(output_dir):
            output_dir = os.path.abspath(output_dir)
        task["output_dir"] = output_dir
    else:
        output_dir = task.get("output_dir") or "workspace/output"
    policy.assert_output_dir_allowed(output_dir)
    if command == "status":
        return command, output_dir
    inputs = task.get("inputs", {}) or {}
    phenotype = inputs.get("phenotype")
    if not phenotype or not os.path.isfile(phenotype):
        raise core.AbgesError("E002", "表型文件缺失或不可读：%r" % phenotype)
    traits = task.get("traits") or []
    if not traits:
        raise core.AbgesError("E002", "traits 不能为空")
    if len(traits) > 5:
        raise core.AbgesError("E002", "traits 数量超过 5（多性状上限）", {"n": len(traits)})
    if task.get("multi_trait") and len(traits) < 2:
        raise core.AbgesError("E002", "multi_trait 为真时 traits 至少 2 个")
    return command, output_dir


def _resolve_run_structure(task_name, workspace_root):
    """
    【精简结构 2026-10-16】建立任务根目录和运行目录。

    目录结构：
        workspace/{task_name}/
            run_{序号}_{timestamp}_{task_name}/

    返回 (task_root, run_dir, run_number, timestamp_str)
    """
    safe_name = identifier(task_name)
    task_root = str(inside(workspace_root, os.path.join(workspace_root, safe_name)))
    core.ensure_dir(task_root)

    # 【2026-10-18 A·根因修复】真实运行目录统一落在 task_root/_runs/ 下（由 StateManager 创建并填充）。
    # 序号改为扫描 _runs/ 内已有 run_* 目录确定，避免此前在 task_root 直接 ensure_dir(run_dir)
    # 产生一个永远为空、从不被写入的"壳目录"——那正是用户看到"输出缺这么多"的混乱来源。
    # 此处不再创建 task_root 下的空壳，仅借用 run_dir 变量名返回 basename 供 StateManager 命名。
    runs_dir = os.path.join(task_root, "_runs")
    core.ensure_dir(runs_dir)
    existing = [d for d in os.listdir(runs_dir)
                if os.path.isdir(os.path.join(runs_dir, d)) and d.startswith("run_")]
    run_number = len(existing) + 1

    ts = core.make_timestamp()
    run_id = "run_%03d_%s_%s" % (run_number, ts, uuid.uuid4().hex[:16])
    run_dir = os.path.join(task_root, run_id)   # 仅用于取 basename 作为 run_id 名；不再创建该目录

    return task_root, run_dir, run_number, ts


def _cleanup_prior_runs(task_root, current_run_id, workspace_root, policy_config):
    """Remove older same-task run folders after successful delivery."""
    output_policy = (policy_config or {}).get("output_policy", {})
    retention = output_policy.get("run_retention", {}) or {}
    if not retention.get("cleanup_old_runs", True):
        return []
    keep_active = max(1, int(retention.get("keep_active_runs", 1)))
    runs_dir = str(inside(task_root, os.path.join(task_root, "_runs")))
    if not os.path.isdir(runs_dir):
        return []
    run_names = [name for name in os.listdir(runs_dir)
                 if name.startswith("run_") and os.path.isdir(os.path.join(runs_dir, name))]
    run_names.sort(key=lambda name: os.path.getmtime(os.path.join(runs_dir, name)), reverse=True)
    if current_run_id not in run_names:
        return []
    active_names = [name for name in run_names if name != current_run_id]
    preserve_count = max(0, keep_active - (1 if current_run_id in run_names else 0))
    to_remove = active_names[preserve_count:]
    if not to_remove:
        return []

    workspace_root = os.path.abspath(workspace_root)
    task_root = str(inside(workspace_root, task_root))
    removed = []
    for name in to_remove:
        source = str(inside(runs_dir, os.path.join(runs_dir, name)))
        shutil.rmtree(source)
        removed.append(name)
    return removed


def _cleanup_internal_task_artifacts(task_root, workspace_root):
    """Remove internal task-root state after it has been consolidated for delivery."""
    workspace_root = os.path.abspath(workspace_root)
    task_root = str(inside(workspace_root, task_root))
    removed = []
    for name in ("context", "metadata", "statistics"):
        target = str(inside(task_root, os.path.join(task_root, name)))
        if os.path.isdir(target):
            shutil.rmtree(target)
            removed.append(name)
    for name in os.listdir(task_root):
        if not name.lower().endswith(".json"):
            continue
        target = str(inside(task_root, os.path.join(task_root, name)))
        if os.path.isfile(target):
            os.remove(target)
            removed.append(name)
    return removed


def _cleanup_resume_checkpoints(run_dir, workspace_root, policy_config):
    """Prune bulky resume checkpoints after delivery while retaining run audit state."""
    retention = ((policy_config or {}).get("output_policy", {})
                 .get("run_retention", {}) or {})
    if not retention.get("cleanup_resume_checkpoints_after_delivery",
                         retention.get("cleanup_run_json_after_delivery", True)):
        return []
    workspace_root = os.path.abspath(workspace_root)
    run_dir = str(inside(workspace_root, run_dir))
    removed = []
    for current, _dirnames, filenames in os.walk(run_dir, topdown=False):
        # inside() deliberately rejects the root itself. os.walk yields run_dir
        # as its final entry, so validate only descendants and retain the
        # already validated root for that one iteration.
        current = os.path.abspath(current)
        if os.path.normcase(current) != os.path.normcase(run_dir):
            current = str(inside(run_dir, current))
        for filename in filenames:
            # Keep state, metadata, result, context and QC JSON for audit/status.
            # Only the serialized blackboard checkpoints and derived matrix are
            # disposable after a successful delivery; they are not published.
            if filename.startswith("checkpoint_") and filename.lower().endswith(".json"):
                target = str(inside(run_dir, os.path.join(current, filename)))
                if os.path.isfile(target):
                    os.remove(target)
                    removed.append(os.path.relpath(target, run_dir))
            elif filename == "agent_result_matrix.json":
                target = str(inside(run_dir, os.path.join(current, filename)))
                if os.path.isfile(target):
                    os.remove(target)
                    removed.append(os.path.relpath(target, run_dir))
    return removed


def _cleanup_run_scratch_directories(run_dir, workspace_root, policy_config):
    """Remove bulky transient folders only after terminal delivery succeeds."""
    retention = ((policy_config or {}).get("output_policy", {})
                 .get("run_retention", {}) or {})
    if not retention.get("cleanup_run_scratch_after_delivery", False):
        return []
    workspace_root = os.path.abspath(workspace_root)
    run_dir = str(inside(workspace_root, run_dir))
    removed = []
    for name in ("change_logs", "intermediate"):
        target = str(inside(run_dir, os.path.join(run_dir, name)))
        if os.path.isdir(target):
            shutil.rmtree(target)
            removed.append(name)
    return removed


def _update_latest_symlink(link_path, target):
    """更新 latest 符号链接，指向最新运行目录。"""
    try:
        import shutil as _sh2
        if os.path.islink(link_path):
            os.remove(link_path)
        elif os.path.exists(link_path):
            # 非符号链接的文件（不应存在），跳过
            return
        os.symlink(target, link_path)
    except OSError:
        # Windows 下符号链接可能需要管理员权限，忽略失败
        pass


def _resolve_runner(spec):
    """解析 --runner module:func 为可调用对象；缺省返回占位执行器。"""
    if not spec:
        return _placeholder_runner
    module_path, func_name = (spec.split(":", 1)
                              if ":" in spec else (spec, "run"))
    import importlib
    module = importlib.import_module(module_path)
    return getattr(module, func_name)


def _consolidate_deliverables(state, output_dir, audit=None, policy=None):
    from abges_runtime.delivery import publish
    if output_dir and not os.path.isabs(output_dir):
        output_dir = core.resolve_project_path(output_dir)
    return publish(state, output_dir, policy, core.make_deliverable_ignore,
                   _normalize_deliverable_names, _deliverable_prefix(state), audit)


# 交付目录内「<前缀>_<8位日期>_<6位时间><尾部>」形态的运行时间戳文件名
_RUN_STAMPED_NAME = re.compile(r"^[A-Za-z0-9]+_(?P<date>\d{8})_(?P<time>\d{6})(?P<tail>.*)$")


def _deliverable_prefix(state, fallback=""):
    """
    交付件固定命名前缀 = 品系段 + 首个性状，例如 SAMPLE_POPULATION_EN。
    品系段取自 task_id 的第一段（SAMPLE_POPULATION_EN_PBLUP_GenBatch → SAMPLE_POPULATION）。
    """
    try:
        task = dict(state.state.get("task") or {})
    except Exception:
        task = {}
    task_id = str(task.get("task_id") or task.get("task_name") or "").strip()
    traits = task.get("traits") or []
    trait = str(traits[0]).strip() if traits else ""
    strain = ""
    if task_id:
        parts = [part for part in re.split(r"[_\-]", task_id) if part]
        if parts:
            strain = parts[0]
    if strain and trait:
        return "%s_%s" % (strain, trait)
    return trait or strain or fallback


def _normalize_deliverable_names(output_dir, prefix):
    """
    【2026-10-20 固定输出】把交付目录内带运行时间戳的产物名规约为固定名。

    引擎与报告产物原名形如 `EN_20261020_223326.vars`、`EN_20261020_223431_报告.docx`、
    `EN_20261020_223431_figures/`，时间戳每次运行都变，导致「每次输出结果都不一样」的观感。
    此处统一规约为 `SAMPLE_POPULATION_EN.vars`、`SAMPLE_POPULATION_EN_报告.docx`、`SAMPLE_POPULATION_EN_figures/`，
    使每次运行后交付目录的文件名逐字一致。

    ⚠ 仅作用于对外交付目录；内部 `_runs/run_NNN` 仍保留原始时间戳名，审计轨迹不丢失。
    """
    renamed = []
    if not prefix:
        return renamed
    for sub in ("output", "reports"):
        base = os.path.join(output_dir, sub)
        if not os.path.isdir(base):
            continue
        # 先改文件（目录改名会在其后自底向上进行，避免父目录改名导致子路径失效）
        for dirpath, _dirnames, filenames in os.walk(base):
            for name in filenames:
                match = _RUN_STAMPED_NAME.match(name)
                if not match:
                    continue
                src_path = os.path.join(dirpath, name)
                dst_path = os.path.join(dirpath, prefix + match.group("tail"))
                if src_path == dst_path or os.path.exists(dst_path):
                    continue
                os.rename(src_path, dst_path)
                renamed.append(os.path.relpath(dst_path, output_dir))
        for dirpath, dirnames, _filenames in os.walk(base, topdown=False):
            for name in dirnames:
                match = _RUN_STAMPED_NAME.match(name)
                if not match:
                    continue
                src_path = os.path.join(dirpath, name)
                dst_path = os.path.join(dirpath, prefix + match.group("tail"))
                if src_path == dst_path or os.path.exists(dst_path):
                    continue
                os.rename(src_path, dst_path)
                renamed.append(os.path.relpath(dst_path, output_dir))
    return renamed


def _purge_nondeliverable(output_dir):
    """
    【2026-10-20 兜底净化】删除交付目录内按中央过滤器判定「不应交付」的项。

    规则复用 `core.is_deliverable_path`，排除 JSON、诊断日志与摘要 Markdown，
    因此本函数是过滤器的「执行版」：固化时已过滤一次，此处再清一次，
    确保固化之后被外部写入或历史残留污染的内容也被清掉，交付目录状态每次一致。
    """
    removed = []
    real_root = os.path.realpath(output_dir)
    for sub in ("reports", "output", "input"):
        base = os.path.join(output_dir, sub)
        if not os.path.isdir(base):
            continue
        for dirpath, dirnames, filenames in os.walk(base, topdown=False):
            for name in filenames:
                path = os.path.join(dirpath, name)
                if core.is_deliverable_path(path, root=output_dir):
                    continue
                # 【S1】符号链接/junction 逃逸防护：解析真实路径后必须仍在交付根内，
                # 否则绝不删除（避免遍历到目录外的文件）。
                real = os.path.realpath(path)
                if not core.is_relative_inside(real, real_root):
                    continue
                try:
                    os.remove(path)
                    removed.append(os.path.relpath(path, output_dir))
                except OSError:
                    pass
            for name in dirnames:
                if name != core.ARCHIVE_DIR_NAME:
                    continue
                path = os.path.join(dirpath, name)
                real = os.path.realpath(path)
                if not core.is_relative_inside(real, real_root):
                    continue
                try:
                    shutil.rmtree(path)
                    removed.append(os.path.relpath(path, output_dir))
                except OSError:
                    pass
    return removed


def main(argv=None):
    parser = argparse.ArgumentParser(description="ABGES 家禽遗传评估入口")
    parser.add_argument("task", help="task 实例 YAML 路径")
    parser.add_argument("--runner", help="真实阶段执行器 module:func，缺省用占位执行器")
    parser.add_argument("--resume-run", help="resume a checksum-verified Harness checkpoint")
    parser.add_argument("--mode", default="auto",
                        choices=["auto", "interactive", "deny"],
                        help="人工介入模式")
    args = parser.parse_args(argv)

    try:
        task = _load_task(args.task)
    except core.AbgesError as ae:
        print(json.dumps({"status": "failed", "error": ae.message,
                          "detail": ae.detail}, ensure_ascii=False, indent=2))
        return 1

    policy = _policy_mod.PolicyEngine()

    try:
        command, output_dir = _validate_task(task, policy)
    except core.AbgesError as ae:
        print(json.dumps({"status": "failed", "error": ae.message,
                          "detail": ae.detail}, ensure_ascii=False, indent=2))
        return 1

    if command == "status":
        # 新结构：列出 workspace/ 下各任务目录及其 _runs/
        workspace_root = core.resolve_project_path("workspace")
        task_runs = {}
        for name in os.listdir(workspace_root):
            task_dir = os.path.join(workspace_root, name)
            if os.path.isdir(task_dir):
                runs_dir = os.path.join(task_dir, "_runs")
                if os.path.isdir(runs_dir):
                    runs = sorted(os.listdir(runs_dir), reverse=True)
                    task_runs[name] = runs
        print(json.dumps({"status": "ok", "task_runs": task_runs}, ensure_ascii=False, indent=2))
        return 0

    availability = _detect_availability(task)
    task_name = task.get("task_name") or task.get("task_id") \
                or os.path.splitext(os.path.basename(args.task))[0]
    workspace_root = core.resolve_project_path("workspace")

    if args.resume_run:
        state = _state_mod.StateManager.resume(args.resume_run, expected_task=task)
        previous, requested = dict(state.state.get("task") or {}), dict(task)
        previous.pop("command", None); requested.pop("command", None)
        previous.pop("gateway_context", None); requested.pop("gateway_context", None)
        # allow 方案决策注入：recommend_decisions 允许在 resume 时追加，不触发 diff 报错
        previous.pop("recommend_decisions", None)
        requested.pop("recommend_decisions", None)
        # 语义覆盖：先生拍板 choose_one 决策会改变模型结构，
        # diff 比较时将 previous 也同步为先生拍板后的值，使 diff 为空。
        incoming_decisions = task.get("recommend_decisions") or []
        if incoming_decisions:
            for item in incoming_decisions:
                did = item.get("decision_id", "")
                chosen = item.get("chosen")
                if not chosen:
                    continue
                if did == "choose_one_gen_vs_hatchdays" and chosen == "Gen":
                    previous["covariates"] = []
                    requested["covariates"] = []
        if previous != requested:
            diffs = {k: {"prev": previous.get(k), "req": requested.get(k)}
                     for k in set(list(previous.keys()) + list(requested.keys()))
                     if previous.get(k) != requested.get(k)}
            print(json.dumps({"status": "failed", "error": "resume task differs from original task",
                              "incoming_decisions_debug": incoming_decisions,
                              "task_recommend_debug": task.get("recommend_decisions"),
                              "diffs": diffs}, ensure_ascii=False, indent=2))
            return 1
        # 先生拍板注入：将 recommend_decisions 写入 state，供 orchestrator 读取并注入 hitl
        if incoming_decisions:
            merged_task = dict(state.state.get("task") or {})
            merged_task["recommend_decisions"] = incoming_decisions
            state.bind_task(merged_task)
            # blackboard 的 __task__ 也要同步（_inject_resolved_recommendations 读的是 blackboard）
            state.put("__task__", merged_task)
        task_name = state.state.get("task", {}).get("task_name") or task_name
        run_id = os.path.basename(state.run_dir)  # 纯 run 文件夹名（如 run_004_...），不含 task name 前缀
        task_root = os.path.dirname(os.path.dirname(state.run_dir))  # workspace/{task_name}
    else:
        task_root, run_dir, run_number, ts = _resolve_run_structure(task_name, workspace_root)
        run_id = os.path.basename(run_dir)  # e.g. run_001_20261016_165104_<TASK_NAME>
        state = _state_mod.StateManager(run_id, run_root=os.path.join(task_root, "_runs"))
        state.bind_task(task)
    audit = _audit_mod.AuditHub(state.dirs["logs"], run_id)
    # 【S6】日志系统初始化：logs 目录已由 StateManager 建立，此处是全流程最早的挂载点，
    # 之后 router / gatekeeper / tool-executor / orchestrator / 报告 / 固化 各阶段的
    # logging 调用都会同时落到 <run_dir>/logs/abges_<run_id>.log 与 stderr。
    try:
        _log_path = core.setup_logger(
            state.dirs["logs"], run_id,
            level=getattr(logging,
                          str(os.environ.get("ABGES_LOG_LEVEL", "INFO")).upper(),
                          logging.INFO))
        audit.event("logger_initialized", log_path=_log_path)
    except Exception as exc:        # 日志不可用不得阻断主流程，但必须可见
        print("[WARN] 日志初始化失败：%s" % exc)
    gateway_context = dict(task.get("gateway_context") or {})
    if gateway_context:
        audit.event("mcp_harness_bound", **gateway_context)
    gatekeeper = _gatekeeper_mod.Gatekeeper(audit=audit)
    router = _router_mod.Router(audit=audit)
    tool_executor = _tool_mod.ToolExecutor(policy, audit)
    error_handler = _error_mod.ErrorHandler(policy, audit)
    hitl = _hitl_mod.HumanInTheLoop(policy, audit, mode=args.mode)

    try:
        model_decision = router.select_model(availability,
                                              explicit=task.get("model", "auto"))
    except core.AbgesError as ae:
        state.add_error("router", "MODEL", ae.message, blocking=True)
        state.set_status("failed")
        print(json.dumps({"status": "failed", "error": ae.message,
                          "detail": ae.detail}, ensure_ascii=False, indent=2))
        return 1

    # 原始数据只读守卫：读入即登记校验和，交付前复核（对应 G6-06）
    inputs = task.get("inputs", {}) or {}
    for role, input_path in physical_inputs(inputs).items():
        path = str(input_path)
        if path and os.path.isfile(path):
            policy.raw_guard.register(role, path)
            # Preserve physical PLINK inputs for reproducibility. Raw phenotype
            # and pedigree sources remain immutable at their original location;
            # processed engine-ready files are archived by the stage runners.
            if not role.startswith("genotype"):
                continue
            import shutil as _shutil2
            _src = os.path.abspath(path)
            _dst_dir = state.dirs.get("input")
            if _dst_dir and os.path.isdir(_dst_dir):
                _dst = os.path.join(_dst_dir, os.path.basename(_src))
                try:
                    _shutil2.copy2(_src, _dst)
                except Exception as exc:
                    # 归档失败不阻断主流程，但必须留痕（治理铁律：不静默）
                    logger.warning("输入文件归档失败：%s → %s（%s）", _src, _dst, exc)

    context_manager = _context_mod.ContextManager(state.run_dir, run_id, task)
    # 【新结构 2026-10-16】task_root 写入 state 黑板，供各阶段通过 context 获取
    state.put("task_root", task_root)
    orchestrator = Orchestrator(state, gatekeeper, router, policy, audit,
                                error_handler, hitl, tool_executor, context_manager)
    stage_runner = _resolve_runner(args.runner)

    summary = orchestrator.run(task, availability, model_decision, stage_runner,
                               command=command, resume=bool(args.resume_run))

    # 交付前：原始数据复核（G6-06 诚实交付/只读铁律）
    unchanged, violations = policy.raw_guard.verify_unchanged()
    if not unchanged:
        summary["status"] = "failed"
        summary["errors"].append({
            "stage": "delivery",
            "code": "G6-06",
            "message": "原始数据校验和不一致：%s" % violations,
            "blocking": True,
        })
        summary["status_basis"] = {"rule": "G6-06", "violations": violations}
        state.add_error("delivery", "G6-06", "原始数据校验和不一致", blocking=True)
    state.set_status(summary["status"])

    # 运行元数据（铁律五/六：可复现、时间戳化）
    meta_payload = {
        "run_id": run_id,
        "timestamp_start": state.state.get("created_at"),
        "timestamp_end": datetime.datetime.now().isoformat(timespec="seconds"),
        "full_command_line": " ".join(sys.argv),
        "input_files": {
            role: core.describe_input_file(p)
            for role, p in physical_inputs(inputs).items()
        },
        "output_files": list(summary.get("artifacts", {}).keys()),
        "status": summary["status"],
        "model": model_decision.model_id,
        "gate_summary": summary["gate_summary"],
        "warnings": summary["warnings"],
        "errors": summary["errors"],
        "availability": availability,
        "raw_data_guard": policy.raw_guard.to_dict(),
        "gateway_context": dict(task.get("gateway_context") or {}),
    }
    meta_prefix = os.path.join(state.run_dir, "reports", "run_metadata")
    try:
        core.write_run_metadata(meta_prefix, meta_payload)
    except core.AbgesError as ae:
        summary["warnings"].append({
            "stage": "delivery",
            "code": "META_WRITE",
            "message": "运行元数据写入警告：%s" % ae.message,
            "affected_count": None,
            "action_taken": None,
        })

    result_path = os.path.join(state.run_dir, "reports", "result.json")
    core.write_json(result_path, summary)

    # 失败、预检/计划及待决策运行只保存在 _runs 中，不触碰固定交付目录。
    run_errors = summary.get("errors", [])
    if command != "run":
        delivery_skip_reason = "command_%s_does_not_publish" % command
    elif summary.get("status") == "failed" or run_errors:
        delivery_skip_reason = "run_failed_previous_delivery_preserved"
    elif summary.get("pending_decisions"):
        delivery_skip_reason = "pending_decisions_previous_delivery_preserved"
    else:
        delivery_skip_reason = None

    if delivery_skip_reason:
        summary["delivery"] = {
            "status": "skipped",
            "reason": delivery_skip_reason,
            "previous_delivery_preserved": True,
        }
        audit.event("delivery_skipped", reason=delivery_skip_reason)
    else:
        try:
            summary["delivery"] = _consolidate_deliverables(
                state, task.get("output_dir"), audit, policy)
            if summary["delivery"].get("status") == "completed":
                removed_nondeliverables = _purge_nondeliverable(task.get("output_dir"))
                if removed_nondeliverables:
                    audit.event("nondeliverable_purged", files=removed_nondeliverables)
        except Exception as exc:
            logger.exception("Delivery publication failed")
            summary["status"] = "failed"
            summary["delivery"] = {"status": "failed",
                                    "previous_delivery_preserved": True}
            summary.setdefault("errors", []).append({"stage": "delivery", "code": "DELIVERY_FAILED",
                                                     "message": "Delivery publication failed; previous version retained where rollback succeeded",
                                                     "blocking": True})
            state.add_error("delivery", "DELIVERY_FAILED", str(exc), blocking=True)
            state.set_status("failed")
    meta_payload["status"] = summary["status"]
    core.write_run_metadata(meta_prefix, meta_payload)
    core.write_json(result_path, summary)
    if (summary.get("status") in ("completed", "partial")
            and summary.get("delivery", {}).get("status") == "completed"
            and not summary.get("pending_decisions")):
        try:
            removed = _cleanup_prior_runs(task_root, run_id, workspace_root, policy.config)
            if removed:
                logger.info("清理此前运行：%s", removed)
        except Exception as exc:
            logger.exception("Prior run cleanup failed")
            summary.setdefault("warnings", []).append({
                "stage": "delivery", "code": "RUN_CLEANUP",
                "message": "旧运行清理失败：%s" % exc,
                "affected_count": None, "action_taken": "旧运行目录仍在活动任务目录"})
            core.write_json(result_path, summary)
    if (summary.get("delivery", {}).get("status") == "completed"
            and not summary.get("pending_decisions")):
        try:
            _cleanup_internal_task_artifacts(task_root, workspace_root)
        except Exception as exc:
            logger.exception("Internal task artifact cleanup failed")
            summary.setdefault("warnings", []).append({
                "stage": "delivery", "code": "TASK_ARTIFACT_CLEANUP",
                "message": "任务目录辅助文件清理失败：%s" % exc,
                "affected_count": None, "action_taken": "未清理失败项"})
            core.write_json(result_path, summary)
    if (summary.get("status") in ("completed", "partial")
            and summary.get("delivery", {}).get("status") == "completed"
            and not summary.get("pending_decisions")):
        try:
            # This run is terminal: keep the state/audit JSON but invalidate its
            # resumable checkpoint pointer before pruning bulky blackboard files.
            state.state["resume_checkpoint"] = None
            state.state["resume_state"] = "terminal_after_delivery"
            state.save()
            removed_checkpoints = _cleanup_resume_checkpoints(
                state.run_dir, workspace_root, policy.config)
            logger.info("清理已结束运行检查点：%d 个", len(removed_checkpoints))
            removed_scratch = _cleanup_run_scratch_directories(
                state.run_dir, workspace_root, policy.config)
            logger.info("清理已结束运行临时目录：%s", removed_scratch)
        except Exception as exc:
            logger.exception("Run JSON cleanup failed")
            summary.setdefault("warnings", []).append({
                "stage": "delivery", "code": "RUN_CHECKPOINT_CLEANUP",
                "message": "运行检查点清理失败：%s" % exc,
                "affected_count": None, "action_taken": "未完成清理"})
    fixed_output_dir = task.get("output_dir")
    if fixed_output_dir and not os.path.isabs(fixed_output_dir):
        fixed_output_dir = core.resolve_project_path(fixed_output_dir)
    fixed_output_dir = fixed_output_dir and os.path.abspath(fixed_output_dir)

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("任务目录: %s" % task_root)
    if fixed_output_dir:
        print("交付目录: %s" % fixed_output_dir)
    return 0 if summary["status"] in ("completed", "partial") else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, TypeError, OSError, yaml.YAMLError, core.AbgesError) as exc:
        logger.exception("Harness execution failed")
        print(json.dumps({"status": "failed", "errors": [{"code": getattr(exc, "code", "E002"),
              "message": "Task failed; inspect the local audit log"}]}))
        sys.exit(1)
