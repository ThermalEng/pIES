"""隔离求解器子进程封装(03 规格 0.2/§9.4、12.2 计算 Worker 职责)。

计算子任务运行在受控隔离子进程, 以支持:
- 资源限制(RLIMIT_AS 内存上限; RLIMIT_CPU 超时兜底);
- 硬超时(SIGTERM → 5 s 宽限 → SIGKILL, 进程组整体终止防孤儿孙进程);
- 取消(调用方传入 cancel_event, 轮询到即立即终止);
- 序列化: 请求/响应经 stdin/stdout 传递 base64(pickle) 数据, 子进程
  只向 stdout 写结果帧, 其余诊断输出一律走 stderr, 保证协议不被污染。

协议帧(单行 base64):
    请求: {"fn": "<模块.函数名>", "args": [...], "timeout_sec": float,
            "mem_limit_mb": int|None, "pythonpath": [父进程 sys.path]}
    响应: {"ok": true, "result": ...}
        | {"ok": false, "timed_out": bool, "canceled": bool,
           "error": str, "traceback": str}

调用示例:
    resp = run_solver_isolated(
        "iesplan.engines.eval_run.evaluate_plan",
        (plan, data, axis, options), timeout_sec=600.0,
    )
    if not resp["ok"]:
        ...  # 超时/取消/异常
    result = resp["result"]  # EvalResult / PlanningResult 等(可 pickle 对象)
"""

from __future__ import annotations

import base64
import importlib
import logging
import os
import pickle
import signal
import subprocess
import sys
import threading
import time
import traceback
from typing import Any

logger = logging.getLogger(__name__)

#: 超时后 SIGTERM 宽限秒数(让求解器写 incumbent 检查点, 03 §6.1)
TERM_GRACE_SECONDS = 5.0
#: 轮询间隔(取消事件/超时探测)
_POLL_INTERVAL = 0.05


def _resolve_callable(fn_name: str) -> Any:
    """按 "模块.路径.函数" 解析可调用对象。"""
    module_path, _, attr = fn_name.rpartition(".")
    if not module_path:
        raise ValueError(f"函数名必须是完整限定名: {fn_name!r}")
    module = importlib.import_module(module_path)
    fn = module
    for part in attr.split("."):
        fn = getattr(fn, part)
    if not callable(fn):
        raise TypeError(f"{fn_name!r} 不可调用")
    return fn


def _terminate_process(proc: subprocess.Popen, *, timed_out: bool) -> None:
    """终止子进程及其进程组(SIGTERM 宽限 → SIGKILL), 防孤儿进程。"""
    if proc.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        proc.terminate()
    deadline = time.monotonic() + TERM_GRACE_SECONDS
    while time.monotonic() < deadline and proc.poll() is None:
        time.sleep(_POLL_INTERVAL)
    if proc.poll() is None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
    try:
        proc.wait(timeout=5.0)
    except subprocess.TimeoutExpired:  # pragma: no cover - 极端情况下兜底
        logger.error("子进程 %s 无法回收(超时=%s)", proc.pid, timed_out)


def run_solver_isolated(
    fn: str | Any,
    args: tuple[Any, ...] = (),
    *,
    timeout_sec: float = 600.0,
    mem_limit_mb: int | None = None,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    """在隔离子进程中执行求解函数(03 §9.4: 资源限制/超时/取消/孤儿清理)。

    参数:
        fn: 函数对象或 "模块.函数" 限定名(子进程内重新导入, 避免闭包不可 pickle)。
        args: 位置参数元组(须可 pickle; numpy 数组/TimeAxis 均可)。
        timeout_sec: 硬超时秒数; 超时 → SIGTERM → SIGKILL。
        mem_limit_mb: 子进程内存上限(MB, 仅 Linux resource 生效); None = 不限制。
        cancel_event: 取消事件; 置位后立即终止子进程(调用方取消信号)。
    返回:
        {"ok": true, "result": ...} 或 {"ok": false, "timed_out": bool,
        "canceled": bool, "error": str, "traceback": str, "elapsed_s": float}。
    """
    fn_name = f"{fn.__module__}.{fn.__qualname__}" if callable(fn) else str(fn)
    request = {
        "fn": fn_name,
        "args": list(args),
        "timeout_sec": float(timeout_sec),
        "mem_limit_mb": mem_limit_mb,
        "pythonpath": list(sys.path),  # 子进程复用父进程导入路径(测试/容器场景)
    }
    payload = base64.b64encode(pickle.dumps(request))
    t0 = time.monotonic()
    proc = subprocess.Popen(
        [sys.executable, "-m", "iesplan.worker.solver_process"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True,  # 独立进程组, 便于整体终止
    )

    try:
        assert proc.stdin is not None
        proc.stdin.write(payload)
        proc.stdin.close()
    except BrokenPipeError as exc:  # 子进程启动即失败
        proc.wait()
        return _fail_response(f"子进程启动失败: {exc}", timed_out=False, canceled=False, t0=t0)

    # 主线程轮询: 支持超时与取消事件(communicate 会阻塞, 无法响应取消)
    deadline = t0 + float(timeout_sec) + TERM_GRACE_SECONDS
    while True:
        if cancel_event is not None and cancel_event.is_set():
            _terminate_process(proc, timed_out=False)
            return _fail_response("任务已取消, 子进程终止", timed_out=False, canceled=True, t0=t0)
        if time.monotonic() > deadline:
            _terminate_process(proc, timed_out=True)
            return _fail_response(
                f"求解超时({timeout_sec:g} s), 已终止子进程", timed_out=True, canceled=False, t0=t0
            )
        if proc.poll() is not None:
            break
        time.sleep(_POLL_INTERVAL)

    out = proc.stdout.read() if proc.stdout is not None else b""
    err = proc.stderr.read() if proc.stderr is not None else b""
    if proc.returncode != 0:
        # 子进程退出码为信息性: 结果帧(stdout)才是权威(如 RLIMIT_AS 命中时
        # 异常捕获后写帧再退出 1), 帧不可解析时回退 stderr
        try:
            response = pickle.loads(base64.b64decode(out))
            if isinstance(response, dict) and not response.get("ok"):
                response["elapsed_s"] = round(time.monotonic() - t0, 3)
                return response
        except Exception:  # noqa: BLE001 - 帧损坏, 回退 stderr
            pass
        return _fail_response(
            f"子进程退出码 {proc.returncode}: {err.decode(errors='replace')[-2000:]}",
            timed_out=False, canceled=False, t0=t0,
        )
    try:
        response = pickle.loads(base64.b64decode(out))
    except Exception as exc:  # noqa: BLE001 - 反序列化失败视为协议错误
        return _fail_response(f"子进程响应解析失败: {exc}: {err.decode(errors='replace')[-500:]}",
                              timed_out=False, canceled=False, t0=t0)
    if not isinstance(response, dict):
        return _fail_response("子进程响应格式非法", timed_out=False, canceled=False, t0=t0)
    response["elapsed_s"] = round(time.monotonic() - t0, 3)
    if not response.get("ok"):
        logger.error("求解子进程失败: %s", response.get("error"))
    return response


def _fail_response(error: str, *, timed_out: bool, canceled: bool, t0: float) -> dict[str, Any]:
    """构造失败响应(统一字段)。"""
    return {
        "ok": False,
        "timed_out": timed_out,
        "canceled": canceled,
        "error": error,
        "traceback": "",
        "elapsed_s": round(time.monotonic() - t0, 3),
    }


# ---------------------------------------------------------------------------
# 子进程测试辅助(供 tests 直接调用, 验证隔离/超时/取消行为)
# ---------------------------------------------------------------------------


def _sleep(seconds: float) -> float:
    """睡眠指定秒数后返回(超时终止测试用)。"""
    time.sleep(seconds)
    return seconds


def _identity(value: Any) -> Any:
    """原样返回(隔离进程往返序列化测试用)。"""
    return value


def _allocate_memory(mb: float) -> int:
    """分配指定 MB 内存并返回字节数(内存限制测试用)。"""
    data = bytearray(int(mb) * 1024 * 1024)
    return len(data)


# ---------------------------------------------------------------------------
# 子进程入口(python -m iesplan.worker.solver_process)
# ---------------------------------------------------------------------------


def _child_main() -> int:
    """子进程主入口: 读请求帧 → 设置资源限制 → 执行 → 写响应帧。

    设计约束: stdout 只承载响应帧(base64 pickle), 日志/错误一律 stderr。
    """
    raw = sys.stdin.buffer.read()
    try:
        request = pickle.loads(base64.b64decode(raw))
    except Exception as exc:  # noqa: BLE001
        _child_write({"ok": False, "error": f"请求帧解析失败: {exc}"})
        return 2

    for path in request.get("pythonpath", []):
        if path and path not in sys.path:
            sys.path.append(path)

    timeout_sec = float(request.get("timeout_sec") or 0.0)
    mem_limit_mb = request.get("mem_limit_mb")
    try:
        # 资源限制(仅类 Unix 提供 resource; 失败不阻断执行, 超时由父进程兜底)
        import resource  # noqa: PLC0415 - 延迟导入, Windows 不可用

        if mem_limit_mb:
            limit = int(mem_limit_mb) * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
        if timeout_sec > 0:
            # CPU 秒数兜底(防无限循环); 超限由内核发 SIGXCPU
            resource.setrlimit(resource.RLIMIT_CPU, (int(timeout_sec) + 30, int(timeout_sec) + 30))
    except (ImportError, ValueError, OSError):  # pragma: no cover - 平台差异
        pass

    try:
        fn = _resolve_callable(request["fn"])
        result = fn(*request["args"])
        _child_write({"ok": True, "result": result})
        return 0
    except BaseException as exc:  # noqa: BLE001 - 子进程边界, 全量捕获转错误帧
        _child_write({
            "ok": False,
            "timed_out": False,
            "canceled": False,
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(),
        })
        return 1


def _child_write(response: dict[str, Any]) -> None:
    """子进程写响应帧(stdout 单行 base64)。"""
    sys.stdout.buffer.write(base64.b64encode(pickle.dumps(response)))
    sys.stdout.buffer.flush()


if __name__ == "__main__":  # pragma: no cover - 子进程入口
    raise SystemExit(_child_main())
