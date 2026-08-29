/**
 * 建模 feature 表单状态(未提交 UI 输入; 与服务器 DTO 严格区分)。
 *
 * - number 以文本字符串保存(允许编辑中半成品, 提交时经 mapper 解析校验);
 * - boolean 直接保存布尔;
 * - data_repeat/data_predict 保存临时文件引用(临时已上传 ≠ 模型已保存);
 * - 路径键与模板 inputs 叶子路径一致(如 "properties.peak_power_kw.value")。
 */

/** 模板表单字段值(按叶子路径索引)。 */
export type FormFieldValue =
  | { kind: 'number'; text: string }
  | { kind: 'boolean'; checked: boolean }
  | { kind: 'string'; text: string }
  | { kind: 'data'; file_ref: string | null; file_name: string | null }
  | { kind: 'array'; items: Array<Record<string, FormFieldValue>> }

/** 模板表单状态: 值 + 触碰标记(touched 字段显示即时校验反馈)。 */
export interface TemplateFormState {
  values: Record<string, FormFieldValue>
  touched: Record<string, boolean>
}

/** 直接 YAML 编辑表单状态。 */
export interface YamlFormState {
  yaml_text: string
  touched: boolean
}

/** 表单字段即时校验错误(纯 mapper 产出; 后端校验始终是权威闸门)。 */
export interface FormFieldError {
  /** 叶子路径。 */
  path: string
  /** 本地化消息键(前端预检查文案)。 */
  message_key: string
  params?: Record<string, unknown>
}
