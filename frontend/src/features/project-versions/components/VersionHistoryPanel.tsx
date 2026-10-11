/**
 * 版本历史面板(项目版本 feature 对外暴露的组合组件)。
 *
 * - 列表: 版本号/名称/描述/创建人/创建时间/原因/来源草稿修订, 当前版本徽标;
 *   行为空仅当请求成功且数组为空, 加载/请求失败必须显错(不伪装空列表)。
 * - 创建: 对话框内名称(必填)+描述(可选); 提交中禁用重复点击, 失败保留输入;
 *   归档项目后端拒收(ConflictError)时展示后端错误, 不新增前端权限策略。
 * - 详情: 选中行打开, 展示同一版本元数据(基线/币种/schema/内容引用);
 *   详情端点只返回版本元数据, 本页不声称展示完整模型内容。
 */

import { useState } from 'react'

import { Alert, Badge, Button, Card, Dialog, EmptyState, FormField, Input, Spinner, Table, TBody, TD, TH, THead, TR, Textarea } from '../../../components/ui'
import { useI18n } from '../../../i18n'
import { formatDateTime } from '../../../lib/format'
import { buildCreateRequest, VersionFormError } from '../mappers'
import type { VersionCreateForm, VersionDetailState, VersionModel } from '../model'
import type { UseProjectVersions } from '../hooks/useProjectVersions'

/** 创建对话框(受控表单; 失败保留输入, 由调用方关闭)。 */
function CreateDialog({
  open,
  form,
  setForm,
  formError,
  submitting,
  submitError,
  onSubmit,
  onClose,
}: {
  open: boolean
  form: VersionCreateForm
  setForm: (form: VersionCreateForm) => void
  formError: string | null
  submitting: boolean
  submitError: string | null
  onSubmit: () => void
  onClose: () => void
}) {
  const { t } = useI18n()
  return (
    <Dialog
      open={open}
      onClose={onClose}
      title={t('ies.versions.create_title')}
      footer={
        <>
          <Button variant="secondary" onClick={onClose} disabled={submitting}>
            {t('ies.common.cancel')}
          </Button>
          <Button variant="primary" onClick={onSubmit} disabled={submitting}>
            {submitting ? <Spinner size="sm" /> : null}
            {t('ies.versions.create_submit')}
          </Button>
        </>
      }
    >
      <FormField label={t('ies.versions.field_name')} htmlFor="version-name" required error={formError}>
        <Input
          id="version-name"
          value={form.name}
          maxLength={200}
          invalid={formError !== null}
          onChange={(e) => setForm({ ...form, name: e.target.value })}
        />
      </FormField>
      <FormField label={t('ies.versions.field_description')} htmlFor="version-description">
        <Textarea
          id="version-description"
          value={form.description}
          maxLength={2000}
          rows={3}
          onChange={(e) => setForm({ ...form, description: e.target.value })}
        />
      </FormField>
      {submitError ? (
        <Alert variant="error" title={t('ies.versions.create_failed')}>
          {submitError}
        </Alert>
      ) : null}
    </Dialog>
  )
}

/** 版本详情对话框(仅版本元数据, 非完整模型内容)。 */
function DetailDialog({ detail, onClose }: { detail: Exclude<VersionDetailState, { status: 'idle' }>; onClose: () => void }) {
  const { t } = useI18n()
  return (
    <Dialog open onClose={onClose} title={t('ies.versions.detail_title')} footer={
      <Button variant="secondary" onClick={onClose}>
        {t('ies.common.close')}
      </Button>
    }>
      {detail.status === 'loading' ? (
        <div role="status"><Spinner size="md" /> {t('ies.common.loading')}</div>
      ) : detail.status === 'failed' ? (
        <Alert variant="error" title={t('ies.versions.detail_failed')}>
          {detail.message}
        </Alert>
      ) : (
        <dl className="ies-definition-list">
          <dt>{t('ies.versions.col_no')}</dt>
          <dd>v{detail.version.version_no}</dd>
          <dt>{t('ies.versions.field_name')}</dt>
          <dd>{detail.version.name}</dd>
          <dt>{t('ies.versions.field_description')}</dt>
          <dd>{detail.version.description ?? '—'}</dd>
          <dt>{t('ies.versions.col_reason')}</dt>
          <dd>{detail.version.reason}</dd>
          <dt>{t('ies.versions.col_source')}</dt>
          <dd>
            {detail.version.source_draft_id === null || detail.version.source_draft_revision === null
              ? '—'
              : t('ies.versions.source_revision', { revision: detail.version.source_draft_revision })}
          </dd>
          <dt>{t('ies.versions.col_baseline')}</dt>
          <dd>
            {detail.version.project_baseline.resolution} · {detail.version.project_baseline.start_year} ·{' '}
            {detail.version.project_baseline.scenario_mode}
          </dd>
          <dt>{t('ies.versions.col_meta')}</dt>
          <dd>
            {detail.version.currency} · schema v{detail.version.schema_version} ·{' '}
            {t('ies.versions.created_by', { user: detail.version.created_by })} ·{' '}
            {formatDateTime(detail.version.created_at)}
          </dd>
        </dl>
      )}
    </Dialog>
  )
}

/**
 * 版本历史面板: 页面只组合本组件, 不直接拼 DTO。
 * currentVersionId 用于标注当前版本徽标(来自 workbench 项目摘要)。
 */
export function VersionHistoryPanel({
  store,
  currentVersionId,
}: {
  store: UseProjectVersions
  currentVersionId: number | null
}) {
  const { t } = useI18n()
  const [dialogOpen, setDialogOpen] = useState(false)
  const [form, setForm] = useState<VersionCreateForm>({ name: '', description: '' })
  const [formError, setFormError] = useState<string | null>(null)

  const openCreate = () => {
    setForm({ name: '', description: '' })
    setFormError(null)
    store.clearCreateError()
    setDialogOpen(true)
  }

  const submit = async () => {
    let dto
    try {
      dto = buildCreateRequest(form)
    } catch (err) {
      if (err instanceof VersionFormError) {
        setFormError(t('ies.versions.name_required'))
        return
      }
      throw err
    }
    setFormError(null)
    const created: VersionModel | null = await store.submitCreate(dto)
    if (created !== null) setDialogOpen(false)
  }

  return (
    <div className="ies-versions">
      <Card
        title={t('ies.versions.history_title')}
        actions={
          <Button variant="primary" onClick={openCreate}>
            {t('ies.versions.create_open')}
          </Button>
        }
      >
        {store.loadError ? (
          <Alert variant="error" title={t('ies.versions.load_failed')}>
            {store.loadError}
          </Alert>
        ) : store.versions === null || store.reloading ? (
          <div role="status"><Spinner size="md" /> {t('ies.common.loading')}</div>
        ) : store.versions.length === 0 ? (
          <EmptyState
            title={t('ies.versions.empty_title')}
            description={t('ies.versions.empty_desc')}
            action={
              <Button variant="primary" onClick={openCreate}>
                {t('ies.versions.create_open')}
              </Button>
            }
          />
        ) : (
          <Table>
            <THead>
              <TR>
                <TH>{t('ies.versions.col_no')}</TH>
                <TH>{t('ies.versions.field_name')}</TH>
                <TH>{t('ies.versions.col_created')}</TH>
                <TH>{t('ies.versions.col_reason')}</TH>
                <TH>{t('ies.versions.col_source')}</TH>
                <TH>{t('ies.versions.col_current')}</TH>
              </TR>
            </THead>
            <TBody>
              {store.versions.map((v) => (
                <TR key={v.id} clickable onClick={() => store.openDetail(v.id)}>
                  <TD>v{v.version_no}</TD>
                  <TD>{v.name}</TD>
                  <TD>{formatDateTime(v.created_at)}</TD>
                  <TD>{v.reason}</TD>
                  <TD>{v.source_draft_revision === null ? '—' : `r${v.source_draft_revision}`}</TD>
                  <TD>
                    {v.id === currentVersionId ? (
                      <Badge variant="primary" label={t('ies.versions.current_badge')} />
                    ) : null}
                  </TD>
                </TR>
              ))}
            </TBody>
          </Table>
        )}
      </Card>
      <CreateDialog
        open={dialogOpen}
        form={form}
        setForm={setForm}
        formError={formError}
        submitting={store.creating}
        submitError={store.createError}
        onSubmit={submit}
        onClose={() => setDialogOpen(false)}
      />
      {detailOpen(store.detail) ? (
        <DetailDialog detail={store.detail} onClose={store.closeDetail} />
      ) : null}
    </div>
  )
}

function detailOpen(detail: VersionDetailState): detail is Exclude<VersionDetailState, { status: 'idle' }> {
  return detail.status !== 'idle'
}
