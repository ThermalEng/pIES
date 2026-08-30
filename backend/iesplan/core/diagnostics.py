"""诊断体系:诊断对象与诊断码目录。

设计约束见开发者指南 contracts.md:
- 诊断码格式 `<域>-<类别>-<三位序号>`(如 DATA-TS-001),码一旦发布即永久稳定;
- severity 取值 blocking | error | warning | info,blocking 为独立布尔与 severity 正交;
- 诊断码与消息键一一对应(如 DATA-TS-001 ↔ ies.diag.data.ts_dup);
- 后端只输出 code + message_key + params,不输出任何人类可读文案。

本模块只引用 04 第 5.3 节与第 9 节已登记的诊断码;新码集中在 NEW_DIAG_CODES 声明。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from dataclasses import replace as dc_replace
from datetime import UTC, datetime
from types import MappingProxyType

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
    "DATA-META-003": "ies.device-data schema 标识或版本无法识别: {schema} {schema_version}",
    "DATA-META-004": "元数据枚举值非法: {field}={value}(允许 {allowed})",
    "DATA-META-005": "timestamp_mode=fixed_offset 必须提供固定 UTC 偏移且范围在 -840..840",
    "DATA-META-006": "series_mode=periodic 必须提供 period(day|week|year)",
    "DATA-META-007": "固定 UTC 偏移越界(-840..840): {value}",
    "DATA-META-008": "device_model/device_id 与被校验的设备描述符不匹配: 声明 {declared}, 期望 {expected}",
    "DATA-META-009": "文件声明的 device_model 未注册: {device_model}",
    "DATA-META-010": (
        "ies.device-data 2.0.0 声明的设备内容摘要与目标设备不匹配: "
        "声明 {declared}, 期望 {expected}"
    ),
    "DATA-META-011": (
        "ies.device-data 2.0.0 文件 source_mode 与设备接口声明的预定义来源模式"
        "不匹配: {column} 声明 {mode}, 文件 {source_mode}"
    ),
    "DATA-META-012": (
        "计算序列绑定的项目基线摘要不匹配: 声明 {declared}, 期望 {expected}"
    ),
    "DATA-DIAL-001": "CSV 方言不符合 ies.device-data 契约: {detail}",
    "DATA-COL-003": "CSV 列未在设备模型 predefined interfaces 中声明: {column}",
    "DATA-COL-004": "CSV 列重复: {column}",
    "DATA-COL-005": "CSV 缺少设备模型必需的 predefined interface 列: {column}",
    "DATA-COL-006": "列单位与设备模型声明不一致: {column} {actual} != {expected}",
    "DATA-COL-007": "数据列缺少 unit.<column> 单位声明: {column}",
    # DATA-VAL-001 同时是 DataValidationError 的包络码(HTTP 400, 见 services/dataset.py);
    # 此处按诊断语义登记(与 DIAG_MESSAGE_KEYS val_type_range 一致)。
    "DATA-VAL-001": "列值不符合设备模型 value_type 或范围: {column}",
    "DATA-VAL-002": "缺失值未在设备模型中声明允许: {column}",
    "DATA-STEP-001": "step 必须是非负十进制整数: {value}",
    "DATA-STEP-002": "原始文件 step 必须严格递增且不重复",
    "DATA-STEP-003": "计算文件 step 必须从 0 开始连续递增到 point_count-1",
    "DATA-STEP-004": "step 点数与期望不匹配: 期望 {expected}, 实际 {actual}",
    "DATA-TIME-001": "timeline 时间戳未严格递增或重复",
    "DATA-TIME-002": "timeline 时间戳与声明分辨率不对齐",
    "DATA-TIME-003": "同文件混用带 Z/带偏移/无偏移时间戳",
    "DATA-TIME-004": "periodic 行数与周期/分辨率不匹配",
    "DATA-TIME-005": "时间戳无法唯一换算到 UTC(非法格式或缺少偏移声明)",
    "DATA-TIME-006": "时间戳形态与声明 timestamp_mode 不匹配: {value}"
    "(期望模式 {timestamp_mode}, 实际形态 {form})",
    "DATA-ARR-001": "数组长度与时间轴长度不一致",
    "DATA-SUM-001": "规范化摘要与内容不一致(内容被修改后摘要失效)",
    "CONFIG-VAL-001": "计算配置校验失败(阻断性诊断,HTTP 422;包络码,见 api/config.py)",
    "API-REQ-001": (
        "请求体校验失败(422;FastAPI/Pydantic RequestValidationError 包络,"
        "见 main.py;业务域复用同码但 message_key 不同)"
    ),
    # assembly 域诊断。消息键与修复键由 assembly.diags 自治维护；core 只承担
    # 宪法 §8.3 要求的稳定码登记，不持有装配业务文案或运行期可变注册状态。
    "ASM-SYN-001": "装配 YAML 解析失败或根节点类型非法",
    "ASM-SYN-002": "装配文件包含未知章节",
    "ASM-SYN-003": "装配格式版本不受支持",
    "ASM-SYN-004": "装配必填字段缺失",
    "ASM-SYN-005": "装配字段类型或枚举值非法",
    "ASM-SYN-006": "ies.assembly schema 标识无法识别",
    "ASM-SYN-007": "装配引用未固定精确版本",
    "ASM-SYN-008": "装配包含禁止字段或执行信息",
    "ASM-SYN-009": "装配资源路径非法",
    "ASM-EDGE-001": "连接起点不是输出端口",
    "ASM-EDGE-002": "连接终点不是输入端口",
    "ASM-EDGE-003": "连接两端载能类型不一致",
    "ASM-EDGE-004": "连接两端物理量不一致",
    "ASM-EDGE-005": "连接两端单位量纲不可换算",
    "ASM-EDGE-006": "装配连接形成自环",
    "ASM-EDGE-007": "装配包含重复连接",
    "ASM-EDGE-008": "双向端口连接缺少确定方向",
    "ASM-EDGE-009": "连接容量非正数",
    "ASM-REF-001": "设备实例 ID 重复",
    "ASM-REF-002": "设备模型未注册",
    "ASM-REF-003": "端口引用不存在",
    "ASM-REF-004": "数据集引用缺失或不完整",
    "ASM-REF-005": "显式端口声明与设备描述符不一致",
    "ASM-INPUT-001": "设备输入端口没有输入连接",
    "ASM-INPUT-002": "设备必填参数缺失",
    "ASM-INPUT-003": "设备参数值越界或不符合枚举",
    "ASM-INPUT-004": "负荷设备缺少数据绑定",
    "ASM-INPUT-005": "数据绑定单位与设备输入量纲不兼容",
    "ASM-INPUT-006": "参数未在设备模型中声明",
    "ASM-PIPE-001": "管道设备缺少 delay_steps",
    "ASM-PIPE-002": "管道 delay_steps 超出时间轴范围",
    "ASM-PIPE-003": "管道设备未形成完整通路",
    "ASM-SOLV-001": "系统母线缺少能源来源",
    "ASM-SOLV-002": "系统母线缺少能源去向",
    "ASM-SOLV-003": "固定供需导致系统必然不可行",
    "ASM-SOLV-004": "系统存在互斥或过度约束",
    "ASM-SOLV-005": "有状态设备形成因果环",
    "ASM-SOLV-006": "装配包含孤立设备或母线",
    "ASM-SOLV-007": "系统自由度提示",
    "ASM-CONST-001": "装配约束表达式语法错误",
    "ASM-CONST-002": "装配约束表达式量纲不一致",
    "ASM-CONST-003": "装配约束引用未定义符号",
    "ASM-RES-001": "装配资源不可读或摘要不一致",
    "ASM-CALC-001": "calculation.mode 非法",
    "ASM-CALC-002": "calculation.options 非法",
    "ASM-OUT-001": "输出引用未定义设备或端口",
    "ASM-ART-001": "规范文本、摘要与校验回执不一致",
    "ASM-CONV-001": "旧装配形态无法唯一迁移到 ies.assembly 1.0.0",
    # 装配 2.0.0 接口网络纯协议校验(0.8.0 切片): 值域冲突/预定义绑定/内容锁
    "ASM-EDGE-010": "连接两端有效区间无交集(值域冲突)",
    "ASM-BIND-001": "预定义接口绑定非法(来源模式/数据引用/接口类型不匹配)",
    "ASM-LOCK-001": "设备实例 definition 与提供的 descriptor 内容锁不一致",
    # 技术方程建模 2.0.0 contract(0.8.0 切片): 公共 AST 与数学贡献诊断
    "MOD-EQ-001": "方程表达式语法非法: {detail}",
    "MOD-EQ-002": "方程引用了未声明的标识符: {name}(只允许 properties/interfaces/equations.variables)",
    "MOD-EQ-003": "方程量纲/单位冲突: {detail}",
    "MOD-EQ-004": "方程存在循环引用: {cycle}",
    "MOD-EQ-005": "关系输出冲突: {detail}",
    "MOD-EQ-006": "状态变量缺少 initial 初值: {name}",
    "MOD-EQ-007": "非时变 property 被时间索引引用: {name}",
    "MOD-EQ-008": "方程引用了 blind 接口(不连接、不接收数据): {name}",
    # PROJ 域: 项目模型候选门禁与保存(application/projects 用例, 切片 dm2-A)。
    # 配套数据文件的存在性、摘要、归属和内容契约都在同一门禁校验。
    "PROJ-MDL-001": "候选模型引用的临时数据文件不存在或不可用: {data_ref}(期望存在对象,实际不可用)",
    "PROJ-MDL-002": (
        "临时数据文件内容摘要与声明不一致: "
        "{data_ref}(期望 {expected_sha256},实际 {actual_sha256})"
    ),
    "PROJ-MDL-003": "临时数据文件归属与上传会话不一致: {data_ref}(该对象不属于 upload_id={upload_id})",
    "PROJ-MDL-004": "最终设备 ID 无法通过身份校验: {final_id}(由基础 ID {base_device_id} 追加 _N 后缀后非法)",
    "PROJ-MDL-005": "候选模型校验失败, 保存被拒绝(不写项目模型目录、不登记清单、不分配编号)",
    "PROJ-MDL-006": "候选模型 YAML 解析失败: {detail}",
    # 用户自定义模型模板域(application/model_templates 用例, 切片 dm2)。
    # 草稿保存/发布共用同一校验门禁; 生命周期操作以标准错误信封返回。
    "TPL-MDL-001": "模板 YAML 解析失败: {detail}",
    "TPL-MDL-002": "模板校验失败, 保存/发布被拒绝(不落盘、不产生 revision)",
    "TPL-MDL-003": "模板草稿已被其他操作更新(期望 {expected_revision}, 当前 {current_revision})",
    "TPL-MDL-004": "模板或模板 revision 不存在",
    "TPL-MDL-005": "模板当前状态不允许该生命周期操作: {status}",
    "TPL-MDL-006": "模板没有可发布的草稿内容(需先保存草稿)",
    "TPL-MDL-007": "已发布模板禁止删除(发布 revision 与内容证据必须保留)",
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
            "DATA-META-008": "meta_model_mismatch",
            "DATA-META-009": "meta_model_unregistered",
            "DATA-META-010": "meta_content_mismatch",
            "DATA-META-011": "meta_source_mode_mismatch",
            "DATA-META-012": "meta_project_baseline_mismatch",
            "DATA-DIAL-001": "dialect_invalid",
            "DATA-COL-003": "col_undeclared",
            "DATA-COL-004": "col_duplicate",
            "DATA-COL-005": "col_required_missing",
            "DATA-COL-006": "col_unit_mismatch",
            "DATA-COL-007": "col_unit_declaration_missing",
            "DATA-VAL-001": "val_type_range",
            "DATA-VAL-002": "val_missing_not_allowed",
            "DATA-STEP-001": "step_invalid",
            "DATA-STEP-002": "step_not_monotonic",
            "DATA-STEP-003": "step_not_contiguous",
            "DATA-STEP-004": "step_count_mismatch",
            "DATA-TIME-001": "time_not_monotonic",
            "DATA-TIME-002": "time_not_aligned",
            "DATA-TIME-003": "time_mixed_zone",
            "DATA-TIME-004": "time_period_row_count",
            "DATA-TIME-005": "time_convert_failed",
            "DATA-TIME-006": "time_form_mode_mismatch",
            "DATA-ARR-001": "array_length_mismatch",
            "DATA-SUM-001": "summary_mismatch",
        }.items()
    },
    **{
        code: "ies.diag.proj." + suffix
        for code, suffix in {
            "PROJ-MDL-001": "model_data_missing",
            "PROJ-MDL-002": "model_data_digest_mismatch",
            "PROJ-MDL-003": "model_data_owner_mismatch",
            "PROJ-MDL-004": "model_identity_failed",
            "PROJ-MDL-005": "model_validation_failed",
            "PROJ-MDL-006": "model_yaml_parse",
        }.items()
    },
    **{
        code: "ies.diag.tpl." + suffix
        for code, suffix in {
            "TPL-MDL-001": "template_yaml_parse",
            "TPL-MDL-002": "template_validation_failed",
            "TPL-MDL-003": "template_revision_conflict",
            "TPL-MDL-004": "template_not_found",
            "TPL-MDL-005": "template_status_invalid",
            "TPL-MDL-006": "template_revision_required",
            "TPL-MDL-007": "template_already_published",
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
            "DATA-META-008",
            "DATA-META-009",
            "DATA-META-010",
            "DATA-META-011",
            "DATA-META-012",
            "DATA-DIAL-001",
            "DATA-COL-003",
            "DATA-COL-004",
            "DATA-COL-005",
            "DATA-COL-006",
            "DATA-COL-007",
            "DATA-VAL-001",
            "DATA-VAL-002",
            "DATA-STEP-001",
            "DATA-STEP-002",
            "DATA-STEP-003",
            "DATA-STEP-004",
            "DATA-TIME-001",
            "DATA-TIME-002",
            "DATA-TIME-003",
            "DATA-TIME-004",
            "DATA-TIME-005",
            "DATA-TIME-006",
            "DATA-ARR-001",
            "DATA-SUM-001",
        )
    },
    **{
        code: "ies.fix.proj.model_data"
        for code in ("PROJ-MDL-001", "PROJ-MDL-002", "PROJ-MDL-003")
    },
    "PROJ-MDL-004": "ies.fix.proj.model_identity",
    "PROJ-MDL-005": "ies.fix.proj.model_validation",
    "PROJ-MDL-006": "ies.fix.proj.model_yaml",
    **{
        code: "ies.fix.tpl.template"
        for code in ("TPL-MDL-001", "TPL-MDL-002")
    },
    "TPL-MDL-003": "ies.fix.tpl.revision_conflict",
    "TPL-MDL-004": "ies.fix.tpl.not_found",
    "TPL-MDL-005": "ies.fix.tpl.status_invalid",
    "TPL-MDL-006": "ies.fix.tpl.revision_required",
    "TPL-MDL-007": "ies.fix.tpl.already_published",
}


@dataclass(frozen=True, slots=True)
class Diagnostic:
    """一条用户可见的问题/提示数据(04 §5.4)。

    后端只产数据与消息键,不输出人类可读文案。字段与 04 §5.4 JSON 对齐,
    可序列化(JSON)、可入库(审计)、可跨版本保留(code 稳定)。

    本类型深度不可变:字段集合固定,构造后禁止赋值(FrozenInstanceError);
    params/location 经递归只读包装(任意嵌套 dict/list 均不可修改),
    ref_ids 为 tuple。需要变更字段时使用 ``replace()`` 或派生方法生成新对象。
    """

    code: str
    severity: str = SEVERITY_ERROR
    blocking: bool = False
    message_key: str = ""
    params: Mapping[str, object] = field(default_factory=dict)
    location: Mapping[str, object] | None = None
    fix_hint_key: str = ""
    ref_ids: tuple[str, ...] = ()
    occurred_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    source: str = ""
    trace_id: str = ""
    project_id: str = ""
    task_id: str = ""
    suppressed: bool = False

    def __post_init__(self) -> None:
        """补齐默认消息键与修复键,冻结容器并校验严重度取值(frozen 标准模式)。"""
        if self.severity not in SEVERITIES:
            raise ValueError(f"非法严重度: {self.severity!r},允许值 {SEVERITIES}")
        if not self.message_key:
            object.__setattr__(self, "message_key", DIAG_MESSAGE_KEYS.get(self.code, "ies.diag.generic"))
        if not self.fix_hint_key:
            object.__setattr__(self, "fix_hint_key", DIAG_FIX_HINT_KEYS.get(self.code, "ies.fix.generic"))
        if self.code not in DIAG_MESSAGE_KEYS and self.code not in NEW_DIAG_CODES:
            raise ValueError(f"未登记诊断码: {self.code!r};新码须在 NEW_DIAG_CODES 中声明")
        # 深度不可变: 容器字段统一冻结为只读视图(tuple/MappingProxyType)
        object.__setattr__(self, "params", _freeze_mapping(self.params))
        if self.location is not None:
            if not isinstance(self.location, Mapping):
                raise TypeError(
                    f"location 须为 Mapping 或 None,得到 {type(self.location).__name__}"
                )
            object.__setattr__(self, "location", _freeze_mapping(self.location))
        object.__setattr__(self, "ref_ids", tuple(self.ref_ids))

    def to_dict(self) -> dict:
        """序列化为 JSON 兼容字典(04 §5.4 结构)。

        返回值是独立副本: params/location 转回普通 dict/list(任意嵌套),
        可直接 json.dumps;修改返回值不影响诊断对象本身。
        """
        return {
            "code": self.code,
            "severity": self.severity,
            "blocking": self.blocking,
            "message_key": self.message_key,
            "params": _thaw_mapping(self.params),
            "location": _thaw_mapping(self.location) if self.location is not None else None,
            "fix_hint_key": self.fix_hint_key,
            "ref_ids": list(self.ref_ids),
            "occurred_at": self.occurred_at,
            "source": self.source,
            "trace_id": self.trace_id,
            "project_id": self.project_id,
            "task_id": self.task_id,
            "suppressed": self.suppressed,
        }

    def replace(self, **changes: object) -> Diagnostic:
        """返回替换指定字段后的新诊断对象(原对象不变)。

        等价 ``dataclasses.replace``,但容器字段(params/ref_ids/location)按
        构造语义重新冻结,调用方无需关心只读视图类型。
        """
        return dc_replace(self, **changes)

    def with_context(
        self,
        *,
        project_id: str | None = None,
        task_id: str | None = None,
        trace_id: str | None = None,
        source: str | None = None,
    ) -> Diagnostic:
        """跨层补充上下文(project/task/trace/source)的派生辅助方法。

        仅覆盖显式传入且当前为空的字段,已有上下文不被静默覆盖;
        未传入任何值时返回等价新对象。
        """
        updates: dict[str, str] = {}
        for name, value in (
            ("project_id", project_id),
            ("task_id", task_id),
            ("trace_id", trace_id),
            ("source", source),
        ):
            if value is not None and not getattr(self, name):
                updates[name] = value
        return self.replace(**updates) if updates else self.replace()


def _freeze_value(value: object) -> object:
    """递归冻结容器为只读视图(dict→MappingProxyType, list→tuple)。

    嵌套层级不受限: 任意深度的 dict/list 都被只读包装, 标量与元组原样保留,
    保证构造后无法通过任何途径修改 params/location 的内容(深度不可变)。
    """
    if isinstance(value, Mapping):
        return MappingProxyType({k: _freeze_value(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_value(v) for v in value)
    return value


def _freeze_mapping(mapping: Mapping[str, object]) -> Mapping[str, object]:
    """把映射递归冻结为只读视图(任意嵌套 dict/list 一并只读化)。"""
    return _freeze_value(mapping)  # type: ignore[return-value]


def _thaw_value(value: object) -> object:
    """递归解冻单值(dict→普通 dict, tuple→list, 标量原样; 与 _freeze_value 互逆)。"""
    if isinstance(value, Mapping):
        return {k: _thaw_value(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return [_thaw_value(v) for v in value]
    return value


def _thaw_mapping(mapping: Mapping[str, object]) -> dict[str, object]:
    """递归解冻为普通 dict/list(JSON 可序列化, 与 _freeze_value 互逆)。"""
    return {k: _thaw_value(v) for k, v in mapping.items()}


def make_diag(
    code: str,
    severity: str = SEVERITY_ERROR,
    message_key: str = "",
    fix_hint_key: str = "",
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
        ref_ids=tuple(ref_ids or ()),
        source=source,
        trace_id=trace_id,
        project_id=project_id,
        task_id=task_id,
    )
