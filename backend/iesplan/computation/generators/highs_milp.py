"""首个真实 GeneratorProvider：``ies.generator.highs_milp@1.0.0``。

纯内存确定性转换：五项直接输入 → ``SolverBundle`` 完整聚合
(manifest + 输入文件字节)，不写临时目录，不启动 solver，不访问
DB/网络/对象存储/环境变量/用户目录，不读取随机全局状态。

能力范围见 ``DESCRIPTOR``：mode=planning、precision=medium、
solver=``ies.solver.highs@1.7.2``、空 options、输出 solution/metrics、
constant tariff；数学能力即 ``build_project_model`` 现有 lowerer 明确
支持的部分，超出即明确拒绝，不近似、不回退。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from iesplan.assembly.contracts import ValidatedAssemblyArtifact
from iesplan.computation.errors import ComputationUnavailableError
from iesplan.computation.generators.descriptor import GeneratorDescriptor
from iesplan.computation.generators.mps import write_mps
from iesplan.computation.generators.project import build_project_model, finance_component_declarations
from iesplan.computation.protocols import (
    CalculationConfig,
    SolverBundle,
    SolverBundleManifest,
)
from iesplan.computation.sequences import MaterializedSeries
from iesplan.finance import EffectiveFinanceConfig
from iesplan.modeling import DeviceMathContribution

GENERATOR_ID = "ies.generator.highs_milp"
GENERATOR_VERSION = "1.0.0"
GENERATOR_REF = f"{GENERATOR_ID}@{GENERATOR_VERSION}"
SOLVER_REF = "ies.solver.highs@1.7.2"
EXECUTOR_REF = "ies.executor.local_process@1.0.0"
ADAPTER_REF = "ies.result-adapter.highs@1.0.0"
ASSEMBLY_SCHEMA_VERSION = "2.0.0"

#: seed 为 null 时 descriptor 明示的固定种子。
FIXED_SEED = 0

#: 本 provider 支持的输出请求（metrics 为 ResultAdapter 后处理请求，
#: 不对应求解器第二个原始文件）。
SUPPORTED_OUTPUTS = ("solution", "metrics")

PROBLEM_PATH = "input/problem.mps"
PARAMETERS_PATH = "input/parameters.json"
SOLUTION_PATH = "output/solution.sol"
SOLUTION_MEDIA_TYPE = "text/plain"

_EMPTY_OPTIONS_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}

DESCRIPTOR = GeneratorDescriptor(
    generator_id=GENERATOR_ID,
    version=GENERATOR_VERSION,
    options_schema=dict(_EMPTY_OPTIONS_SCHEMA),
    assembly_schema_version=ASSEMBLY_SCHEMA_VERSION,
    modes=("planning",),
    precisions=("medium",),
    capabilities=("affine", "mutex_bigm"),
    solvers=(SOLVER_REF,),
    executors=(EXECUTOR_REF,),
    equation_contracts=("ies.equation.ast",),
    result_adapter=ADAPTER_REF,
    deterministic=True,
    resource_limits={},
    unit_system="SI",
    media_types=("application/mps", "application/json"),
    seed_semantics="config.seed 直接固定进 Bundle；为 null 时使用固定种子 0；不读取随机全局状态",
)


class HighsMilpProvider:
    """HiGHS MILP 生成器实现(无状态纯转换，可直接实例化)。"""

    __slots__ = ()

    @property
    def descriptor(self) -> GeneratorDescriptor:
        """公开能力描述符（与 ``ref`` 单一来源）。"""
        return DESCRIPTOR

    @property
    def ref(self) -> str:
        """生成器精确引用（即描述符引用，不另存第二份）。"""
        return DESCRIPTOR.ref

    def available(self) -> bool:
        """纯内存实现恒可用."""
        return True

    def generate(
        self,
        artifact: ValidatedAssemblyArtifact,
        models: Mapping[str, DeviceMathContribution],
        series: tuple[MaterializedSeries, ...],
        finance: EffectiveFinanceConfig,
        config: CalculationConfig,
    ) -> SolverBundle:
        """由五项直接输入生成完整 SolverBundle 聚合。

        通用选择门禁已由 ``GeneratorRegistry.resolve`` 完成，此处信任；
        依次执行本 provider 特有检查（outputs/实际绑定 tariff，不匹配即
        明确拒绝）→ ``build_project_model`` + ``write_mps``（数学超出
        lowerer 能力即明确不支持）→ manifest + 文件字节原子组装。
        同一五项输入与本 provider 版本恒得逐字节相同的聚合。
        """
        self._check_capabilities(artifact, config, finance)
        seed = config.seed if config.seed is not None else FIXED_SEED
        model, unit_map, objective_unit = build_project_model(
            artifact, models, series, finance, config
        )
        mps_text, offset = write_mps(model)
        components = finance_component_declarations(artifact, models, series, finance)
        metadata = self._metadata(
            artifact, unit_map, offset, objective_unit, seed, config, components
        )
        manifest = SolverBundleManifest(
            bundle={"generator": self.ref, "solver": SOLVER_REF},
            inputs=[
                {"path": PROBLEM_PATH, "media_type": "application/mps"},
                {"path": PARAMETERS_PATH, "media_type": "application/json"},
            ],
            command={
                "executor": EXECUTOR_REF,
                "executable": SOLVER_REF,
                "arguments": [
                    PROBLEM_PATH,
                    "--solution_file",
                    SOLUTION_PATH,
                    "--time_limit",
                    str(config.time_limit_seconds),
                    "--random_seed",
                    str(seed),
                ],
                "working_directory": ".",
                "timeout_seconds": config.time_limit_seconds,
                "network": False,
                "success_exit_codes": [0],
            },
            outputs=[
                {
                    "path": SOLUTION_PATH,
                    "media_type": SOLUTION_MEDIA_TYPE,
                    "required": True,
                }
            ],
            result_adapter=ADAPTER_REF,
            extensions={},
        )
        return SolverBundle(
            manifest=manifest,
            files={
                PROBLEM_PATH: mps_text.encode("utf-8"),
                PARAMETERS_PATH: metadata,
            },
        )

    def _check_capabilities(
        self,
        artifact: ValidatedAssemblyArtifact,
        config: CalculationConfig,
        finance: EffectiveFinanceConfig,
    ) -> None:
        """本 provider 特有检查（通用门禁已由 registry 完成，此处信任）。

        未知输出请求为用户计算请求错误(``ValueError``)，明确拒绝，不忽略。
        时变电价限制只看装配实际绑定的计费价格：快照中未被绑定的价格条目
        不进入数学，不触发拒绝。
        """
        for name in config.outputs:
            if name not in SUPPORTED_OUTPUTS:
                raise ValueError(
                    f"不支持的输出请求: {name!r}（本 provider 只支持 "
                    f"{list(SUPPORTED_OUTPUTS)!r}，未知输出不忽略）"
                )
        doc = json.loads(artifact.canonical_text)
        bindings = doc.get("tariff_bindings") or {}
        for binding_id in sorted(bindings):
            price_id = bindings[binding_id]["price"]
            kind = finance.energy_prices[price_id].kind
            if kind != "constant":
                raise ComputationUnavailableError(
                    f"本 provider 只支持 constant tariff，计费绑定 "
                    f"{binding_id!r} 的价格 {price_id!r} 为 {kind!r}",
                    reason="unsupported-tariff",
                )

    def _metadata(
        self,
        artifact: ValidatedAssemblyArtifact,
        unit_map: Mapping[str, str],
        offset: float,
        objective_unit: str,
        seed: int,
        config: CalculationConfig,
        components: Mapping[str, Mapping[str, object]],
    ) -> bytes:
        """ResultAdapter 后续必需的最小规范 JSON 元数据（规范字节）。

        只记录反向换算与追溯必需项（unit_map、目标偏移、目标业务单位、
        财务分量求值声明 components、请求 outputs、固定 seed、precision、
        装配/引用追溯），不复制完整 assembly、models、finance，不写 hash。
        components 为分量 ident → {unit, caliber, constant, terms}，与目标
        同一分量展开路径，供配套适配器用同一次真实解求值发布。
        """
        doc = json.loads(artifact.canonical_text)
        assembly = doc.get("assembly") or {}
        metadata = {
            "assembly": {
                "id": assembly.get("id"),
                "schema_version": doc.get("schema_version"),
            },
            "components": {
                ident: {
                    "unit": decl["unit"],
                    "caliber": decl["caliber"],
                    "constant": decl["constant"],
                    "terms": dict(decl["terms"]),
                }
                for ident, decl in components.items()
            },
            "generator": self.ref,
            "objective_offset": offset,
            "objective_unit": objective_unit,
            "outputs": list(config.outputs),
            "precision": config.precision,
            "result_adapter": ADAPTER_REF,
            "seed": seed,
            "solver": SOLVER_REF,
            "unit_map": dict(sorted(unit_map.items())),
        }
        return (
            json.dumps(
                metadata, sort_keys=True, ensure_ascii=False, indent=2,
            ).encode("utf-8")
            + b"\n"
        )


#: 组合根显式注册的内置 provider 单例（无状态，可共享）。
HIGHS_MILP_PROVIDER = HighsMilpProvider()


__all__ = [
    "ADAPTER_REF",
    "ASSEMBLY_SCHEMA_VERSION",
    "DESCRIPTOR",
    "EXECUTOR_REF",
    "FIXED_SEED",
    "GENERATOR_ID",
    "GENERATOR_REF",
    "GENERATOR_VERSION",
    "HIGHS_MILP_PROVIDER",
    "HighsMilpProvider",
    "PARAMETERS_PATH",
    "PROBLEM_PATH",
    "SOLUTION_MEDIA_TYPE",
    "SOLUTION_PATH",
    "SOLVER_REF",
    "SUPPORTED_OUTPUTS",
]
