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

本模块只声明边界形状与不可变数据载体，不实现任何 0.8 算法。当前计算
Worker 在组合根明确拒绝启动。

深度不可变: 所有跨越边界的载体均为 frozen dataclass, 容器字段在构造时
递归冻结(映射 → ``MappingProxyType``, 序列 → ``tuple``); ``to_dict()``
一律返回可独立改写的普通 ``dict``/``list`` 副本。
"""

from __future__ import annotations

import math
import re
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


def freeze_json_value(value: object) -> object:
    """递归冻结 JSON 值(dict → MappingProxy, list/tuple → tuple)。"""
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("边界映射键须为字符串")
        return MappingProxyType({key: freeze_json_value(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(freeze_json_value(item) for item in value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"边界字段须为 JSON 值, 得到 {type(value).__name__}")


def thaw_json_value(value: object) -> object:
    """递归解冻为普通 dict/list(与 ``freeze_json_value`` 互逆)。"""
    if isinstance(value, Mapping):
        return {str(key): thaw_json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw_json_value(item) for item in value]
    return value


_SEMVER_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")


_ID_SEGMENT_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


def check_provider_ref(value: object, name: str) -> str:
    """校验公开 provider 选择引用(``<稳定ID>@<语义化版本>`` 形态)。

    稳定 ID 由点号分隔的非空段组成，段内只允许字母、数字、下划线与
    短横线（如 ``ies.device.pv``、``acme.generator.highs_milp``）；
    空格、空段与其他字符一律拒绝。
    """
    if not isinstance(value, str) or value.count("@") != 1:
        raise ValueError(f"计算配置 {name} 须为 <稳定ID>@<语义化版本> 形态: {value!r}")
    stable_id, version = value.split("@")
    segments = stable_id.split(".")
    if not segments or any(not _ID_SEGMENT_PATTERN.match(seg) for seg in segments):
        raise ValueError(f"计算配置 {name} 稳定 ID 非法: {stable_id!r}")
    if not _SEMVER_PATTERN.match(version):
        raise ValueError(f"计算配置 {name} 版本非法: {version!r}")
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

    固定一次计算的全部选择: ``mode``、``predictions``（预测目标/算法/
    参数，可空）、``generator`` / ``solver`` 精确引用
    （``<稳定ID>@<语义化版本>``）、``precision``（low|medium|high）、
    ``convergence``（阶段二收敛，可空）、``time_limit_seconds``、
    ``options``（可空）、随机种子与输出请求。
    计算按项目基线完整连续序列进行，本配置不含离散化字段，也不含规划
    目标/变量/约束、设备参数、财务配置、资源或装配内容。多个
    data_predict 时收敛是否必填由后续 application 结合 artifact 判断，
    本载体不重读装配。
    """

    schema: str = CALCULATION_CONFIG_SCHEMA
    schema_version: str = CALCULATION_CONFIG_VERSION
    mode: str = ""
    predictions: tuple[Mapping[str, object], ...] | None = None
    generator: str = ""
    solver: str = ""
    precision: str = ""
    convergence: Mapping[str, object] | None = None
    time_limit_seconds: int = 0
    options: Mapping[str, object] | None = None
    seed: int | None = None
    outputs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.schema != CALCULATION_CONFIG_SCHEMA:
            raise ValueError(f"计算配置 schema 非法: {self.schema!r}")
        if self.schema_version != CALCULATION_CONFIG_VERSION:
            raise ValueError(f"计算配置版本非法: {self.schema_version!r}")
        if not isinstance(self.mode, str) or not self.mode:
            raise ValueError(f"mode 须为非空字符串: {self.mode!r}")
        check_provider_ref(self.generator, "generator")
        check_provider_ref(self.solver, "solver")
        if self.precision not in ("low", "medium", "high"):
            raise ValueError(f"precision 须为 low|medium|high: {self.precision!r}")
        object.__setattr__(self, "predictions", _freeze_predictions(self.predictions))
        object.__setattr__(self, "convergence", _freeze_convergence(self.convergence))
        if (
            not isinstance(self.time_limit_seconds, int)
            or isinstance(self.time_limit_seconds, bool)
            or self.time_limit_seconds <= 0
        ):
            raise ValueError(
                f"time_limit_seconds 须为正整数: {self.time_limit_seconds!r}"
            )
        if self.options is None:
            object.__setattr__(self, "options", None)
        elif not isinstance(self.options, Mapping):
            raise TypeError("options 须为 Mapping 或 None")
        else:
            object.__setattr__(self, "options", freeze_json_value(dict(self.options)))
        if self.seed is not None and not isinstance(self.seed, int):
            raise TypeError("seed 须为整数或 None")
        outputs = tuple(self.outputs)
        if not outputs:
            raise ValueError("outputs 不得为空")
        for name in outputs:
            if not isinstance(name, str) or not name:
                raise ValueError(f"输出请求须为非空字符串: {name!r}")
        object.__setattr__(self, "outputs", outputs)

    def to_dict(self) -> dict[str, Any]:
        """序列化为 JSON 兼容独立副本（predictions 按目标稳定排序）。"""
        return {
            "schema": self.schema,
            "schema_version": self.schema_version,
            "mode": self.mode,
            "predictions": (
                [_thaw_entry(entry) for entry in self.predictions]
                if self.predictions is not None
                else None
            ),
            "generator": self.generator,
            "solver": self.solver,
            "precision": self.precision,
            "convergence": (
                dict(thaw_json_value(self.convergence))
                if self.convergence is not None
                else None
            ),
            "time_limit_seconds": self.time_limit_seconds,
            "options": (
                thaw_json_value(self.options) if self.options is not None else None
            ),
            "seed": self.seed,
            "outputs": list(self.outputs),
        }


def _check_relative_path(path: object, name: str) -> str:
    """校验 Bundle 内相对路径（非空，不以 ``/`` 开头，不含 ``..`` 段）。"""
    if not isinstance(path, str) or not path:
        raise ValueError(f"{name} 须为非空相对路径: {path!r}")
    if path.startswith("/") or ".." in path.split("/"):
        raise ValueError(f"{name} 路径越界: {path!r}")
    return path


def _check_media_type(media_type: object, name: str) -> str:
    """校验媒体类型非空字符串。"""
    if not isinstance(media_type, str) or not media_type:
        raise ValueError(f"{name} 须为非空媒体类型: {media_type!r}")
    return media_type


def _freeze_file_entries(entries: object, name: str) -> tuple[Mapping[str, object], ...]:
    """冻结 inputs/outputs 条目（[{path, media_type(, required)}]，拒绝未知键）。"""
    if isinstance(entries, Mapping) or not isinstance(entries, (list, tuple)):
        raise TypeError(f"{name} 须为条目序列，得到 {type(entries).__name__}")
    frozen = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise TypeError(f"{name} 条目须为 Mapping，得到 {type(entry).__name__}")
        if name == "outputs":
            allowed = ("path", "media_type", "required")
        else:
            allowed = ("path", "media_type")
        extra = sorted(k for k in entry if k not in allowed)
        if extra:
            raise ValueError(f"{name} 条目不接受额外字段: {extra!r}")
        for key in ("path", "media_type"):
            if key not in entry:
                raise ValueError(f"{name} 条目缺必需字段: {key}")
        _check_relative_path(entry["path"], f"{name}.path")
        _check_media_type(entry["media_type"], f"{name}.media_type")
        if name == "outputs":
            if "required" not in entry or not isinstance(entry["required"], bool):
                raise ValueError(f"{name} 条目 required 须为布尔值")
        frozen.append(freeze_json_value(dict(entry)))
    if not frozen:
        raise ValueError(f"{name} 不得为空")
    return tuple(frozen)


def _thaw_entry(entry: Mapping[str, object]) -> dict[str, Any]:
    """解冻单个文件条目为独立副本。"""
    return dict(thaw_json_value(entry))


def _freeze_prediction_target(target: object) -> Mapping[str, object]:
    """冻结预测目标（恰为 {instance, interface}，均为非空字符串）。"""
    if not isinstance(target, Mapping) or set(target) != {"instance", "interface"}:
        raise ValueError(f"prediction.target 须恰为 instance/interface 二键: {target!r}")
    for key in ("instance", "interface"):
        if not isinstance(target[key], str) or not target[key]:
            raise ValueError(f"prediction.target.{key} 须为非空字符串: {target.get(key)!r}")
    return freeze_json_value(dict(target))


def _freeze_predictions(
    predictions: object,
) -> tuple[Mapping[str, object], ...] | None:
    """冻结预测配置（可空；每项 {target, algorithm, parameters}，目标不重复）。

    algorithm 本切片只校验精确引用形态，不做注册表查找。
    返回按 (instance, interface) 稳定排序的元组。
    """
    if predictions is None:
        return None
    if isinstance(predictions, Mapping) or not isinstance(predictions, (list, tuple)):
        raise TypeError(
            f"predictions 须为序列或 None，得到 {type(predictions).__name__}"
        )
    frozen = []
    seen: set[tuple[str, str]] = set()
    for entry in predictions:
        if not isinstance(entry, Mapping) or set(entry) != {
            "target",
            "algorithm",
            "parameters",
        }:
            raise ValueError(
                "prediction 条目须恰为 target/algorithm/parameters 三键: "
                f"{entry!r}"
            )
        target = _freeze_prediction_target(entry["target"])
        check_provider_ref(entry["algorithm"], "prediction.algorithm")
        if not isinstance(entry["parameters"], Mapping):
            raise TypeError("prediction.parameters 须为 Mapping")
        key = (target["instance"], target["interface"])
        if key in seen:
            raise ValueError(f"prediction.target 不得重复: {key!r}")
        seen.add(key)
        frozen.append(
            freeze_json_value(
                {
                    "target": dict(target),
                    "algorithm": entry["algorithm"],
                    "parameters": dict(entry["parameters"]),
                }
            )
        )
    frozen.sort(key=lambda e: (e["target"]["instance"], e["target"]["interface"]))
    return tuple(frozen)


def _freeze_convergence(convergence: object) -> Mapping[str, object] | None:
    """冻结阶段二收敛配置（可空；{tolerance, max_iterations} 恰二键）。

    tolerance 为正有限数，max_iterations 为正整数。多个 data_predict 时
    是否必填由后续 application 结合 artifact 判断，本载体不重读装配。
    """
    if convergence is None:
        return None
    if not isinstance(convergence, Mapping) or set(convergence) != {
        "tolerance",
        "max_iterations",
    }:
        raise ValueError(
            "convergence 须恰为 tolerance/max_iterations 二键: "
            f"{convergence!r}"
        )
    tolerance = convergence["tolerance"]
    if (
        not isinstance(tolerance, (int, float))
        or isinstance(tolerance, bool)
        or not math.isfinite(tolerance)
        or tolerance <= 0
    ):
        raise ValueError(f"convergence.tolerance 须为正有限数: {tolerance!r}")
    max_iterations = convergence["max_iterations"]
    if (
        not isinstance(max_iterations, int)
        or isinstance(max_iterations, bool)
        or max_iterations <= 0
    ):
        raise ValueError(
            f"convergence.max_iterations 须为正整数: {max_iterations!r}"
        )
    return freeze_json_value(dict(convergence))


def _freeze_command(command: object) -> Mapping[str, object]:
    """冻结结构化命令（必需 executor/executable/arguments，拒绝未知键）。"""
    if not isinstance(command, Mapping):
        raise TypeError("command 须为结构化 Mapping（禁止 shell 字符串）")
    allowed = (
        "executor",
        "executable",
        "arguments",
        "working_directory",
        "environment",
        "timeout_seconds",
        "memory_mib",
        "cpu_cores",
        "network",
        "success_exit_codes",
    )
    extra = sorted(k for k in command if k not in allowed)
    if extra:
        raise ValueError(f"command 不接受额外字段: {extra!r}")
    for key in ("executor", "executable", "arguments"):
        if key not in command:
            raise ValueError(f"command 缺必需字段: {key}")
    check_provider_ref(command["executor"], "command.executor")
    check_provider_ref(command["executable"], "command.executable")
    arguments = command["arguments"]
    if not isinstance(arguments, (list, tuple)) or any(
        not isinstance(a, str) for a in arguments
    ):
        raise TypeError("command.arguments 须为字符串序列")
    if "working_directory" in command:
        _check_relative_path(command["working_directory"], "command.working_directory")
    if "environment" in command:
        environment = command["environment"]
        if not isinstance(environment, Mapping) or any(
            not isinstance(k, str) or not isinstance(v, str)
            for k, v in environment.items()
        ):
            raise TypeError("command.environment 须为字符串映射")
    for key in ("timeout_seconds", "memory_mib", "cpu_cores"):
        if key in command and (
            not isinstance(command[key], (int, float))
            or isinstance(command[key], bool)
            or command[key] <= 0
        ):
            raise ValueError(f"command.{key} 须为正数")
    if "network" in command and not isinstance(command["network"], bool):
        raise TypeError("command.network 须为布尔值")
    if "success_exit_codes" in command:
        codes = command["success_exit_codes"]
        if (
            not isinstance(codes, (list, tuple))
            or not codes
            or any(not isinstance(c, int) or isinstance(c, bool) for c in codes)
        ):
            raise ValueError("command.success_exit_codes 须为非空整数序列")
    return freeze_json_value(dict(command))


@dataclass(frozen=True, slots=True)
class SolverBundle:
    """一次求解的可执行不可变交接包(生产者 GeneratorProvider)。

    字段与 ``formats/solver-bundle.md`` 的 manifest 一一对应，不增删：
    ``bundle{generator, solver}`` 追溯所选精确引用；
    ``inputs[{path, media_type}]`` 声明只读输入文件；
    ``command`` 为结构化命令数据(执行器/可执行文件 ID、逐参数字符串、
    Bundle 内规范相对路径、硬资源限制)，不是 shell 字符串；
    ``outputs[{path, media_type, required}]`` 为允许写入的输出清单；
    ``result_adapter`` 为配套结果适配器精确引用；
    ``extensions`` 为规范预留的开放扩展点。
    条目与映射在构造时冻结；未知字段当场拒绝，不静默丢弃。
    """

    schema: str = SOLVER_BUNDLE_SCHEMA
    schema_version: str = SOLVER_BUNDLE_VERSION
    bundle: Mapping[str, object] = field(default_factory=dict)
    inputs: tuple[Mapping[str, object], ...] = ()
    command: Mapping[str, object] = field(default_factory=dict)
    outputs: tuple[Mapping[str, object], ...] = ()
    result_adapter: str = ""
    extensions: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.schema != SOLVER_BUNDLE_SCHEMA:
            raise ValueError(f"Bundle schema 非法: {self.schema!r}")
        if self.schema_version != SOLVER_BUNDLE_VERSION:
            raise ValueError(f"Bundle 版本非法: {self.schema_version!r}")
        if not isinstance(self.bundle, Mapping):
            raise TypeError("bundle 须为 Mapping")
        if set(self.bundle) != {"generator", "solver"}:
            raise ValueError(
                f"bundle 须恰为 generator/solver 二键: {sorted(self.bundle)!r}"
            )
        check_provider_ref(self.bundle["generator"], "bundle.generator")
        check_provider_ref(self.bundle["solver"], "bundle.solver")
        object.__setattr__(
            self, "bundle", freeze_json_value(dict(self.bundle))
        )
        object.__setattr__(self, "inputs", _freeze_file_entries(self.inputs, "inputs"))
        object.__setattr__(self, "command", _freeze_command(self.command))
        object.__setattr__(self, "outputs", _freeze_file_entries(self.outputs, "outputs"))
        if not self.result_adapter:
            raise ValueError("Bundle 缺必需引用: result_adapter")
        check_provider_ref(self.result_adapter, "result_adapter")
        if not isinstance(self.extensions, Mapping):
            raise TypeError("extensions 须为 Mapping")
        object.__setattr__(self, "extensions", freeze_json_value(dict(self.extensions)))

    def to_dict(self) -> dict[str, Any]:
        """序列化为 JSON 兼容独立副本。"""
        return {
            "schema": self.schema,
            "schema_version": self.schema_version,
            "bundle": thaw_json_value(self.bundle),
            "inputs": [_thaw_entry(entry) for entry in self.inputs],
            "command": thaw_json_value(self.command),
            "outputs": [_thaw_entry(entry) for entry in self.outputs],
            "result_adapter": self.result_adapter,
            "extensions": thaw_json_value(self.extensions),
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
        object.__setattr__(self, "detail", freeze_json_value(self.detail))

    def to_dict(self) -> dict[str, Any]:
        """序列化为 JSON 兼容独立副本。"""
        return {
            "bundle_id": self.bundle_id,
            "generator_ref": self.generator_ref,
            "solver_ref": self.solver_ref,
            "status": self.status,
            "exit_code": self.exit_code,
            "detail": thaw_json_value(self.detail),
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
        object.__setattr__(self, "outputs", freeze_json_value(self.outputs))

    def to_dict(self) -> dict[str, Any]:
        """序列化为 JSON 兼容独立副本。"""
        return {
            "schema": self.schema,
            "schema_version": self.schema_version,
            "bundle_id": self.bundle_id,
            "status": self.status,
            "business_outcome": self.business_outcome,
            "outputs": thaw_json_value(self.outputs),
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
    "check_provider_ref",
    "ResultAdapter",
    "SolverBundle",
    "SolverRuntime",
    "freeze_json_value",
    "thaw_json_value",
]
