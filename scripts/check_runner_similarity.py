# -*- coding: utf-8 -*-
"""P1-4 漂移哨兵：检查三个 stage_runner 同名函数的相似度是否发生劣化。

背景（审计 S9 / M10）：
    pblup / gblup / ssgblup 三个 stage_runner 共 4,047 行，函数同名且结构高度相似。
    其中差异是「有意特化」还是「无意识漂移」，单看代码无法判定，
    但「漂移」有一个可观测特征：同名函数的相似度随时间下降。

本工具做什么：
    1. 计算每个同名函数在三个 runner 之间的两两相似度（按行 SequenceMatcher）；
    2. 与基线 scripts/runner_similarity_baseline.json 比对；
    3. 任一函数相似度较基线下降超过 --tol（默认 0.15）→ 判定为疑似漂移，退出码 1。

本工具不做什么：
    · 不自动合并/抽取基类（需先生拍板）
    · 不修改任何业务代码
    · 不把「相似度低」直接判为缺陷——低相似度只是提示需要人工确认

用法：
    python scripts/check_runner_similarity.py                 # 校验模式（CI）
    python scripts/check_runner_similarity.py --update        # 重算并写基线
    python scripts/check_runner_similarity.py --report        # 只打印报告
"""
import argparse
import ast
import difflib
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
RUNNERS = os.path.join(ROOT, "runners")
BASELINE = os.path.join(HERE, "runner_similarity_baseline.json")

FILES = {
    "pblup": "pblup_stage_runner.py",
    "gblup": "gblup_stage_runner.py",
    "ssgblup": "ssgblup_stage_runner.py",
}

# 已知为「有意特化」的函数：其相似度不作漂移判定，仅记录
INTENTIONAL = {
    "_stage_planner": "三种模型的建模本质不同（A 矩阵 / G 矩阵 / H 矩阵），"
                      "且 pblup 独有永久环境效应、gblup 独有 GRM 缓存，属有意特化",
    "_stage_genotype": "PBLUP 不使用基因型，gblup/ssgblup 才有此阶段",
    "_stage_pedigree": "GBLUP 不依赖系谱，pblup/ssgblup 才有此阶段",
    "_validate_multi_trait": "ssgblup 显式拒绝 multi_trait=true（抛 E013），无需该校验",
}


def _strip_docstring(node):
    """去掉函数体的 docstring 节点，使比对只针对真实代码。"""
    body = list(node.body)
    if body and isinstance(body[0], ast.Expr):
        value = body[0].value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            body = body[1:]
    node.body = body
    return node


def top_funcs(path):
    """返回 {函数名: 归一化源码}。

    归一化 = 去掉注释（ast.unparse 天然不带）+ 去掉 docstring。
    这样「把三份实现统一委托给基类」这类**有意的**重构不会被误判为漂移，
    只有真实逻辑分叉才会触发。
    """
    src = io.open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    out = {}
    for n in tree.body:
        if isinstance(n, ast.FunctionDef):
            out[n.name] = ast.unparse(_strip_docstring(n))
    return out


# 归一化后行数低于此值的函数，单行差异即可让相似度剧烈跳动，
# 视为「信号不足」跳过漂移判定，避免误伤有意的统一重构。
MIN_LINES_FOR_DRIFT_CHECK = 20


def pair_ratios(name, tables):
    """返回 {比较对: 相似度}，只比较同名函数实际存在的 runner。

    相似度按「归一化源码」逐行计算：已剥离注释与 docstring，
    因此重命名参数、改写注释不会误报漂移，只有真实逻辑分叉才会。
    """
    present = [k for k in ("pblup", "gblup", "ssgblup") if name in tables[k]]
    out = {}
    for i in range(len(present)):
        for j in range(i + 1, len(present)):
            a, b = present[i], present[j]
            la = tables[a][name].splitlines()
            lb = tables[b][name].splitlines()
            if min(len(la), len(lb)) < MIN_LINES_FOR_DRIFT_CHECK:
                continue        # 信号不足，不作漂移判定
            out["%s|%s" % (a, b)] = difflib.SequenceMatcher(
                None, la, lb).ratio()
    return out


def build():
    tables = {k: top_funcs(os.path.join(RUNNERS, f)) for k, f in FILES.items()}
    names = sorted({n for t in tables.values() for n in t})
    report = {}
    for name in names:
        ratios = pair_ratios(name, tables)
        if ratios:
            report[name] = {k: round(v, 4) for k, v in sorted(ratios.items())}
    return report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--update", action="store_true", help="重算并覆盖基线文件")
    ap.add_argument("--report", action="store_true", help="只打印报告，不做判定")
    ap.add_argument("--tol", type=float, default=0.15,
                    help="允许的相似度下降幅度（默认 0.15）")
    args = ap.parse_args()

    report = build()

    if args.update:
        with io.open(BASELINE, "w", encoding="utf-8", newline="") as fh:
            json.dump(report, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        print("已更新基线: %s（%d 个函数）" % (BASELINE, len(report)))
        return 0

    print("=" * 74)
    print("stage_runner 同名函数相似度报告")
    print("=" * 74)
    print("%-26s %-14s %8s %8s" % ("函数名", "比较对", "当前", "基线"))
    print("-" * 74)

    baseline = {}
    if os.path.isfile(BASELINE):
        baseline = json.loads(io.open(BASELINE, encoding="utf-8").read())
    else:
        print("（未找到基线文件，将按 --report 处理）")

    drift = []
    for name in sorted(report):
        for pair, cur in sorted(report[name].items()):
            base = (baseline.get(name) or {}).get(pair)
            flag = ""
            if base is not None and (base - cur) > args.tol:
                flag = "  <== 疑似漂移"
                drift.append((name, pair, base, cur))
            print("%-26s %-14s %8.3f %8s%s"
                  % (name if pair == sorted(report[name])[0] else "",
                     pair, cur,
                     ("%.3f" % base) if base is not None else "-", flag))
    print("-" * 74)

    if args.report or not baseline:
        return 0

    if drift:
        print("\n【判定】检出 %d 处疑似漂移，请人工确认是有意特化还是无意识分叉："
              % len(drift))
        for name, pair, base, cur in drift:
            print("  - %s (%s): 基线 %.3f → 当前 %.3f，下降 %.3f"
                  % (name, pair, base, cur, base - cur))
            if name in INTENTIONAL:
                print("      已知备注：%s" % INTENTIONAL[name])
        return 1

    print("【判定】未检出相似度劣化。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
