/**
 * 页面级文案资源(数据管理页 / 计算配置页专用)。
 *
 * 背景:主消息表(i18n/messages_zh|en.ts)由基础层并行维护,为避免并行
 * 编辑冲突,本文件收纳这两个页面新增的文案键,键名仍遵循 "ies.*" 约定;
 * 查询顺序:本表 -> 全局消息表 -> 键名本身(与全局 translate 回退行为一致)。
 *
 * 用法:pt('ies.data.resolution') 等价于全局 t(),支持 {name} 占位插值。
 */

import { getLocale, translate } from './index'
import type { Locale } from './index'

const PAGE_MESSAGES_ZH: Record<string, string> = {
  // -------------------------------------------------------------------------
  // 数据管理(设计输入 §8 数据约束 / §15.3 时序字段)
  // -------------------------------------------------------------------------
  'ies.data.resolution': '时间分辨率',
  'ies.data.resolution_15min': '15 分钟',
  'ies.data.resolution_30min': '30 分钟',
  'ies.data.resolution_1h': '60 分钟(1 小时)',
  'ies.data.utc_offset': '固定 UTC 偏移',
  'ies.data.utc_note': '数据时间戳按此固定偏移解释;一个数据集内偏移必须固定,不使用夏令时切换',
  'ies.data.field_desc': '字段描述',
  'ies.data.add_field': '添加字段',
  'ies.data.remove_field': '删除字段',
  'ies.data.quality_good': '质量良好',
  'ies.data.interpolation_notes': '插值说明',
  'ies.data.binding': '绑定状态',
  'ies.data.bound': '已绑定',
  'ies.data.unbound': '未绑定',
  'ies.data.validation_passed': '已通过校验,可绑定计算',
  'ies.data.blocking_unresolved': '存在阻断性错误,未通过校验,不可绑定计算',
  'ies.data.fix_and_reupload': '请按修复建议修正数据文件后上传新版本',
  'ies.data.diagnostics': '诊断',
  'ies.data.sample_generate': '内置样例数据',
  'ies.data.sample_title': '生成合成数据',
  'ies.data.sample_desc':
    '基于典型工业园区场景合成的全年逐时数据(标准非闰年 365 天,确定性生成可复现)',
  'ies.data.sample_upload': '生成并上传',
  'ies.data.template_downloaded': '模板已下载',
  'ies.data.upload_result': '上传结果',
  'ies.data.row_count_expect': '期望行数:365 天 × 24 小时 × {steps} 段/小时 = {rows}(首列时间戳 ISO8601,严格递增且不重复)',
  'ies.data.file': '数据文件(CSV)',
  'ies.data.file_required': '请选择要上传的 CSV 文件',
  'ies.data.version_detail': '版本详情',
  'ies.data.provenance_detail': '溯源详情',
  'ies.data.no_versions': '该数据集暂无版本',
  'ies.data.latest': '最新',
  'ies.data.rows': '行',
  'ies.data.provenance_source': '来源',
  'ies.data.provenance_version': '来源版本',
  'ies.data.quality_pass': '通过',
  'ies.data.calendar_note': '标准非闰年 365 天',

  // -------------------------------------------------------------------------
  // 计算配置(设计输入 §9.2 参数/变量/目标/约束)
  // -------------------------------------------------------------------------
  'ies.config.economic_params': '经济参数',
  'ies.config.evaluation_period': '评价周期(年)',
  'ies.config.discount_rate': '折现率(%)',
  'ies.config.tax_rate': '税率(%)',
  'ies.config.depreciation_years': '折旧年限(年)',
  'ies.config.currency': '币种',
  'ies.config.min_irr_note': '最低可接受 IRR 为独立硬约束,与折现率无关;结果低于该值时视为不可接受',
  'ies.config.hard_constraint': '硬约束',
  'ies.config.hard_constraint_notice': '最低税后项目投资 IRR 为硬约束,不可被目标权重抵消',
  'ies.config.hard_irr_summary': '税后项目投资 IRR ≥ {value}',
  'ies.config.hard_irr_unset': '未设置最低 IRR(建议设置以保护项目收益底线)',
  'ies.config.new_device_variables': '新增设备容量变量',
  'ies.config.existing_devices_fixed': '存量设备(容量固定,不参与容量优化)',
  'ies.config.initial_value': '初值',
  'ies.config.lower_bound': '下界',
  'ies.config.upper_bound': '上界',
  'ies.config.add_variable': '添加变量',
  'ies.config.remove_variable': '删除变量',
  'ies.config.variable_hint': '初值/上下界留空时使用设备注册表默认值',
  'ies.config.primary_objective': '主目标',
  'ies.config.objective_irr_max': '税后项目投资 IRR 最大化(默认)',
  'ies.config.objective_npv_max': 'NPV 最大化',
  'ies.config.objective_equity_irr_max': '资本金 IRR 最大化',
  'ies.config.carbon_target': '碳排放目标',
  'ies.config.carbon_cap_enable': '设置运行期碳排放上限',
  'ies.config.carbon_cap': '碳排放上限(tCO₂/年)',
  'ies.config.predefined_constraints': '预定义约束',
  'ies.config.advanced_mode': '高级模式:自定义表达式约束',
  'ies.config.advanced_hint':
    '受限语法:仅支持四则运算(+ - * / // %)、幂(^)与比较(<= >= == < >);函数仅限白名单(abs、min、max、clamp、sqrt、pow、round、if 等);标识符必须为已声明变量/参数/时序列字段;禁止赋值、循环与函数定义。表达式提交后由后端解析校验(EXPR-* 诊断)。',
  'ies.config.expression_name': '表达式名称',
  'ies.config.expression': '表达式',
  'ies.config.add_expression': '添加',
  'ies.config.remove_expression': '删除',
  'ies.config.expression_placeholder': '例如:co2_annual <= 5000',
  'ies.config.alg_mode': '算法选择方式',
  'ies.config.alg_auto': '自动选择(由系统按模型特征推荐)',
  'ies.config.alg_manual': '手动选择',
  'ies.config.alg_capability': '支持的变量类型',
  'ies.config.alg_cap_continuous': '连续',
  'ies.config.alg_cap_discrete': '连续 + 整数/0-1',
  'ies.config.alg_incompat_discrete':
    '算法 {algo} 不支持整数/0-1 离散变量(当前配置含 {count} 个),请选择 MILP、启发式或遗传算法',
  'ies.config.alg_custom_note': '自定义算法由后端校验;兼容性未确认前建议保持默认算法',
  'ies.config.saved_ok': '配置已保存(版本 {version})',
  'ies.config.no_project': '请从项目工作台进入计算配置页面',
  'ies.config.fixed': '固定',
  'ies.config.capacity': '容量',
  'ies.config.validation_diagnostics': '校验诊断',
  'ies.config.con_energy_balance': '电力供需平衡',
  'ies.config.con_heat_balance': '热力供需平衡',
  'ies.config.con_cooling_balance': '冷量供需平衡',
  'ies.config.con_no_reverse_feed': '禁止反送电(电网侧无售电)',
  'ies.config.con_capacity_bounds': '设备出力不超过容量上下限',
  'ies.config.con_soc_limits': '电池 SOC 运行限值',
  'ies.config.con_co2_cap': '运行期碳排放上限约束',
  'ies.config.err.name_required': '配置名称不能为空',
  'ies.config.err.period_invalid': '评价周期必须为不小于 1 的整数(年)',
  'ies.config.err.rate_range': '{field} 取值应在 0–100 之间',
  'ies.config.err.depreciation_invalid': '折旧年限必须为不小于 1 的整数(年)',
  'ies.config.err.min_irr_range': '最低可接受 IRR 取值应在 0–100 之间(%)',
  'ies.config.err.variable_name_required': '变量 {index} 名称不能为空',
  'ies.config.err.variable_name_dup': '变量名 {name} 重复,请使用唯一名称',
  'ies.config.err.variable_bounds': '变量 {name} 的下界应不大于上界',
  'ies.config.err.variable_initial_range': '变量 {name} 的初值应在上下界范围内',
  'ies.config.err.alg_incompat': '算法 {algo} 与当前配置不兼容:{reason}',
  'ies.config.err.carbon_cap_invalid': '碳排放上限必须为大于 0 的数(tCO₂/年)',
  'ies.config.err.expression_required': '表达式名称与内容不能为空',
  'ies.config.err.expression_name_dup': '表达式名称 {name} 重复',

  // -------------------------------------------------------------------------
  // 诊断渲染(数据/配置共用)
  // -------------------------------------------------------------------------
  'ies.diag.location': '位置',
  'ies.diag.field': '字段',
  'ies.diag.rows': '行',
  'ies.diag.fix_hint': '修复建议',
  'ies.diag.no_diagnostics': '未发现诊断问题',
  'ies.diag.loc.config': '配置',
  'ies.diag.loc.dataset': '数据集',
  'ies.diag.loc.dataset_version': '数据集版本',
  'ies.diag.loc.field': '字段',
  'ies.diag.loc.formula': '表达式',
  'ies.diag.loc.variable': '变量',
  'ies.diag.loc.algorithm': '算法',
  'ies.diag.loc.device': '设备',
  'ies.diag.loc.project': '项目',
  'ies.diag.loc.model': '模型',
  'ies.diag.loc.task': '任务',
  'ies.diag.loc.result': '结果',
  'ies.diag.loc.object': '对象',
  'ies.diag.loc.param': '参数',
}

const PAGE_MESSAGES_EN: Record<string, string> = {
  // Data management (Design Input §8 data constraints / §15.3 time-series fields)
  'ies.data.resolution': 'Resolution',
  'ies.data.resolution_15min': '15 min',
  'ies.data.resolution_30min': '30 min',
  'ies.data.resolution_1h': '60 min (1 h)',
  'ies.data.utc_offset': 'Fixed UTC offset',
  'ies.data.utc_note':
    'Timestamps are interpreted at this fixed offset; the offset must stay constant within a dataset (no DST switching)',
  'ies.data.field_desc': 'Field descriptions',
  'ies.data.add_field': 'Add field',
  'ies.data.remove_field': 'Remove field',
  'ies.data.quality_good': 'Good quality',
  'ies.data.interpolation_notes': 'Interpolation notes',
  'ies.data.binding': 'Binding',
  'ies.data.bound': 'Bound',
  'ies.data.unbound': 'Unbound',
  'ies.data.validation_passed': 'Validation passed; ready to bind',
  'ies.data.blocking_unresolved': 'Blocking errors unresolved; validation failed; cannot bind',
  'ies.data.fix_and_reupload': 'Fix the data file per the suggestions and upload a new version',
  'ies.data.diagnostics': 'Diagnostics',
  'ies.data.sample_generate': 'Built-in sample data',
  'ies.data.sample_title': 'Generate synthetic data',
  'ies.data.sample_desc':
    'Synthetic full-year time-series data based on a typical industrial park (standard 365-day non-leap year, deterministic and reproducible)',
  'ies.data.sample_upload': 'Generate & upload',
  'ies.data.template_downloaded': 'Template downloaded',
  'ies.data.upload_result': 'Upload result',
  'ies.data.row_count_expect':
    'Expected rows: 365 d × 24 h × {steps} slots/h = {rows} (first column timestamps ISO8601, strictly increasing, no duplicates)',
  'ies.data.file': 'Data file (CSV)',
  'ies.data.file_required': 'Please choose a CSV file to upload',
  'ies.data.version_detail': 'Version details',
  'ies.data.provenance_detail': 'Provenance details',
  'ies.data.no_versions': 'No versions for this dataset',
  'ies.data.latest': 'Latest',
  'ies.data.rows': 'rows',
  'ies.data.provenance_source': 'Source',
  'ies.data.provenance_version': 'Source version',
  'ies.data.quality_pass': 'Pass',
  'ies.data.calendar_note': 'Standard 365-day non-leap year',

  // Calc config (Design Input §9.2 params / variables / objectives / constraints)
  'ies.config.economic_params': 'Economic parameters',
  'ies.config.evaluation_period': 'Evaluation period (years)',
  'ies.config.discount_rate': 'Discount rate (%)',
  'ies.config.tax_rate': 'Tax rate (%)',
  'ies.config.depreciation_years': 'Depreciation years',
  'ies.config.currency': 'Currency',
  'ies.config.min_irr_note':
    'Minimum acceptable IRR is a hard constraint independent of the discount rate; results below it are unacceptable',
  'ies.config.hard_constraint': 'Hard constraint',
  'ies.config.hard_constraint_notice':
    'Minimum after-tax project IRR is a hard constraint; it cannot be offset by objective weights',
  'ies.config.hard_irr_summary': 'After-tax project IRR ≥ {value}',
  'ies.config.hard_irr_unset': 'No minimum IRR set (recommended to protect the project return floor)',
  'ies.config.new_device_variables': 'New device capacity variables',
  'ies.config.existing_devices_fixed': 'Existing devices (fixed capacity, not optimized)',
  'ies.config.initial_value': 'Initial',
  'ies.config.lower_bound': 'Lower bound',
  'ies.config.upper_bound': 'Upper bound',
  'ies.config.add_variable': 'Add variable',
  'ies.config.remove_variable': 'Remove variable',
  'ies.config.variable_hint': 'Leave initial/bounds empty to use device registry defaults',
  'ies.config.primary_objective': 'Primary objective',
  'ies.config.objective_irr_max': 'Maximize after-tax project IRR (default)',
  'ies.config.objective_npv_max': 'Maximize NPV',
  'ies.config.objective_equity_irr_max': 'Maximize equity IRR',
  'ies.config.carbon_target': 'Carbon target',
  'ies.config.carbon_cap_enable': 'Set an annual carbon emission cap',
  'ies.config.carbon_cap': 'Carbon cap (tCO₂/year)',
  'ies.config.predefined_constraints': 'Predefined constraints',
  'ies.config.advanced_mode': 'Advanced mode: custom expression constraints',
  'ies.config.advanced_hint':
    'Restricted syntax: arithmetic (+ - * / // %) and power (^) plus comparisons (<= >= == < >) only; whitelisted functions only (abs, min, max, clamp, sqrt, pow, round, if, ...); identifiers must be declared variables, parameters or time-series fields; no assignments, loops or function definitions. Expressions are parsed and validated by the backend (EXPR-* diagnostics).',
  'ies.config.expression_name': 'Expression name',
  'ies.config.expression': 'Expression',
  'ies.config.add_expression': 'Add',
  'ies.config.remove_expression': 'Remove',
  'ies.config.expression_placeholder': 'e.g. co2_annual <= 5000',
  'ies.config.alg_mode': 'Algorithm selection',
  'ies.config.alg_auto': 'Auto (recommended by the system)',
  'ies.config.alg_manual': 'Manual',
  'ies.config.alg_capability': 'Supported variable types',
  'ies.config.alg_cap_continuous': 'Continuous',
  'ies.config.alg_cap_discrete': 'Continuous + integer/binary',
  'ies.config.alg_incompat_discrete':
    'Algorithm {algo} does not support integer/binary variables ({count} configured); choose MILP, heuristic or GA',
  'ies.config.alg_custom_note': 'Custom algorithms are validated by the backend; keep the default until confirmed',
  'ies.config.saved_ok': 'Config saved (version {version})',
  'ies.config.no_project': 'Open the calculation config from a project workbench',
  'ies.config.fixed': 'Fixed',
  'ies.config.capacity': 'Capacity',
  'ies.config.validation_diagnostics': 'Validation diagnostics',
  'ies.config.con_energy_balance': 'Electricity supply-demand balance',
  'ies.config.con_heat_balance': 'Heat supply-demand balance',
  'ies.config.con_cooling_balance': 'Cooling supply-demand balance',
  'ies.config.con_no_reverse_feed': 'No reverse feed-in to the grid',
  'ies.config.con_capacity_bounds': 'Device output within capacity bounds',
  'ies.config.con_soc_limits': 'Battery SOC operating limits',
  'ies.config.con_co2_cap': 'Annual carbon emission cap',
  'ies.config.err.name_required': 'Config name is required',
  'ies.config.err.period_invalid': 'Evaluation period must be an integer of at least 1 (years)',
  'ies.config.err.rate_range': '{field} must be between 0 and 100',
  'ies.config.err.depreciation_invalid': 'Depreciation years must be an integer of at least 1',
  'ies.config.err.min_irr_range': 'Minimum acceptable IRR must be between 0 and 100 (%)',
  'ies.config.err.variable_name_required': 'Variable #{index} needs a name',
  'ies.config.err.variable_name_dup': 'Variable name {name} is duplicated; use unique names',
  'ies.config.err.variable_bounds': 'Variable {name}: lower bound must not exceed upper bound',
  'ies.config.err.variable_initial_range': 'Variable {name}: initial value must lie within its bounds',
  'ies.config.err.alg_incompat': 'Algorithm {algo} is incompatible with this config: {reason}',
  'ies.config.err.carbon_cap_invalid': 'Carbon cap must be a number greater than 0 (tCO₂/year)',
  'ies.config.err.expression_required': 'Expression name and content are required',
  'ies.config.err.expression_name_dup': 'Expression name {name} is duplicated',

  // Diagnostic rendering (data / config shared)
  'ies.diag.location': 'Location',
  'ies.diag.field': 'Field',
  'ies.diag.rows': 'Row',
  'ies.diag.fix_hint': 'Fix suggestion',
  'ies.diag.no_diagnostics': 'No diagnostics found',
  'ies.diag.loc.config': 'Config',
  'ies.diag.loc.dataset': 'Dataset',
  'ies.diag.loc.dataset_version': 'Dataset version',
  'ies.diag.loc.field': 'Field',
  'ies.diag.loc.formula': 'Expression',
  'ies.diag.loc.variable': 'Variable',
  'ies.diag.loc.algorithm': 'Algorithm',
  'ies.diag.loc.device': 'Device',
  'ies.diag.loc.project': 'Project',
  'ies.diag.loc.model': 'Model',
  'ies.diag.loc.task': 'Task',
  'ies.diag.loc.result': 'Result',
  'ies.diag.loc.object': 'Object',
  'ies.diag.loc.param': 'Parameter',
}

function table(locale: Locale): Record<string, string> {
  return locale === 'zh' ? PAGE_MESSAGES_ZH : PAGE_MESSAGES_EN
}

/** {name} 占位插值(与全局 translate 一致)。 */
function interpolate(template: string, params?: Record<string, unknown> | null): string {
  if (!params) return template
  return template.replace(/\{(\w+)\}/g, (match, name: string) => {
    const value = params[name]
    return value === undefined || value === null ? match : String(value)
  })
}

/**
 * 页面级翻译:先查本表,再回退全局消息表,最后回退键名本身。
 * 可在 React 组件内直接调用(非 hook,无需 provider 重新渲染,语言切换时随全局表联动)。
 */
export function pt(key: string, params?: Record<string, unknown> | null): string {
  const lang = getLocale()
  const template = table(lang)[key]
  if (template !== undefined) return interpolate(template, params)
  return translate(key, params, lang)
}
