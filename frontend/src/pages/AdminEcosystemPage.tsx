/**
 * 管理员生态治理页(/admin/ecosystem, 默认导出 AdminEcosystemPage, 仅管理员):
 * - 正交双开关(market_mode × local_upload_requires_approval)与单源 pip/market/gateway URL 配置;
 * - 待审批队列(审批通过/拒绝原因);
 * - 本地目录与镜像卸载清理(展示 409 ECO-UNINSTALL-IN-USE 在途任务阻止提示)。
 */

import { useCallback, useEffect, useMemo, useState } from 'react'

import {
  adminUninstallArtifactImage,
  approveEcosystemArtifact,
  formatArtifactDiagnostics,
  getEcosystemSettings,
  listEcosystemArtifacts,
  rejectEcosystemArtifact,
  resolveArtifactLifecycleStatus,
  updateEcosystemSettings,
} from '../features/customization'
import type { LocalArtifactDto, MarketModeDto } from '../features/customization'
import { errorMessage } from '../i18n'
import { pt } from '../i18n/pageMessages'
import { ApiError } from '../types'
import {
  Alert,
  Badge,
  Button,
  Card,
  Checkbox,
  Dialog,
  FormField,
  Input,
  Select,
  Spinner,
  Table,
  TBody,
  TD,
  TH,
  THead,
  TR,
} from '../components/ui'

export default function AdminEcosystemPage() {
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [notice, setNotice] = useState<{ kind: 'success' | 'error'; text: string } | null>(null)
  const [busyKey, setBusyKey] = useState<string | null>(null)

  const [marketMode, setMarketMode] = useState<MarketModeDto>('local')
  const [requiresApproval, setRequiresApproval] = useState(true)
  const [pipIndexUrl, setPipIndexUrl] = useState('')
  const [marketIndexUrl, setMarketIndexUrl] = useState('')
  const [gatewayUrl, setGatewayUrl] = useState('')
  const [artifacts, setArtifacts] = useState<LocalArtifactDto[]>([])

  const [rejectTarget, setRejectTarget] = useState<LocalArtifactDto | null>(null)
  const [rejectReason, setRejectReason] = useState('')

  const reload = useCallback(async () => {
    setLoading(true)
    setLoadError(null)
    try {
      const [cfg, items] = await Promise.all([
        getEcosystemSettings(),
        listEcosystemArtifacts(),
      ])
      setMarketMode(cfg.market_mode)
      setRequiresApproval(cfg.local_upload_requires_approval)
      setPipIndexUrl(cfg.pip_index_url ?? '')
      setMarketIndexUrl(cfg.market_index_url ?? '')
      setGatewayUrl(cfg.submission_gateway_url ?? '')
      setArtifacts(items)
    } catch (err) {
      setLoadError(errorMessage(err))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void reload()
  }, [reload])

  const pendingQueue = useMemo(
    () => artifacts.filter((a) => a.approval_status === 'pending'),
    [artifacts],
  )

  const handleSaveSettings = async () => {
    setBusyKey('settings')
    setNotice(null)
    try {
      const saved = await updateEcosystemSettings({
        market_mode: marketMode,
        local_upload_requires_approval: requiresApproval,
        pip_index_url: pipIndexUrl.trim() || null,
        market_index_url: marketIndexUrl.trim() || null,
        submission_gateway_url: gatewayUrl.trim() || null,
      })
      setMarketMode(saved.market_mode)
      setRequiresApproval(saved.local_upload_requires_approval)
      setPipIndexUrl(saved.pip_index_url ?? '')
      setMarketIndexUrl(saved.market_index_url ?? '')
      setGatewayUrl(saved.submission_gateway_url ?? '')
      setNotice({ kind: 'success', text: pt('ies.admin_eco.settings_saved') })
    } catch (err) {
      setNotice({ kind: 'error', text: errorMessage(err) })
    } finally {
      setBusyKey(null)
    }
  }

  const handleApprove = async (art: LocalArtifactDto) => {
    setBusyKey(`approve-${art.id}`)
    setNotice(null)
    try {
      const res = await approveEcosystemArtifact(art.id)
      setNotice({ kind: 'success', text: pt('ies.admin_eco.approved_ok', { id: res.stable_id }) })
      await reload()
    } catch (err) {
      setNotice({ kind: 'error', text: errorMessage(err) })
    } finally {
      setBusyKey(null)
    }
  }

  const handleConfirmReject = async () => {
    if (!rejectTarget) return
    setBusyKey(`reject-${rejectTarget.id}`)
    setNotice(null)
    try {
      const res = await rejectEcosystemArtifact(rejectTarget.id, rejectReason.trim())
      setNotice({ kind: 'success', text: pt('ies.admin_eco.rejected_ok', { id: res.stable_id }) })
      setRejectTarget(null)
      setRejectReason('')
      await reload()
    } catch (err) {
      setNotice({ kind: 'error', text: errorMessage(err) })
    } finally {
      setBusyKey(null)
    }
  }

  const handleAdminUninstall = async (art: LocalArtifactDto) => {
    setBusyKey(`uninstall-${art.id}`)
    setNotice(null)
    try {
      const res = await adminUninstallArtifactImage(art.id)
      setNotice({
        kind: 'success',
        text: pt('ies.admin_eco.uninstall_ok', {
          id: res.artifact.stable_id,
          status: res.image_status,
        }),
      })
      await reload()
    } catch (err) {
      if (err instanceof ApiError && err.code === 'ECO-UNINSTALL-IN-USE') {
        const count = Number(err.params?.inflight_count ?? 0)
        setNotice({
          kind: 'error',
          text: pt('ies.admin_eco.uninstall_in_use', { id: art.stable_id, count }),
        })
        await reload()
      } else {
        setNotice({ kind: 'error', text: errorMessage(err) })
      }
    } finally {
      setBusyKey(null)
    }
  }

  if (loading && artifacts.length === 0 && !loadError) {
    return (
      <div className="ies-page-placeholder">
        <Spinner size="lg" />
      </div>
    )
  }

  return (
    <div className="ies-stack">
      <div className="ies-page-header">
        <div>
          <h1 className="ies-page-title">{pt('ies.admin_eco.title')}</h1>
          <p className="ies-page-subtitle">{pt('ies.admin_eco.desc')}</p>
        </div>
        <Button variant="secondary" size="sm" onClick={() => void reload()} loading={loading}>
          {pt('ies.eco.refresh')}
        </Button>
      </div>

      {loadError ? <Alert variant="error" title={loadError} /> : null}
      {notice ? (
        <Alert
          variant={notice.kind === 'success' ? 'success' : 'error'}
          title={notice.text}
          closable
          onClose={() => setNotice(null)}
        />
      ) : null}

      <Card
        title={pt('ies.admin_eco.settings_title')}
        actions={
          <Button
            variant="primary"
            size="sm"
            loading={busyKey === 'settings'}
            disabled={busyKey !== null}
            onClick={() => void handleSaveSettings()}
          >
            {pt('ies.admin_eco.save_settings')}
          </Button>
        }
      >
        <div className="ies-form-grid">
          <FormField label={pt('ies.admin_eco.market_mode')} htmlFor="eco-market-mode">
            <Select
              id="eco-market-mode"
              value={marketMode}
              onChange={(e) => setMarketMode(e.target.value as MarketModeDto)}
            >
              <option value="local">{pt('ies.eco.mode_local')}</option>
              <option value="online">{pt('ies.eco.mode_online')}</option>
            </Select>
          </FormField>
          <FormField label={pt('ies.admin_eco.approval_switch')} htmlFor="eco-approval-switch">
            <Checkbox
              id="eco-approval-switch"
              checked={requiresApproval}
              onChange={(e) => setRequiresApproval(e.target.checked)}
              label={pt('ies.admin_eco.approval_switch_label')}
            />
          </FormField>
          <FormField label="pip_index_url" htmlFor="eco-pip-url">
            <Input
              id="eco-pip-url"
              value={pipIndexUrl}
              onChange={(e) => setPipIndexUrl(e.target.value)}
              placeholder="https://pypi.org/simple"
            />
          </FormField>
          <FormField label="market_index_url" htmlFor="eco-market-url">
            <Input
              id="eco-market-url"
              value={marketIndexUrl}
              onChange={(e) => setMarketIndexUrl(e.target.value)}
              placeholder="https://market.example.com/index.json"
            />
          </FormField>
          <FormField label="submission_gateway_url" htmlFor="eco-gateway-url">
            <Input
              id="eco-gateway-url"
              value={gatewayUrl}
              onChange={(e) => setGatewayUrl(e.target.value)}
              placeholder="https://submit.example.com"
            />
          </FormField>
        </div>
      </Card>

      <Card title={pt('ies.admin_eco.pending_title', { count: pendingQueue.length })}>
        {pendingQueue.length === 0 ? (
          <p className="ies-text-secondary">{pt('ies.admin_eco.pending_empty')}</p>
        ) : (
          <Table>
            <THead>
              <TR>
                <TH>ID</TH>
                <TH>{pt('ies.eco.col_kind')}</TH>
                <TH>{pt('ies.eco.col_version')}</TH>
                <TH>{pt('ies.admin_eco.col_origin')}</TH>
                <TH>{pt('ies.eco.col_actions')}</TH>
              </TR>
            </THead>
            <TBody>
              {pendingQueue.map((art) => (
                <TR key={art.id} data-pending-artifact={art.stable_id}>
                  <TD><code>{art.stable_id}</code></TD>
                  <TD>{pt(`ies.eco.kind.${art.kind}`)}</TD>
                  <TD>{art.kind === 'device_model' ? `r${art.version}` : art.version}</TD>
                  <TD>{art.source_origin}</TD>
                  <TD>
                    <div className="ies-flex">
                      <Button
                        size="sm"
                        variant="primary"
                        loading={busyKey === `approve-${art.id}`}
                        disabled={busyKey !== null}
                        onClick={() => void handleApprove(art)}
                      >
                        {pt('ies.admin_eco.approve')}
                      </Button>
                      <Button
                        size="sm"
                        variant="danger"
                        disabled={busyKey !== null}
                        onClick={() => {
                          setRejectTarget(art)
                          setRejectReason('')
                        }}
                      >
                        {pt('ies.admin_eco.reject')}
                      </Button>
                    </div>
                  </TD>
                </TR>
              ))}
            </TBody>
          </Table>
        )}
      </Card>

      <Card title={pt('ies.admin_eco.catalog_title')}>
        {artifacts.length === 0 ? (
          <p className="ies-text-secondary">{pt('ies.eco.submissions_empty')}</p>
        ) : (
          <Table>
            <THead>
              <TR>
                <TH>ID</TH>
                <TH>{pt('ies.eco.col_kind')}</TH>
                <TH>{pt('ies.eco.col_version')}</TH>
                <TH>{pt('ies.eco.col_status')}</TH>
                <TH>{pt('ies.eco.col_diag')}</TH>
                <TH>{pt('ies.eco.col_actions')}</TH>
              </TR>
            </THead>
            <TBody>
              {artifacts.map((art) => {
                const life = resolveArtifactLifecycleStatus(art)
                const diagText = formatArtifactDiagnostics(art.diagnostics)
                return (
                  <TR key={art.id} data-admin-artifact={art.stable_id}>
                    <TD><code>{art.stable_id}</code></TD>
                    <TD>{pt(`ies.eco.kind.${art.kind}`)}</TD>
                    <TD>{art.kind === 'device_model' ? `r${art.version}` : art.version}</TD>
                    <TD>
                      <Badge
                        variant={life === 'ready' ? 'success' : life === 'build_failed' || life === 'rejected' ? 'danger' : 'warning'}
                        size="sm"
                        label={pt(`ies.eco.life.${life}`)}
                      />
                    </TD>
                    <TD>{diagText ?? '—'}</TD>
                    <TD>
                      <Button
                        size="sm"
                        variant="danger"
                        loading={busyKey === `uninstall-${art.id}`}
                        disabled={busyKey !== null}
                        onClick={() => void handleAdminUninstall(art)}
                      >
                        {pt('ies.admin_eco.uninstall_cleanup')}
                      </Button>
                    </TD>
                  </TR>
                )
              })}
            </TBody>
          </Table>
        )}
      </Card>

      <Dialog
        open={rejectTarget !== null}
        onClose={() => setRejectTarget(null)}
        title={pt('ies.admin_eco.reject_title', { id: rejectTarget?.stable_id ?? '' })}
        footer={
          <div className="ies-flex">
            <Button variant="secondary" size="sm" onClick={() => setRejectTarget(null)}>
              {pt('ies.custom.back')}
            </Button>
            <Button
              variant="danger"
              size="sm"
              loading={rejectTarget !== null && busyKey === `reject-${rejectTarget.id}`}
              onClick={() => void handleConfirmReject()}
            >
              {pt('ies.admin_eco.confirm_reject')}
            </Button>
          </div>
        }
      >
        <FormField label={pt('ies.admin_eco.reject_reason')} htmlFor="eco-reject-reason">
          <Input
            id="eco-reject-reason"
            value={rejectReason}
            onChange={(e) => setRejectReason(e.target.value)}
            placeholder={pt('ies.admin_eco.reject_reason_placeholder')}
          />
        </FormField>
      </Dialog>
    </div>
  )
}
