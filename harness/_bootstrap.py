# -*- coding: utf-8 -*-
"""
Harness 引导模块
================
统一解决三件事，避免每个组件各写一遍：

1. 本机运行约束：注入 D 盘 venv 的 site-packages（此前的隔离启动模式已废止，
   site 初始化被跳过），并把 TEMP/TMP 使用系统临时目录。
2. 工具层可导入：把 tools/Python、tools/HIBLUP、tools/PLINK 加入 sys.path。
3. 组件互相调用：harness 子目录含连字符（policy-engine 等）无法直接 import，
   统一用 load_component() 按路径加载并登记到 sys.modules。

任何 harness 组件的第一行代码都应是 `from _bootstrap import ...` 或
先执行 bootstrap()，否则在 -S 模式下会因找不到 pandas/yaml 而失败。
"""

import os
import sys
import importlib.util

# ---------------------------------------------------------------
# 路径常量
# ---------------------------------------------------------------
HARNESS_DIR = os.path.dirname(os.path.abspath(__file__))
ABGES_ROOT = os.path.abspath(os.path.join(HARNESS_DIR, os.pardir))

SITE_PACKAGES = None  # retained only as a compatibility constant
PREFERRED_TEMP = os.environ.get("TEMP") or os.environ.get("TMP") or os.getcwd()

_BOOTSTRAPPED = False
_COMPONENT_CACHE = {}


def bootstrap():
    """
    准备运行环境。可重复调用，只生效一次。
    返回实际生效的临时目录，便于调用方记录。
    """
    global _BOOTSTRAPPED
    if _BOOTSTRAPPED:
        return os.environ.get("TEMP")

    # 1. site-packages 注入（-S 模式下必需）
    # Dependencies belong to the selected interpreter, never another environment.
    if sys.flags.no_site:
        raise RuntimeError("ABGES requires normal virtualenv startup; remove -S")

    # 2. 工具层路径
    for relative in ("tools/Python", "tools/HIBLUP", "tools/PLINK"):
        candidate = os.path.join(ABGES_ROOT, *relative.split("/"))
        if os.path.isdir(candidate) and candidate not in sys.path:
            sys.path.insert(0, candidate)

    # 3. harness 自身（供 load_component 内部再引用 _bootstrap）
    if HARNESS_DIR not in sys.path:
        sys.path.insert(0, HARNESS_DIR)

    # 4. 项目根：使 runners/ 这类项目内包可被 --runner 的 importlib 解析
    #    （脚本模式下 sys.path[0] 是入口脚本目录，不含项目根，必须显式加入）
    if ABGES_ROOT not in sys.path:
        sys.path.insert(0, ABGES_ROOT)

    # 5. 临时目录：优先系统临时目录；不可用时退回项目内，绝不因此中断任务
    temp_dir = PREFERRED_TEMP
    try:
        if not os.path.isdir(temp_dir):
            os.makedirs(temp_dir)
        probe = os.path.join(temp_dir, ".abges_bootstrap_probe")
        with open(probe, "w") as handle:
            handle.write("ok")
        os.remove(probe)
    except OSError:
        temp_dir = os.path.join(ABGES_ROOT, ".workbuddy_temp")
        if not os.path.isdir(temp_dir):
            os.makedirs(temp_dir)
    os.environ["TEMP"] = temp_dir
    os.environ["TMP"] = temp_dir

    _BOOTSTRAPPED = True
    return temp_dir


def load_component(component_dir, module_name):
    """
    按路径加载 harness 组件模块。

    参数
    ----
    component_dir : str   harness 下的子目录名，如 "policy-engine"
    module_name   : str   模块文件名（不含 .py），如 "policy_engine"

    返回已导入的模块对象。同一模块只加载一次。
    """
    bootstrap()
    cache_key = "%s/%s" % (component_dir, module_name)
    if cache_key in _COMPONENT_CACHE:
        return _COMPONENT_CACHE[cache_key]

    module_path = os.path.join(HARNESS_DIR, component_dir, module_name + ".py")
    if not os.path.isfile(module_path):
        raise ImportError("harness 组件不存在：%s" % module_path)

    # 【修复2026-10-16】在加载目标组件前，先将同目录下所有辅助模块
    # 统一预注册到 sys.modules（两个键），彻底避免裸 import 在 exec_module
    # 上下文中找不到同目录 .py 文件的问题。
    _preload_support_modules(component_dir, module_name)

    registered_name = "abges_harness_" + module_name
    spec = importlib.util.spec_from_file_location(registered_name, module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[registered_name] = module
    spec.loader.exec_module(module)
    _COMPONENT_CACHE[cache_key] = module
    return module


def _preload_support_modules(component_dir, current_module_name):
    """
    扫描 component_dir 目录下所有 .py 文件（排除 __init__ 和当前加载模块），
    逐个预加载到 sys.modules（两个键），确保裸 import 路径均能找到。

    注册键策略（以 flow-enforcer/errors.py 为例）：
      - 键A：abges_harness_<name>   —— 正式注册名，与 load_component 一致
      - 键B：<name>                  —— 裸导入时的回退键
    两键均注册，确保任意导入路径均能找到；已存在则跳过。
    """
    component_dir_abs = os.path.join(HARNESS_DIR, component_dir)
    if not os.path.isdir(component_dir_abs):
        return

    try:
        py_files = [
            f for f in os.listdir(component_dir_abs)
            if f.endswith(".py")
            and f != "__init__.py"
            and f != (current_module_name + ".py")
        ]
    except OSError:
        return

    for py_file in py_files:
        module_name = py_file[:-3]          # "errors"
        module_path = os.path.join(component_dir_abs, py_file)

        for key in (
            "abges_harness_" + module_name,  # 键A
            module_name,                      # 键B（裸导入回退）
        ):
            if key in sys.modules:
                continue
            spec = importlib.util.spec_from_file_location(key, module_path)
            mod = importlib.util.module_from_spec(spec)
            sys.modules[key] = mod
            try:
                spec.loader.exec_module(mod)
            except Exception:
                # 加载失败则清除注册键，让后续可重试或降级处理
                sys.modules.pop(key, None)


def project_path(*parts):
    """拼接 ABGES 根目录下的绝对路径。"""
    return os.path.join(ABGES_ROOT, *parts)
