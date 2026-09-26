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
    mat = materialize_non_predictive_series(artifact, resources)
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
        resource_usage={"max_rss_bytes": 123},
    )
    write_receipt_envelope(str(target), outcome=outcome)
    back, unexecuted = read_receipt_envelope(str(target))
    assert unexecuted == ""
    assert back is not None and back.exit_code == 0
    assert back.stdout == b"ok"
    assert back.resource_usage["max_rss_bytes"] == 123
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


def test_provider_unavailable_without_exchange(tmp_path) -> None:
    """交换目录缺失即 unavailable，执行明确拒绝（无 Docker）。"""
    provider = _ipc_provider(str(tmp_path / "nope"))
    assert provider.available() is False
    with pytest.raises(ComputationUnavailableError) as exc_info:
        provider.execute({"executor": "x"}, str(tmp_path))
    assert exc_info.value.reason == "no-exchange"


def test_provider_rejects_stale_heartbeat(tmp_path) -> None:
    """心跳失活即拒不递交（无 Docker；旧心跳 + 无 runner）。"""
    exchange = tmp_path / "xchg"
    exchange.mkdir()
    (exchange / "heartbeat.json").write_text(
        json.dumps(
            {
                "schema": "ies.ipc.exchange.v1",
                "updated_at": "2020-01-01T00:00:00+00:00",
            }
        ),
        "utf-8",
    )
    assert read_heartbeat(str(exchange)) is not None
    assert not heartbeat_fresh(str(exchange))
    provider = _ipc_provider(str(exchange))
    assert provider.available() is True
    with pytest.raises(ComputationUnavailableError) as exc_info:
        provider.execute({"executor": "x"}, str(tmp_path))
    assert exc_info.value.reason == "runner-unreachable"


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
        assert usage["max_rss_bytes"] > 0
        assert usage["memory_limit_bytes"] == 512 * 1024 * 1024
        assert usage["pids_limit"] == 128
        assert math.isclose(usage["cpu_limit_cores"], 2.0)
        assert "oom_kill_delta" not in usage
        assert [entry for entry in os.listdir(exchange) if os.path.isdir(os.path.join(exchange, entry))] == []
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
        # 叠加命名空间内仅 lo 网卡与 runner 启动期外连探针（能连上
        # 即拒绝服务），共同证明真实断网而非声明。
        networks = info["NetworkSettings"]["Networks"]
        assert set(networks) == {"none"}
        assert not networks["none"].get("IPAddress")
        assert runner.exec("ls", "/sys/class/net") == "lo"
        assert host_config["ReadonlyRootfs"] is True
        assert host_config["Memory"] == 512 * 1024 * 1024
        assert host_config["PidsLimit"] == 128
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
        assert usage["oom_kill_delta"] >= 1
        assert usage["memory_limit_bytes"] == 160 * 1024 * 1024
        assert outputs == {}
        assert tuple(receipt.outputs) == ()
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


def test_heartbeat_fresh_and_stale_unit(tmp_path) -> None:
    """心跳读写与失活判定（无 Docker；时间确定性）。"""
    from iesplan.computation.runtime.file_exchange import (
        HEARTBEAT_STALE_AFTER_SECONDS,
    )
    from iesplan.computation.runtime.runner_service import _write_heartbeat

    exchange = tmp_path / "xchg"
    exchange.mkdir()
    assert read_heartbeat(str(exchange)) is None
    assert not heartbeat_fresh(str(exchange))
    _write_heartbeat(str(exchange))
    updated = read_heartbeat(str(exchange))
    assert updated is not None
    assert heartbeat_fresh(str(exchange), now=updated)
    assert not heartbeat_fresh(
        str(exchange),
        now=datetime.fromisoformat("2030-01-01T00:00:00+00:00"),
    )
    assert HEARTBEAT_STALE_AFTER_SECONDS > 0


def test_collect_back_skips_identical_replaces_changed_unit(tmp_path) -> None:
    """回收集语义：输入未变跳过不复写，新增/改写原子落盘无残留。"""
    from iesplan.computation.runtime.runner_service import _collect_back

    attempt = tmp_path / "attempt"
    work = tmp_path / "work"
    (attempt / "input").mkdir(parents=True)
    (work / "input").mkdir(parents=True)
    (work / "output").mkdir(parents=True)
    (attempt / "input" / "problem.mps").write_bytes(b"mps-bytes")
    (work / "input" / "problem.mps").write_bytes(b"mps-bytes")
    (work / "output" / "solution.sol").write_bytes(b"sol")
    _collect_back(str(work), str(attempt))
    assert (attempt / "input" / "problem.mps").read_bytes() == b"mps-bytes"
    assert (attempt / "output" / "solution.sol").read_bytes() == b"sol"
    (work / "output" / "solution.sol").write_bytes(b"sol2")
    _collect_back(str(work), str(attempt))
    assert (attempt / "output" / "solution.sol").read_bytes() == b"sol2"
    leftovers = list(attempt.rglob("*.collecting"))
    assert leftovers == []
