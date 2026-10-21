/**
 * 财务配置页(0.14.5-D1 起 FinanceProfile/Overrides 编辑器)。
 *
 * - 画像:GET 列表 → 选择已登记 → PUT 引用/切换; 登记为完整 JSON 文档
 *   POST(正式财务格式, 不拆税种字段、不设税率默认); 404 未引用是正常
 *   状态, 与读取失败区分。
 * - 覆盖:GET 无覆盖 404 即空(与失败区分); 全文 JSON 编辑 PUT(自有链
 *   乐观锁, 首次为 null); 保存/清空/切换画像后提示规划已失效(沿原业务
 *   流程, 不替用户重建); 409 保留编辑仅提示冲突, 由用户主动重载。
 * - Effective 只读展示(404 即未生成); 保存失败保留输入并展示后端字段诊断。
 * - 财务三件套与封闭 CalculationConfig 分开提交, 本页不碰计算配置。
 *
 * 挂载方式:路由 /projects/:id/finance 由 useParams 取项目 id;
 * 也可由工作台以 <FinancePage projectId={id} /> 方式嵌入。
 */

import { useEffect, useState } from 'react'
import { useParams } from 'react-router-dom'

import {
  deleteFinanceOverrides,
  getEffectiveFinance,
  getFinanceOverrides,
  getProjectFinanceProfile,
  listFinanceProfiles,
  mapProfileRowsToOptions,
  registerFinanceProfile,
  saveFinanceOverrides,
  setProjectFinanceProfile,
} from '../features/finance'
import type { FinanceProfileOption } from '../features/finance'
import { Alert, Button, Card, FormField, Select, Spinner, Textarea } from '../components/ui'
import { translateError, useI18n } from '../i18n'
import { pt } from '../i18n/pageMessages'
import { ApiError } from '../types'

export interface FinancePageProps {
  projectId?: number
}

/** 解析 JSON 文本域: 空字符串按缺省处理, 解析失败返回错误信息。 */
function parseJsonField(text: string): { value: Record<string, unknown> | null; error: string | null } {
  const trimmed = text.trim()
  if (!trimmed) return { value: null, error: pt('ies.finance.doc_required') }
  try {
    const parsed = JSON.parse(trimmed) as unknown
    if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
      return { value: null, error: pt('ies.finance.doc_must_be_object') }
    }
    return { value: parsed as Record<string, unknown>, error: null }
  } catch {
    return { value: null, error: pt('ies.finance.invalid_json') }
  }
}

function pretty(value: unknown): string {
  if (value === null || value === undefined) return ''
  return JSON.stringify(value, null, 2)
}

/** 后端诊断详情(字段/位置/参数): 原样展示, 不改写。 */
function diagnosticDetail(err: unknown): string | null {
  if (!(err instanceof ApiError)) return null
  const parts: string[] = []
  if (err.code && err.code !== 'API-UNKNOWN') parts.push(`code=${err.code}`)
  if (err.location) parts.push(`location=${JSON.stringify(err.location)}`)
  if (err.params && Object.keys(err.params).length > 0) {
    parts.push(`params=${JSON.stringify(err.params)}`)
  }
  return parts.length > 0 ? parts.join(' ') : null
}

export default function FinancePage(props: FinancePageProps) {
  const { t } = useI18n()
  const { id } = useParams()
  const pid = props.projectId ?? (id === undefined ? undefined : Number(id))

  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [options, setOptions] = useState<FinanceProfileOption[]>([])
  const [boundId, setBoundId] = useState<string | null>(null)
  const [boundRegion, setBoundRegion] = useState('')
  const [selectedId, setSelectedId] = useState('')
  const [binding, setBinding] = useState(false)
  const [registerText, setRegisterText] = useState('')
  const [registering, setRegistering] = useState(false)
  const [overridesText, setOverridesText] = useState('')
  const [overridesRevision, setOverridesRevision] = useState<number | null>(null)
  const [overridesAbsent, setOverridesAbsent] = useState(false)
  const [savingOverrides, setSavingOverrides] = useState(false)
  const [overridesConflict, setOverridesConflict] = useState(false)
  const [effectiveText, setEffectiveText] = useState<string | null>(null)
  const [fieldError, setFieldError] = useState<string | null>(null)
  const [fieldDetail, setFieldDetail] = useState<string | null>(null)
  const [notice, setNotice] = useState<{ kind: 'success' | 'error'; text: string } | null>(null)
  const [planningHint, setPlanningHint] = useState(false)

  async function refreshOverlays(projectIdNum: number): Promise<void> {
    try {
      const ov = await getFinanceOverrides(projectIdNum)
      setOverridesText(pretty(ov.finance_overrides))
      setOverridesRevision(ov.revision)
      setOverridesAbsent(false)
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) {
        setOverridesText('')
        setOverridesRevision(null)
        setOverridesAbsent(true)
      } else {
        throw err
      }
    }
    try {
      const eff = await getEffectiveFinance(projectIdNum)
      setEffectiveText(pretty(eff.effective_finance_config))
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) {
        setEffectiveText(null)
      } else {
        throw err
      }
    }
    setOverridesConflict(false)
  }

  async function reloadAll(projectIdNum: number): Promise<void> {
    setLoading(true)
    setError(null)
    try {
      const listed = await listFinanceProfiles()
      setOptions(mapProfileRowsToOptions(listed.items))
      try {
        const bound = await getProjectFinanceProfile(projectIdNum)
        const doc = bound.finance_profile as Record<string, unknown>
        const profile = doc.profile as Record<string, unknown> | undefined
        const row = bound.row as Record<string, unknown>
        const current = typeof profile?.id === 'string' ? (profile.id as string) : ''
        setBoundId(current || null)
        setBoundRegion(typeof row.region === 'string' ? (row.region as string) : '')
        setSelectedId((prev) => prev || current)
      } catch (err) {
        if (err instanceof ApiError && err.status === 404) {
          setBoundId(null)
          setBoundRegion('')
        } else {
          throw err
        }
      }
      await refreshOverlays(projectIdNum)
    } catch (err) {
      setError(translateError(err as ApiError))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    if (pid === undefined) {
      setLoading(false)
      return
    }
    const projectIdNum: number = pid
    let cancelled = false
    void (async () => {
      setLoading(true)
      setError(null)
      try {
        const listed = await listFinanceProfiles()
        if (cancelled) return
        setOptions(mapProfileRowsToOptions(listed.items))
        try {
          const bound = await getProjectFinanceProfile(projectIdNum)
          if (cancelled) return
          const doc = bound.finance_profile as Record<string, unknown>
          const profile = doc.profile as Record<string, unknown> | undefined
          const row = bound.row as Record<string, unknown>
          const current = typeof profile?.id === 'string' ? (profile.id as string) : ''
          setBoundId(current || null)
          setBoundRegion(typeof row.region === 'string' ? (row.region as string) : '')
          setSelectedId(current)
        } catch (err) {
          if (cancelled) return
          if (err instanceof ApiError && err.status === 404) {
            setBoundId(null)
            setBoundRegion('')
          } else {
            throw err
          }
        }
        if (cancelled) return
        await refreshOverlays(projectIdNum)
      } catch (err) {
        if (!cancelled) setError(translateError(err as ApiError))
      } finally {
        if (!cancelled) setLoading(false)
      }
    })()
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pid])

  if (pid === undefined) return null
  const projectIdNum: number = pid

  /** 引用/切换画像: 成功提示规划失效(不重建), 失败保留选择。 */
  async function bindSelected(): Promise<void> {
    if (!selectedId) {
      setFieldError(pt('ies.finance.profile_required'))
      setFieldDetail(null)
      return
    }
    setBinding(true)
    setFieldError(null)
    setFieldDetail(null)
    setNotice(null)
    try {
      const res = await setProjectFinanceProfile(projectIdNum, selectedId)
      const doc = res.finance_profile as Record<string, unknown>
      const profile = doc.profile as Record<string, unknown> | undefined
      setBoundId(typeof profile?.id === 'string' ? (profile.id as string) : selectedId)
      await refreshOverlays(projectIdNum)
      setPlanningHint(true)
      setNotice({ kind: 'success', text: pt('ies.finance.bound_ok', { id: selectedId }) })
    } catch (err) {
      setFieldError(translateError(err as ApiError))
      setFieldDetail(diagnosticDetail(err))
    } finally {
      setBinding(false)
    }
  }

  /** 登记画像: 全文 JSON, 失败保留输入并展示后端诊断。 */
  async function registerDoc(): Promise<void> {
    const parsed = parseJsonField(registerText)
    if (parsed.error || parsed.value === null) {
      setFieldError(parsed.error ?? pt('ies.finance.invalid_json'))
      setFieldDetail(null)
      return
    }
    setRegistering(true)
    setFieldError(null)
    setFieldDetail(null)
    setNotice(null)
    try {
      const res = await registerFinanceProfile(parsed.value)
      const listed = await listFinanceProfiles()
      setOptions(mapProfileRowsToOptions(listed.items))
      const doc = res.finance_profile as Record<string, unknown>
      const profile = doc.profile as Record<string, unknown> | undefined
      if (typeof profile?.id === 'string') setSelectedId(profile.id as string)
      setNotice({ kind: 'success', text: pt('ies.finance.registered_ok') })
    } catch (err) {
      setFieldError(translateError(err as ApiError))
      setFieldDetail(diagnosticDetail(err))
    } finally {
      setRegistering(false)
    }
  }

  /** 保存覆盖: 失败保留输入; 409 仅提示冲突, 由用户主动重载。 */
  async function saveOverridesDoc(): Promise<void> {
    const parsed = parseJsonField(overridesText)
    if (parsed.error || parsed.value === null) {
      setFieldError(parsed.error ?? pt('ies.finance.invalid_json'))
      setFieldDetail(null)
      return
    }
    setSavingOverrides(true)
    setFieldError(null)
    setFieldDetail(null)
    setNotice(null)
    try {
      await saveFinanceOverrides(projectIdNum, parsed.value, overridesRevision)
      // 后端保存响应同名 revision 只保留 Effective 侧: 重读覆盖取真实 revision。
      await refreshOverlays(projectIdNum)
      setPlanningHint(true)
      setNotice({ kind: 'success', text: pt('ies.finance.overrides_saved') })
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        setOverridesConflict(true)
      }
      setFieldError(translateError(err as ApiError))
      setFieldDetail(diagnosticDetail(err))
    } finally {
      setSavingOverrides(false)
    }
  }

  /** 清空覆盖: 回退 Profile 裸合并; 无覆盖时禁用。 */
  async function clearOverrides(): Promise<void> {
    if (overridesRevision === null) return
    setSavingOverrides(true)
    setFieldError(null)
    setFieldDetail(null)
    setNotice(null)
    try {
      await deleteFinanceOverrides(projectIdNum, overridesRevision)
      await refreshOverlays(projectIdNum)
      setPlanningHint(true)
      setNotice({ kind: 'success', text: pt('ies.finance.overrides_cleared') })
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        setOverridesConflict(true)
      }
      setFieldError(translateError(err as ApiError))
      setFieldDetail(diagnosticDetail(err))
    } finally {
      setSavingOverrides(false)
    }
  }

  if (loading) {
    return (
      <div className="ies-section">
        <Spinner label={t('ies.common.loading')} />
      </div>
    )
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--ies-space-4)' }}>
      <div className="ies-page-header">
        <div>
          <h1 className="ies-page-title">{pt('ies.finance.title')}</h1>
          <p className="ies-page-subtitle">{pt('ies.finance.subtitle')}</p>
        </div>
      </div>

      {error ? (
        <Alert variant="error" closable onClose={() => setError(null)}>
          {error}
        </Alert>
      ) : null}
      {notice ? (
        <Alert variant={notice.kind === 'success' ? 'success' : 'error'} closable onClose={() => setNotice(null)}>
          {notice.text}
        </Alert>
      ) : null}
      {fieldError ? (
        <Alert variant="error" title={fieldError} closable onClose={() => { setFieldError(null); setFieldDetail(null) }}>
          {fieldDetail ?? pt('ies.finance.see_diagnostics')}
        </Alert>
      ) : null}
      {planningHint ? (
        <Alert variant="warning" closable onClose={() => setPlanningHint(false)}>
          {pt('ies.finance.planning_invalidated')}
        </Alert>
      ) : null}

      <Card title={pt('ies.finance.profile_section')}>
        <dl className="ies-kv">
          <dt>{pt('ies.finance.profile_current')}</dt>
          <dd>{boundId ? `${boundId}${boundRegion ? ` (${boundRegion})` : ''}` : pt('ies.finance.profile_unbound')}</dd>
        </dl>
        <div style={{ marginTop: 'var(--ies-space-3)' }}>
          <FormField label={pt('ies.finance.profile_select')} htmlFor="finance-profile-select">
            <Select
              id="finance-profile-select"
              value={selectedId}
              onChange={(e) => setSelectedId(e.target.value)}
            >
              <option value="">{pt('ies.finance.profile_choose')}</option>
              {options.map((opt) => (
                <option key={opt.profileId} value={opt.profileId}>
                  {opt.profileId}{opt.region ? ` (${opt.region})` : ''}
                </option>
              ))}
            </Select>
          </FormField>
          <div style={{ marginTop: 'var(--ies-space-2)' }}>
            <Button onClick={() => void bindSelected()} loading={binding} disabled={binding || !selectedId}>
              {pt('ies.finance.profile_bind')}
            </Button>
          </div>
        </div>
      </Card>

      <Card title={pt('ies.finance.register_section')}>
        <p style={{ fontSize: 'var(--ies-fs-sm)', color: 'var(--ies-color-text-secondary)' }}>
          {pt('ies.finance.register_desc')}
        </p>
        <div style={{ marginTop: 'var(--ies-space-3)' }}>
          <FormField label={pt('ies.finance.profile_doc')} htmlFor="finance-profile-doc">
            <Textarea
              id="finance-profile-doc"
              rows={10}
              value={registerText}
              onChange={(e) => setRegisterText(e.target.value)}
              placeholder={pt('ies.finance.profile_doc_placeholder')}
            />
          </FormField>
          <div style={{ marginTop: 'var(--ies-space-2)' }}>
            <Button onClick={() => void registerDoc()} loading={registering} disabled={registering}>
              {pt('ies.finance.register_btn')}
            </Button>
          </div>
        </div>
      </Card>

      <Card title={pt('ies.finance.overrides_section')}>
        {overridesAbsent ? (
          <p style={{ fontSize: 'var(--ies-fs-sm)', color: 'var(--ies-color-text-secondary)' }}>
            {pt('ies.finance.overrides_none')}
          </p>
        ) : (
          <dl className="ies-kv">
            <dt>{pt('ies.finance.overrides_revision')}</dt>
            <dd>{overridesRevision ?? '—'}</dd>
          </dl>
        )}
        <div style={{ marginTop: 'var(--ies-space-3)' }}>
          <FormField label={pt('ies.finance.overrides_doc')} htmlFor="finance-overrides-doc">
            <Textarea
              id="finance-overrides-doc"
              rows={10}
              value={overridesText}
              onChange={(e) => setOverridesText(e.target.value)}
              placeholder={pt('ies.finance.overrides_doc_placeholder')}
            />
          </FormField>
          <div className="ies-flex" style={{ marginTop: 'var(--ies-space-2)', gap: 'var(--ies-space-2)' }}>
            <Button onClick={() => void saveOverridesDoc()} loading={savingOverrides} disabled={savingOverrides}>
              {pt('ies.finance.save_btn')}
            </Button>
            <Button
              variant="secondary"
              onClick={() => void clearOverrides()}
              loading={savingOverrides}
              disabled={savingOverrides || overridesRevision === null}
            >
              {pt('ies.finance.clear_btn')}
            </Button>
            {overridesConflict ? (
              <Button variant="secondary" onClick={() => void reloadAll(projectIdNum)}>
                {pt('ies.finance.conflict_reload')}
              </Button>
            ) : null}
          </div>
        </div>
      </Card>

      <Card title={pt('ies.finance.effective_section')}>
        {effectiveText === null ? (
          <p style={{ fontSize: 'var(--ies-fs-sm)', color: 'var(--ies-color-text-secondary)' }}>
            {pt('ies.finance.effective_absent')}
          </p>
        ) : (
          <pre className="ies-pre">{effectiveText}</pre>
        )}
      </Card>
    </div>
  )
}
