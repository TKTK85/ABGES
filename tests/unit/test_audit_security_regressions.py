"""Failure-path regression coverage for the 2026-10-21 audit repairs."""
import hashlib
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from abges_runtime import checkpoints, delivery
from abges_runtime.storage import atomic_json, identifier, resolve_run, sha256
from abges_runtime.tasks import input_manifest, validate_task
from abges_mcp import control_plane as cp, service
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'harness'))
from _bootstrap import load_component


@pytest.mark.parametrize('name', ['../escape', '..', '.', '/root', 'C:\\temp', 'a/b', 'a\\b', 'NUL', 'name.', 'bad\x00name'])
def test_path_identifiers_reject_escape(name):
    with pytest.raises(ValueError): identifier(name)


@pytest.mark.parametrize('task', [{'traits': 'ACL'}, {'inputs': []}, {'traits': ['A', 'A']}, {'task_name': '..'}, {'engine_params': []}])
def test_task_types_rejected_before_io(task):
    with pytest.raises(ValueError): validate_task(task)


def test_atomic_json_preserves_previous_on_serialization_failure(tmp_path):
    target = tmp_path / 'state.json'
    atomic_json(target, {'good': True})
    with pytest.raises(TypeError): atomic_json(target, {'bad': object()})
    assert json.loads(target.read_text()) == {'good': True}


def test_nested_and_legacy_run_lookup(tmp_path, monkeypatch):
    for path, run_id in [(tmp_path / 'legacy', 'legacy'), (tmp_path / 'task/_runs/new', 'new')]:
        path.mkdir(parents=True)
        atomic_json(path / 'state.json', {'run_id': run_id, 'status': 'failed'})
    monkeypatch.setattr(service, 'RUN_ROOT', tmp_path)
    assert {r['run_id'] for r in service.list_runs()} == {'new', 'legacy'}
    assert service.get_run_summary('new')['run_id'] == 'new'
    assert [r['run_id'] for r in service.list_runs(1, allowed_ids={'legacy'})] == ['legacy']
    with pytest.raises(ValueError): resolve_run(tmp_path, '../new')


def test_checkpoint_roundtrip_frames_and_result_objects(tmp_path):
    from abges_phenotype import PhenotypeQCResult
    result = PhenotypeQCResult()
    result.frame = pd.DataFrame({'ID': ['001', '002'], 'value': [1.0, float('nan')]})
    data = {'qc': result, 'ids': {'001'}, 'tuple': (1, 2)}
    restored = checkpoints.decode(json.loads(json.dumps(checkpoints.encode(data))))
    pd.testing.assert_frame_equal(restored['qc'].frame, result.frame)
    assert restored['tuple'] == (1, 2) and restored['ids'] == {'001'}
    with pytest.raises(ValueError): checkpoints.decode({'type': 'result', 'module': 'os', 'name': 'system'})


def test_resume_rejects_legacy_pickle_and_changed_inputs(tmp_path):
    State = load_component('state-manager', 'state_manager').StateManager
    input_file = tmp_path / 'input.csv'; input_file.write_text('ID,value\na,1\n')
    task = {
        'task_id': 'safe', 'command': 'run', 'model': 'pblup',
        'traits': ['EN'], 'inputs': {'phenotype': str(input_file)},
    }
    state = State('safe', run_root=str(tmp_path)); state.bind_task(task)
    state.put('values', [1]); state.checkpoint_agent_result('01')
    checkpoint_file = Path(state.blackboard_path)
    state.checkpoint_agent_result('02')
    assert checkpoint_file.exists() and checkpoint_file == Path(state.blackboard_path)
    checkpoint = json.loads(Path(state.state_path).read_text())['resume_checkpoint']
    assert checkpoint['format'] == 'data-json-v1'
    assert checkpoint['blackboard_sha256']
    input_file.write_text('ID,value\na,2\n')
    with pytest.raises(Exception, match='inputs changed'):
        State.resume('safe', run_root=str(tmp_path), expected_task=task)
    payload = json.loads(Path(state.state_path).read_text())
    payload['resume_checkpoint'].pop('format')
    atomic_json(state.state_path, payload)
    with pytest.raises(Exception, match='Legacy pickle'):
        State.resume('safe', run_root=str(tmp_path))


def test_genotype_manifest_hashes_all_physical_files(tmp_path):
    prefix = tmp_path / 'geno'
    for ext in ['.bed', '.bim', '.fam']: Path(str(prefix) + ext).write_bytes(b'abc')
    before = input_manifest({'genotype': str(prefix)})
    bed = Path(str(prefix) + '.bed'); stat = bed.stat()
    bed.write_bytes(b'xyz'); os.utime(bed, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert len(before) == 3 and before != input_manifest({'genotype': str(prefix)})


def test_delivery_copy_failure_retains_both_previous_directories(tmp_path, monkeypatch):
    run, output = tmp_path / 'run', tmp_path / 'published'
    for base in [run, output]:
        for name in ['reports', 'output']:
            (base / name).mkdir(parents=True)
            (base / name / 'value.txt').write_text(base.name)
    original = delivery.shutil.copytree
    def broken(src, dst, **kwargs):
        if Path(src).name == 'output':
            Path(dst).mkdir(); (Path(dst) / 'partial').write_text('partial')
            raise OSError('simulated full disk')
        return original(src, dst, **kwargs)
    monkeypatch.setattr(delivery.shutil, 'copytree', broken)
    with pytest.raises(OSError):
        delivery.publish(SimpleNamespace(run_dir=str(run)), output,
                         SimpleNamespace(assert_output_dir_allowed=lambda p: None),
                         lambda root: None, lambda root, prefix: None, 'test')
    assert all((output / name / 'value.txt').read_text() == 'published' for name in ['reports', 'output'])
    assert not list(output.glob('.previous_*'))


def test_delivery_second_switch_failure_rolls_back_pair(tmp_path, monkeypatch):
    run, output = tmp_path / 'run', tmp_path / 'published'
    for base in [run, output]:
        for name in ['reports', 'output']:
            (base / name).mkdir(parents=True)
            (base / name / 'value.txt').write_text(base.name)
    original = delivery.os.replace
    def broken(src, dst):
        if Path(src).parent.name.startswith('.staging_') and Path(src).name == 'output':
            raise OSError('simulated rename failure')
        return original(src, dst)
    monkeypatch.setattr(delivery.os, 'replace', broken)
    with pytest.raises(OSError):
        delivery.publish(SimpleNamespace(run_dir=str(run)), output,
                         SimpleNamespace(assert_output_dir_allowed=lambda p: None),
                         lambda root: None, lambda root, prefix: None, 'test')
    assert all((output / name / 'value.txt').read_text() == 'published' for name in ['reports', 'output'])
    assert not list(output.glob('.previous_*'))


def test_delivery_cleans_previous_snapshot_after_success(tmp_path):
    run, output = tmp_path / 'run', tmp_path / 'published'
    for root, value in ((run, 'new'), (output, 'old')):
        for folder in ('reports', 'output'):
            target = root / folder
            target.mkdir(parents=True)
            (target / 'value.txt').write_text(value)
    result = delivery.publish(
        SimpleNamespace(run_dir=str(run)), output,
        SimpleNamespace(assert_output_dir_allowed=lambda path: None),
        lambda root: (lambda directory, names: set()),
        lambda root, prefix: None, 'test')
    assert result['status'] == 'completed'
    assert all((output / name / 'value.txt').read_text() == 'new'
               for name in ('reports', 'output'))
    assert not list(output.glob('.previous_*'))
    assert not list(output.glob('.staging_*'))


def setup_control(tmp_path, monkeypatch):
    monkeypatch.setattr(cp, 'CONTROL', tmp_path)
    monkeypatch.setattr(cp, 'JOBS', tmp_path / 'jobs')
    monkeypatch.setattr(cp, 'APPROVALS', tmp_path / 'approvals')
    registry = {user: {'role': role, 'token_sha256': hashlib.sha256(user.encode()).hexdigest()}
                for user, role in [('alice', 'operator'), ('bob', 'operator'), ('approve', 'approver')]}
    monkeypatch.setenv('ABGES_MCP_ACCESS_JSON', json.dumps(registry))


def test_resume_requires_owner_and_target_bound_approval(tmp_path, monkeypatch):
    setup_control(tmp_path, monkeypatch)
    cp._write(cp._job_path('job_old'), {'actor': {'identity': 'alice'}, 'run_id': 'run_old'})
    task = ('task_id: resume_sample\nmodel: pblup\ncommand: run\n'
            'traits: [EN]\ninputs:\n  phenotype: D:/data/sample.tsv\n')
    approval = cp.issue_approval('approve', 'approve', task, resume_run='run_old')
    with pytest.raises(service.GatewayError, match='access denied'):
        cp.submit('bob', 'bob', task, 'run', True, 'run_old', approval['approval_id'], approval['approval_token'])
    assert cp.submit('alice', 'alice', task, 'run', True, 'run_old', approval['approval_id'], approval['approval_token'])['resume_run'] == 'run_old'
    approval = cp.issue_approval('approve', 'approve', task)
    with pytest.raises(service.GatewayError, match='resume target'):
        cp.submit('alice', 'alice', task, 'run', True, 'run_old', approval['approval_id'], approval['approval_token'])


def test_concurrent_claim_has_one_winner_and_stale_write_is_fenced(tmp_path, monkeypatch):
    setup_control(tmp_path, monkeypatch)
    cp._write(cp._job_path('job_race'), {'job_id': 'job_race', 'status': 'queued', 'actor': {'identity': 'alice'}})
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: cp._claim_next_job(), range(4)))
    winners = [r for r in results if r]
    assert len(winners) == 1
    path, job = winners[0]
    stale = dict(job); stale['claim_token'] = 'stale'
    assert not cp._finish_job(path, stale)
    assert not cp._renew_lease(path, 'stale')
    assert cp._renew_lease(path, job['claim_token'])


def test_engine_manifest_missing_and_unregistered_fail_closed(tmp_path, monkeypatch):
    module = load_component('tool-executor', 'tool_executor')
    executor = module.ToolExecutor(None, None)
    monkeypatch.setattr(module, 'ENGINE_MANIFEST_REL', str(tmp_path / 'absent.json'))
    with pytest.raises(Exception, match='manifest'): executor.assert_engine_integrity('hiblup.exe')
    monkeypatch.setattr(executor, '_load_engine_manifest', lambda: [{'path': 'other.exe', 'sha256': 'a' * 64}])
    with pytest.raises(Exception, match='registered'): executor.assert_engine_integrity('hiblup.exe')


@pytest.mark.parametrize('expr', ['X.sum()', 'X.__class__', '2 ** 999999', '__import__(1)', '[X]'])
def test_formula_rejects_calls_attributes_and_resource_expressions(expr):
    from abges_traits import _safe_expression
    with pytest.raises(Exception): _safe_expression(expr, {'X': pd.Series([1.0])})


def test_formula_preserves_vector_arithmetic():
    from abges_traits import _safe_expression
    assert _safe_expression('X / 2 + 1', {'X': pd.Series([2.0, 4.0])}).tolist() == [2.0, 3.0]


def test_live_worker_not_requeued_on_delayed_heartbeat(tmp_path, monkeypatch):
    setup_control(tmp_path, monkeypatch)
    cp._write(cp._running_path('job_live'), {'job_id': 'job_live', 'status': 'running',
              'attempt': 1, 'worker_pid': os.getpid(), 'lease_expires_at': '2000-01-01T00:00:00'})
    assert cp.recover_stale_jobs() == []


def test_subprocess_output_bound_and_timeout():
    import subprocess
    from abges_runtime.processes import run_process
    with pytest.raises(ValueError, match='bounded'):
        run_process([sys.executable, '-c', 'print("x" * 1000)'], timeout=10, max_bytes=100)
    with pytest.raises(subprocess.TimeoutExpired):
        run_process([sys.executable, '-c', 'import time;time.sleep(20)'], timeout=0.1)


def test_grm_cache_invalidates_on_content_and_engine_parameters(tmp_path, monkeypatch):
    import abges_grm_cache as cache
    prefix = str(tmp_path / 'geno')
    for suffix in ('.bed', '.bim', '.fam'): Path(prefix + suffix).write_bytes(b'abc')
    binary = tmp_path / 'hiblup.exe'; binary.write_bytes(b'test binary')
    monkeypatch.setattr(cache.hiblup_tool, 'resolve_hiblup_binary', lambda _: str(binary))
    calls = []
    def build(genotype_prefix, output_prefix, **kwargs):
        paths = cache._grm_file_set(output_prefix, kwargs['matrix_type'])
        for path in paths: Path(path).write_bytes(b'matrix')
        calls.append(kwargs)
        return {'files': paths, 'duration': 1.0}
    monkeypatch.setattr(cache, 'build_grm', build)
    executor = SimpleNamespace(assert_engine_integrity=lambda *a, **k: True)
    options = dict(strain_id='line', genotype_prefix=prefix, cache_dir=str(tmp_path / 'cache'), executor=executor)
    initial = cache.get_or_build_grm(**options)
    assert not initial['cached']
    assert cache.get_or_build_grm(**options)['cached']
    bed = Path(prefix + '.bed'); stat = bed.stat()
    bed.write_bytes(b'xyz'); os.utime(bed, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert not cache.get_or_build_grm(**options)['cached']
    rebuilt = cache.get_or_build_grm(**options, alpha=0.1)
    assert not rebuilt['cached']
    assert rebuilt['prefix'] != initial['prefix']
    assert Path(initial['prefix'] + '.bin').read_bytes() == b'matrix'
    assert len(calls) == 3


def test_unapproved_grm_execution_cannot_call_engine(tmp_path, monkeypatch):
    import abges_grm_cache as cache
    prefix = str(tmp_path / 'geno')
    for suffix in ('.bed', '.bim', '.fam'): Path(prefix + suffix).write_bytes(b'abc')
    monkeypatch.setattr(cache.hiblup_tool, 'resolve_hiblup_binary', lambda _: 'hiblup.exe')
    monkeypatch.setattr(cache.hiblup_tool, 'run_hiblup', lambda *a, **k: pytest.fail('engine executed'))
    with pytest.raises(Exception, match='ToolExecutor'):
        cache.build_grm(prefix, str(tmp_path / 'matrix'))
