# -*- coding: utf-8 -*-
"""ABGES 回归测试：闸门审计必须记录 stage 归属。

背景：同一闸门被多个阶段绑定（G1/G2 → 01、02；G4 → 06、07；G5 → 07、08），
同一 check_id 因而会被评估多次、依据各不相同。审计记录若不带 stage，
多条同 check_id 记录无法归因，违反系统铁律第十二节（可追溯）。

本测试锁定三点：
  1. gate_audit_{run_id}.jsonl 每条记录都含非空 stage；
  2. (check_id, stage) 组合唯一，不出现无法区分的重复；
  3. 主审计流水 audit_{run_id}.jsonl 的 gate 事件同样带 stage。
"""
import sys, os, json, shutil, tempfile
from pathlib import Path

ROOT = str(Path(__file__).resolve().parents[2])
sys.path.insert(0, os.path.join(ROOT, 'tools', 'Python'))
sys.path.insert(0, os.path.join(ROOT, 'harness'))
os.chdir(ROOT)  # gate-rules.yaml 按项目相对路径加载



def check(name, fn):
    """执行一项检查；失败即抛出，交由 pytest 逐项归因（不再静默记录）。"""
    detail = fn()
    if detail:
        print("PASS  %-52s %s" % (name, detail))
    return detail


from _bootstrap import bootstrap, load_component

bootstrap()
audit_hub = load_component('audit', 'audit_hub')
gatekeeper_mod = load_component('gatekeeper', 'gatekeeper')

TMP = os.path.join(tempfile.gettempdir(), 'abges_gate_stage_test')
RUN_ID = 'unit_gate_stage'


def _fresh_hub():
    if os.path.isdir(TMP):
        shutil.rmtree(TMP)
    os.makedirs(TMP)
    return audit_hub.AuditHub(TMP, RUN_ID)


def _read_jsonl(path):
    with open(path, encoding='utf-8') as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _evaluate_g1_twice():
    """用 G1 模拟 01、02 两阶段各评一次，产出审计记录。"""
    hub = _fresh_hub()
    keeper = gatekeeper_mod.Gatekeeper(audit=hub)
    checks = keeper.gate_checks('G1')
    if not checks:
        raise AssertionError('G1 未定义任何检查，测试前提不成立')
    facts = {item['id']: {'passed': True, 'actual': 1, 'threshold': 1}
             for item in checks}
    keeper.evaluate('G1', facts, stage='01')
    keeper.evaluate('G1', facts, stage='02')
    return hub, len(checks)


def case_stage_present():
    hub, n_checks = _evaluate_g1_twice()
    rows = _read_jsonl(hub.gate_path)
    if len(rows) != n_checks * 2:
        raise AssertionError('期望 %d 条闸门记录，实得 %d' % (n_checks * 2, len(rows)))
    missing = [r for r in rows if not r.get('stage')]
    if missing:
        raise AssertionError('存在 %d 条缺 stage 归属的记录' % len(missing))
    stages = sorted({r['stage'] for r in rows})
    if stages != ['01', '02']:
        raise AssertionError('stage 取值异常：%r' % (stages,))
    return 'records=%d,stages=%s' % (len(rows), ','.join(stages))


def case_check_stage_unique():
    hub, n_checks = _evaluate_g1_twice()
    rows = _read_jsonl(hub.gate_path)
    keys = [(r['check_id'], r['stage']) for r in rows]
    if len(set(keys)) != len(keys):
        raise AssertionError('(check_id, stage) 组合出现重复，无法区分归属')
    # 同一 check_id 必须恰好出现两次（两个阶段各一次）
    from collections import Counter
    per_check = Counter(r['check_id'] for r in rows)
    if set(per_check.values()) != {2}:
        raise AssertionError('每个 check 应恰好两条记录，实得 %r' % dict(per_check))
    return 'unique_pairs=%d,per_check=2' % len(set(keys))


def case_main_trail_has_stage():
    hub, _ = _evaluate_g1_twice()
    trail_path = None
    for name in os.listdir(TMP):
        if name.startswith('audit_') and name.endswith('.jsonl'):
            trail_path = os.path.join(TMP, name)
            break
    if trail_path is None:
        raise AssertionError('未找到主审计流水文件')
    gates = [r for r in _read_jsonl(trail_path) if r.get('event') == 'gate']
    if not gates:
        raise AssertionError('主审计流水中没有 gate 事件')
    missing = [r for r in gates if not r.get('stage')]
    if missing:
        raise AssertionError('主流水中 %d 条 gate 事件缺 stage' % len(missing))
    return 'trail_gate_events=%d,all_with_stage' % len(gates)


# ------------------------------------------------------------------
# pytest 用例包装（由脚本式 check() 自动转换，断言逻辑与判定条件均未改动）
# ------------------------------------------------------------------

def test_stage_present():
    check('闸门审计每条含 stage 归属', case_stage_present)

def test_check_stage_unique():
    check('(check_id, stage) 组合唯一', case_check_stage_unique)

def test_main_trail_has_stage():
    check('主审计流水 gate 事件带 stage', case_main_trail_has_stage)
