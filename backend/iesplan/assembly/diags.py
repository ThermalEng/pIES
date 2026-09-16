"""ASM 域诊断码目录(设计约束见开发者指南 contracts.md)。

码格式遵循 04 §5.1:<域>-<类别>-<三位序号>;码一经发布永久稳定;
码 ↔ 消息键一一对应(ies.diag.asm.*);修复键独立维护(ies.fix.asm.*)。

登记方式:稳定码统一在 ``core/diagnostics.py::NEW_DIAG_CODES`` 静态登记；
消息键与修复键由本模块自治维护。禁止通过导入副作用改写 core 全局目录。
装配模块统一调用 ``make_asm_diag``，显式把本域消息键传给核心诊断对象。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from iesplan.core.diagnostics import SEVERITY_ERROR, Diagnostic
from iesplan.core.diagnostics import make_diag as _make_diag

# ---------------------------------------------------------------------------
# 阶段 A:语法与结构(parser 内完成)
# ---------------------------------------------------------------------------
ASM_SYN_PARSE = "ASM-SYN-001"  # YAML 解析失败/类型错误(键非字符串、值类型不符、未知键)
ASM_SYN_SECTION = "ASM-SYN-002"  # 未知章节
ASM_SYN_VERSION = "ASM-SYN-003"  # schema_version 不受支持(≠ "2.0.0")
ASM_SYN_FIELD = "ASM-SYN-004"  # 必填字段缺失
ASM_SYN_TYPE = "ASM-SYN-005"  # 键类型错误(如 params 非 map、枚举值非法)

# ---------------------------------------------------------------------------
# ies.assembly 2.0.0 契约:结构 / 资源 / 输出 / 产物一致性(装配不含 calculation)
# ---------------------------------------------------------------------------
ASM_SYN_SCHEMA = "ASM-SYN-006"  # schema 标识无法识别(≠ ies.assembly)
ASM_SYN_VERSION_PIN = "ASM-SYN-007"  # 引用必须固定精确版本(拒绝 latest/范围版本/未版本化别名)
ASM_SYN_FORBIDDEN = "ASM-SYN-008"  # 禁止字段(shell/command/executable/函数模块路径/环境变量/凭证)
ASM_SYN_PATH = "ASM-SYN-009"  # 资源路径非法(绝对路径/.. 逃逸/宿主机路径)
ASM_RES_INVALID = "ASM-RES-001"  # 资源文件不可读(不产生可执行产物)
ASM_OUTPUT_REF = "ASM-OUT-001"  # outputs 引用未定义设备/端口
ASM_CONV_UNMAPPABLE = "ASM-CONV-001"  # 旧项目形态无法映射到 ies.assembly 2.0.0(迁移/导出阻断)
ASM_INPUT_UNDECLARED = "ASM-INPUT-006"  # 参数未在设备模型声明(只允许已声明字段)
ASM_INPUT_PROP_UNIT = "ASM-INPUT-007"  # 属性单位与设备模型量纲不可换算

# ---------------------------------------------------------------------------
# 阶段 B:连接合法性(输入对输出、参数性质一致)
# ---------------------------------------------------------------------------
ASM_EDGE_BAD_SOURCE = "ASM-EDGE-001"  # 边起点不是输出端口(输入对输出)
ASM_EDGE_BAD_SINK = "ASM-EDGE-002"  # 边终点不是输入端口且非母线汇合写法(输入对输出倒挂)
ASM_EDGE_CARRIER = "ASM-EDGE-003"  # 两端载体不一致
ASM_EDGE_QUANTITY = "ASM-EDGE-004"  # 两端物理量不一致
ASM_EDGE_UNIT_DIM = "ASM-EDGE-005"  # 两端单位量纲不可换算
ASM_EDGE_SELF_LOOP = "ASM-EDGE-006"  # 自环(同一设备同一端口连到自身)
ASM_EDGE_DUPLICATE = "ASM-EDGE-007"  # 同两端同载体重复边(多边并行)
ASM_EDGE_LOOSE_BIDI = "ASM-EDGE-008"  # 双向-双向直连且母线无其他确定方向端口(警告)

# ---------------------------------------------------------------------------
# 装配 2.0.0 接口网络纯协议校验(0.8.0 切片): 连接/绑定/内容锁
# ---------------------------------------------------------------------------
ASM_EDGE_RANGE_CONFLICT = "ASM-EDGE-010"  # 连接两端有效区间无交集(值域冲突)
ASM_EDGE_BAD_ENDPOINT = "ASM-EDGE-011"  # 连接端点不可连接(predefined/blind 永不进入 connections)
ASM_BIND_INVALID = "ASM-BIND-001"  # 预定义绑定非法(接口类型/来源模式/数据引用不匹配)
ASM_FIN_INVALID = "ASM-FIN-001"  # finance 精确引用/有效快照非法(引用形状/快照身份/解析)
ASM_COST_BIND_INVALID = "ASM-BIND-002"  # 设备成本绑定非法(实例缺席/类别/driver/量纲)
ASM_TAR_INVALID = "ASM-TAR-001"  # 能源计费绑定非法(price/实例/接口/载体/方向/序列元数据)
ASM_LOCK_MISMATCH = "ASM-LOCK-001"  # 设备实例 definition 与 descriptor 内容锁不一致

# ---------------------------------------------------------------------------
# 模型引用与输入完备(引用/输入判定由校验器直接执行)
# ---------------------------------------------------------------------------
ASM_REF_MODEL_UNREG = "ASM-REF-002"  # 模型命令未注册(注册表快照无 ies.device.*@version)
ASM_REF_PORT_UNDEF = "ASM-REF-003"  # 端口引用 <dev>.<port> 不存在

ASM_INPUT_UNFED = "ASM-INPUT-001"  # 设备输入端口无来边(输入不完备)
ASM_INPUT_RANGE = "ASM-INPUT-003"  # 参数值越界/枚举不符(error 级,非阻断)

# ---------------------------------------------------------------------------
# 阶段 D:整体可解性(母线级约束不足/过度)
# ---------------------------------------------------------------------------
ASM_SOLV_NO_SOURCE = "ASM-SOLV-001"  # 约束不足:母线无源(只有汇)
ASM_SOLV_NO_SINK = "ASM-SOLV-002"  # 约束不足/能量无归处:母线无汇(只有源,无储能/export)
ASM_SOLV_INFEASIBLE = "ASM-SOLV-003"  # 必然不可行:无任何可调手段且 Σ固定供给 < Σ需求
ASM_SOLV_OVER_CONSTRAINED = "ASM-SOLV-004"  # 约束过度:互斥固定约束(供给>需求无调节、禁反送与过剩并存)
ASM_SOLV_ORPHAN = "ASM-SOLV-006"  # 孤立设备/孤立母线(警告)
ASM_SOLV_DOF = "ASM-SOLV-007"  # 自由度提示(info)

# ---------------------------------------------------------------------------
# 规划表达式(复用 core/expression.py 公共引擎, 只接受普通标识符 Name)
# ---------------------------------------------------------------------------
ASM_PLAN_SYNTAX = "ASM-PLAN-001"  # 规划表达式语法错误(复用 EXPR-SYN-001 语义)
ASM_PLAN_DIM = "ASM-PLAN-002"  # 规划表达式量纲/币种不一致(复用 EXPR-DIM-001 语义)
ASM_PLAN_UNDEF = "ASM-PLAN-003"  # 规划表达式引用未定义标识符/未实际产生的分量/沉没分量/保留名冲突

# ---------------------------------------------------------------------------
# 码 → 消息键 / 修复键映射(与 core/diagnostics.py DIAG_MESSAGE_KEYS 同构)
# ---------------------------------------------------------------------------
ASM_MESSAGE_KEYS: dict[str, str] = {
    # syntax.*
    ASM_SYN_PARSE: "ies.diag.asm.syntax.parse",
    ASM_SYN_SECTION: "ies.diag.asm.syntax.unknown_section",
    ASM_SYN_VERSION: "ies.diag.asm.syntax.version",
    ASM_SYN_FIELD: "ies.diag.asm.syntax.missing_field",
    ASM_SYN_TYPE: "ies.diag.asm.syntax.bad_type",
    ASM_SYN_SCHEMA: "ies.diag.asm.syntax.schema_unknown",
    ASM_SYN_VERSION_PIN: "ies.diag.asm.syntax.version_not_pinned",
    ASM_SYN_FORBIDDEN: "ies.diag.asm.syntax.forbidden_field",
    ASM_SYN_PATH: "ies.diag.asm.syntax.path_invalid",
    # edge.*
    ASM_EDGE_BAD_SOURCE: "ies.diag.asm.edge.bad_source",
    ASM_EDGE_BAD_SINK: "ies.diag.asm.edge.bad_sink",
    ASM_EDGE_CARRIER: "ies.diag.asm.edge.carrier",
    ASM_EDGE_QUANTITY: "ies.diag.asm.edge.quantity",
    ASM_EDGE_UNIT_DIM: "ies.diag.asm.edge.unit_dim",
    ASM_EDGE_SELF_LOOP: "ies.diag.asm.edge.self_loop",
    ASM_EDGE_DUPLICATE: "ies.diag.asm.edge.duplicate",
    ASM_EDGE_LOOSE_BIDI: "ies.diag.asm.edge.loose_bidi",
    ASM_EDGE_RANGE_CONFLICT: "ies.diag.asm.edge.range_conflict",
    ASM_EDGE_BAD_ENDPOINT: "ies.diag.asm.edge.bad_endpoint",
    ASM_BIND_INVALID: "ies.diag.asm.bind.invalid",
    ASM_FIN_INVALID: "ies.diag.asm.finance.invalid",
    ASM_COST_BIND_INVALID: "ies.diag.asm.cost_bind.invalid",
    ASM_LOCK_MISMATCH: "ies.diag.asm.lock.mismatch",
    # ref.*
    ASM_REF_MODEL_UNREG: "ies.diag.asm.ref.model_unregistered",
    ASM_REF_PORT_UNDEF: "ies.diag.asm.ref.port_undefined",
    # input.*
    ASM_INPUT_UNFED: "ies.diag.asm.input.port_unfed",
    ASM_INPUT_RANGE: "ies.diag.asm.input.param_range",
    ASM_INPUT_UNDECLARED: "ies.diag.asm.input.param_undeclared",
    ASM_INPUT_PROP_UNIT: "ies.diag.asm.input.prop_unit_dim",
    # res.*
    ASM_RES_INVALID: "ies.diag.asm.res.invalid",
    # tariff.*
    ASM_TAR_INVALID: "ies.diag.asm.tariff.invalid",
    # out.*
    ASM_OUTPUT_REF: "ies.diag.asm.out.ref_undefined",
    # conv.*
    ASM_CONV_UNMAPPABLE: "ies.diag.asm.conv.unmappable",
    # solv.*
    ASM_SOLV_NO_SOURCE: "ies.diag.asm.solv.no_source",
    ASM_SOLV_NO_SINK: "ies.diag.asm.solv.no_sink",
    ASM_SOLV_INFEASIBLE: "ies.diag.asm.solv.infeasible",
    ASM_SOLV_OVER_CONSTRAINED: "ies.diag.asm.solv.over_constrained",
    ASM_SOLV_ORPHAN: "ies.diag.asm.solv.orphan",
    ASM_SOLV_DOF: "ies.diag.asm.solv.dof_info",
    # plan.*
    ASM_PLAN_SYNTAX: "ies.diag.asm.plan.syntax",
    ASM_PLAN_DIM: "ies.diag.asm.plan.dim",
    ASM_PLAN_UNDEF: "ies.diag.asm.plan.undefined_symbol",
}

ASM_FIX_HINT_KEYS: dict[str, str] = {
    code: "ies.fix.asm." + key[len("ies.diag.asm."):]
    for code, key in ASM_MESSAGE_KEYS.items()
}

#: 集中登记(供测试断言与 core/diagnostics.py NEW_DIAG_CODES 模式引用)
ASM_ALL_CODES: tuple[str, ...] = tuple(ASM_MESSAGE_KEYS)

#: 消息键类别前缀
ASM_KEY_CATEGORIES: tuple[str, ...] = (
    "syntax",
    "edge",
    "bind",
    "finance",
    "cost_bind",
    "tariff",
    "lock",
    "ref",
    "input",
    "solv",
    "plan",
    "res",
    "out",
    "artifact",
    "conv",
)


def make_asm_diag(
    code: str,
    severity: str = SEVERITY_ERROR,
    *,
    blocking: bool | None = None,
    params: Mapping[str, object] | None = None,
    location: Mapping[str, object] | None = None,
    ref_ids: Sequence[str] | None = None,
    source: str = "",
    trace_id: str = "",
    project_id: str = "",
    task_id: str = "",
) -> Diagnostic:
    """构造 ASM 域诊断，不依赖或修改 core 的业务消息目录。"""
    try:
        message_key = ASM_MESSAGE_KEYS[code]
        fix_hint_key = ASM_FIX_HINT_KEYS[code]
    except KeyError as exc:
        raise ValueError(f"未登记 ASM 诊断码: {code!r}") from exc
    return _make_diag(
        code,
        severity,
        message_key,
        fix_hint_key,
        blocking=blocking,
        params=params,
        location=location,
        ref_ids=ref_ids,
        source=source,
        trace_id=trace_id,
        project_id=project_id,
        task_id=task_id,
    )


__all__ = [
    "ASM_SYN_PARSE",
    "ASM_SYN_SECTION",
    "ASM_SYN_VERSION",
    "ASM_SYN_FIELD",
    "ASM_SYN_TYPE",
    "ASM_SYN_SCHEMA",
    "ASM_SYN_VERSION_PIN",
    "ASM_SYN_FORBIDDEN",
    "ASM_SYN_PATH",
    "ASM_RES_INVALID",
    "ASM_TAR_INVALID",
    "ASM_OUTPUT_REF",
    "ASM_CONV_UNMAPPABLE",
    "ASM_INPUT_UNDECLARED",
    "ASM_INPUT_PROP_UNIT",
    "ASM_EDGE_BAD_SOURCE",
    "ASM_EDGE_BAD_SINK",
    "ASM_EDGE_CARRIER",
    "ASM_EDGE_QUANTITY",
    "ASM_EDGE_UNIT_DIM",
    "ASM_EDGE_SELF_LOOP",
    "ASM_EDGE_DUPLICATE",
    "ASM_EDGE_LOOSE_BIDI",
    "ASM_EDGE_RANGE_CONFLICT",
    "ASM_EDGE_BAD_ENDPOINT",
    "ASM_BIND_INVALID",
    "ASM_FIN_INVALID",
    "ASM_COST_BIND_INVALID",
    "ASM_LOCK_MISMATCH",
    "ASM_REF_MODEL_UNREG",
    "ASM_REF_PORT_UNDEF",
    "ASM_INPUT_UNFED",
    "ASM_INPUT_RANGE",
    "ASM_SOLV_NO_SOURCE",
    "ASM_SOLV_NO_SINK",
    "ASM_SOLV_INFEASIBLE",
    "ASM_SOLV_OVER_CONSTRAINED",
    "ASM_SOLV_ORPHAN",
    "ASM_SOLV_DOF",
    "ASM_PLAN_SYNTAX",
    "ASM_PLAN_DIM",
    "ASM_PLAN_UNDEF",
    "ASM_MESSAGE_KEYS",
    "ASM_FIX_HINT_KEYS",
    "ASM_ALL_CODES",
    "ASM_KEY_CATEGORIES",
    "make_asm_diag",
]
