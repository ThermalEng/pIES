/**
 * 建模画布页面级文案(zh/en 双语)。
 *
 * 不与全局 i18n 消息表(messages_zh/en.ts)耦合:该表由多个并行 agent 共同
 * 维护,页面独有文案放在本文件,避免合入冲突;全局已存在的键(ies.common.*、
 * ies.modeling.*、ies.severity.* 等)优先通过 useI18n().t() 使用。
 */

import { getLocale, interpolate } from '../../i18n'

interface Entry {
  zh: string
  en: string
}

const TEXTS: Record<string, Entry> = {
  // 设备面板
  'panel.title': { zh: '设备面板', en: 'Device palette' },
  'panel.hint': { zh: '拖拽到画布添加设备', en: 'Drag onto the canvas to add' },
  'panel.load_failed': { zh: '设备类型加载失败,可重试', en: 'Failed to load device types. Retry available.' },
  'panel.retry': { zh: '重试', en: 'Retry' },
  'panel.none': { zh: '无可用设备类型', en: 'No device types available' },

  // 画布
  'canvas.placeholder': { zh: '从左侧面板拖拽设备,或双击画布添加', en: 'Drag devices from the left panel, or double-click the canvas' },
  'canvas.drop_new': { zh: '放置 {type} 设备', en: 'Place {type} device' },

  // 工具栏
  'toolbar.zoom_in': { zh: '放大', en: 'Zoom in' },
  'toolbar.zoom_out': { zh: '缩小', en: 'Zoom out' },
  'toolbar.fit': { zh: '适应视图', en: 'Fit view' },
  'toolbar.validate': { zh: '校验', en: 'Validate' },
  'toolbar.validating': { zh: '校验中…', en: 'Validating…' },

  // 保存
  'save.button': { zh: '保存', en: 'Save' },
  'save.auto': { zh: '自动保存', en: 'Auto-save' },
  'save.dirty': { zh: '有未保存更改', en: 'Unsaved changes' },
  'save.saving': { zh: '保存中…', en: 'Saving…' },
  'save.saved': { zh: '已保存', en: 'Saved' },
  'save.error': { zh: '保存失败', en: 'Save failed' },
  'save.conflict': { zh: '保存冲突', en: 'Save conflict' },
  'save.conflict_banner': {
    zh: '该项目已在其他会话中修改。为避免覆盖他人更改,本地修改尚未保存。',
    en: 'This project was modified in another session. Local changes were not saved to avoid overwriting them.',
  },
  'save.conflict_reload': { zh: '加载服务器版本', en: 'Load server version' },
  'save.conflict_force': { zh: '仍要覆盖保存', en: 'Save anyway' },
  'save.no_revision': { zh: '修订号未知', en: 'Revision unknown' },
  'save.wait': { zh: '设备正在落库,请稍候再连', en: 'Device is syncing, reconnect shortly' },

  // 端口与连接
  'port.solar': { zh: '太阳辐射', en: 'Solar' },
  'port.direction_out': { zh: '输出', en: 'Output' },
  'port.direction_in': { zh: '输入', en: 'Input' },
  'conn.from': { zh: '源', en: 'From' },
  'conn.to': { zh: '汇', en: 'To' },
  'conn.disconnect': { zh: '断开连接', en: 'Disconnect' },
  'conn.disconnect_ok': { zh: '连接已断开', en: 'Connection removed' },
  'conn.none_selected': { zh: '选择一条连接以查看详情', en: 'Select a connection to view details' },
  'conn.incompatible_type': { zh: '能源类型不兼容:{a} 与 {b} 类型不同', en: 'Energy type mismatch: {a} and {b} differ' },
  'conn.incompatible_direction': {
    zh: '连接方向不兼容:需从输出端(out,源)指向输入端(in,汇),当前为 {a} → {b}',
    en: 'Direction mismatch: must go from an output (source) to an input (sink), got {a} → {b}',
  },
  'conn.incompatible_same_device': { zh: '不能将设备连接到自身', en: 'Cannot connect a device to itself' },
  'conn.incompatible_duplicate': { zh: '这两个端口之间已存在相同连接', en: 'A connection already exists between these ports' },
  'conn.incompatible_solar': { zh: '太阳辐射端口为资源输入,不支持连线', en: 'Solar ports are resource inputs and cannot be wired' },

  // 设备节点
  'node.kind_existing': { zh: '存量', en: 'Existing' },
  'node.kind_new': { zh: '新增', en: 'New' },
  'node.fidelity_low': { zh: 'P1 简化', en: 'P1 simple' },
  'node.fidelity_medium': { zh: 'P2 标准', en: 'P2 standard' },
  'node.fidelity_high': { zh: 'P3 详细', en: 'P3 detailed' },
  'node.capacity': { zh: '容量', en: 'Capacity' },
  'node.capacity_none': { zh: '容量未设置', en: 'Capacity not set' },

  // 侧栏
  'sidebar.title': { zh: '设备属性', en: 'Device properties' },
  'sidebar.none': { zh: '选择设备以编辑参数', en: 'Select a device to edit parameters' },
  'sidebar.delete': { zh: '删除设备', en: 'Delete device' },
  'sidebar.delete_confirm': {
    zh: '确定删除设备 {name}?相关连接将一并删除。',
    en: 'Delete device {name}? Its connections will be removed too.',
  },
  'sidebar.params_none': { zh: '该设备没有可编辑参数', en: 'This device has no editable parameters' },
  'sidebar.restore_default': { zh: '恢复默认值', en: 'Restore default' },
  'sidebar.help': { zh: '参数帮助', en: 'Parameter help' },
  'sidebar.param_invalid': { zh: '取值应在 [{min}, {max}] 内', en: 'Value must be within [{min}, {max}]' },
  'sidebar.param_unset': { zh: '(不设置)', en: '(unset)' },
  'sidebar.param_opt': { zh: '可作优化变量', en: 'Optimizable' },
  'sidebar.param_dict_incomplete': { zh: '请填写全部子项', en: 'Fill in all sub-values' },
  'sidebar.param_dict_invalid': { zh: '子项 {key} 不是有效数值', en: 'Sub-value {key} is not a valid number' },

  // 诊断
  'diag.title': { zh: '校验诊断', en: 'Validation diagnostics' },
  'diag.count': { zh: '{count} 条诊断', en: '{count} diagnostics' },
  'diag.empty': { zh: '校验通过,无诊断', en: 'Validation passed, no diagnostics' },
  'diag.locate': { zh: '定位', en: 'Locate' },
  'diag.local_title': { zh: '连接问题', en: 'Connection issue' },
  'diag.close': { zh: '关闭诊断', en: 'Dismiss' },
  'diag.source_graph': { zh: '系统图', en: 'System graph' },

  // 加载
  'load.error': { zh: '模型加载失败:{reason}', en: 'Failed to load model: {reason}' },
  'load.empty_hint': { zh: '尚未保存设备与连接', en: 'No devices or connections saved yet' },
}

/** 按当前语言取页面级文案。 */
export function lt(key: string, params?: Record<string, string | number>): string {
  const locale = getLocale()
  const entry = TEXTS[key]
  const template = entry ? (locale === 'zh' ? entry.zh : entry.en) : key
  return interpolate(template, params)
}
