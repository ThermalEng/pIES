"""computation 接线测试(F3): 真实最小调用链与 unavailable 语义。

覆盖(生产代码 + 测试一并修改, 不恢复静默默认):

- 无模块全局网关: ``iesplan.worker.runner`` 与阶段网关不再持有任何
  全局可赋值计算入口;
- 真实阶段顺序: 三个独立最小替身分别验证三协议, 记录调用
  generate → run → adapt, 且 Bundle/回执对象在段间原样透传(非重造);
- unavailable 语义矩阵: None/空目录/缺键/自报不可用, reason 结构化;
- 输入形状错误(快照缺失/未签发装配/非法结局)为调用方错误, 绝不成功;
- bootstrap 真实装配注入路径: 真实 ``assemble_compute_worker()`` 的
  ``computation_providers`` 经阶段网关收拢为 unavailable, 经执行闭环
  落 failed(非 lease_rejected); main 入口把装配目录原样注入 Daemon;
- 端到端: 注入完整三段假能力后 calc 任务真实走完执行闭环并落 completed。

数据库: SQLite :memory:(StaticPool 共享连接); 队列: memory;
对象存储: settings.data_dir → tmp_path(均见 worker_testkit)。
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

os.environ.setdefault("IESPLAN_DB_URL", "sqlite+pysqlite://")
os.environ.setdefault("IESPLAN_QUEUE", "memory")

import pytest  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402
from worker_testkit import setup_environment  # noqa: E402

from iesplan.application import worker as worker_app  # noqa: E402
from iesplan.application.worker import compute_cases as compute_gateway  # noqa: E402
from iesplan.computation import (  # noqa: E402
    CalculationConfig,
    ComputationUnavailableError,
    ComputeResult,
    ExecutionReceipt,
    GeneratorProvider,
    ResultAdapter,
    SolverBundle,
    SolverRuntime,
)
from iesplan.core.diagnostics import TASK_SOLVE_FAILED  # noqa: E402
from iesplan.db import Base  # noqa: E402
from iesplan.tasks import (  # noqa: E402
    CalcSnapshotRecord,  # noqa: E402
    queue,  # noqa: E402
)
from iesplan.tasks.persistence import Task, TaskDiagnostic  # noqa: E402
from iesplan.worker import runner  # noqa: E402


# ---------------------------------------------------------------------------
# 测试环境
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    """模块级 SQLite 内存引擎(StaticPool: 所有会话共享同一连接)。"""
    eng = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture(autouse=True)
def _clean_state(engine: Engine) -> Iterator[None]:
    """每个测试前重置内存队列, 结束后清空全部表(避免测试间串扰)。"""
    queue.force_memory()
    yield
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())


@pytest.fixture()
def db(engine: Engine) -> Iterator[Session]:
    """函数级共享会话(服务与测试共用, 提交由调用方控制)。"""
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as session:
        yield session


@pytest.fixture()
def factory(engine: Engine):
    """会话工厂(runner.run_task 唯一接受的会话来源)。"""
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@pytest.fixture()
def env(db: Session, tmp_path: Path) -> dict[str, Any]:
    """迷你 calc 任务环境(含快照与数据集, 快照含已签发装配文本与回执)。"""
    return setup_environment(db, tmp_path, task_type="calc")


def _snapshot_of(db: Session, env: dict[str, Any]) -> CalcSnapshotRecord:
    """由测试环境快照行取公开记录(阶段网关唯一接受的输入形态)。"""
    record = worker_app.get_snapshot_record(db, env["snapshot"].id)
    assert record is not None
    return record


# ---------------------------------------------------------------------------
# 独立最小替身(每协议一个, 不共用)
# ---------------------------------------------------------------------------


def _test_bundle(bundle_id: str = "wire-bundle-1") -> SolverBundle:
    return SolverBundle(
        bundle_id=bundle_id,
        generator_ref="test.generator@0.0.0",
        solver_ref="test.solver@0.0.0",
        config_id="test-config",
        inputs={"input/problem.mps": "mps-bytes"},
        command={"executor": "test.executor@0.0.0", "arguments": ["input/problem.mps"]},
        declared_outputs=("output/solution.json",),
        result_adapter_ref="test.result-adapter@0.0.0",
    )


class _GeneratorFake:
    """仅实现生成段的替身: 消费装配产物/资源/配置, 产出 Bundle。"""

    def __init__(self, calls: list[str]) -> None:
        self._calls = calls

    @property
    def ref(self) -> str:
        return "test.generator@0.0.0"

    def available(self) -> bool:
        return True

    def generate(self, artifact, resources, config) -> SolverBundle:
        assert artifact.canonical_text
        assert isinstance(resources, Mapping)
        assert isinstance(config, CalculationConfig)
        self._calls.append("generate")
        return _test_bundle()


class _RuntimeFake:
    """仅实现执行段的替身: 消费 Bundle, 产出回执与原始输出。"""

    def __init__(self, calls: list[str]) -> None:
        self._calls = calls
        self.seen_bundle: SolverBundle | None = None

    @property
    def ref(self) -> str:
        return "test.solver@0.0.0"

    def available(self) -> bool:
        return True

    def run(self, bundle: SolverBundle):
        assert isinstance(bundle, SolverBundle)
        self.seen_bundle = bundle
        self._calls.append("run")
        receipt = ExecutionReceipt(
            bundle_id=bundle.bundle_id, generator_ref=bundle.generator_ref,
            solver_ref=bundle.solver_ref, status="succeeded", exit_code=0,
        )
        return receipt, {"output/solution.json": {"objective": 1.0}}


class _AdapterFake:
    """仅实现适配段的替身: 消费 Bundle/回执/声明输出, 产出统一结果。"""

    def available(self) -> bool:
        return True

    def __init__(self, calls: list[str], outcome: str = "normal_completion") -> None:
        self._calls = calls
        self._outcome = outcome
        self.seen: tuple[Any, Any, Any] | None = None

    def adapt(self, bundle, receipt, outputs) -> ComputeResult:
        assert isinstance(bundle, SolverBundle)
        assert isinstance(receipt, ExecutionReceipt)
        assert isinstance(outputs, Mapping)
        self.seen = (bundle, receipt, outputs)
        self._calls.append("adapt")
        return ComputeResult(
            bundle_id=bundle.bundle_id, status="succeeded",
            business_outcome=self._outcome, outputs={"objective": 1.0},
        )


def _recording_providers(
    calls: list[str], outcome: str = "normal_completion"
) -> dict[str, Any]:
    return {
        worker_app.GENERATOR_PROVIDER_KEY: _GeneratorFake(calls),
        worker_app.SOLVER_RUNTIME_KEY: _RuntimeFake(calls),
        worker_app.RESULT_ADAPTER_KEY: _AdapterFake(calls, outcome),
    }


# ---------------------------------------------------------------------------
# 1. 无模块全局网关
# ---------------------------------------------------------------------------


def test_no_module_global_compute_gateway() -> None:
    """runner 与计算阶段网关均无模块全局计算入口(只能调用参数注入)。"""
    assert not hasattr(runner, "compute_gateway")
    assert not hasattr(runner, "ComputeUnavailableError")
    assert not hasattr(compute_gateway, "compute_gateway")
    assert not hasattr(compute_gateway, "providers")
    assert not hasattr(compute_gateway, "provider")


def test_independent_doubles_cover_each_protocol() -> None:
    """三个独立替身各只通过自身协议的结构判定(无三协议合一假对象)。"""
    calls: list[str] = []
    gen, runtime, adapter = (
        _GeneratorFake(calls), _RuntimeFake(calls), _AdapterFake(calls),
    )
    assert isinstance(gen, GeneratorProvider)
    assert not isinstance(gen, SolverRuntime)
    assert not isinstance(gen, ResultAdapter)
    assert isinstance(runtime, SolverRuntime)
    assert not isinstance(runtime, GeneratorProvider)
    assert not isinstance(runtime, ResultAdapter)
    assert isinstance(adapter, ResultAdapter)
    assert not isinstance(adapter, GeneratorProvider)
    assert not isinstance(adapter, SolverRuntime)


# ---------------------------------------------------------------------------
# 2. 真实阶段顺序与透传
# ---------------------------------------------------------------------------


def test_stage_order_generate_run_adapt(db: Session, env: dict[str, Any]) -> None:
    """阶段网关按 generate → run → adapt 真实调用, 段间对象原样透传。"""
    calls: list[str] = []
    providers = _recording_providers(calls)
    stages: list[tuple[float, str]] = []

    payload = worker_app.run_compute_stage(
        _snapshot_of(db, env),
        providers=providers,
        progress_fn=lambda percent, stage, detail: stages.append((percent, stage)),
    )

    assert calls == ["generate", "run", "adapt"]
    assert [stage for _, stage in stages] == ["generate", "solve", "adapt"]
    runtime = providers[worker_app.SOLVER_RUNTIME_KEY]
    adapter = providers[worker_app.RESULT_ADAPTER_KEY]
    assert isinstance(runtime, _RuntimeFake) and isinstance(adapter, _AdapterFake)
    seen_bundle, seen_receipt, seen_outputs = adapter.seen or (None, None, None)
    assert seen_bundle is runtime.seen_bundle
    assert seen_receipt is not None and seen_receipt.bundle_id == seen_bundle.bundle_id
    assert seen_outputs == {"output/solution.json": {"objective": 1.0}}
    assert payload["outcome"] == "normal_completion"
    assert payload["result_kind"] == "compute_result"
    assert payload["bundle_id"] == "wire-bundle-1"
    assert payload["outputs"] == {"objective": 1.0}


def test_production_providers_dict_is_consumed(db: Session, env: dict[str, Any]) -> None:
    """ApplicationContext.computation_providers 被阶段网关真实消费。

    空目录即结构化 unavailable(生产现状), 非无人读的字典: 传入非空目录
    则阶段网关真实取用其中能力并执行。
    """
    assert worker_app.run_compute_stage is compute_gateway.run_compute_stage
    with pytest.raises(ComputationUnavailableError):
        worker_app.run_compute_stage(_snapshot_of(db, env), providers={})
    calls: list[str] = []
    payload = worker_app.run_compute_stage(
        _snapshot_of(db, env), providers=_recording_providers(calls),
    )
    assert calls == ["generate", "run", "adapt"]
    assert payload["outcome"] == "normal_completion"


# ---------------------------------------------------------------------------
# 3. unavailable 语义矩阵
# ---------------------------------------------------------------------------


class _UnavailableGenerator:
    """自报不可用的生成器(0.8 延期语义)。"""

    @property
    def ref(self) -> str:
        return "test.generator@0.0.0"

    def available(self) -> bool:
        return False

    def generate(self, artifact, resources, config) -> SolverBundle:
        raise AssertionError("不可用能力不得被调用")


def _unavailable_variant(
    calls: list[str], key: str
) -> dict[str, Any]:
    """把指定键替换为自报不可用的能力, 其余透传。"""
    providers = _recording_providers(calls)
    if key == worker_app.GENERATOR_PROVIDER_KEY:
        providers[key] = _UnavailableGenerator()
    elif key == worker_app.SOLVER_RUNTIME_KEY:
        runtime = providers[key]
        assert isinstance(runtime, _RuntimeFake)
        runtime.available = lambda: False  # type: ignore[method-assign]
    else:
        adapter = providers[key]
        assert isinstance(adapter, _AdapterFake)
        adapter.available = lambda: False  # type: ignore[attr-defined]
    return providers


def test_unavailable_matrix(db: Session, env: dict[str, Any]) -> None:
    """无 provider 诸形态一律结构化 unavailable, reason 明确, 绝不成功。"""
    snapshot = _snapshot_of(db, env)
    calls: list[str] = []
    no_provider_cases: list[Any] = [
        None,
        {},
        {"generator": _GeneratorFake(calls)},
        ["not-a-mapping"],
    ]
    for providers in no_provider_cases:
        with pytest.raises(ComputationUnavailableError) as exc_info:
            worker_app.run_compute_stage(snapshot, providers=providers)
        assert exc_info.value.reason == "no-provider"
    assert calls == []
    for key in (
        worker_app.GENERATOR_PROVIDER_KEY,
        worker_app.SOLVER_RUNTIME_KEY,
        worker_app.RESULT_ADAPTER_KEY,
    ):
        with pytest.raises(ComputationUnavailableError) as exc_info:
            worker_app.run_compute_stage(
                snapshot, providers=_unavailable_variant([], key),
            )
        assert exc_info.value.reason == "deferred-0.8"


# ---------------------------------------------------------------------------
# 4. 输入形状错误(调用方错误, 绝不成功)
# ---------------------------------------------------------------------------


def test_stage_input_shape_errors(db: Session, env: dict[str, Any]) -> None:
    """快照缺失/未签发装配/非法结局均为调用方错误, 不得落成功。"""
    calls: list[str] = []
    providers = _recording_providers(calls)
    with pytest.raises(ValueError, match="缺快照"):
        worker_app.run_compute_stage(None, providers=providers)
    assert calls == []

    snapshot = _snapshot_of(db, env)
    assert snapshot.canonical_assembly_text
    unsigned = CalcSnapshotRecord(
        id=snapshot.id, project_version_id=snapshot.project_version_id,
        dataset_version_ids=snapshot.dataset_version_ids,
        calc_config_snapshot=dict(snapshot.calc_config_snapshot),
        random_seed=snapshot.random_seed,
        canonical_assembly_text=None, assembly_receipt=None,
    )
    with pytest.raises(ValueError, match="未签发"):
        worker_app.run_compute_stage(unsigned, providers=providers)
    assert calls == []

    bad_outcome = _recording_providers(calls, outcome="not_a_real_outcome")
    with pytest.raises(ValueError, match="非法业务结局"):
        worker_app.run_compute_stage(snapshot, providers=bad_outcome)


# ---------------------------------------------------------------------------
# 5. bootstrap 真实装配注入路径
# ---------------------------------------------------------------------------


def test_bootstrap_real_assembly_injects_empty_providers(
    db: Session, env: dict[str, Any], factory, engine: Engine
) -> None:
    """真实组合根装配的 provider 目录经阶段网关收拢为 unavailable。

    生产现状(0.8 未实现): 目录为空 → 结构化 unavailable, 经执行闭环落
    failed + TASK-SOLVE-001, 非 lease_rejected, 无结果写入, 不伪造成功。
    """
    from iesplan.bootstrap import assemble_compute_worker

    ctx = assemble_compute_worker()
    assert ctx.computation_providers == {}
    assert ctx.session_factory is not None
    # 真实装配会显式编排注册各领域 metadata(新增表进入 Base.metadata);
    # 本文件引擎为模块级内存库, 此处幂等建表, 使后续清理与测试共享同一 schema。
    Base.metadata.create_all(engine)

    with pytest.raises(ComputationUnavailableError) as exc_info:
        worker_app.run_compute_stage(
            _snapshot_of(db, env), providers=ctx.computation_providers,
        )
    assert exc_info.value.reason == "no-provider"

    with factory() as claim_db:
        claim = worker_app.acquire_attempt(claim_db, env["task"].id, "w-wire")
        assert claim is not None
        claim_db.commit()
    status = runner.run_task(
        factory, claim, worker_id="w-wire", isolate=False,
        computation_providers=ctx.computation_providers,
    )
    assert status == "failed", status
    with factory() as check_db:
        row = check_db.get(Task, env["task"].id)
        assert row is not None and row.status == "failed"
        diags = (
            check_db.query(TaskDiagnostic)
            .filter(TaskDiagnostic.task_id == env["task"].id)
            .all()
        )
    assert diags
    solve = [d for d in diags if d.code == TASK_SOLVE_FAILED]
    assert solve, [(d.code, d.message) for d in diags]
    assert all("内部错误" not in (d.message or "") for d in solve)
    assert all("no-provider" in (d.message or "") for d in solve)


def test_main_forwards_bootstrap_providers_to_daemon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """main 入口把装配目录原样注入 Daemon(生产调用证据)。"""
    import sys
    import types
    from types import SimpleNamespace

    import iesplan.worker.main as worker_main

    sentinel_factory = object()
    sentinel_providers = {"generator": object()}

    module = types.ModuleType("iesplan.bootstrap")
    module.assemble_compute_worker = lambda: SimpleNamespace(  # type: ignore[attr-defined]
        session_factory=sentinel_factory, computation_providers=sentinel_providers,
    )
    module.assemble_io_worker = lambda: SimpleNamespace(  # type: ignore[attr-defined]
        session_factory=sentinel_factory, computation_providers={},
    )
    monkeypatch.setitem(sys.modules, "iesplan.bootstrap", module)

    captured: dict[str, Any] = {}

    class _FakeWorker:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            captured.update(kwargs)

        def run(self) -> None:
            captured["runs"] = True

    monkeypatch.setattr(worker_main, "Worker", _FakeWorker)
    assert worker_main.main(["--worker-type", "compute"]) == 0
    assert captured["session_factory"] is sentinel_factory
    assert captured["computation_providers"] is sentinel_providers
    assert captured["runs"] is True


# ---------------------------------------------------------------------------
# 6. 端到端: 注入完整能力后 calc 任务真实走完闭环
# ---------------------------------------------------------------------------


def test_run_task_with_injected_providers_completes(
    db: Session, env: dict[str, Any], factory
) -> None:
    """calc 任务经真实阶段网关 + 注入能力走完执行闭环并落 completed。"""
    calls: list[str] = []
    with factory() as claim_db:
        claim = worker_app.acquire_attempt(claim_db, env["task"].id, "w-wire")
        assert claim is not None
        claim_db.commit()
    status = runner.run_task(
        factory, claim, worker_id="w-wire", isolate=False,
        computation_providers=_recording_providers(calls),
    )
    assert status == "completed", status
    assert calls == ["generate", "run", "adapt"]
    with factory() as check_db:
        row = check_db.get(Task, env["task"].id)
        assert row is not None and row.status == "completed"
        assert row.business_outcome == "normal_completion"


def test_run_task_without_providers_fails_closed(
    db: Session, env: dict[str, Any], factory
) -> None:
    """缺省(无注入能力)即结构化失败: failed, 非 lease_rejected, 有诊断。"""
    with factory() as claim_db:
        claim = worker_app.acquire_attempt(claim_db, env["task"].id, "w-wire")
        assert claim is not None
        claim_db.commit()
    status = runner.run_task(factory, claim, worker_id="w-wire", isolate=False)
    assert status == "failed", status
    assert status != "lease_rejected"
    with factory() as check_db:
        row = check_db.get(Task, env["task"].id)
        assert row is not None and row.status == "failed"
        codes = [
            d.code
            for d in check_db.query(TaskDiagnostic)
            .filter(TaskDiagnostic.task_id == env["task"].id)
            .all()
        ]
    assert TASK_SOLVE_FAILED in codes
    assert "TASK-LEASE-001" not in codes
