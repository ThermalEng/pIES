/**
 * 项目包导入对话框(展示 useImportProposal 状态机; 不直接拼请求 JSON)。
 *
 * 关闭语义: 仅关闭本地界面。取消确认不发送请求、不删除后端提案; 写请求
 * 飞行中禁用关闭, 避免关闭后丢失实际确认结果。确认前不展示、不导航新项目。
 */

import { useRef } from 'react'

import { Alert, Button, Dialog, Spinner } from '../../../components/ui'
import { useI18n } from '../../../i18n'
import { useImportProposal } from '../hooks/useImportProposal'

export interface ImportProposalDialogProps {
  open: boolean
  onClose: () => void
  /** 确认成功后刷新列表(由调用方传入)。 */
  onImported: () => void
  /** 确认成功后打开新项目(由调用方传入项目 id)。 */
  onOpenProject: (projectId: number) => void
}

export function ImportProposalDialog({ open, onClose, onImported, onOpenProject }: ImportProposalDialogProps) {
  const { t } = useI18n()
  const fileRef = useRef<HTMLInputElement | null>(null)
  const { state, busy, actions } = useImportProposal(onImported)

  const handleClose = () => {
    if (busy) return
    actions.reset()
    onClose()
  }

  const handlePick = () => {
    fileRef.current?.click()
  }

  const handleFileChange = () => {
    const file = fileRef.current?.files?.[0]
    if (fileRef.current) fileRef.current.value = ''
    if (!file) return
    actions.upload(file)
  }

  const closeLabel = t('ies.common.close')

  return (
    <Dialog
      open={open}
      onClose={handleClose}
      title={t('ies.import.dialog_title')}
      footer={
        <>
          {state.status === 'proposed' || state.status === 'confirmFailed' ? (
            <Button variant="primary" onClick={actions.confirm}>
              {t('ies.import.confirm_submit')}
            </Button>
          ) : null}
          {state.status === 'done' ? (
            <Button variant="primary" onClick={() => onOpenProject(state.project.id)}>
              {t('ies.import.open_project')}
            </Button>
          ) : null}
          <Button variant="secondary" disabled={busy} onClick={handleClose}>
            {state.status === 'done' ? closeLabel : t('ies.common.cancel')}
          </Button>
        </>
      }
    >
      <input
        ref={fileRef}
        id="import-package-file"
        type="file"
        accept=".zip,application/zip"
        hidden
        disabled={busy}
        onChange={handleFileChange}
      />
      {state.status === 'idle' ? (
        <div>
          <p>{t('ies.import.pick_hint')}</p>
          <Button variant="secondary" onClick={handlePick}>
            {t('ies.import.pick_file')}
          </Button>
        </div>
      ) : null}
      {state.status === 'uploading' ? (
        <div role="status">
          <Spinner label={t('ies.import.uploading', { file: state.fileName })} />
          <p>{t('ies.import.uploading', { file: state.fileName })}</p>
        </div>
      ) : null}
      {state.status === 'uploadFailed' ? (
        <div>
          <Alert variant="error" title={t('ies.import.upload_failed', { file: state.fileName })}>
            <p>{state.message}</p>
            {state.reasons.length > 0 ? (
              <ul>
                {state.reasons.map((reason) => (
                  <li key={reason}>{reason}</li>
                ))}
              </ul>
            ) : null}
          </Alert>
          <Button variant="secondary" onClick={handlePick}>
            {t('ies.import.pick_another')}
          </Button>
        </div>
      ) : null}
      {state.status === 'proposed' || state.status === 'confirming' || state.status === 'confirmFailed' ? (
        <div>
          <p>{t('ies.import.proposal_hint')}</p>
          <dl>
            <dt>{t('ies.import.snapshot_name')}</dt>
            <dd>{state.view.name ?? t('ies.import.unknown_value')}</dd>
            <dt>{t('ies.import.snapshot_currency')}</dt>
            <dd>{state.view.currency ?? t('ies.import.unknown_value')}</dd>
            <dt>{t('ies.import.staged_count')}</dt>
            <dd>{state.view.stagedCount ?? t('ies.import.unknown_value')}</dd>
          </dl>
          <p>{t('ies.import.confirm_impact')}</p>
          {state.status === 'confirming' ? (
            <div role="status">
              <Spinner label={t('ies.import.confirming')} />
            </div>
          ) : null}
          {state.status === 'confirmFailed' ? (
            <Alert variant="error" title={t('ies.import.confirm_failed')}>
              <p>{state.message}</p>
              <Button variant="secondary" size="sm" onClick={actions.confirm}>
                {t('ies.common.retry')}
              </Button>
            </Alert>
          ) : null}
        </div>
      ) : null}
      {state.status === 'done' ? (
        <Alert variant="success" title={t('ies.import.done_title', { name: state.project.name })}>
          <p>{t('ies.import.done_hint')}</p>
        </Alert>
      ) : null}
    </Dialog>
  )
}
