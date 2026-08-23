"""诊断体系:诊断对象与诊断码目录。

设计约束见开发者指南 contracts.md:
- 诊断码格式 `<域>-<类别>-<三位序号>`(如 DATA-TS-001),码一旦发布即永久稳定;
- severity 取值 blocking | error | warning | info,blocking 为独立布尔与 severity 正交;
- 诊断码与消息键一一对应(如 DATA-TS-001 ↔ ies.diag.data.ts_dup);
- 后端只输出 code + message_key + params,不输出任何人类可读文案。

本模块只引用 04 第 5.3 节与第 9 节已登记的诊断码;新码集中在 NEW_DIAG_CODES 声明。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

# ---------------------------------------------------------------------------
# 严重度常量
# ---------------------------------------------------------------------------
SEVERITY_BLOCKING = "blocking"
SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"
SEVERITY_INFO = "info"

SEVERITIES: tuple[str, ...] = (SEVERITY_BLOCKING, SEVERITY_ERROR, SEVERITY_WARNING, SEVERITY_INFO)

# ---------------------------------------------------------------------------
# 诊断码目录(04 文档已登记码,按域-类别-编号排列)
# ---------------------------------------------------------------------------

# DATA 域:时序列/列/值
DATA_TS_DUP = "DATA-TS-001"  # 时序列重复时间戳
DATA_TS_GAP = "DATA-TS-002"  # 时序列缺口(已插值补齐)
DATA_TS_LEAP = "DATA-TS-003"  # 含闰日(2 月 29 日),标准日历不接受 366 天
DATA_COL_MISSING = "DATA-COL-001"  # 缺少必需列
DATA_COL_UNIT_UNKNOWN = "DATA-COL-002"  # 无法识别列的单位

# CONN 域:连接/拓扑
CONN_TYPE_UNREGISTERED = "CONN-TYPE-002"  # 设备类型未注册
CONN_NODE_ORPHAN = "CONN-NODE-001"  # 设备未连接到任何节点

# PARAM 域:参数校验
PARAM_RNG_OUT = "PARAM-RNG-003"  # 参数越界
PARAM_UNIT_MISMATCH = "PARAM-UNIT-002"  # 单位不匹配
PARAM_UNIT_INCONSISTENT = "PARAM-UNIT-003"  # 平衡方程量纲不一致(02 §2.4)
PARAM_CONFLICT = "PARAM-CONF-001"  # 参数相互冲突

# TASK 域:任务
TASK_QUEUED = "TASK-QUEUE-001"  # 已加入队列
TASK_SOLVE_FAILED = "TASK-SOLVE-001"  # 求解失败
TASK_INFEASIBLE = "TASK-SOLVE-002"  # 无可行解
TASK_BASE_INFEASIBLE = "TASK-SOLVE-003"  # 基准方案无可行解(02 §5.3)
TASK_TIMEOUT = "TASK-TIMEOUT-001"  # 超时
TASK_DATA_SNAPSHOT_MISSING = "TASK-DATA-001"  # 计算快照缺失
TASK_DATA_HASH_MISMATCH = "TASK-DATA-002"  # 快照哈希校验失败

# RES 域:结果
RES_MISSING = "RES-MISS-002"  # 结果缺失
RES_NUM_INVALID = "RES-NUM-001"  # 非数值结果
RES_RANGE_OUT = "RES-RANGE-001"  # 越出物理合理区间
RES_PRECISION_MISMATCH = "RES-RANGE-004"  # 跨精度比较

# SEC 域:安全/注册
SEC_REG_INTEGRITY = "SEC-REG-001"  # 扩展校验失败(校验和/签名)
SEC_REG_SESSION = "SEC-REG-002"  # 扩展访问会话
SEC_REG_DB = "SEC-REG-003"  # 扩展访问 DB
SEC_REG_PATH = "SEC-REG-004"  # 扩展访问任意路径
SEC_REG_SANDBOX = "SEC-REG-005"  # 扩展越界声明/任意代码执行
SEC_REG_NETWORK = "SEC-REG-006"  # 扩展出网
SEC_REG_REFLECTION = "SEC-REG-007"  # 反射/动态加载

# EXPR 域:表达式引擎
EXPR_SYNTAX = "EXPR-SYN-001"  # 语法错误
EXPR_TYPE = "EXPR-TYP-001"  # 类型错误
EXPR_DIM = "EXPR-DIM-001"  # 量纲不一致
EXPR_RANGE = "EXPR-RNG-001"  # 范围检查失败
EXPR_SECURITY = "EXPR-SEC-001"  # 含禁止函数/白名单外标识符
EXPR_RUN = "EXPR-RUN-001"  # 运行期错误(超时/溢出/除零)
EXPR_CODE = "EXPR-CODE-001"  # 变量未在 expr_context 中登记

# PERM 域:权限
PERM_DENIED = "PERM-DENIED-001"  # 无权限执行操作

# SYS 域:系统
SYS_STORE_CORRUPT = "SYS-STORE-001"  # 项目文件损坏
SYS_STORE_MIGRATION_FAILED = "SYS-STORE-002"  # 项目迁移失败
SYS_STORE_QUOTA_EXCEEDED = "SYS-STORE-003"  # 存储空间不足
SYS_CFG_INVALID = "SYS-CFG-001"  # 配置无效(新码,见 NEW_DIAG_CODES)

# ---------------------------------------------------------------------------
# 新码集中声明(04 文档未登记、本实现新引入的码)
# 说明:02 §1.1 要求时间轴行数不符等场景给出诊断,04 未登记对应码,在此集中声明。
# ---------------------------------------------------------------------------
NEW_DIAG_CODES: dict[str, str] = {
    "DATA-TS-004": "时间轴行数与期望值不匹配(期望 {expected} 行,实际 {actual} 行)",
    "DATA-TS-005": "时序列时间戳乱序(未严格递增)",
    "DATA-TS-006": "时序列时间戳超出标准非闰年日历范围",
    "DATA-TS-007": "时序列时间戳与时间轴步长不对齐(存在非整倍于步长的偏差)",
    "RES-MISS-003": "请求的资源不存在",
    "SYS-STORE-004": "保存冲突:项目已在其他会话中修改(04 §9.2 表 E save_conflict 无码,在此登记)",
    # ies.device-data 1.0.0 契约(0.6.0): CSV 元数据/方言/列/时间/数值契约诊断
    "DATA-META-001": "元数据行缺失或重复: {key}(每个元数据键只能出现一次, 且只能在表头之前)",
    "DATA-META-002": "必需的 ies.device-data 元数据缺失: {key}",
    "DATA-META-003": "ies.device-data schema 标识或版本无法识别(期望 ies.device-data 1.0.0)",
    "DATA-META-004": "元数据枚举值非法: {field}={value}(允许 {allowed})",
    "DATA-META-005": "timestamp_mode=fixed_offset 必须提供固定 UTC 偏移且范围在 -840..840",
    "DATA-META-006": "series_mode=periodic 必须提供 period(day|week|year)",
    "DATA-META-007": "固定 UTC 偏移越界(-840..840): {value}",
    "DATA-DIAL-001": "CSV 方言不符合 ies.device-data 契约: {detail}",
    "DATA-COL-003": "CSV 列未在设备模型 data_inputs 中声明: {column}",
    "DATA-COL-004": "CSV 列重复: {column}",
    "DATA-COL-005": "CSV 缺少设备模型必需的 data_inputs 列: {column}",
    "DATA-COL-006": "列单位与设备模型声明不一致: {column} {actual} != {expected}",
    "DATA-VAL-001": "列值不符合设备模型 value_type 或范围: {column}",
    "DATA-VAL-002": "缺失值未在设备模型中声明允许: {column}",
    "DATA-TIME-001": "timeline 时间戳未严格递增或重复",
    "DATA-TIME-002": "timeline 时间戳与声明分辨率不对齐",
    "DATA-TIME-003": "同文件混用带 Z/带偏移/无偏移时间戳",
    "DATA-TIME-004": "periodic 行数与周期/分辨率不匹配",
    "DATA-TIME-005": "时间戳无法唯一换算到 UTC(非法格式或缺少偏移声明)",
    "DATA-ARR-001": "数组长度与时间轴长度不一致",
    "DATA-SUM-001": "规范化摘要与内容不一致(内容被修改后摘要失效)",
}

# ---------------------------------------------------------------------------
# 诊断码 ↔ 消息键映射(04 §5.1 规则 3:一一对应)
# ---------------------------------------------------------------------------
DIAG_MESSAGE_KEYS: dict[str, str] = {
    DATA_TS_DUP: "ies.diag.data.ts_dup",
    DATA_TS_GAP: "ies.diag.data.ts_gap",
    DATA_TS_LEAP: "ies.diag.data.ts_leap",
    DATA_COL_MISSING: "ies.diag.data.col_missing",
    DATA_COL_UNIT_UNKNOWN: "ies.diag.data.col_unit_unknown",
    CONN_TYPE_UNREGISTERED: "ies.diag.conn.type_unregistered",
    CONN_NODE_ORPHAN: "ies.diag.conn.node_orphan",
    PARAM_RNG_OUT: "ies.diag.param.rng_out",
    PARAM_UNIT_MISMATCH: "ies.diag.param.unit_mismatch",
    PARAM_UNIT_INCONSISTENT: "ies.diag.param.unit_mismatch",
    PARAM_CONFLICT: "ies.diag.param.conflict",
    TASK_QUEUED: "ies.diag.task.queued",
    TASK_SOLVE_FAILED: "ies.diag.task.solve_failed",
    TASK_INFEASIBLE: "ies.diag.task.infeasible",
    TASK_BASE_INFEASIBLE: "ies.diag.task.base_infeasible",
    TASK_TIMEOUT: "ies.diag.task.timeout",
    TASK_DATA_SNAPSHOT_MISSING: "ies.diag.task.snapshot_missing",
    TASK_DATA_HASH_MISMATCH: "ies.diag.task.snapshot_hash_mismatch",
    RES_MISSING: "ies.diag.res.metric_missing",
    RES_NUM_INVALID: "ies.diag.res.invalid_nan",
    RES_RANGE_OUT: "ies.diag.res.out_of_range",
    RES_PRECISION_MISMATCH: "ies.diag.res.precision_mismatch",
    SEC_REG_INTEGRITY: "ies.diag.sec.registry_integrity",
    SEC_REG_SANDBOX: "ies.diag.sec.sandbox_violation",
    EXPR_SYNTAX: "ies.expr.syntax_error",
    EXPR_TYPE: "ies.expr.type_error",
    EXPR_DIM: "ies.expr.dim_mismatch",
    EXPR_RANGE: "ies.expr.range_error",
    EXPR_SECURITY: "ies.expr.forbidden_fn",
    EXPR_RUN: "ies.expr.run_error",
    EXPR_CODE: "ies.expr.code_error",
    PERM_DENIED: "ies.diag.perm.denied",
    SYS_STORE_CORRUPT: "ies.diag.store.corrupt",
    SYS_STORE_MIGRATION_FAILED: "ies.diag.store.migration_failed",
    SYS_STORE_QUOTA_EXCEEDED: "ies.diag.store.quota_exceeded",
    SYS_CFG_INVALID: "ies.diag.store.config_invalid",
    **{
        code: "ies.diag.data.ts_" + suffix
        for code, suffix in {
            "DATA-TS-004": "row_count",
            "DATA-TS-005": "out_of_order",
            "DATA-TS-006": "out_of_calendar",
            "DATA-TS-007": "step_misaligned",
        }.items()
    },
    "RES-MISS-003": "ies.diag.res.not_found",
    "SYS-STORE-004": "ies.diag.store.save_conflict",
    **{
        code: "ies.diag.data." + suffix
        for code, suffix in {
            "DATA-META-001": "meta_dup_or_missing",
            "DATA-META-002": "meta_missing_required",
            "DATA-META-003": "meta_schema_unknown",
            "DATA-META-004": "meta_enum_invalid",
            "DATA-META-005": "meta_offset_required",
            "DATA-META-006": "meta_period_required",
            "DATA-META-007": "meta_offset_out_of_range",
            "DATA-DIAL-001": "dialect_invalid",
            "DATA-COL-003": "col_undeclared",
            "DATA-COL-004": "col_duplicate",
            "DATA-COL-005": "col_required_missing",
            "DATA-COL-006": "col_unit_mismatch",
            "DATA-VAL-001": "val_type_range",
            "DATA-VAL-002": "val_missing_not_allowed",
            "DATA-TIME-001": "time_not_monotonic",
            "DATA-TIME-002": "time_not_aligned",
            "DATA-TIME-003": "time_mixed_zone",
            "DATA-TIME-004": "time_period_row_count",
            "DATA-TIME-005": "time_convert_failed",
            "DATA-ARR-001": "array_length_mismatch",
            "DATA-SUM-001": "summary_mismatch",
        }.items()
    },
}

# 每个码对应的修复建议键(独立于消息键维护)
DIAG_FIX_HINT_KEYS: dict[str, str] = {
    DATA_TS_DUP: "ies.fix.data.ts_dup",
    DATA_TS_GAP: "ies.fix.data.ts_gap",
    DATA_TS_LEAP: "ies.fix.data.ts_leap",
    DATA_COL_MISSING: "ies.fix.data.col_missing",
    DATA_COL_UNIT_UNKNOWN: "ies.fix.data.col_unit_unknown",
    CONN_TYPE_UNREGISTERED: "ies.fix.conn.type_unregistered",
    CONN_NODE_ORPHAN: "ies.fix.conn.node_orphan",
    PARAM_RNG_OUT: "ies.fix.param.rng_out",
    PARAM_UNIT_MISMATCH: "ies.fix.param.unit_mismatch",
    PARAM_UNIT_INCONSISTENT: "ies.fix.param.unit_mismatch",
    PARAM_CONFLICT: "ies.fix.param.conflict",
    TASK_SOLVE_FAILED: "ies.fix.task.solve_failed",
    TASK_INFEASIBLE: "ies.fix.task.infeasible",
    TASK_BASE_INFEASIBLE: "ies.fix.task.base_infeasible",
    TASK_TIMEOUT: "ies.fix.task.timeout",
    TASK_DATA_SNAPSHOT_MISSING: "ies.fix.task.snapshot_missing",
    TASK_DATA_HASH_MISMATCH: "ies.fix.task.snapshot_hash_mismatch",
    RES_MISSING: "ies.fix.res.metric_missing",
    RES_NUM_INVALID: "ies.fix.res.invalid_nan",
    RES_RANGE_OUT: "ies.fix.res.out_of_range",
    RES_PRECISION_MISMATCH: "ies.fix.res.precision_mismatch",
    SEC_REG_INTEGRITY: "ies.fix.sec.registry_integrity",
    SEC_REG_SANDBOX: "ies.fix.sec.sandbox_violation",
    EXPR_SYNTAX: "ies.fix.expr.syntax_error",
    EXPR_TYPE: "ies.fix.expr.type_error",
    EXPR_DIM: "ies.fix.expr.dim_mismatch",
    EXPR_RANGE: "ies.fix.expr.range_error",
    EXPR_SECURITY: "ies.fix.expr.forbidden_fn",
    EXPR_RUN: "ies.fix.expr.run_error",
    EXPR_CODE: "ies.fix.expr.code_error",
    PERM_DENIED: "ies.fix.perm.denied",
    SYS_STORE_CORRUPT: "ies.fix.store.corrupt",
    SYS_STORE_MIGRATION_FAILED: "ies.fix.store.migration_failed",
    SYS_STORE_QUOTA_EXCEEDED: "ies.fix.store.quota_exceeded",
    SYS_CFG_INVALID: "ies.fix.store.config_invalid",
    **{
        code: "ies.fix.data.ts_row_count"
        for code in ("DATA-TS-004", "DATA-TS-005", "DATA-TS-006", "DATA-TS-007")
    },
    **{
        code: "ies.fix.data.device_data_contract"
        for code in (
            "DATA-META-001",
            "DATA-META-002",
            "DATA-META-003",
            "DATA-META-004",
            "DATA-META-005",
            "DATA-META-006",
            "DATA-META-007",
            "DATA-DIAL-001",
            "DATA-COL-003",
            "DATA-COL-004",
            "DATA-COL-005",
            "DATA-COL-006",
            "DATA-VAL-001",
            "DATA-VAL-002",
            "DATA-TIME-001",
            "DATA-TIME-002",
            "DATA-TIME-003",
            "DATA-TIME-004",
            "DATA-TIME-005",
            "DATA-ARR-001",
            "DATA-SUM-001",
        )
    },
}


@dataclass(slots=True)
class Diagnostic:
    """一条用户可见的问题/提示数据(04 §5.4)。

    后端只产数据与消息键,不输出人类可读文案。字段与 04 §5.4 JSON 对齐,
    可序列化(JSON)、可入库(审计)、可跨版本保留(code 稳定)。
    """

    code: str
    severity: str = SEVERITY_ERROR
    blocking: bool = False
    message_key: str = ""
    params: dict = field(default_factory=dict)
    location: dict | None = None
    fix_hint_key: str = ""
    ref_ids: list[str] = field(default_factory=list)
    occurred_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    source: str = ""
    trace_id: str = ""
    project_id: str = ""
    task_id: str = ""
    suppressed: bool = False

    def __post_init__(self) -> None:
        """补齐默认消息键与修复键,并校验严重度取值。"""
        if self.severity not in SEVERITIES:
            raise ValueError(f"非法严重度: {self.severity!r},允许值 {SEVERITIES}")
        if not self.message_key:
            self.message_key = DIAG_MESSAGE_KEYS.get(self.code, "ies.diag.generic")
        if not self.fix_hint_key:
            self.fix_hint_key = DIAG_FIX_HINT_KEYS.get(self.code, "ies.fix.generic")
        if self.code not in DIAG_MESSAGE_KEYS and self.code not in NEW_DIAG_CODES:
            raise ValueError(f"未登记诊断码: {self.code!r};新码须在 NEW_DIAG_CODES 中声明")

    def to_dict(self) -> dict:
        """序列化为 JSON 兼容字典(04 §5.4 结构)。"""
        return {
            "code": self.code,
            "severity": self.severity,
            "blocking": self.blocking,
            "message_key": self.message_key,
            "params": self.params,
            "location": self.location,
            "fix_hint_key": self.fix_hint_key,
            "ref_ids": self.ref_ids,
            "occurred_at": self.occurred_at,
            "source": self.source,
            "trace_id": self.trace_id,
            "project_id": self.project_id,
            "task_id": self.task_id,
            "suppressed": self.suppressed,
        }


def make_diag(
    code: str,
    severity: str = SEVERITY_ERROR,
    message_key: str = "",
    fix_hint_key: str = "",
    *,
    blocking: bool | None = None,
    params: dict | None = None,
    location: dict | None = None,
    ref_ids: list[str] | None = None,
    source: str = "",
    trace_id: str = "",
    project_id: str = "",
    task_id: str = "",
) -> Diagnostic:
    """便捷构造诊断对象。

    参数:
        code: 稳定诊断码(须在 DIAG_MESSAGE_KEYS 或 NEW_DIAG_CODES 中登记)。
        severity: 严重度(blocking/error/warning/info)。
        message_key: 文案键;缺省按 code 从目录推导。
        fix_hint_key: 修复建议键;缺省按 code 从目录推导。
        blocking: 是否阻断;None 时按严重度推导(blocking→True,其余 False)。
        其余关键字直接透传至 Diagnostic 字段。
    """
    if blocking is None:
        blocking = severity == SEVERITY_BLOCKING
    return Diagnostic(
        code=code,
        severity=severity,
        blocking=blocking,
        message_key=message_key,
        params=params or {},
        location=location,
        fix_hint_key=fix_hint_key,
        ref_ids=ref_ids or [],
        source=source,
        trace_id=trace_id,
        project_id=project_id,
        task_id=task_id,
    )
