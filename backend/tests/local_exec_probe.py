"""本地进程执行器测试专属可执行 helper（只活在 tests，不进入生产）。

用法：``[python, local_exec_probe.py, <mode>, ...]``，按 mode 执行一个
真实子进程行为并退出。所有输出一律经 stdout/stderr，不写 Bundle 外文件。
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time


def _cmd_echo(args: list[str]) -> int:
    sys.stdout.write(json.dumps(args))
    sys.stdout.flush()
    return 0


def _cmd_exit(args: list[str]) -> int:
    return int(args[0])


def _cmd_sigterm_self(args: list[str]) -> int:
    os.kill(os.getpid(), signal.SIGTERM)
    time.sleep(30)
    return 0


def _cmd_sleep(args: list[str]) -> int:
    """sleep <秒> [ignore-term|trap-exit=N|exit-code=N]：超时行为探针。"""
    seconds = float(args[0])
    ignore_term = "ignore-term" in args[1:]
    trap_code = None
    code = 0
    for item in args[1:]:
        if item.startswith("exit-code="):
            code = int(item.split("=", 1)[1])
        elif item.startswith("trap-exit="):
            trap_code = int(item.split("=", 1)[1])
    if trap_code is not None:
        def _trap_exit(signum, frame):
            sys.exit(trap_code)

        signal.signal(signal.SIGTERM, _trap_exit)
    elif ignore_term:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    time.sleep(seconds)
    return code


def _cmd_spawn_child(args: list[str]) -> int:
    """派生一个 sleep 子进程后自己长睡；监督器超时应回收整组。"""
    child_seconds = float(args[0])
    parent_seconds = float(args[1])
    child = subprocess.Popen(
        [sys.executable, __file__, "sleep", args[0]],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    sys.stdout.write(json.dumps({"child_pid": child.pid}))
    sys.stdout.flush()
    time.sleep(parent_seconds)
    child.wait()
    return 0


def _cmd_big_output(args: list[str]) -> int:
    """向 stdout/stderr 各写指定字节（限长与无死锁探针）。"""
    total = int(args[0])
    chunk = b"o" * 65536
    written = 0
    while written < total:
        piece = chunk[: min(len(chunk), total - written)]
        sys.stdout.buffer.write(piece)
        written += len(piece)
    sys.stdout.buffer.flush()
    chunk = b"e" * 65536
    written = 0
    while written < total:
        piece = chunk[: min(len(chunk), total - written)]
        sys.stderr.buffer.write(piece)
        written += len(piece)
    sys.stderr.buffer.flush()
    return 0


def _cmd_env(args: list[str]) -> int:
    sys.stdout.write(json.dumps(dict(os.environ)))
    sys.stdout.flush()
    return 0


def _cmd_cwd(args: list[str]) -> int:
    sys.stdout.write(os.getcwd())
    sys.stdout.flush()
    return 0


_MODES = {
    "echo": _cmd_echo,
    "exit": _cmd_exit,
    "sigterm-self": _cmd_sigterm_self,
    "sleep": _cmd_sleep,
    "spawn-child": _cmd_spawn_child,
    "big-output": _cmd_big_output,
    "env": _cmd_env,
    "cwd": _cmd_cwd,
}


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] not in _MODES:
        sys.stderr.write(f"unknown mode: {argv[1:]!r}\n")
        return 2
    return _MODES[argv[1]](argv[2:])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
