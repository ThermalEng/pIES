/**
 * useTemplates: 模板列表查询 + 选中模板详情查询。
 *
 * 状态: 服务器事实来自显式 query(feature 层 useState + reload);
 * 加载中/失败/重试可区分, 失败不降级为空列表(宪法 §9.4)。
 */

import { useCallback, useEffect, useState } from 'react'

import { getTemplate, listTemplates } from '../api'
import type { TemplateDocument, TemplateSummary } from '../model'
import { MapperError } from '../model'
import { useI18n } from '../../../i18n'

function errorText(err: unknown): string {
  if (err instanceof MapperError) return err.message
  if (err instanceof Error) return err.message
  return String(err)
}

/** 模板列表(失败保留 null, 由页面展示错误与重试入口)。 */
export function useTemplates() {
  const { locale } = useI18n()
  const [templates, setTemplates] = useState<TemplateSummary[] | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const reload = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const items = await listTemplates(locale)
      setTemplates(items)
    } catch (err) {
      setError(errorText(err))
      setTemplates(null)
    } finally {
      setLoading(false)
    }
  }, [locale])

  useEffect(() => {
    void reload()
  }, [reload])

  return { templates, loading, error, reload }
}

/** 选中模板详情(表单生成输入); 切换模板时丢弃旧详情, 取消旧请求。 */
export function useTemplateDocument(templateId: string | null) {
  const { locale } = useI18n()
  const [document, setDocument] = useState<TemplateDocument | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!templateId) {
      setDocument(null)
      setError(null)
      return
    }
    let cancelled = false
    setLoading(true)
    setError(null)
    getTemplate(templateId, locale)
      .then((doc) => {
        if (!cancelled) setDocument(doc)
      })
      .catch((err) => {
        if (!cancelled) {
          setError(errorText(err))
          setDocument(null)
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [templateId, locale])

  return { document, loading, error }
}
