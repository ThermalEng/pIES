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


def _cmd_spawn_and_exit(args: list[str]) -> int:
    """派生 sleep 子进程（继承管道）后立即正常退出；监督器须收口残留组。"""
    child = subprocess.Popen([sys.executable, __file__, "sleep", args[0]])
    sys.stdout.write(json.dumps({"child_pid": child.pid}))
    sys.stdout.flush()
    return 0


def _cmd_spawn_trap(args: list[str]) -> int:
    """派生忽略 TERM 的 sleep 子进程；自己捕获 TERM 立即退出。

    用法：spawn-trap <子秒> <父秒>；监督器超时后父响应 TERM、子须被 KILL。
    """
    child = subprocess.Popen(
        [sys.executable, __file__, "sleep", args[0], "ignore-term"]
    )
    sys.stdout.write(json.dumps({"child_pid": child.pid}))
    sys.stdout.flush()

    def _trap_exit(signum, frame):
        sys.exit(0)

    signal.signal(signal.SIGTERM, _trap_exit)
    time.sleep(float(args[1]))
    return 0


def _cmd_sleep_with_extra(args: list[str]) -> int:
    """先写未声明文件再长睡；超时与输出违规同时发生时的探针。"""
    target = args[0]
    parent = os.path.dirname(target)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(target, "wb") as handle:
        handle.write(b"extra")
    time.sleep(float(args[1]))
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


def _cmd_write_files(args: list[str]) -> int:
    """write-files <path=size|d:dir>...：在工作目录写声明输出文件后退出。

    ``d:<dir>`` 只建空目录（输出配额不计目录探针），与文件规格无冲突
    （文件规格必含 ``=``）。
    """
    for spec in args:
        if spec.startswith("d:"):
            os.makedirs(spec[2:], exist_ok=True)
            continue
        path, size = spec.rsplit("=", 1)
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(b"f" * int(size))
    return 0


def _cmd_mkfifo(args: list[str]) -> int:
    """在工作目录建命名管道后退出（特殊文件边界探针）。"""
    parent = os.path.dirname(args[0])
    if parent:
        os.makedirs(parent, exist_ok=True)
    os.mkfifo(args[0])
    return 0


def _cmd_mksock(args: list[str]) -> int:
    """在工作目录建 socket 节点后退出（特殊文件边界探针）。"""
    import socket

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.bind(args[0])
    finally:
        sock.close()
    return 0


def _cmd_write_and_sock(args: list[str]) -> int:
    """写声明文件并建一个未声明 socket 节点后退出（组合边界探针）。"""
    sockpath = args[-1]
    for spec in args[:-1]:
        path, size = spec.rsplit("=", 1)
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(b"f" * int(size))
    import socket

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.bind(sockpath)
    finally:
        sock.close()
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
    "sleep-with-extra": _cmd_sleep_with_extra,
    "spawn-child": _cmd_spawn_child,
    "spawn-and-exit": _cmd_spawn_and_exit,
    "spawn-trap": _cmd_spawn_trap,
    "big-output": _cmd_big_output,
    "write-files": _cmd_write_files,
    "write-and-sock": _cmd_write_and_sock,
    "mkfifo": _cmd_mkfifo,
    "mksock": _cmd_mksock,
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
