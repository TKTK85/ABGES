"""Bounded subprocess output and descendant cleanup on timeout."""
import os
import signal
import subprocess
import tempfile


def run_process(args, timeout, cwd=None, env=None, max_bytes=4 * 1024 * 1024):
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        process = subprocess.Popen(args, cwd=cwd, env=env, stdout=stdout, stderr=stderr,
                                   start_new_session=os.name != 'nt')
        try:
            process.wait(timeout=timeout)
        except BaseException:
            if os.name == 'nt':
                subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
            else:
                try: os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError: pass
            process.wait(timeout=30)
            raise
        stdout.seek(0); stderr.seek(0)
        out, err = stdout.read(max_bytes + 1), stderr.read(max_bytes + 1)
        if len(out) > max_bytes or len(err) > max_bytes:
            raise ValueError('subprocess output exceeded its bounded response limit')
        return subprocess.CompletedProcess(args, process.returncode,
                                           out.decode('utf-8', errors='replace'),
                                           err.decode('utf-8', errors='replace'))
