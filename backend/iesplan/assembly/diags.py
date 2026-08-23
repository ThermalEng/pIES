"""ASM 域诊断码目录(设计约束见开发者指南 contracts.md)。

码格式遵循 04 §5.1:<域>-<类别>-<三位序号>;码一经发布永久稳定;
码 ↔ 消息键一一对应(ies.diag.asm.*);修复键独立维护(ies.fix.asm.*)。

登记方式:本模块导入时把 ASM 域码/消息键/修复键**运行期登记**进
core/diagnostics.py 的目录字典(只增不改既有码,不修改任何既有文件),
使 make_diag/Diagnostic 的"未登记码拒绝"校验对 ASM 码放行,同时保持
core/diagnostics.py 的文档目录语义(新码集中声明)。
"""

from __future__ import annotations

from iesplan.core import diagnostics as _core_diags

# ---------------------------------------------------------------------------
# 阶段 A:语法与结构(parser 内完成)
# ---------------------------------------------------------------------------
ASM_SYN_PARSE = "ASM-SYN-001"  # YAML 解析失败/类型错误(键非字符串、值类型不符、未知键)
ASM_SYN_SECTION = "ASM-SYN-002"  # 未知章节
ASM_SYN_VERSION = "ASM-SYN-003"  # schema_version/format_version 不受支持(≠ "1.0.0"/"1.0")
ASM_SYN_FIELD = "ASM-SYN-004"  # 必填字段缺失
ASM_SYN_TYPE = "ASM-SYN-005"  # 键类型错误(如 params 非 map、delay_steps 非整数、枚举值非法)

# ---------------------------------------------------------------------------
# ies.assembly 1.0.0 契约(0.7.0):结构 / 资源 / 计算 / 输出 / 产物一致性
# ---------------------------------------------------------------------------
ASM_SYN_SCHEMA = "ASM-SYN-006"  # schema 标识无法识别(≠ ies.assembly)
ASM_SYN_VERSION_PIN = "ASM-SYN-007"  # 引用必须固定精确版本(拒绝 latest/范围版本/未版本化别名)
ASM_SYN_FORBIDDEN = "ASM-SYN-008"  # 禁止字段(shell/command/executable/函数模块路径/环境变量/凭证)
ASM_SYN_PATH = "ASM-SYN-009"  # 资源路径非法(绝对路径/.. 逃逸/宿主机路径)
ASM_RES_INVALID = "ASM-RES-001"  # 资源文件不可读或摘要不一致(不产生可执行产物)
ASM_CALC_MODE = "ASM-CALC-001"  # calculation.mode 非法
ASM_CALC_OPTIONS = "ASM-CALC-002"  # calculation.options 非法(未知键/非标量/非有限值)
ASM_OUTPUT_REF = "ASM-OUT-001"  # outputs 引用未定义设备/端口
ASM_ART_MISMATCH = "ASM-ART-001"  # 产物一致性校验失败(规范文本/摘要/回执不一致)
ASM_CONV_UNMAPPABLE = "ASM-CONV-001"  # 旧形态无法映射到 ies.assembly 1.0.0(迁移/导出阻断)
ASM_INPUT_UNDECLARED = "ASM-INPUT-006"  # 参数未在设备模型声明(ies.assembly 1.0.0: 只允许已声明字段)

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
ASM_EDGE_ZERO_CAP = "ASM-EDGE-009"  # 边容量为 0 或负值(警告)

# ---------------------------------------------------------------------------
# 阶段 C:模型可解性(引用与输入完备)
# ---------------------------------------------------------------------------
ASM_REF_DUP_DEVICE = "ASM-REF-001"  # 设备实例 id 重复
ASM_REF_MODEL_UNREG = "ASM-REF-002"  # 模型命令未注册(注册表快照无 ies.device.*@version)
ASM_REF_PORT_UNDEF = "ASM-REF-003"  # 端口引用 <dev>.<port> 不存在
ASM_REF_DATASET = "ASM-REF-004"  # 数据集引用缺失(版本/列/分辨率)
ASM_REF_PORT_DECL = "ASM-REF-005"  # 端口显式声明与注册表推导不一致(警告,以注册表为准)

ASM_INPUT_UNFED = "ASM-INPUT-001"  # 设备输入端口无来边(输入不完备)
ASM_INPUT_PARAM = "ASM-INPUT-002"  # 必填参数缺失
ASM_INPUT_RANGE = "ASM-INPUT-003"  # 参数值越界/枚举不符(error 级,非阻断)
ASM_INPUT_LOAD_DATA = "ASM-INPUT-004"  # 负荷类设备缺 data_refs(无 profile 数据不可解)
ASM_INPUT_DATA_UNIT = "ASM-INPUT-005"  # data_refs 声明单位与端口单位量纲不可换算

ASM_PIPE_DELAY_MISSING = "ASM-PIPE-001"  # 管道设备缺 delay_steps(警告,按 1 处理)
ASM_PIPE_DELAY_RANGE = "ASM-PIPE-002"  # delay_steps 超出时间轴范围(≥ 年步数或 ≤ 0)
ASM_PIPE_NOT_PATH = "ASM-PIPE-003"  # 管道设备无入边或无出边(未形成通路,警告)

# ---------------------------------------------------------------------------
# 阶段 D:整体可解性(母线级约束不足/过度)
# ---------------------------------------------------------------------------
ASM_SOLV_NO_SOURCE = "ASM-SOLV-001"  # 约束不足:母线无源(只有汇)
ASM_SOLV_NO_SINK = "ASM-SOLV-002"  # 约束不足/能量无归处:母线无汇(只有源,无储能/export)
ASM_SOLV_INFEASIBLE = "ASM-SOLV-003"  # 必然不可行:无任何可调手段且 Σ固定供给 < Σ需求
ASM_SOLV_OVER_CONSTRAINED = "ASM-SOLV-004"  # 约束过度:互斥固定约束(供给>需求无调节、禁反送与过剩并存)
ASM_SOLV_CAUSAL_CYCLE = "ASM-SOLV-005"  # 因果环:有状态设备(管道/延迟)构成闭环
ASM_SOLV_ORPHAN = "ASM-SOLV-006"  # 孤立设备/孤立母线(警告)
ASM_SOLV_DOF = "ASM-SOLV-007"  # 自由度提示(info)

# ---------------------------------------------------------------------------
# 约束表达式
# ---------------------------------------------------------------------------
ASM_CONST_SYNTAX = "ASM-CONST-001"  # 表达式语法错误(复用 EXPR-SYN-001 语义)
ASM_CONST_DIM = "ASM-CONST-002"  # 表达式量纲不一致(复用 EXPR-DIM-001 语义)
ASM_CONST_UNDEF = "ASM-CONST-003"  # 表达式引用未定义设备/端口/参数符号

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
    ASM_EDGE_ZERO_CAP: "ies.diag.asm.edge.zero_capacity",
    # ref.*
    ASM_REF_DUP_DEVICE: "ies.diag.asm.ref.dup_device",
    ASM_REF_MODEL_UNREG: "ies.diag.asm.ref.model_unregistered",
    ASM_REF_PORT_UNDEF: "ies.diag.asm.ref.port_undefined",
    ASM_REF_DATASET: "ies.diag.asm.ref.dataset_missing",
    ASM_REF_PORT_DECL: "ies.diag.asm.ref.port_decl_mismatch",
    # input.*
    ASM_INPUT_UNFED: "ies.diag.asm.input.port_unfed",
    ASM_INPUT_PARAM: "ies.diag.asm.input.param_missing",
    ASM_INPUT_RANGE: "ies.diag.asm.input.param_range",
    ASM_INPUT_LOAD_DATA: "ies.diag.asm.input.load_no_data",
    ASM_INPUT_DATA_UNIT: "ies.diag.asm.input.data_unit_dim",
    ASM_INPUT_UNDECLARED: "ies.diag.asm.input.param_undeclared",
    # res.*
    ASM_RES_INVALID: "ies.diag.asm.res.invalid",
    # calc.*
    ASM_CALC_MODE: "ies.diag.asm.calc.mode",
    ASM_CALC_OPTIONS: "ies.diag.asm.calc.options",
    # out.*
    ASM_OUTPUT_REF: "ies.diag.asm.out.ref_undefined",
    # artifact.*
    ASM_ART_MISMATCH: "ies.diag.asm.artifact.mismatch",
    # conv.*
    ASM_CONV_UNMAPPABLE: "ies.diag.asm.conv.unmappable",
    # pipe.*
    ASM_PIPE_DELAY_MISSING: "ies.diag.asm.pipe.delay_missing",
    ASM_PIPE_DELAY_RANGE: "ies.diag.asm.pipe.delay_out_of_range",
    ASM_PIPE_NOT_PATH: "ies.diag.asm.pipe.not_in_path",
    # solv.*
    ASM_SOLV_NO_SOURCE: "ies.diag.asm.solv.no_source",
    ASM_SOLV_NO_SINK: "ies.diag.asm.solv.no_sink",
    ASM_SOLV_INFEASIBLE: "ies.diag.asm.solv.infeasible",
    ASM_SOLV_OVER_CONSTRAINED: "ies.diag.asm.solv.over_constrained",
    ASM_SOLV_CAUSAL_CYCLE: "ies.diag.asm.solv.causal_cycle",
    ASM_SOLV_ORPHAN: "ies.diag.asm.solv.orphan",
    ASM_SOLV_DOF: "ies.diag.asm.solv.dof_info",
    # const.*
    ASM_CONST_SYNTAX: "ies.diag.asm.const.syntax",
    ASM_CONST_DIM: "ies.diag.asm.const.dim",
    ASM_CONST_UNDEF: "ies.diag.asm.const.undefined_symbol",
}

ASM_FIX_HINT_KEYS: dict[str, str] = {
    code: "ies.fix.asm." + key[len("ies.diag.asm."):]
    for code, key in ASM_MESSAGE_KEYS.items()
}

#: 集中登记(供测试断言与 core/diagnostics.py NEW_DIAG_CODES 模式引用)
ASM_ALL_CODES: tuple[str, ...] = tuple(ASM_MESSAGE_KEYS)

#: 消息键类别前缀(与 04 §4 登记层级一致)
ASM_KEY_CATEGORIES: tuple[str, ...] = (
    "syntax", "edge", "ref", "input", "pipe", "solv", "const", "res", "calc", "out", "artifact", "conv",
)

# ---------------------------------------------------------------------------
# 运行期登记(导入即生效;只增不改既有码)
# ---------------------------------------------------------------------------
_core_diags.DIAG_MESSAGE_KEYS.update(ASM_MESSAGE_KEYS)
_core_diags.DIAG_FIX_HINT_KEYS.update(ASM_FIX_HINT_KEYS)


def register_asm_codes() -> None:
    """显式登记 ASM 域码(幂等;模块导入时已自动执行,供测试断言使用)。"""
    _core_diags.DIAG_MESSAGE_KEYS.update(ASM_MESSAGE_KEYS)
    _core_diags.DIAG_FIX_HINT_KEYS.update(ASM_FIX_HINT_KEYS)


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
    "ASM_CALC_MODE",
    "ASM_CALC_OPTIONS",
    "ASM_OUTPUT_REF",
    "ASM_ART_MISMATCH",
    "ASM_CONV_UNMAPPABLE",
    "ASM_INPUT_UNDECLARED",
    "ASM_EDGE_BAD_SOURCE",
    "ASM_EDGE_BAD_SINK",
    "ASM_EDGE_CARRIER",
    "ASM_EDGE_QUANTITY",
    "ASM_EDGE_UNIT_DIM",
    "ASM_EDGE_SELF_LOOP",
    "ASM_EDGE_DUPLICATE",
    "ASM_EDGE_LOOSE_BIDI",
    "ASM_EDGE_ZERO_CAP",
    "ASM_REF_DUP_DEVICE",
    "ASM_REF_MODEL_UNREG",
    "ASM_REF_PORT_UNDEF",
    "ASM_REF_DATASET",
    "ASM_REF_PORT_DECL",
    "ASM_INPUT_UNFED",
    "ASM_INPUT_PARAM",
    "ASM_INPUT_RANGE",
    "ASM_INPUT_LOAD_DATA",
    "ASM_INPUT_DATA_UNIT",
    "ASM_PIPE_DELAY_MISSING",
    "ASM_PIPE_DELAY_RANGE",
    "ASM_PIPE_NOT_PATH",
    "ASM_SOLV_NO_SOURCE",
    "ASM_SOLV_NO_SINK",
    "ASM_SOLV_INFEASIBLE",
    "ASM_SOLV_OVER_CONSTRAINED",
    "ASM_SOLV_CAUSAL_CYCLE",
    "ASM_SOLV_ORPHAN",
    "ASM_SOLV_DOF",
    "ASM_CONST_SYNTAX",
    "ASM_CONST_DIM",
    "ASM_CONST_UNDEF",
    "ASM_MESSAGE_KEYS",
    "ASM_FIX_HINT_KEYS",
    "ASM_ALL_CODES",
    "ASM_KEY_CATEGORIES",
    "register_asm_codes",
]
