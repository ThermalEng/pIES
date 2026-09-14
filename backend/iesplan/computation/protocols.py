"""计算边界协议(0.8 延期: 只定义真实边界, 无任何算法实现)。

三段式真实边界(宪法 §4.5):

- ``GeneratorProvider`` 只接受 ``ValidatedAssemblyArtifact``、已固定资源与
  经过公开 schema 校验的 ``CalculationConfig``, 确定性生成 ``SolverBundle``;
- ``SolverRuntime`` 只校验并执行 Bundle 中的结构化命令, 生成
  ``ExecutionReceipt`` 与原始输出;
- ``ResultAdapter`` 只把 Bundle、回执和声明输出映射为带 schema/version 的
  ``ComputeResult``。

单位换算唯一边界: 业务单位到求解器内部单位只在 GeneratorProvider 发生,
反向只在 ResultAdapter 发生; 求解选项在计算包生成时固定。SolverRuntime
不读取装配语义、不判断设备类型、不补数据或切换 solver。

本模块只声明边界形状与不可变数据载体, 不实现任何 0.8 算法。当前无任何
可用 provider(见 ``iesplan.computation.providers``), 任何实际计算请求都
必须以 ``ComputationUnavailableError`` 明确失败, 禁止静默回退、禁止猜测
默认结果。

深度不可变: 所有跨越边界的载体均为 frozen dataclass, 容器字段在构造时
递归冻结(映射 → ``MappingProxyType``, 序列 → ``tuple``); ``to_dict()``
一律返回可独立改写的普通 ``dict``/``list`` 副本。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

from iesplan.assembly.contracts import ValidatedAssemblyArtifact

#: 公开计算配置 schema(进入业务事实前须校验字头)。
CALCULATION_CONFIG_SCHEMA = "ies.calculation-config"
CALCULATION_CONFIG_VERSION = "1.0.0"

#: 求解包 schema(生产者 GeneratorProvider, 消费者 SolverRuntime)。
SOLVER_BUNDLE_SCHEMA = "ies.solver-bundle"
SOLVER_BUNDLE_VERSION = "1.0.0"

#: 统一计算结果 schema。
COMPUTE_RESULT_SCHEMA = "ies.compute-result"
COMPUTE_RESULT_VERSION = "1.0.0"


def _freeze_json(value: object) -> object:
    """递归冻结 JSON 值(dict → MappingProxy, list/tuple → tuple)。"""
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("边界映射键须为字符串")
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"边界字段须为 JSON 值, 得到 {type(value).__name__}")


def _thaw_json(value: object) -> object:
    """递归解冻为普通 dict/list(与 ``_freeze_json`` 互逆)。"""
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


@runtime_checkable
class GeneratorProvider(Protocol):
    """方案生成能力(0.8 未实现: 仅形状, 无实现)。

    纯转换: 直接消费已签发的装配产物, 不得连接数据库/对象服务/网络,
    不得启动求解器, 不得读环境变量, 不得修改项目或任务状态。资源由
    调用方显式传入; 同一装配、资源、配置与生成器版本必须产生相同输入字节。
    """

    @property
    def ref(self) -> str:
        """生成器精确引用(``<generator_id>@<version>`` 形态)。"""
        ...

    def available(self) -> bool:
        """该 provider 当前是否可用(无实现时恒为 False 语义)。"""
        ...

    def generate(
        self,
        artifact: ValidatedAssemblyArtifact,
        resources: Mapping[str, object],
        config: CalculationConfig,
    ) -> SolverBundle:
        """由已签发装配、已固定资源与已校验计算配置生成求解包。

        未签发的装配输入必须拒绝生成, 不产生部分 Bundle; 无可用实现时抛
        ``ComputationUnavailableError``。
        """
        ...


@runtime_checkable
class SolverRuntime(Protocol):
    """求解执行能力(0.8 未实现: 仅形状, 无实现)。

    受控执行层: 只校验 Bundle schema/清单/规范路径并执行其中的结构化命令
    (数据结构, 非 shell 字符串)。不读取装配语义、不构造数学问题、不解释
    设备类型、不修正输入文件。
    """

    @property
    def ref(self) -> str:
        """求解器精确引用(``<solver_id>@<version>`` 形态)。"""
        ...

    def available(self) -> bool:
        """该 runtime 当前是否可用(无实现时恒为 False 语义)。"""
        ...

    def run(
        self, bundle: SolverBundle
    ) -> tuple[ExecutionReceipt, Mapping[str, object]]:
        """执行 Bundle 中的结构化命令, 返回 ``(回执, 原始输出)``。

        成功或失败都尽量形成回执; 无可用实现时抛
        ``ComputationUnavailableError``。
        """
        ...


@runtime_checkable
class ResultAdapter(Protocol):
    """统一计算结果的声明输出适配(0.8 未实现: 仅形状, 无实现)。

    只凭只读 Bundle、回执和声明输出解释结果; 不得重新运行求解器、不得
    访问项目草稿、不得按当前最新 provider 改写历史结果。
    """

    def adapt(
        self,
        bundle: SolverBundle,
        receipt: ExecutionReceipt,
        outputs: Mapping[str, object],
    ) -> ComputeResult:
        """把 Bundle、回执和声明输出映射为统一计算结果。

        不可行/无界/数值异常映射为明确结果状态; 结果格式损坏或非有限值
        即结果协议失败, 不发布伪成功。无可用实现时抛
        ``ComputationUnavailableError``。
        """
        ...


@dataclass(frozen=True, slots=True)
class CalculationConfig:
    """公开计算配置(进入业务事实前已校验字头的求解选项)。

    携带生成器/求解器选择无关的固定求解选项: 随机种子与算法选项(含精度)。
    generator/solver 身份由 provider 自身 ``ref`` 表达, 不在此重复声明。
    """

    schema: str = CALCULATION_CONFIG_SCHEMA
    schema_version: str = CALCULATION_CONFIG_VERSION
    seed: int | None = None
    options: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.schema != CALCULATION_CONFIG_SCHEMA:
            raise ValueError(f"计算配置 schema 非法: {self.schema!r}")
        if self.schema_version != CALCULATION_CONFIG_VERSION:
            raise ValueError(f"计算配置版本非法: {self.schema_version!r}")
        if self.seed is not None and not isinstance(self.seed, int):
            raise TypeError("seed 须为整数或 None")
        if not isinstance(self.options, Mapping):
            raise TypeError("options 须为 Mapping")
        object.__setattr__(self, "options", _freeze_json(self.options))

    def to_dict(self) -> dict[str, Any]:
        """序列化为 JSON 兼容独立副本。"""
        return {
            "schema": self.schema,
            "schema_version": self.schema_version,
            "seed": self.seed,
            "options": _thaw_json(self.options),
        }


@dataclass(frozen=True, slots=True)
class SolverBundle:
    """一次求解的可执行不可变交接包(生产者 GeneratorProvider)。

    ``command`` 为结构化命令数据(执行器/可执行文件 ID、逐参数字符串、
    Bundle 内规范相对路径、硬资源限制), 不是 shell 字符串。
    ``declared_outputs`` 为预先声明的 Bundle 内相对输出路径。
    """

    schema: str = SOLVER_BUNDLE_SCHEMA
    schema_version: str = SOLVER_BUNDLE_VERSION
    bundle_id: str = ""
    generator_ref: str = ""
    solver_ref: str = ""
    config_id: str = ""
    inputs: Mapping[str, object] = field(default_factory=dict)
    command: Mapping[str, object] = field(default_factory=dict)
    declared_outputs: tuple[str, ...] = ()
    result_adapter_ref: str = ""

    def __post_init__(self) -> None:
        if self.schema != SOLVER_BUNDLE_SCHEMA:
            raise ValueError(f"Bundle schema 非法: {self.schema!r}")
        if self.schema_version != SOLVER_BUNDLE_VERSION:
            raise ValueError(f"Bundle 版本非法: {self.schema_version!r}")
        for name in ("bundle_id", "generator_ref", "solver_ref", "result_adapter_ref"):
            if not getattr(self, name):
                raise ValueError(f"Bundle 缺必需引用: {name}")
        if not isinstance(self.inputs, Mapping):
            raise TypeError("inputs 须为 Mapping")
        if not isinstance(self.command, Mapping):
            raise TypeError("command 须为结构化 Mapping(禁止 shell 字符串)")
        declared = tuple(self.declared_outputs)
        for path in declared:
            if not isinstance(path, str) or not path:
                raise ValueError("declared_outputs 须为非空相对路径")
            if path.startswith("/") or ".." in path.split("/"):
                raise ValueError(f"声明输出路径越界: {path!r}")
        object.__setattr__(self, "inputs", _freeze_json(self.inputs))
        object.__setattr__(self, "command", _freeze_json(self.command))
        object.__setattr__(self, "declared_outputs", declared)

    def to_dict(self) -> dict[str, Any]:
        """序列化为 JSON 兼容独立副本。"""
        return {
            "schema": self.schema,
            "schema_version": self.schema_version,
            "bundle_id": self.bundle_id,
            "generator_ref": self.generator_ref,
            "solver_ref": self.solver_ref,
            "config_id": self.config_id,
            "inputs": _thaw_json(self.inputs),
            "command": _thaw_json(self.command),
            "declared_outputs": list(self.declared_outputs),
            "result_adapter_ref": self.result_adapter_ref,
        }


@dataclass(frozen=True, slots=True)
class ExecutionReceipt:
    """求解执行回执(证据, 非可变运行日志索引)。

    记录 Bundle/实现标识、进程协议结果(退出码/终止)与结构化执行状态;
    ``detail`` 承载 stdout/stderr 节选、资源统计等只读执行事实。
    ``success_exit_codes`` 只表示进程协议成功; 不可行/无界等业务状态由
    结果适配器读取求解器输出后映射, 不在此猜测。
    """

    bundle_id: str = ""
    generator_ref: str = ""
    solver_ref: str = ""
    status: str = ""
    exit_code: int | None = None
    detail: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("bundle_id", "generator_ref", "solver_ref", "status"):
            if not getattr(self, name):
                raise ValueError(f"回执缺必需字段: {name}")
        if self.exit_code is not None and not isinstance(self.exit_code, int):
            raise TypeError("exit_code 须为整数或 None")
        if not isinstance(self.detail, Mapping):
            raise TypeError("detail 须为 Mapping")
        object.__setattr__(self, "detail", _freeze_json(self.detail))

    def to_dict(self) -> dict[str, Any]:
        """序列化为 JSON 兼容独立副本。"""
        return {
            "bundle_id": self.bundle_id,
            "generator_ref": self.generator_ref,
            "solver_ref": self.solver_ref,
            "status": self.status,
            "exit_code": self.exit_code,
            "detail": _thaw_json(self.detail),
        }


@dataclass(frozen=True, slots=True)
class ComputeResult:
    """跨越计算边界的统一计算结果(不可变载体, 非算法实现)。

    ``business_outcome`` 为适配器声明的业务结局, 取值必须落在 tasks 域
    公开 ``BUSINESS_OUTCOMES`` 内(由调用阶段网关校验); ``status`` 为求解
    层执行状态, ``outputs`` 为统一结果输出。
    """

    schema: str = COMPUTE_RESULT_SCHEMA
    schema_version: str = COMPUTE_RESULT_VERSION
    bundle_id: str = ""
    status: str = ""
    business_outcome: str = ""
    outputs: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.schema != COMPUTE_RESULT_SCHEMA:
            raise ValueError(f"结果 schema 非法: {self.schema!r}")
        if self.schema_version != COMPUTE_RESULT_VERSION:
            raise ValueError(f"结果版本非法: {self.schema_version!r}")
        for name in ("bundle_id", "status", "business_outcome"):
            if not getattr(self, name):
                raise ValueError(f"结果缺必需字段: {name}")
        if not isinstance(self.outputs, Mapping):
            raise TypeError("outputs 须为 Mapping")
        object.__setattr__(self, "outputs", _freeze_json(self.outputs))

    def to_dict(self) -> dict[str, Any]:
        """序列化为 JSON 兼容独立副本。"""
        return {
            "schema": self.schema,
            "schema_version": self.schema_version,
            "bundle_id": self.bundle_id,
            "status": self.status,
            "business_outcome": self.business_outcome,
            "outputs": _thaw_json(self.outputs),
        }


__all__ = [
    "CALCULATION_CONFIG_SCHEMA",
    "CALCULATION_CONFIG_VERSION",
    "COMPUTE_RESULT_SCHEMA",
    "COMPUTE_RESULT_VERSION",
    "SOLVER_BUNDLE_SCHEMA",
    "SOLVER_BUNDLE_VERSION",
    "CalculationConfig",
    "ComputeResult",
    "ExecutionReceipt",
    "GeneratorProvider",
    "ResultAdapter",
    "SolverBundle",
    "SolverRuntime",
]
