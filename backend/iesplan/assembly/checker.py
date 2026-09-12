"""装配检查器编排:阶段 B(连接合法性)→ C(模型可解性)→ D(整体可解性)→ 约束表达式。

W1-Assembly 收敛:端口/模型解析、单位量纲、电网识别等共享能力已下沉到
``iesplan.assembly.context``(公开接口),阶段规则经 ``iesplan.assembly.rules``
消费 context;本模块仅保留编排入口与结果类型,并为存量调用方保留
同名重导出/兼容别名,公开行为不变。

入口统一返回 CheckResult(diagnostics 全量收集,不做短路,一次检查给出完整清单)。
check_graph_inputs 保留为旧 AssemblySpec 格式的兼容检查入口；生产任务下发已收敛到
validator.validate_project_export，并只消费 ValidatedAssemblyArtifact。

端口解析(注册表推导 + 显式声明覆盖)在 context 完成并缓存到 CheckContext.resolved_ports:
- 设备端口:按设备类型业务方向表(application/models 同约定)推导,载体→(物理量, 标准单位);
- 管道端口:入端 instantaneous / 出端 delayed(延迟步数取 params.delay_steps);
- 显式 `ports:` 声明仅覆盖 capacity(与推导不一致按 ASM-REF-005 告警,注册表为准)。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from iesplan.assembly.context import (
    PIPELINE_MODEL_IDS,
    PORT_TYPE_TO_CARRIER,
    BusSummary,
    CheckContext,
    default_registry,
    derive_device_ports,
    derive_pipeline_ports,
    ensure_ports,
    resolve_model,
    resolve_ports,
    units_compatible,
    yaml_device_ports,
)
from iesplan.assembly.rules.constraints import run_constraint_checks
from iesplan.assembly.schema import AssemblySpec
from iesplan.core.diagnostics import SEVERITY_BLOCKING, SEVERITY_ERROR, Diagnostic
from iesplan.core.errors import AppError


def _port_name(carrier: str, direction: str) -> str:
    """端口命名:in/out 为 "{载体}_{方向}",双向为 "{载体}"。"""
    return f"{carrier}_{direction}" if direction in ("in", "out") else carrier


def _yaml_device_ports(device, type_id):
    """兼容入口:委托 context.yaml_device_ports(存量测试 monkeypatch 点)。"""
    return yaml_device_ports(device, type_id)


def _derive_device_ports(spec, ctx, device):
    """兼容入口:委托 context.derive_device_ports。

    经 ``port_source`` 显式传入本模块命名空间钩子,使存量测试对
    ``checker._yaml_device_ports`` 的 monkeypatch 继续生效。
    """
    return derive_device_ports(spec, ctx, device, port_source=_yaml_device_ports)


def _derive_pipeline_ports(pipe):
    """兼容入口:委托 context.derive_pipeline_ports。"""
    return derive_pipeline_ports(pipe)


# ---------------------------------------------------------------------------
# 结果
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class CheckResult:
    """检查结果:诊断全量 + 母线汇总。"""

    diagnostics: list[Diagnostic]
    buses: list[BusSummary] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """无 error/blocking 级诊断。"""
        return all(d.severity not in (SEVERITY_ERROR, SEVERITY_BLOCKING) for d in self.diagnostics)

    @property
    def blocking_diags(self) -> list[Diagnostic]:
        """blocking/error 级诊断列表。"""
        return [d for d in self.diagnostics if d.severity in (SEVERITY_ERROR, SEVERITY_BLOCKING)]

    def by_code(self, code: str) -> list[Diagnostic]:
        """按诊断码筛选。"""
        return [d for d in self.diagnostics if d.code == code]


class AssemblyCheckError(AppError):
    """旧 AssemblySpec 检查未通过（存在 error/blocking 级诊断）。

    携带完整诊断列表，供仍显式调用 ``check_graph_inputs`` 的兼容入口使用；
    生产任务闸门使用 ``AssemblyValidationError``，不再签发本错误对应的旧产物。
    """

    code = "ASM-CHECK-FAILED"
    message_key = "ies.diag.asm.check_failed"
    http_status = 422

    def __init__(self, diagnostics: list[Diagnostic], message: str = "") -> None:
        self.diagnostics = list(diagnostics)
        super().__init__(
            message or f"装配检查未通过:{len(self.diagnostics)} 条诊断",
            code=self.code,
            message_key=self.message_key,
            params={
                "diag_count": len(self.diagnostics),
                "diagnostics": [d.to_dict() for d in self.diagnostics],
            },
        )


# ---------------------------------------------------------------------------
# 编排入口
# ---------------------------------------------------------------------------


def _default_context(spec: AssemblySpec | None = None) -> CheckContext:
    """默认检查上下文:注册表快照 + 按 spec 时间轴惰性加载。"""
    ctx = CheckContext(registry=default_registry())
    if spec is not None and spec.time_axis is not None:
        ctx.time_axis = {"n": spec.time_axis.steps_per_year, "resolution": spec.time_axis.resolution}
    return ctx


def check_assembly(spec: AssemblySpec, *, ctx: CheckContext | None = None) -> CheckResult:
    """装配对象检查:阶段 B/C/D 全量执行;阶段 A 已由 parse 完成(文本入口再次校验)。"""
    from iesplan.assembly.rules import run_phase_b, run_phase_c, run_phase_d

    ctx = ctx or _default_context(spec)
    ensure_ports(spec, ctx)
    diags = run_phase_b(spec, ctx)
    diags += run_phase_c(spec, ctx)
    d_diags, buses = run_phase_d(spec, ctx)
    diags += d_diags
    diags += run_constraint_checks(spec, ctx)
    diags = diags[: ctx.max_diags]
    return CheckResult(diagnostics=diags, buses=buses)


def check_assembly_text(text: str, *, ctx: CheckContext | None = None) -> CheckResult:
    """文本 → parse(A) → check(B/C/D),一次调用返回完整结果。"""
    from iesplan.assembly.parser import parse_assembly

    result = parse_assembly(text)
    if result.spec is None:
        return CheckResult(diagnostics=list(result.diagnostics))
    return check_assembly(result.spec, ctx=ctx)


def check_graph_inputs(
    project_version_content: dict,
    *,
    datasets: dict[int, dict] | None = None,
    ctx: CheckContext | None = None,
) -> CheckResult:
    """旧项目内容兼容检查：content → build_assembly → check_assembly。

    新生产计算路径不得以此结果作为持久输入；应调用
    ``validator.validate_project_export`` 并消费 ``ValidatedAssemblyArtifact``。
    content 结构：{"model": {devices, ports, connections}, "calc_config": {...}}
    或扁平图结构。
    """
    from iesplan.assembly.builder import build_assembly

    model_part = project_version_content.get("model", project_version_content)
    if not isinstance(model_part, dict):
        model_part = {}
    graph = {
        "devices": model_part.get("devices", []),
        "ports": model_part.get("ports", []),
        "connections": model_part.get("connections", []),
    }
    calc_cfg = project_version_content.get("calc_config")
    calc_config = calc_cfg if isinstance(calc_cfg, dict) else None
    spec = build_assembly(graph, datasets=datasets, calc_config=calc_config)
    # 数据集元信息必须进入检查上下文:缺失版本/列/分辨率检查仅在 ctx.datasets
    # 非 None 时执行(codex 二次审核 High-1: 之前只喂给 builder, 闸门检查被绕过)
    ctx = ctx or _default_context(spec)
    if datasets is not None and ctx.datasets is None:
        ctx.datasets = datasets
    return check_assembly(spec, ctx=ctx)


__all__ = [
    "CheckContext",
    "BusSummary",
    "CheckResult",
    "AssemblyCheckError",
    "check_assembly",
    "check_assembly_text",
    "check_graph_inputs",
    "resolve_ports",
    "resolve_model",
    "ensure_ports",
    "units_compatible",
    "run_constraint_checks",
    "PIPELINE_MODEL_IDS",
    "PORT_TYPE_TO_CARRIER",
]
