"""Prepare deliverables atomically, roll back failures, and clean old copies."""
from pathlib import Path
import os
import shutil
import uuid
from .storage import inside, file_lock


def publish(state, output_dir, policy, ignore, normalize, prefix, audit=None):
    if not output_dir:
        return {'status': 'skipped'}
    output = Path(output_dir).resolve()
    if output == Path(state.run_dir).resolve():
        return {'status': 'skipped'}
    if policy is None:
        raise ValueError('delivery requires a path policy')
    policy.assert_output_dir_allowed(str(output))
    output.mkdir(parents=True, exist_ok=True)
    with file_lock(output / '.delivery.lock'):
        stage = inside(output, output / ('.staging_' + uuid.uuid4().hex))
        backup = inside(output, output / ('.previous_' + uuid.uuid4().hex))
        stage.mkdir(); backup.mkdir()
        replaced, moved = [], []
        try:
            for name in ('input', 'reports', 'output'):
                src = Path(state.run_dir) / name
                if src.is_dir():
                    shutil.copytree(src, stage / name, ignore=ignore(str(src)))
            normalize(str(stage), prefix)
            for name in ('input', 'reports', 'output'):
                new, dst = stage / name, inside(output, output / name)
                if not new.is_dir():
                    continue
                if dst.exists():
                    os.replace(dst, backup / name)
                    moved.append(name)
                os.replace(new, dst)
                replaced.append(name)
        except Exception:
            for name in reversed(replaced):
                dst = inside(output, output / name)
                if dst.is_dir():
                    shutil.rmtree(dst)
            for name in reversed(moved):
                os.replace(backup / name, output / name)
            if backup.is_dir() and not any(backup.iterdir()):
                shutil.rmtree(backup)
            raise
        finally:
            if stage.is_dir():
                shutil.rmtree(stage)
        cleanup_warning = None
        try:
            if backup.is_dir():
                shutil.rmtree(backup)
        except OSError as exc:
            # Publication is complete and must remain successful. Report a
            # stale backup so an operator can clean it without losing delivery.
            cleanup_warning = str(exc)
        if audit:
            audit.event('consolidate_done', output_dir=str(output), dirs=replaced,
                        stale_backup=(str(backup) if cleanup_warning else None))
        result = {'status': 'completed', 'directories': replaced}
        if cleanup_warning:
            result['cleanup_warning'] = cleanup_warning
        return result
