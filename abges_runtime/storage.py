"""Bounded paths, atomic persistence and process-safe local file locks."""
from contextlib import contextmanager
from pathlib import Path, PureWindowsPath
import hashlib
import json
import os
import re
import tempfile
import threading
import time


def identifier(value):
    if (not isinstance(value, str) or not value or len(value) > 160
            or value in {'.', '..'} or value[-1:] in {' ', '.'}
            or re.search(r'[\x00-\x1f<>:"/\\|?*]', value)
            or PureWindowsPath(value).is_absolute()
            or value.split('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL',
                                              *('COM%d' % i for i in range(1, 10)),
                                              *('LPT%d' % i for i in range(1, 10))}):
        raise ValueError('identifier must be one safe directory name')
    return value


def inside(root, path):
    root, path = Path(root).resolve(), Path(path).resolve()
    if path == root or not path.is_relative_to(root):
        raise ValueError('path must remain beneath its authorized root')
    return path


def atomic_json(path, payload, default=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Serialize before touching any existing file.
    content = json.dumps(payload, ensure_ascii=False, indent=2, default=default)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return str(path)


def sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def run_directories(root):
    root = Path(root).resolve()
    candidates = list(root.glob('*/state.json')) + list(root.glob('*/_runs/*/state.json'))
    result = []
    for path in candidates:
        try:
            result.append(inside(root, path.parent))
        except ValueError:
            continue
    return result


def resolve_run(root, run_id):
    identifier(run_id)
    matches = [p for p in run_directories(root) if p.name == run_id]
    if len(matches) != 1:
        raise ValueError('run_id not found or ambiguous')
    return matches[0]


_locks = {}
_locks_guard = threading.Lock()


@contextmanager
def file_lock(path, timeout=30):
    """OS releases the lock on crash; distinct threads also serialize."""
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _locks_guard:
        lock = _locks.setdefault(str(path), threading.Lock())
    if not lock.acquire(timeout=timeout):
        raise TimeoutError('local transaction lock timed out')
    acquired = False
    try:
        with path.open('a+b') as handle:
            handle.seek(0, 2)
            if handle.tell() == 0:
                handle.write(b'0'); handle.flush()
            deadline = time.monotonic() + timeout
            while True:
                try:
                    handle.seek(0)
                    if os.name == 'nt':
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError('local transaction lock timed out')
                    time.sleep(0.02)
            try:
                yield
            finally:
                if acquired:
                    handle.seek(0)
                    if os.name == 'nt':
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(handle, fcntl.LOCK_UN)
    finally:
        lock.release()
