"""0.9-F：隔离 solver runner + 文件交换执行的 Docker 集成证据。

本文件只有一部分是纯单元测试；标 ``needs_docker`` 的测试驱动宿主
Docker（测试线束，非生产实现），需要以下线束条件：

- 测试容器挂载宿主 Docker Socket（仅线束需要；生产 runner 绝不挂载）；
- 命名卷 ``pies-09f-xchg`` 挂载到测试容器的 ``/xchg``（与 runner 共享
  的交换卷；生产由部署预配置）；
- 镜像 ``pies-09f-test:tmp``（可用 ``PIES_TEST_IMAGE`` 覆盖）。

示例：

.. code-block:: console

    docker volume create pies-09f-xchg
    docker run --rm \\
        -v /var/run/docker.sock:/var/run/docker.sock \\
        -v pies-09f-xchg:/xchg \\
        pies-09f-test:tmp \\
        python -m pytest tests/test_isolated_runner.py -q

线束条件不满足时 docker 测试跳过并说明原因；单元测试恒运行。
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

import pytest

try:
    import docker as _docker_sdk
    from docker.errors import DockerException as _DockerException
except ImportError:  # pragma: no cover - 线束缺 SDK 即跳过
    _docker_sdk = None
    _DockerException = Exception

from iesplan.assembly import validate_assembly_text
from iesplan.computation import (
    HIGHS_EXECUTABLE_REF,
    HIGHS_MILP_PROVIDER,
    AttemptContext,
    CalculationConfig,
    ComputationUnavailableError,
    DeploymentPolicy,
    ExecutorDescriptor,
    FileExchangeExecutorProvider,
    SolverRuntime,
    build_executor_registry,
    build_registry,
    materialize_non_predictive_series,
)
from iesplan.computation.generators.highs_milp import (
    GENERATOR_REF,
    SOLUTION_PATH,
    SOLVER_REF,
)
from iesplan.computation.runtime.file_exchange import (
    heartbeat_fresh,
    read_heartbeat,
)
from iesplan.devices import init_registry, list_devices
from iesplan.finance import effective_from_yaml
from iesplan.modeling import build_math_contribution

HERE = Path(__file__).resolve().parent
BACKEND_ROOT = HERE.parent
SAMPLES = BACKEND_ROOT / "iesplan" / "assembly" / "samples"

RUNNER_IMAGE = os.environ.get("PIES_TEST_IMAGE", "pies-09f-test:tmp")
EXCHANGE_ROOT = "/xchg"
VOLUME_NAME = "pies-09f-xchg"

EXECUTOR_ID = "ies.executor.local_process"
EXECUTOR_VERSION = "1.0.0"

needs_docker = pytest.mark.skipif(_docker_sdk is None, reason="缺 docker SDK（线束条件）")


def _client():
    """线束 Docker 客户端（生产代码绝不使用）。"""
    assert _docker_sdk is not None
    return _docker_sdk.from_env(timeout=60)


def _require_harness() -> str:
    """线束就绪检查；返回本次测试独占的交换根。"""
    try:
        _client().ping()
    except _DockerException:
        pytest.skip("宿主 Docker 不可达（线束条件）")
    if not os.path.isdir(EXCHANGE_ROOT):
        pytest.skip("交换卷未挂载到 /xchg（线束条件：-v pies-09f-xchg:/xchg）")
    root = os.path.join(EXCHANGE_ROOT, uuid.uuid4().hex)
    os.makedirs(root, exist_ok=False)
    # 部署期预配语义由线束模拟：交换根归 runner 身份可写，
    # 客户端（root）不受影响；attempt 子目录由 provider 置 777。
    os.chmod(root, 0o777)
    return root


def _campus_bundle(time_limit_seconds: int = 600):
    """正式路径生成真实 campus Bundle（非手写 manifest）。"""
    init_registry()
    text = (SAMPLES / "valid" / "campus.assembly.yaml").read_text(encoding="utf-8")
    validated = validate_assembly_text(text, package_dir=SAMPLES)
    assert validated.ok
    artifact = validated.artifact
    contributions = {}
    for doc in list_devices():
        built = build_math_contribution(doc)
        assert built.ok
        contributions[doc.device.id] = built.contribution
    doc = json.loads(artifact.canonical_text)
    models = {inst: contributions[dev["definition"]["id"]] for inst, dev in doc["devices"].items()}
    finance = effective_from_yaml(
        (SAMPLES / "finance" / "campus.effective-finance.yaml").read_text(encoding="utf-8")
    )

    def collect(node, found: set):
        if isinstance(node, dict):
            content = node.get("content")
            if isinstance(content, dict) and "object_id" in content:
                found.add(content["object_id"])
            for value in node.values():
                collect(value, found)
        elif isinstance(node, list):
            for value in node:
                collect(value, found)

    object_ids: set = set()
    collect(doc["devices"], object_ids)
    resources = {}
    for object_id in sorted(object_ids):
        rel = object_id[5:] if object_id.startswith("file:") else object_id
        resources[object_id] = (SAMPLES / rel).read_bytes()
    mat = materialize_non_predictive_series(artifact, resources, evaluation_years=1)
    assert not mat.pending_predict
    config = CalculationConfig(
        mode="planning",
        generator=GENERATOR_REF,
        solver=SOLVER_REF,
        precision="medium",
        time_limit_seconds=time_limit_seconds,
        outputs=["solution", "metrics"],
        seed=None,
    )
    provider = build_registry((HIGHS_MILP_PROVIDER,)).resolve(config)
    return provider.generate(artifact, models, mat.series, finance, config)


def _ipc_provider(exchange_dir: str) -> FileExchangeExecutorProvider:
    return FileExchangeExecutorProvider(
        ExecutorDescriptor(
            executor_id=EXECUTOR_ID,
            version=EXECUTOR_VERSION,
            executables=(HIGHS_EXECUTABLE_REF,),
        ),
        exchange_dir,
    )


def _runtime(exchange_dir: str) -> SolverRuntime:
    policy = DeploymentPolicy(
        executables=(HIGHS_EXECUTABLE_REF,),
        environment_vars=(),
        max_timeout_seconds=600,
        max_memory_mib=4096,
        max_cpu_cores=4,
        allow_network=False,
        max_output_files=16,
        max_output_bytes=8_388_608,
    )
    return SolverRuntime(build_executor_registry([_ipc_provider(exchange_dir)]), policy)


def _attempt() -> AttemptContext:
    return AttemptContext(
        attempt_id="attempt-09f",
        bundle_id="bundle-09f",
        assembly_id="assembly-09f",
        calculation_config_id="config-09f",
    )


class _Runner:
    """测试驱动的隔离 runner 容器（线束，非生产代码）。"""

    def __init__(self, exchange_dir: str, name: str) -> None:
        self.exchange_dir = exchange_dir
        self.name = name
        self.container = None

    def start(self, memory: str = "512m") -> _Runner:
        assert _docker_sdk is not None
        # runner 与 provider 认领同一交换根：容器侧路径为卷下同名子目录。
        runner_exchange = "/exchange/" + os.path.basename(self.exchange_dir)
        self.container = _client().containers.run(
            RUNNER_IMAGE,
            [
                "python",
                "-m",
                "iesplan.computation.runtime.runner_service",
                runner_exchange,
                "/work",
            ],
            detach=True,
            name=self.name,
            network_mode="none",
            user="65534",
            read_only=True,
            # /work 归 runner 身份所有，否则建 attempt 子目录无权限。
            tmpfs={"/tmp": "", "/work": "uid=65534,gid=65534,mode=0755"},
            mem_limit=memory,
            nano_cpus=2_000_000_000,
            pids_limit=128,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
            volumes={VOLUME_NAME: {"bind": "/exchange", "mode": "rw"}},
        )
        return self

    def wait_ready(self, timeout: float = 120.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if heartbeat_fresh(self.exchange_dir):
                return
            time.sleep(0.2)
        raise AssertionError("runner 心跳未就绪（启动自检未通过）")

    def exec(self, *args: str) -> str:
        assert self.container is not None
        code, output = self.container.exec_run(list(args))
        assert code == 0, output.decode("utf-8", "replace")[-500:]
        return output.decode("utf-8").strip()

    def inspect(self) -> dict:
        assert self.container is not None
        self.container.reload()
        return dict(self.container.attrs)

    def stop(self) -> None:
        if self.container is not None:
            try:
                self.container.remove(force=True)
            except _DockerException:
                pass
            self.container = None

    def pids_current(self) -> int:
        return int(self.exec("cat", "/sys/fs/cgroup/pids.current"))

    def work_empty(self) -> bool:
        return self.exec("ls", "-A", "/work") == ""


def _assert_exchange_drained(exchange_dir: str) -> None:
    """交换根无残留 attempt 目录（成功即清理，异常保留现场）。"""
    left = [entry for entry in os.listdir(exchange_dir) if os.path.isdir(os.path.join(exchange_dir, entry))]
    assert left == []


def _run_worker(exchange_dir: str, bundle, stop=None, timeout: float = 600.0):
    """worker 侧执行（带测试线看门狗，防永久挂起）。"""
    outcome: dict = {}

    def target() -> None:
        try:
            outcome["result"] = _runtime(exchange_dir).run(bundle, _attempt(), stop=stop)
        except Exception as exc:  # noqa: BLE001 - 收拢给断言
            outcome["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(timeout=timeout)
    assert not thread.is_alive(), "worker 执行挂起（runner 失联或挂死）"
    if "error" in outcome:
        raise outcome["error"]
    return outcome["result"]


def test_exchange_envelope_roundtrip_unit(tmp_path) -> None:
    """信封纯单元：请求装配/读取、收据写读往返（无 Docker）。"""
    from iesplan.computation.protocols import ProcessOutcome
    from iesplan.computation.runtime.file_exchange import (
        build_request_bundle,
        read_receipt_envelope,
        read_request,
        write_receipt_envelope,
    )

    workdir = tmp_path / "work"
    (workdir / "input").mkdir(parents=True)
    (workdir / "input" / "problem.mps").write_bytes(b"NAME\n")
    (workdir / "output").mkdir()
    target = tmp_path / "attempt"
    command = {
        "executor": "ies.executor.local_process@1.0.0",
        "executable": HIGHS_EXECUTABLE_REF,
        "arguments": ["input/problem.mps"],
        "timeout_seconds": 10,
    }
    build_request_bundle(command, str(workdir), str(target))
    assert read_request(str(target))["executable"] == HIGHS_EXECUTABLE_REF
    assert (target / "input" / "problem.mps").read_bytes() == b"NAME\n"
    outcome = ProcessOutcome(
        exit_code=0,
        termination_reason="exited",
        started_at="2026-09-26T00:00:00Z",
        ended_at="2026-09-26T00:00:01Z",
        stdout=b"ok",
        stderr=b"",
        resource_usage={
            "applied": {"memory_limit_bytes": 1},
            "observed": {"oom_kill_delta": 2},
        },
    )
    write_receipt_envelope(str(target), outcome=outcome)
    back, unexecuted = read_receipt_envelope(str(target))
    assert unexecuted == ""
    assert back is not None and back.exit_code == 0
    assert back.stdout == b"ok"
    assert back.resource_usage["applied"]["memory_limit_bytes"] == 1
    assert back.resource_usage["observed"]["oom_kill_delta"] == 2
    write_receipt_envelope(str(target), outcome=None, unexecuted="fenced")
    back, unexecuted = read_receipt_envelope(str(target))
    assert back is None and unexecuted == "fenced"


def test_exchange_rejects_bad_envelopes_unit(tmp_path) -> None:
    """非法信封一律拒绝（无 Docker）。"""
    from iesplan.computation.runtime.file_exchange import (
        read_receipt_envelope,
        read_request,
    )

    target = tmp_path / "attempt"
    target.mkdir()
    with pytest.raises(ValueError, match="信封"):
        read_request(str(target))
    (target / "command.json").write_text('{"schema": "bogus"}', "utf-8")
    with pytest.raises(ValueError, match="信封"):
        read_request(str(target))
    (target / "receipt.json").write_text("not-json{", "utf-8")
    with pytest.raises(ValueError, match="信封"):
        read_receipt_envelope(str(target))
    (target / "receipt.json").write_text(
        '{"schema": "ies.ipc.exchange.v1", "outcome": {}, "unexecuted": "x"}',
        "utf-8",
    )
    with pytest.raises(ValueError, match="二选一"):
        read_receipt_envelope(str(target))


def test_receipt_rejects_string_truncated_flags_unit(tmp_path) -> None:
    """截断标记字符串 "false" 不得 bool() 误转为 True，直传由类型检查拒绝。"""
    from iesplan.computation.runtime.file_exchange import (
        read_receipt_envelope,
    )

    target = tmp_path / "attempt"
    target.mkdir()
    (target / "receipt.json").write_text(
        json.dumps(
            {
                "schema": "ies.ipc.exchange.v1",
                "outcome": {
                    "exit_code": 0,
                    "signal_number": None,
                    "termination_reason": "exited",
                    "started_at": "2026-09-26T00:00:00Z",
                    "ended_at": "2026-09-26T00:00:01Z",
                    "stdout": "",
                    "stderr": "",
                    "stdout_truncated": "false",
                    "stderr_truncated": "false",
                    "resource_usage": {},
                },
            }
        ),
        "utf-8",
    )
    with pytest.raises(ValueError, match="响应过程结果非法"):
        read_receipt_envelope(str(target))


def test_provider_unavailable_without_exchange(tmp_path) -> None:
    """交换目录缺失即 unavailable，执行明确拒绝（无 Docker）。"""
    provider = _ipc_provider(str(tmp_path / "nope"))
    assert provider.available() is False
    with pytest.raises(ComputationUnavailableError) as exc_info:
        provider.execute({"executor": "x"}, str(tmp_path))
    assert exc_info.value.reason == "no-exchange"


def _write_heartbeat_file(exchange, updated_at: str) -> None:
    (exchange / "heartbeat.json").write_text(
        json.dumps({"schema": "ies.ipc.exchange.v1", "updated_at": updated_at}),
        "utf-8",
    )


def test_provider_rejects_stale_heartbeat(tmp_path) -> None:
    """心跳过期即 unavailable，注册表原子拒绝，执行拒不递交（无 Docker）。"""
    exchange = tmp_path / "xchg"
    exchange.mkdir()
    _write_heartbeat_file(exchange, "2020-01-01T00:00:00+00:00")
    assert read_heartbeat(str(exchange)) is not None
    assert not heartbeat_fresh(str(exchange))
    provider = _ipc_provider(str(exchange))
    assert provider.available() is False
    with pytest.raises(ComputationUnavailableError) as exc_info:
        build_executor_registry([provider])
    assert exc_info.value.reason == "no-executor"
    with pytest.raises(ComputationUnavailableError) as exc_info:
        provider.execute({"executor": "x"}, str(tmp_path))
    assert exc_info.value.reason == "runner-unreachable"


def test_provider_rejects_missing_heartbeat(tmp_path) -> None:
    """心跳缺失即 unavailable，注册表原子拒绝（无 Docker）。"""
    exchange = tmp_path / "xchg"
    exchange.mkdir()
    provider = _ipc_provider(str(exchange))
    assert provider.available() is False
    with pytest.raises(ComputationUnavailableError) as exc_info:
        build_executor_registry([provider])
    assert exc_info.value.reason == "no-executor"


def test_fetch_never_overwrites_pre_existing_unit(tmp_path) -> None:
    """取回永不覆盖递交前已有文件：0444 输入保留，新文件落盘（无 Docker，
    不依赖任何特权身份）。"""
    from iesplan.computation.runtime.file_exchange import _relative_files

    provider = _ipc_provider(str(tmp_path / "xchg-not-used"))
    workdir = tmp_path / "work"
    (workdir / "input").mkdir(parents=True)
    staged = workdir / "input" / "problem.mps"
    staged.write_bytes(b"staged-bytes")
    staged.chmod(0o444)
    pre_existing = _relative_files(str(workdir))
    assert pre_existing == {"input/problem.mps"}
    attempt = tmp_path / "attempt"
    (attempt / "input").mkdir(parents=True)
    (attempt / "input" / "problem.mps").write_bytes(b"runner-bytes")
    (attempt / "output").mkdir()
    (attempt / "output" / "solution.sol").write_bytes(b"sol")
    provider._fetch_outputs(str(attempt), str(workdir), pre_existing)
    assert staged.read_bytes() == b"staged-bytes"
    assert (workdir / "output" / "solution.sol").read_bytes() == b"sol"


@needs_docker
def test_runner_success_end_to_end() -> None:
    """真实 campus Bundle 经隔离 runner 成功求解（默认 512m）。"""
    exchange = _require_harness()
    runner = _Runner(exchange, f"pies-09f-ok-{uuid.uuid4().hex[:8]}")
    try:
        runner.start(memory="512m").wait_ready()
        bundle = _campus_bundle()
        receipt, outputs = _run_worker(exchange, bundle)
        assert receipt.status == "succeeded"
        assert receipt.executor_ref == f"{EXECUTOR_ID}@{EXECUTOR_VERSION}"
        assert receipt.executable_ref == HIGHS_EXECUTABLE_REF
        assert receipt.exit_code == 0
        assert receipt.signal_number is None
        assert receipt.termination_reason == "exited"
        assert set(outputs) == {SOLUTION_PATH}
        text = outputs[SOLUTION_PATH].decode("utf-8")
        assert text.startswith("HIGHS_SOLUTION_V1\n")
        assert "model_status: HighsModelStatus.kOptimal" in text
        usage = dict(receipt.resource_usage)
        assert set(usage) == {"applied"}
        applied = usage["applied"]
        assert applied["memory_limit_bytes"] == 512 * 1024 * 1024
        assert applied["pids_limit"] == 128
        assert math.isclose(applied["cpu_limit_cores"], 2.0)
        _assert_exchange_drained(exchange)
    finally:
        runner.stop()


@needs_docker
def test_runner_container_config_proves_isolation() -> None:
    """容器配置断言隔离部署事实（用户/网络/只读/挂载/无凭证/无socket）。"""
    exchange = _require_harness()
    runner = _Runner(exchange, f"pies-09f-cfg-{uuid.uuid4().hex[:8]}")
    try:
        runner.start(memory="512m").wait_ready()
        info = runner.inspect()
        config = info["Config"]
        host_config = info["HostConfig"]
        assert config["User"] == "65534"
        # network=none 即挂载空网络：键为 none 且无分配 IP；
        # 叠加命名空间内仅 lo 网卡与 runner 启动期命名空间自检，
        # 共同证明真实断网而非声明。
        networks = info["NetworkSettings"]["Networks"]
        assert set(networks) == {"none"}
        assert not networks["none"].get("IPAddress")
        assert runner.exec("ls", "/sys/class/net") == "lo"
        assert host_config["ReadonlyRootfs"] is True
        assert host_config["Memory"] == 512 * 1024 * 1024
        assert host_config["PidsLimit"] == 128
        assert host_config.get("CapDrop") == ["ALL"]
        assert "no-new-privileges:true" in (host_config.get("SecurityOpt") or [])
        env = config["Env"] or []
        assert not any(
            key.startswith(("IESPLAN_DB_", "IESPLAN_REDIS", "IESPLAN_SECRET"))
            for key in (item.partition("=")[0] for item in env)
        )
        mounts = info["Mounts"]
        assert any(
            mount.get("Type") == "volume"
            and mount.get("Name") == VOLUME_NAME
            and mount.get("Destination") == "/exchange"
            for mount in mounts
        )
        assert not any(
            "docker.sock" in str(mount.get("Source", ""))
            or "docker.sock" in str(mount.get("Destination", ""))
            for mount in mounts
        )
        tmpfs = host_config.get("Tmpfs") or {}
        assert "/tmp" in tmpfs and "/work" in tmpfs
        assert runner.exec("id", "-u") == "65534"
        assert runner.exec("whoami") == "nobody"
    finally:
        runner.stop()


@needs_docker
def test_runner_oom_end_to_end() -> None:
    """160m runner 真实 OOM：reason oom + 证据，不猜测。"""
    exchange = _require_harness()
    runner = _Runner(exchange, f"pies-09f-oom-{uuid.uuid4().hex[:8]}")
    try:
        runner.start(memory="160m").wait_ready()
        bundle = _campus_bundle()
        receipt, outputs = _run_worker(exchange, bundle, timeout=420.0)
        assert receipt.status == "process_failed"
        assert receipt.termination_reason == "oom"
        assert receipt.exit_code is None
        assert receipt.signal_number == 9
        assert "OOM" in receipt.error
        usage = dict(receipt.resource_usage)
        assert set(usage) == {"applied", "observed"}
        assert usage["observed"]["oom_kill_delta"] >= 1
        assert usage["applied"]["memory_limit_bytes"] == 160 * 1024 * 1024
        assert outputs == {}
        assert tuple(receipt.outputs) == ()
        # runner 存活并可继续服务：心跳新鲜、工作目录清空。
        assert heartbeat_fresh(exchange)
        assert runner.work_empty()
        _assert_exchange_drained(exchange)
    finally:
        runner.stop()


@needs_docker
def test_runner_cancel_no_orphans() -> None:
    """运行中取消：cancelled 收尾、无孤儿、runner 继续服务。"""
    exchange = _require_harness()
    runner = _Runner(exchange, f"pies-09f-cancel-{uuid.uuid4().hex[:8]}")
    try:
        runner.start(memory="512m").wait_ready()
        baseline_pids = runner.pids_current()
        bundle = _campus_bundle()
        stop_at = time.monotonic() + 1.5
        receipt, outputs = _run_worker(
            exchange,
            bundle,
            stop=lambda: "cancelled" if time.monotonic() >= stop_at else None,
        )
        assert receipt.status == "cancelled"
        assert receipt.termination_reason == "cancelled"
        assert outputs == {}
        assert runner.work_empty()
        assert runner.pids_current() == baseline_pids
        assert heartbeat_fresh(exchange)
        second, second_outputs = _run_worker(exchange, bundle)
        assert second.status == "succeeded"
        assert set(second_outputs) == {SOLUTION_PATH}
    finally:
        runner.stop()


@needs_docker
def test_runner_serves_two_attempts_serially() -> None:
    """同一长驻 runner 连续两个 attempt：均成功、工作目录清空、无孤儿、
    resource_usage 各自只有 applied 层。"""
    exchange = _require_harness()
    runner = _Runner(exchange, f"pies-09f-reuse-{uuid.uuid4().hex[:8]}")
    try:
        runner.start(memory="512m").wait_ready()
        baseline_pids = runner.pids_current()
        bundle = _campus_bundle()
        for _ in range(2):
            receipt, outputs = _run_worker(exchange, bundle)
            assert receipt.status == "succeeded"
            assert receipt.termination_reason == "exited"
            assert set(outputs) == {SOLUTION_PATH}
            assert set(dict(receipt.resource_usage)) == {"applied"}
            assert runner.work_empty()
            assert runner.pids_current() == baseline_pids
            assert heartbeat_fresh(exchange)
            _assert_exchange_drained(exchange)
    finally:
        runner.stop()


@needs_docker
def test_runner_timeout_no_orphans() -> None:
    """正式短超时 Bundle：timed_out 收尾、无孤儿。"""
    exchange = _require_harness()
    runner = _Runner(exchange, f"pies-09f-timeout-{uuid.uuid4().hex[:8]}")
    try:
        runner.start(memory="512m").wait_ready()
        baseline_pids = runner.pids_current()
        bundle = _campus_bundle(time_limit_seconds=2)
        assert bundle.manifest.command["timeout_seconds"] == 2
        receipt, outputs = _run_worker(exchange, bundle)
        assert receipt.status == "timed_out"
        assert receipt.termination_reason == "timeout"
        assert outputs == {}
        assert runner.work_empty()
        assert runner.pids_current() == baseline_pids
    finally:
        runner.stop()


def _run_runner_once(extra_kwargs, timeout: int = 120, command=None):
    """以给定覆盖启动一次 runner，返回（退出码，日志尾）。"""
    assert _docker_sdk is not None
    if command is None:
        command = [
            "python",
            "-m",
            "iesplan.computation.runtime.runner_service",
            "/exchange",
            "/work",
        ]
    kwargs = {
        "detach": True,
        "name": f"pies-09f-neg-{uuid.uuid4().hex[:8]}",
        "network_mode": "none",
        "read_only": True,
        "tmpfs": {"/tmp": "", "/work": ""},
        "volumes": {VOLUME_NAME: {"bind": "/exchange", "mode": "rw"}},
    }
    kwargs.update(extra_kwargs)
    container = _client().containers.run(RUNNER_IMAGE, command, **kwargs)
    try:
        result = container.wait(timeout=timeout)
        logs = container.logs(stderr=True).decode("utf-8", "replace")[-500:]
        return int(result.get("StatusCode", -1)), logs
    finally:
        try:
            container.remove(force=True)
        except _DockerException:
            pass


@needs_docker
def test_runner_refuses_root() -> None:
    """以 root 启动即拒绝服务（非 root 门禁真实）。"""
    _require_harness()
    code, logs = _run_runner_once({"user": "0"})
    assert code != 0
    assert "root" in logs


@needs_docker
def test_runner_refuses_unwritable_exchange() -> None:
    """交换目录不可写即原子拒绝，不留心跳与假就绪。"""
    from iesplan.computation.runtime.file_exchange import HEARTBEAT_FILE

    exchange = _require_harness()
    narrow = os.path.join(exchange, "narrow")
    # root 0755 子目录：runner 身份（65534）可进不可写。
    os.makedirs(narrow, exist_ok=False)
    container_path = "/exchange/" + os.path.basename(exchange) + "/narrow"
    code, logs = _run_runner_once(
        {
            "user": "65534",
            "tmpfs": {"/tmp": "", "/work": "uid=65534,gid=65534,mode=0755"},
        },
        command=[
            "python",
            "-m",
            "iesplan.computation.runtime.runner_service",
            container_path,
            "/work",
        ],
    )
    assert code != 0
    assert "ies-solver-runner" in logs
    assert not os.path.exists(os.path.join(narrow, HEARTBEAT_FILE))


def test_provider_pre_stop_raises_without_submit(tmp_path) -> None:
    """启动前停止即抛取消原因，不装配请求（无 Docker）。"""
    exchange = tmp_path / "xchg"
    exchange.mkdir()
    provider = _ipc_provider(str(exchange))
    with pytest.raises(ComputationUnavailableError) as exc_info:
        provider.execute({"executor": "x"}, str(tmp_path), stop=lambda: "fenced")
    assert exc_info.value.reason == "fenced"
    assert list(exchange.iterdir()) == []


def _runner_identity() -> dict:
    """与线束 provider 描述符匹配的 runner 心跳身份。"""
    return {
        "executor_ref": f"{EXECUTOR_ID}@{EXECUTOR_VERSION}",
        "executable_refs": [HIGHS_EXECUTABLE_REF],
        "applied": {
            "memory_limit_bytes": 1,
            "cpu_limit_cores": 1.0,
            "pids_limit": 1,
        },
    }


def _write_fresh_heartbeat(exchange, identity: dict | None = None) -> None:
    """写新鲜心跳；默认带匹配身份，显式传参可构造错配。"""
    from datetime import UTC as _UTC

    payload = {"schema": "ies.ipc.exchange.v1", "updated_at": datetime.now(_UTC).isoformat()}
    payload.update(identity if identity is not None else _runner_identity())
    (exchange / "heartbeat.json").write_text(json.dumps(payload), "utf-8")


def test_heartbeat_fresh_and_stale_unit(tmp_path) -> None:
    """心跳读写与失活判定（无 Docker；时间确定性）。"""
    from iesplan.computation.runtime.file_exchange import (
        HEARTBEAT_STALE_AFTER_SECONDS,
        read_heartbeat_identity,
    )
    from iesplan.computation.runtime.runner_service import _write_heartbeat

    exchange = tmp_path / "xchg"
    exchange.mkdir()
    assert read_heartbeat(str(exchange)) is None
    assert not heartbeat_fresh(str(exchange))
    assert read_heartbeat_identity(str(exchange)) is None
    _write_heartbeat(str(exchange), _runner_identity())
    updated = read_heartbeat(str(exchange))
    assert updated is not None
    assert heartbeat_fresh(str(exchange), now=updated)
    assert not heartbeat_fresh(
        str(exchange),
        now=datetime.fromisoformat("2030-01-01T00:00:00+00:00"),
    )
    identity = read_heartbeat_identity(str(exchange))
    assert identity is not None
    assert identity["executor_ref"] == f"{EXECUTOR_ID}@{EXECUTOR_VERSION}"
    assert identity["executable_refs"] == [HIGHS_EXECUTABLE_REF]
    assert HEARTBEAT_STALE_AFTER_SECONDS > 0


def test_provider_accepts_matching_heartbeat_identity_unit(tmp_path) -> None:
    """心跳新鲜且身份匹配即 available，注册表发布（无 Docker）。"""
    exchange = tmp_path / "xchg"
    exchange.mkdir()
    _write_fresh_heartbeat(exchange)
    provider = _ipc_provider(str(exchange))
    assert provider.available() is True
    registry = build_executor_registry([provider])
    assert registry.get(f"{EXECUTOR_ID}@{EXECUTOR_VERSION}") is provider


def test_provider_rejects_mismatched_heartbeat_identity_unit(tmp_path) -> None:
    """心跳新鲜但身份错配即不可用（无 Docker）：执行器不同、可执行文件
    缺少/不同/多报任一、边界缺键。"""
    cases = [
        {**_runner_identity(), "executor_ref": "ies.executor.other@9.9.9"},
        {**_runner_identity(), "executable_refs": ["ies.solver.other@9.9.9"]},
        {
            **_runner_identity(),
            "executable_refs": [HIGHS_EXECUTABLE_REF, "ies.solver.other@9.9.9"],
        },
        {**_runner_identity(), "applied": {"memory_limit_bytes": 1}},
    ]
    for index, identity in enumerate(cases):
        exchange = tmp_path / f"xchg-{index}"
        exchange.mkdir()
        _write_fresh_heartbeat(exchange, identity)
        provider = _ipc_provider(str(exchange))
        assert provider.available() is False
        with pytest.raises(ComputationUnavailableError) as exc_info:
            build_executor_registry([provider])
        assert exc_info.value.reason == "no-executor"
        with pytest.raises(ComputationUnavailableError) as exc_info:
            provider.execute({"executor": "x"}, str(tmp_path))
        assert exc_info.value.reason == "runner-unreachable"


def test_materialize_marks_inputs_readonly_unit(tmp_path) -> None:
    """materialize 铺入后请求文件只读，求解侧误写即失败（无 Docker）。"""
    import stat as stat_module

    from iesplan.computation.runtime.runner_service import _materialize

    attempt = tmp_path / "attempt"
    (attempt / "input").mkdir(parents=True)
    (attempt / "command.json").write_text("{}", "utf-8")
    (attempt / "input" / "problem.mps").write_bytes(b"mps-bytes")
    work = tmp_path / "work"
    work.mkdir()
    _materialize(str(attempt), str(work))
    assert (work / "input" / "problem.mps").read_bytes() == b"mps-bytes"
    mode = stat_module.S_IMODE(os.stat(work / "input" / "problem.mps").st_mode)
    assert mode == 0o444
    assert not (work / "command.json").exists()
