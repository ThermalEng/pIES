/**
 * useTemplateForm: 模板输入表单状态(未提交输入, 瞬时 UI 状态)。
 *
 * - 切换模板时按叶子默认值重建表单;
 * - 修改字段即触发即时校验(仅展示 touched 字段的错误, 前端预检查;
 *   后端校验始终是权威闸门);
 * - 数组字段以 {kind:'array', items} 整体管理(受控结构编辑器)。
 */

import { useCallback, useEffect, useMemo, useState } from 'react'

import { defaultFormValues, validateFormValues } from '../mappers'
import type { FormFieldError, FormFieldValue } from '../form'
import type { TemplateDocument } from '../model'

export interface TemplateFormController {
  /** 表单值(路径 → 字段值; 数组路径 → {kind:'array', items})。 */
  values: Record<string, FormFieldValue>
  /** 触碰过的字段(用于展示即时校验)。 */
  touched: Record<string, boolean>
  /** 全部字段错误(前端预检查)。 */
  errors: FormFieldError[]
  /** 已触碰字段的错误(页面默认展示这些)。 */
  visibleErrors: FormFieldError[]
  hasErrors: boolean
  setField: (path: string, value: FormFieldValue, markTouched?: boolean) => void
  /** 数组路径整体更新(受控结构编辑器)。 */
  setArray: (path: string, items: Array<Record<string, FormFieldValue>>) => void
  /** 标记整个表单已触碰(提交时展示全部错误)。 */
  markAllTouched: () => void
  reset: () => void
}

export function useTemplateForm(document: TemplateDocument | null): TemplateFormController {
  const [values, setValues] = useState<Record<string, FormFieldValue>>({})
  const [touched, setTouched] = useState<Record<string, boolean>>({})

  // 模板切换 → 重建表单(默认值 + 清空触碰标记)
  useEffect(() => {
    if (document) {
      setValues(defaultFormValues(document.inputs))
      setTouched({})
    }
  }, [document])

  const errors = useMemo(
    () => (document ? validateFormValues(document.inputs, values) : []),
    [document, values],
  )
  const visibleErrors = useMemo(
    () => errors.filter((e) => touched[e.path] === true),
    [errors, touched],
  )
  const hasErrors = errors.length > 0

  const setField = useCallback((path: string, value: FormFieldValue, markTouched = true) => {
    setValues((prev) => ({ ...prev, [path]: value }))
    if (markTouched) {
      setTouched((prev) => (prev[path] ? prev : { ...prev, [path]: true }))
    }
  }, [])

  const setArray = useCallback((path: string, items: Array<Record<string, FormFieldValue>>) => {
    setValues((prev) => ({ ...prev, [path]: { kind: 'array', items } }))
    setTouched((prev) => (prev[path] ? prev : { ...prev, [path]: true }))
  }, [])

  const markAllTouched = useCallback(() => {
    setTouched(() => {
      const all: Record<string, boolean> = {}
      for (const e of errors) all[e.path] = true
      return all
    })
  }, [errors])

  const reset = useCallback(() => {
    if (document) {
      setValues(defaultFormValues(document.inputs))
      setTouched({})
    }
  }, [document])

  return { values, touched, errors, visibleErrors, hasErrors, setField, setArray, markAllTouched, reset }
}
