/**
 * 管理员审计面板(展示 useAdminAudit 状态机; 只读, 无写入)。
 *
 * 展示既有事件的时间/操作/操作者/对象身份与追溯字段; 名称不可得时显示
 * 真实 ID, 不查当前名字补历史。对象标识必须与对象类型一起筛选, 不得
 * 冒充项目过滤。加载/空集合/失败分别展示, 失败保留筛选并可重试。
 */

import { useEffect, useState } from 'react'

import { Alert, Button, EmptyState, FormField, Input, Spinner, Table, TBody, TD, TH, THead, TR } from '../../../components/ui'
import { useI18n } from '../../../i18n'
import { formatDateTime } from '../../../lib/format'
import type { AuditEntry } from '../../../types'
import { useAdminAudit } from '../hooks/useAdminAudit'

/** 操作者展示: 类型恒保留(如 system), ID 缺席仅省略、不伪造用户。 */
function actorText(entry: AuditEntry): string {
  if (entry.actor_type === null) return '—'
  if (entry.actor_id === null) return entry.actor_type
  return `${entry.actor_type} #${entry.actor_id}`
}

function entityText(entry: AuditEntry): string {
  if (entry.entity_type === null || entry.entity_id === null) return '—'
  return `${entry.entity_type} #${entry.entity_id}`
}

function traceText(entry: AuditEntry): string {
  return entry.request_id ?? entry.trace_id ?? '—'
}

export function AdminAuditPanel() {
  const { t } = useI18n()
  const { state, actions } = useAdminAudit()
  const [action, setAction] = useState('')
  const [entityType, setEntityType] = useState('')
  const [entityId, setEntityId] = useState('')
  const [entityIdError, setEntityIdError] = useState<string | null>(null)

  useEffect(() => {
    actions.search({})
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const applyFilters = () => {
    const act = action.trim()
    const et = entityType.trim()
    const eid = entityId.trim()
    if (eid !== '' && et === '') {
      setEntityIdError(t('ies.audit.entity_id_hint'))
      return
    }
    if (eid !== '' && !/^\d+$/.test(eid)) {
      setEntityIdError(t('ies.audit.entity_id_invalid'))
      return
    }
    setEntityIdError(null)
    actions.search({
      ...(act === '' ? {} : { action: act }),
      ...(et === '' ? {} : { entity_type: et }),
      ...(eid === '' ? {} : { entity_id: Number(eid) }),
    })
  }

  const resetFilters = () => {
    setAction('')
    setEntityType('')
    setEntityId('')
    setEntityIdError(null)
    actions.search({})
  }

  const showInitialLoading = state.loading && state.entries === null && state.error === null

  return (
    <div>
      <div className="ies-projects__toolbar" role="group" aria-label={t('ies.audit.filters')}>
        <FormField label={t('ies.admin.audit_action')} htmlFor="audit-filter-action">
          <Input
            id="audit-filter-action"
            value={action}
            placeholder="project.version_created"
            onChange={(event) => setAction(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter') applyFilters()
            }}
          />
        </FormField>
        <FormField label={t('ies.audit.entity_type')} htmlFor="audit-filter-entity-type">
          <Input
            id="audit-filter-entity-type"
            value={entityType}
            placeholder="project_version"
            onChange={(event) => setEntityType(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter') applyFilters()
            }}
          />
        </FormField>
        <FormField label={t('ies.audit.entity_id')} htmlFor="audit-filter-entity-id" error={entityIdError}>
          <Input
            id="audit-filter-entity-id"
            value={entityId}
            inputMode="numeric"
            invalid={entityIdError !== null}
            onChange={(event) => {
              setEntityId(event.target.value)
              setEntityIdError(null)
            }}
            onKeyDown={(event) => {
              if (event.key === 'Enter') applyFilters()
            }}
          />
        </FormField>
        <div>
          <Button variant="primary" size="sm" onClick={applyFilters}>
            {t('ies.audit.apply_filters')}
          </Button>
          <Button variant="secondary" size="sm" onClick={resetFilters}>
            {t('ies.audit.reset_filters')}
          </Button>
        </div>
      </div>

      {showInitialLoading ? (
        <div role="status">
          <Spinner label={t('ies.common.loading')} />
        </div>
      ) : null}

      {state.error !== null && state.entries === null ? (
        <Alert variant="error" title={t('ies.common.load_failed', { reason: state.error })}>
          <Button variant="secondary" size="sm" onClick={actions.retry}>
            {t('ies.common.retry')}
          </Button>
        </Alert>
      ) : null}

      {state.entries !== null && state.entries.length === 0 && state.error === null ? (
        <EmptyState icon="info" title={t('ies.audit.empty')} />
      ) : null}

      {state.entries !== null && state.entries.length > 0 ? (
        <Table>
          <THead>
            <TR>
              <TH>{t('ies.admin.audit_time')}</TH>
              <TH>{t('ies.admin.audit_action')}</TH>
              <TH>{t('ies.admin.audit_actor')}</TH>
              <TH>{t('ies.audit.entity')}</TH>
              <TH>{t('ies.audit.trace')}</TH>
            </TR>
          </THead>
          <TBody>
            {state.entries.map((entry) => (
              <TR key={entry.id} data-entry-id={entry.id}>
                <TD>
                  <span title={entry.occurred_at}>{formatDateTime(entry.occurred_at)}</span>
                </TD>
                <TD>{entry.action}</TD>
                <TD>{actorText(entry)}</TD>
                <TD>{entityText(entry)}</TD>
                <TD>{traceText(entry)}</TD>
              </TR>
            ))}
          </TBody>
        </Table>
      ) : null}

      {state.error !== null && state.entries !== null ? (
        <Alert variant="error" title={t('ies.common.load_failed', { reason: state.error })}>
          <Button variant="secondary" size="sm" onClick={actions.retry}>
            {t('ies.common.retry')}
          </Button>
        </Alert>
      ) : null}

      {state.nextCursor !== null && state.error === null ? (
        <div className="ies-projects__loadmore">
          <Button variant="secondary" loading={state.loadingMore} onClick={actions.loadMore}>
            {t('ies.common.more')}
          </Button>
        </div>
      ) : null}
    </div>
  )
}
