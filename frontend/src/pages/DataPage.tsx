/**
 * 数据管理页(设计输入 §8 数据约束 / §15.3 时序数据字段)。
 *
 * - 数据集列表:名称 / 最新版本 / 质量报告摘要(缺失率、异常率、插值说明)/
 *   溯源 / 许可证 / 绑定状态;
 * - 上传:选择分辨率(15/30/60 分钟)+ 固定 UTC 偏移 + 字段描述 + 文件;
 *   后端返回质量报告与诊断列表(阻断/警告分级,经 location 定位到字段/行);
 *   阻断性错误未修复时版本不可绑定计算;
 * - 内置样例数据:客户端生成 365 天非闰年合成 CSV,走同一上传管线;
 * - 模板下载:后端模板接口,不可用时回退本地标准模板;
 * - 版本历史与质量报告详情、数据预览。
 *
 * 挂载方式:路由 /projects/:id/data 由 useParams 取项目 id;
 * 也可由工作台以 <DataPage projectId={id} /> 方式嵌入。
 */

import { useEffect, useState } from 'react'
import { useParams } from 'react-router-dom'

import { api, downloadBlob } from '../api/client'
import { DiagnosticsList } from '../components/DiagnosticsList'
import {
  Alert,
  Badge,
  Button,
  Card,
  Dialog,
  EmptyState,
  FormField,
  Icon,
  IconButton,
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
import { translateError, useI18n } from '../i18n'
import { pt } from '../i18n/pageMessages'
import {
  RESOLUTION_OPTIONS,
  csvTemplate,
  expectedRows,
  resolutionOption,
  syntheticCsv,
} from '../lib/datasetCsv'
import { formatDateTime, formatPercent } from '../lib/format'
import { ApiError } from '../types'
import type { Dataset, DatasetSample, DatasetVersion, Diagnostic, Project } from '../types'

export interface DataPageProps {
  projectId?: number
}

/** 支持的固定 UTC 偏移(分钟,整点,对齐数据库 CHECK -720..840)。 */
const OFFSET_OPTIONS: readonly number[] = (() => {
  const list: number[] = []
  for (let m = -720; m <= 840; m += 60) list.push(m)
  return list
})()

function errorText(err: unknown): string {
  return err instanceof ApiError ? translateError(err) : pt('ies.error.unknown', { reason: String(err) })
}

function utcLabel(minutes: number): string {
  const sign = minutes >= 0 ? '+' : '-'
  const abs = Math.abs(minutes)
  return `UTC${sign}${String(Math.floor(abs / 60)).padStart(2, '0')}:${String(abs % 60).padStart(2, '0')}`
}

function provenanceSummary(p: Record<string, unknown> | null): string {
  if (!p) return '—'
  const s = (k: string) => (typeof p[k] === 'string' && p[k] ? (p[k] as string) : null)
  const source = s('source') ?? s('source_category') ?? s('origin')
  const ver = s('source_version') ?? s('version')
  if (source && ver) return `${source} (${ver})`
  if (source) return source
  return JSON.stringify(p).slice(0, 80)
}

/** 最新版本质量摘要徽章(文字 + 图标 + 形状三重编码)。 */
function QualityBadge({ version }: { version: DatasetVersion | undefined }) {
  const q = version?.quality_report
  if (!q) return <Badge label="—" variant="neutral" />
  const missing = q.missing_rate ?? 0
  const outlier = q.outlier_rate ?? 0
  const notes = q.interpolation_notes?.length ?? 0
  const stats = `${pt('ies.data.missing_rate')} ${formatPercent(missing)} · ${pt('ies.data.outlier_rate')} ${formatPercent(outlier)}`
  if (missing > 0.01 || outlier > 0.01) {
    return <Badge label={stats} variant="warning" icon="warning" shape="triangle" />
  }
  if (notes > 0) {
    return (
      <Badge
        label={`${stats} · ${pt('ies.data.interpolation_notes')} ${notes}`}
        variant="info"
        icon="info"
      />
    )
  }
  return <Badge label={pt('ies.data.quality_good')} variant="success" icon="check" />
}

// ---------------------------------------------------------------------------
// 上传结果
// ---------------------------------------------------------------------------

interface UploadOutcome {
  version: DatasetVersion
  diagnostics: Diagnostic[]
}

interface FieldRow {
  key: string
  name: string
  unit: string
  description: string
}

let FIELD_SEQ = 0

function defaultFields(): FieldRow[] {
  return [
    { key: 'f0', name: 'e_load', unit: 'kW', description: '' },
    { key: 'f1', name: 'h_load', unit: 'kW', description: '' },
    { key: 'f2', name: 'c_load', unit: 'kW', description: '' },
    { key: 'f3', name: 't_ambient', unit: '°C', description: '' },
  ]
}

function sampleFields(): FieldRow[] {
  return [
    { key: 'f0', name: 'e_load', unit: 'kW', description: '电负荷' },
    { key: 'f1', name: 'h_load', unit: 'kW', description: '热负荷' },
    { key: 'f2', name: 'c_load', unit: 'kW', description: '冷负荷' },
    { key: 'f3', name: 't_ambient', unit: '°C', description: '环境温度' },
    { key: 'f4', name: 'ghi', unit: 'W/m²', description: '水平面总辐照度' },
    { key: 'f5', name: 'pv_availability', unit: '0-1', description: '光伏可用率' },
    { key: 'f6', name: 'buy_price', unit: 'CNY/kWh', description: '分时购电价格' },
    { key: 'f7', name: 'grid_emission_factor', unit: 'kgCO2/kWh', description: '电网排放因子' },
  ]
}

/** 统一上传管线:附带分辨率/UTC 偏移/字段定义/许可证(设计输入 §8.2)。 */
function runUpload(opts: {
  projectId?: number
  name: string
  description?: string
  resolutionValue: string
  offsetMinutes: number
  license?: string
  fields: FieldRow[]
  file: File
}): Promise<UploadOutcome> {
  const opt = resolutionOption(opts.resolutionValue)
  return api.datasets
    .uploadDetailed({
      project_id: opts.projectId,
      name: opts.name,
      description: opts.description || undefined,
      timeline: opt.timeline,
      resolution: opt.value,
      fixed_utc_offset_minutes: opts.offsetMinutes,
      fields: Object.fromEntries(
        opts.fields
          .filter((f) => f.name.trim())
          .map((f) => [
            f.name.trim(),
            { unit: f.unit.trim() || null, description: f.description.trim() || undefined },
          ]),
      ),
      license: opts.license || undefined,
      file: opts.file,
    })
    .then((res) => ({ version: res, diagnostics: res.diagnostics ?? [] }))
}

/** 上传结果面板:质量报告摘要 + 阻断/警告诊断 + 绑定状态。 */
function UploadResultPanel({ result }: { result: UploadOutcome }) {
  const { version, diagnostics } = result
  const q = version.quality_report
  const blocking = diagnostics.filter((d) => d.blocking || d.severity === 'blocking').length
  return (
    <div className="ies-upload-result">
      <div className="ies-flex" style={{ flexWrap: 'wrap' }}>
        <Badge label={`v${version.version_no}`} variant="primary" />
        <Badge label={`${pt('ies.data.resolution')}: ${version.resolution}`} variant="neutral" />
        <Badge label={utcLabel(version.fixed_utc_offset_minutes)} variant="neutral" />
        {version.license ? <Badge label={version.license} variant="neutral" /> : null}
        <Badge label={pt('ies.data.binding')} variant="neutral" />
      </div>
      <div className="ies-flex" style={{ flexWrap: 'wrap', margin: 'var(--ies-space-2) 0' }}>
        <span className="ies-form-message">
          {pt('ies.data.missing_rate')}: {q ? formatPercent(q.missing_rate) : '—'}
        </span>
        <span className="ies-form-message">
          {pt('ies.data.outlier_rate')}: {q ? formatPercent(q.outlier_rate) : '—'}
        </span>
        {q?.interpolation_notes
          ? q.interpolation_notes.map((note) => (
              <span key={note} className="ies-mono">
                {note}
              </span>
            ))
          : null}
      </div>
      {blocking > 0 ? (
        <Alert variant="error" title={pt('ies.data.blocking_unresolved')}>
          {pt('ies.data.fix_and_reupload')}
        </Alert>
      ) : (
        <Alert variant="success" title={pt('ies.data.validation_passed')} />
      )}
      {diagnostics.length > 0 ? (
        <div style={{ marginTop: 'var(--ies-space-3)' }}>
          <h4 className="ies-config-section-title">{pt('ies.data.diagnostics')}</h4>
          <DiagnosticsList diagnostics={diagnostics} />
        </div>
      ) : null}
    </div>
  )
}

// ---------------------------------------------------------------------------
// 上传对话框
// ---------------------------------------------------------------------------

interface UploadDialogProps {
  projectId?: number
  defaultOffsetMinutes: number
  open: boolean
  onClose: () => void
  onUploaded: () => void
}

function UploadDialog({ projectId, defaultOffsetMinutes, open, onClose, onUploaded }: UploadDialogProps) {
  const { t } = useI18n()
  const [name, setName] = useState('')
  const [resolution, setResolution] = useState('1h')
  const [offset, setOffset] = useState(defaultOffsetMinutes)
  const [fields, setFields] = useState<FieldRow[]>(defaultFields)
  const [license, setLicense] = useState('')
  const [description, setDescription] = useState('')
  const [file, setFile] = useState<File | null>(null)
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<UploadOutcome | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!open) return
    setName('')
    setResolution('1h')
    setOffset(defaultOffsetMinutes)
    setFields(defaultFields())
    setLicense('')
    setDescription('')
    setFile(null)
    setBusy(false)
    setResult(null)
    setError(null)
  }, [open, defaultOffsetMinutes])

  const opt = resolutionOption(resolution)

  function updateField(key: string, part: 'name' | 'unit' | 'description', value: string) {
    setFields((prev) => prev.map((f) => (f.key === key ? { ...f, [part]: value } : f)))
  }

  async function submit() {
    if (!name.trim()) {
      setError(t('ies.common.required_field'))
      return
    }
    if (!file) {
      setError(pt('ies.data.file_required'))
      return
    }
    setBusy(true)
    setError(null)
    try {
      const res = await runUpload({
        projectId,
        name,
        description,
        resolutionValue: resolution,
        offsetMinutes: offset,
        license,
        fields,
        file,
      })
      setResult(res)
    } catch (err) {
      setError(errorText(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Dialog
      open={open}
      onClose={onClose}
      title={t('ies.data.upload')}
      size="lg"
      footer={
        result ? (
          <>
            <Button variant="secondary" onClick={() => setResult(null)}>
              {t('ies.data.upload')}
            </Button>
            <Button
              variant="primary"
              onClick={() => {
                onClose()
                onUploaded()
              }}
            >
              {t('ies.common.ok')}
            </Button>
          </>
        ) : (
          <>
            <Button variant="secondary" onClick={onClose}>
              {t('ies.common.cancel')}
            </Button>
            <Button variant="primary" loading={busy} onClick={() => void submit()} disabled={busy}>
              {t('ies.data.upload')}
            </Button>
          </>
        )
      }
    >
      {result ? (
        <UploadResultPanel result={result} />
      ) : (
        <>
          <div className="ies-meta-grid">
            <FormField label={t('ies.common.name')} required htmlFor="dset-name">
              <Input
                id="dset-name"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="e.g. 园区负荷 2025"
              />
            </FormField>
            <FormField
              label={pt('ies.data.resolution')}
              htmlFor="dset-res"
              hint={pt('ies.data.row_count_expect', {
                steps: 60 / opt.minutes,
                rows: expectedRows(opt),
              })}
            >
              <Select id="dset-res" value={resolution} onChange={(e) => setResolution(e.target.value)}>
                {RESOLUTION_OPTIONS.map((r) => (
                  <option key={r.value} value={r.value}>
                    {pt(`ies.data.resolution_${r.value}`)}
                  </option>
                ))}
              </Select>
            </FormField>
          </div>
          <FormField label={pt('ies.data.utc_offset')} htmlFor="dset-offset" hint={pt('ies.data.utc_note')}>
            <Select id="dset-offset" value={offset} onChange={(e) => setOffset(Number(e.target.value))}>
              {OFFSET_OPTIONS.map((m) => (
                <option key={m} value={m}>
                  {utcLabel(m)}
                </option>
              ))}
            </Select>
          </FormField>
          <FormField label={pt('ies.data.field_desc')}>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--ies-space-2)' }}>
              {fields.map((f, idx) => (
                <div key={f.key} className="ies-field-row">
                  <Input
                    aria-label={`${pt('ies.data.field_desc')} ${idx + 1} ${t('ies.common.name')}`}
                    value={f.name}
                    placeholder="field_name"
                    onChange={(e) => updateField(f.key, 'name', e.target.value)}
                  />
                  <Input
                    aria-label={`${pt('ies.data.field_desc')} ${idx + 1} ${t('ies.data.field_unit')}`}
                    value={f.unit}
                    placeholder={t('ies.data.field_unit')}
                    onChange={(e) => updateField(f.key, 'unit', e.target.value)}
                  />
                  <Input
                    aria-label={`${pt('ies.data.field_desc')} ${idx + 1} ${t('ies.common.description')}`}
                    value={f.description}
                    placeholder={t('ies.common.description')}
                    onChange={(e) => updateField(f.key, 'description', e.target.value)}
                  />
                  <IconButton
                    aria-label={pt('ies.data.remove_field')}
                    onClick={() => setFields((prev) => prev.filter((row) => row.key !== f.key))}
                  >
                    <Icon name="trash" size={14} />
                  </IconButton>
                </div>
              ))}
              <div>
                <Button
                  variant="ghost"
                  size="sm"
                  icon="plus"
                  onClick={() =>
                    setFields((prev) => [
                      ...prev,
                      { key: `f${++FIELD_SEQ}`, name: '', unit: '', description: '' },
                    ])
                  }
                >
                  {pt('ies.data.add_field')}
                </Button>
              </div>
            </div>
          </FormField>
          <div className="ies-meta-grid">
            <FormField label={pt('ies.data.license')} htmlFor="dset-license">
              <Input id="dset-license" value={license} onChange={(e) => setLicense(e.target.value)} placeholder="CC-BY-4.0" />
            </FormField>
            <FormField label={t('ies.common.description')} htmlFor="dset-desc">
              <Input id="dset-desc" value={description} onChange={(e) => setDescription(e.target.value)} />
            </FormField>
          </div>
          <FormField label={pt('ies.data.file')} required htmlFor="dset-file">
            <Input
              id="dset-file"
              type="file"
              accept=".csv,text/csv"
              onChange={(e) => setFile(e.target.files?.[0] ?? null)}
            />
          </FormField>
          {error ? <Alert variant="error">{error}</Alert> : null}
        </>
      )}
    </Dialog>
  )
}

// ---------------------------------------------------------------------------
// 内置样例数据对话框
// ---------------------------------------------------------------------------

interface SampleDialogProps {
  projectId?: number
  defaultOffsetMinutes: number
  open: boolean
  onClose: () => void
  onUploaded: () => void
}

function SampleDialog({ projectId, defaultOffsetMinutes, open, onClose, onUploaded }: SampleDialogProps) {
  const { t } = useI18n()
  const [name, setName] = useState('')
  const [resolution, setResolution] = useState('1h')
  const [license, setLicense] = useState('')
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<UploadOutcome | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!open) return
    setName('')
    setResolution('1h')
    setLicense('')
    setBusy(false)
    setResult(null)
    setError(null)
  }, [open])

  const opt = resolutionOption(resolution)

  async function generate() {
    setBusy(true)
    setError(null)
    try {
      const csv = syntheticCsv(opt)
      const file = new File([csv], `${name.trim() || 'sample'}.csv`, { type: 'text/csv' })
      const res = await runUpload({
        projectId,
        name: name.trim() || pt('ies.data.sample_generate'),
        resolutionValue: resolution,
        offsetMinutes: defaultOffsetMinutes,
        license,
        fields: sampleFields(),
        file,
      })
      setResult(res)
    } catch (err) {
      setError(errorText(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Dialog
      open={open}
      onClose={onClose}
      title={pt('ies.data.sample_title')}
      size="md"
      footer={
        result ? (
          <>
            <Button variant="secondary" onClick={() => setResult(null)}>
              {t('ies.data.upload')}
            </Button>
            <Button
              variant="primary"
              onClick={() => {
                onClose()
                onUploaded()
              }}
            >
              {t('ies.common.ok')}
            </Button>
          </>
        ) : (
          <>
            <Button variant="secondary" onClick={onClose}>
              {t('ies.common.cancel')}
            </Button>
            <Button variant="primary" loading={busy} onClick={() => void generate()} disabled={busy}>
              {pt('ies.data.sample_upload')}
            </Button>
          </>
        )
      }
    >
      {result ? (
        <UploadResultPanel result={result} />
      ) : (
        <>
          <Alert variant="info">{pt('ies.data.sample_desc')}</Alert>
          <div style={{ marginTop: 'var(--ies-space-4)' }}>
            <FormField label={t('ies.common.name')} htmlFor="sample-name">
              <Input id="sample-name" value={name} onChange={(e) => setName(e.target.value)} placeholder="园区示例数据" />
            </FormField>
            <FormField
              label={pt('ies.data.resolution')}
              htmlFor="sample-res"
              hint={pt('ies.data.row_count_expect', {
                steps: 60 / opt.minutes,
                rows: expectedRows(opt),
              })}
            >
              <Select id="sample-res" value={resolution} onChange={(e) => setResolution(e.target.value)}>
                {RESOLUTION_OPTIONS.map((r) => (
                  <option key={r.value} value={r.value}>
                    {pt(`ies.data.resolution_${r.value}`)}
                  </option>
                ))}
              </Select>
            </FormField>
            <FormField label={pt('ies.data.license')} htmlFor="sample-license">
              <Input id="sample-license" value={license} onChange={(e) => setLicense(e.target.value)} placeholder="CC-BY-4.0" />
            </FormField>
          </div>
          {error ? <Alert variant="error">{error}</Alert> : null}
        </>
      )}
    </Dialog>
  )
}

// ---------------------------------------------------------------------------
// 版本历史与质量报告详情
// ---------------------------------------------------------------------------

function VersionDetail({ version }: { version: DatasetVersion }) {
  const { t } = useI18n()
  const q = version.quality_report
  return (
    <div style={{ marginTop: 'var(--ies-space-4)' }}>
      <h3 className="ies-config-section-title" style={{ marginTop: 0 }}>
        {pt('ies.data.version_detail')} v{version.version_no}
      </h3>
      <div className="ies-meta-grid">
        <div>
          <h4 className="ies-config-section-title" style={{ marginTop: 0 }}>
            {pt('ies.data.quality')}
          </h4>
          <div className="ies-flex" style={{ flexWrap: 'wrap' }}>
            <span className="ies-form-message">
              {pt('ies.data.missing_rate')}: {q ? formatPercent(q.missing_rate) : '—'}
            </span>
            <span className="ies-form-message">
              {pt('ies.data.outlier_rate')}: {q ? formatPercent(q.outlier_rate) : '—'}
            </span>
          </div>
          {q?.interpolation_notes?.length ? (
            <ul style={{ margin: 'var(--ies-space-2) 0 0', paddingLeft: 'var(--ies-space-4)' }}>
              {q.interpolation_notes.map((note) => (
                <li key={note} className="ies-mono">
                  {note}
                </li>
              ))}
            </ul>
          ) : null}
          <p className="ies-mono" style={{ marginTop: 'var(--ies-space-2)' }}>
            sha256: {version.content_hash}
          </p>
        </div>
        <div>
          <h4 className="ies-config-section-title" style={{ marginTop: 0 }}>
            {pt('ies.data.provenance_detail')}
          </h4>
          <pre className="ies-pre">{version.provenance ? JSON.stringify(version.provenance, null, 2) : '—'}</pre>
        </div>
      </div>
      <h4 className="ies-config-section-title">{t('ies.data.fields')}</h4>
      <Table>
        <THead>
          <TR>
            <TH>{t('ies.common.name')}</TH>
            <TH>{t('ies.common.type')}</TH>
            <TH>{t('ies.data.field_unit')}</TH>
            <TH>{t('ies.common.description')}</TH>
          </TR>
        </THead>
        <TBody>
          {Object.entries(version.fields ?? {}).map(([fieldName, f]) => (
            <TR key={fieldName}>
              <TD>
                <span className="ies-mono">{fieldName}</span>
              </TD>
              <TD>{f.type}</TD>
              <TD>{f.unit ?? '—'}</TD>
              <TD>{f.description ?? '—'}</TD>
            </TR>
          ))}
        </TBody>
      </Table>
    </div>
  )
}

function VersionsDialog({
  dataset,
  versions,
  open,
  onClose,
}: {
  dataset: Dataset | null
  versions: DatasetVersion[]
  open: boolean
  onClose: () => void
}) {
  const { t } = useI18n()
  const [selected, setSelected] = useState<DatasetVersion | null>(null)

  useEffect(() => {
    if (open) setSelected(null)
  }, [open])

  const sorted = [...versions].sort((a, b) => b.version_no - a.version_no)

  return (
    <Dialog
      open={open}
      onClose={onClose}
      title={`${dataset?.name ?? ''} · ${t('ies.data.versions')}`}
      size="lg"
      footer={
        <Button variant="primary" onClick={onClose}>
          {t('ies.common.close')}
        </Button>
      }
    >
      <Table>
        <THead>
          <TR>
            <TH>#</TH>
            <TH>{t('ies.common.created_at')}</TH>
            <TH>{t('ies.data.timeline')}</TH>
            <TH>{pt('ies.data.resolution')}</TH>
            <TH>{pt('ies.data.utc_offset')}</TH>
            <TH>{pt('ies.data.quality')}</TH>
            <TH>{pt('ies.data.license')}</TH>
          </TR>
        </THead>
        <TBody>
          {sorted.length === 0 ? (
            <TR>
              <TD colSpan={7}>{pt('ies.data.no_versions')}</TD>
            </TR>
          ) : (
            sorted.map((v) => (
              <TR key={v.id} clickable onClick={() => setSelected(v)}>
                <TD>
                  <div className="ies-flex">
                    <Badge label={`v${v.version_no}`} variant={selected?.id === v.id ? 'primary' : 'neutral'} />
                    {v.version_no === sorted[0].version_no ? (
                      <Badge label={pt('ies.data.latest')} variant="success" size="sm" />
                    ) : null}
                  </div>
                </TD>
                <TD>{formatDateTime(v.created_at)}</TD>
                <TD>{t(`ies.data.timeline_${v.timeline}`)}</TD>
                <TD>
                  <span className="ies-mono">{v.resolution}</span>
                </TD>
                <TD>{utcLabel(v.fixed_utc_offset_minutes)}</TD>
                <TD>
                  <QualityBadge version={v} />
                </TD>
                <TD>{v.license ?? '—'}</TD>
              </TR>
            ))
          )}
        </TBody>
      </Table>
      {selected ? <VersionDetail version={selected} /> : null}
    </Dialog>
  )
}

// ---------------------------------------------------------------------------
// 数据预览
// ---------------------------------------------------------------------------

function PreviewDialog({
  dataset,
  open,
  onClose,
}: {
  dataset: Dataset | null
  open: boolean
  onClose: () => void
}) {
  const { t } = useI18n()
  const [sample, setSample] = useState<DatasetSample | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!open || !dataset) return
    let cancelled = false
    setSample(null)
    setError(null)
    api.datasets
      .sample(dataset.id)
      .then((s) => {
        if (!cancelled) setSample(s)
      })
      .catch((err) => {
        if (!cancelled) setError(errorText(err))
      })
    return () => {
      cancelled = true
    }
  }, [open, dataset])

  return (
    <Dialog
      open={open}
      onClose={onClose}
      title={`${dataset?.name ?? ''} · ${t('ies.data.sample')}`}
      size="lg"
      footer={
        <Button variant="primary" onClick={onClose}>
          {t('ies.common.close')}
        </Button>
      }
    >
      {error ? <Alert variant="error">{error}</Alert> : null}
      {!sample && !error ? <Spinner /> : null}
      {sample ? (
        <>
          <p className="ies-form-message">
            {t('ies.data.row_count')}: {sample.total_rows}
          </p>
          <Table>
            <THead>
              <TR>
                {sample.headers.map((h) => (
                  <TH key={h}>{h}</TH>
                ))}
              </TR>
            </THead>
            <TBody>
              {sample.rows.slice(0, 20).map((row, ri) => (
                <TR key={ri}>
                  {row.map((cell, ci) => (
                    <TD key={ci}>{String(cell)}</TD>
                  ))}
                </TR>
              ))}
            </TBody>
          </Table>
        </>
      ) : null}
    </Dialog>
  )
}

// ---------------------------------------------------------------------------
// 页面主体
// ---------------------------------------------------------------------------

export function DataPage({ projectId }: DataPageProps) {
  const { id } = useParams()
  const pid = projectId ?? (id !== undefined && Number.isFinite(Number(id)) ? Number(id) : undefined)
  const { t } = useI18n()

  const [datasets, setDatasets] = useState<Dataset[] | null>(null)
  const [versions, setVersions] = useState<Record<number, DatasetVersion[]>>({})
  const [project, setProject] = useState<Project | null>(null)
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [notice, setNotice] = useState<{ kind: 'success' | 'error'; text: string } | null>(null)
  const [uploadOpen, setUploadOpen] = useState(false)
  const [sampleOpen, setSampleOpen] = useState(false)
  const [versionsTarget, setVersionsTarget] = useState<Dataset | null>(null)
  const [previewTarget, setPreviewTarget] = useState<Dataset | null>(null)
  const [templateBusy, setTemplateBusy] = useState(false)
  const [refreshTick, setRefreshTick] = useState(0)

  useEffect(() => {
    let cancelled = false
    async function load() {
      setLoading(true)
      setLoadError(null)
      try {
        const page = await api.datasets.list(pid !== undefined ? { project_id: pid, limit: 100 } : { limit: 100 })
        if (cancelled) return
        setDatasets(page.items)
        if (pid !== undefined) {
          api.projects
            .get(pid)
            .then((p) => {
              if (!cancelled) setProject(p)
            })
            .catch(() => undefined)
        }
        // 每个数据集拉取版本历史(最新版本含质量报告/溯源/许可证)
        const entries = await Promise.all(
          page.items.map(async (ds) => {
            try {
              const vs = await api.datasets.versions(ds.id)
              return [ds.id, vs] as const
            } catch {
              return [ds.id, [] as DatasetVersion[]] as const
            }
          }),
        )
        if (!cancelled) setVersions(Object.fromEntries(entries))
      } catch (err) {
        if (!cancelled) setLoadError(errorText(err))
      } finally {
        if (!cancelled) setLoading(false)
      }
    }
    void load()
    return () => {
      cancelled = true
    }
  }, [pid, refreshTick])

  function latestVersion(ds: Dataset): DatasetVersion | undefined {
    const vs = versions[ds.id] ?? []
    return vs.length > 0 ? vs.reduce((a, b) => (a.version_no >= b.version_no ? a : b)) : undefined
  }

  async function downloadTemplate() {
    setTemplateBusy(true)
    try {
      const blob = await api.datasets.template()
      downloadBlob(blob, 'ies_data_template.csv')
    } catch {
      // 后端模板暂不可用:回退本地标准模板(表头 + 示例行)
      downloadBlob(new Blob([csvTemplate()], { type: 'text/csv' }), 'ies_data_template.csv')
    } finally {
      setTemplateBusy(false)
      setNotice({ kind: 'success', text: pt('ies.data.template_downloaded') })
    }
  }

  if (loading && !datasets) {
    return (
      <div className="ies-page-placeholder">
        <Spinner size="lg" />
      </div>
    )
  }

  if (loadError && !datasets) {
    return (
      <div className="ies-page-placeholder">
        <Alert variant="error" title={t('ies.common.load_failed', { reason: loadError })} />
        <Button variant="secondary" onClick={() => setRefreshTick((x) => x + 1)}>
          {t('ies.common.retry')}
        </Button>
      </div>
    )
  }

  return (
    <div>
      <div className="ies-page-header">
        <div>
          <h1 className="ies-page-title">{t('ies.data.title')}</h1>
          <p className="ies-page-subtitle">
            {project ? `${project.name} · ` : ''}
            {pt('ies.data.resolution')} 15/30/60 min · {pt('ies.data.calendar_note')} ·{' '}
            {pt('ies.data.utc_offset')} {project ? utcLabel(project.fixed_utc_offset_minutes) : '±14h'}
          </p>
        </div>
        <div className="ies-flex">
          <Button variant="secondary" size="md" icon="download" loading={templateBusy} onClick={() => void downloadTemplate()}>
            {t('ies.data.template')}
          </Button>
          <Button variant="secondary" size="md" icon="plus" onClick={() => setSampleOpen(true)}>
            {pt('ies.data.sample_generate')}
          </Button>
          <Button variant="primary" size="md" icon="upload" onClick={() => setUploadOpen(true)}>
            {t('ies.data.upload')}
          </Button>
        </div>
      </div>

      {notice ? (
        <Alert variant={notice.kind} closable onClose={() => setNotice(null)}>
          {notice.text}
        </Alert>
      ) : null}

      <Card title={t('ies.data.dataset')} flush actions={<Badge label={String(datasets?.length ?? 0)} variant="neutral" />}>
        {datasets && datasets.length === 0 ? (
          <EmptyState
            icon="upload"
            title={t('ies.common.no_data')}
            action={
              <Button variant="primary" icon="upload" onClick={() => setUploadOpen(true)}>
                {t('ies.data.upload')}
              </Button>
            }
          />
        ) : (
          <Table>
            <THead>
              <TR>
                <TH>{t('ies.common.name')}</TH>
                <TH>{t('ies.data.version')}</TH>
                <TH>{pt('ies.data.quality')}</TH>
                <TH>{pt('ies.data.provenance_source')}</TH>
                <TH>{pt('ies.data.license')}</TH>
                <TH>{pt('ies.data.binding')}</TH>
                <TH>{t('ies.common.actions')}</TH>
              </TR>
            </THead>
            <TBody>
              {(datasets ?? []).map((ds) => {
                const v = latestVersion(ds)
                return (
                  <TR key={ds.id}>
                    <TD>
                      <div style={{ fontWeight: 600 }}>{ds.name}</div>
                      <span className="ies-mono">#{ds.id}</span>
                    </TD>
                    <TD>
                      {v ? (
                        <div className="ies-flex" style={{ flexWrap: 'wrap' }}>
                          <Badge label={`v${v.version_no}`} variant="primary" size="sm" />
                          <span className="ies-mono">
                            {v.resolution} · {utcLabel(v.fixed_utc_offset_minutes)}
                          </span>
                        </div>
                      ) : (
                        <Badge label={pt('ies.data.no_versions')} variant="neutral" size="sm" />
                      )}
                    </TD>
                    <TD>
                      <QualityBadge version={v} />
                    </TD>
                    <TD>
                      <span className="ies-mono">{provenanceSummary(v?.provenance ?? null)}</span>
                    </TD>
                    <TD>{v?.license ?? '—'}</TD>
                    <TD>
                      {ds.project_id !== null ? (
                        <Badge label={pt('ies.data.bound')} variant="success" icon="check" />
                      ) : (
                        <Badge label={pt('ies.data.unbound')} variant="neutral" icon="info" />
                      )}
                    </TD>
                    <TD>
                      <div className="ies-flex">
                        <Button variant="ghost" size="sm" onClick={() => setVersionsTarget(ds)}>
                          {t('ies.data.versions')}
                        </Button>
                        <Button variant="ghost" size="sm" onClick={() => setPreviewTarget(ds)}>
                          {t('ies.data.sample')}
                        </Button>
                      </div>
                    </TD>
                  </TR>
                )
              })}
            </TBody>
          </Table>
        )}
      </Card>

      <UploadDialog
        projectId={pid}
        defaultOffsetMinutes={project?.fixed_utc_offset_minutes ?? 480}
        open={uploadOpen}
        onClose={() => setUploadOpen(false)}
        onUploaded={() => setRefreshTick((x) => x + 1)}
      />
      <SampleDialog
        projectId={pid}
        defaultOffsetMinutes={project?.fixed_utc_offset_minutes ?? 480}
        open={sampleOpen}
        onClose={() => setSampleOpen(false)}
        onUploaded={() => setRefreshTick((x) => x + 1)}
      />
      <VersionsDialog
        dataset={versionsTarget}
        versions={versionsTarget ? versions[versionsTarget.id] ?? [] : []}
        open={versionsTarget !== null}
        onClose={() => setVersionsTarget(null)}
      />
      <PreviewDialog dataset={previewTarget} open={previewTarget !== null} onClose={() => setPreviewTarget(null)} />
    </div>
  )
}

export default DataPage
