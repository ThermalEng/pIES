/** Structured authoring over the existing Finance Profile/Overrides JSON.
 * The JSON document is the only editable authority; no new saved draft type.
 * Monthly overrides write all twelve months at once, never inherit patches.
 */
import { Alert, Button, FormField, Input, Select } from '../../../components/ui'

type Json = Record<string, unknown>
type Price = Json & {
  kind?: string
  carrier?: string
  direction?: string
  value?: { value: string; unit: string }
  ref?: string
  series_meta?: { resolution: string; point_count: number; unit: string }
  months?: Record<string, { ref: string; series_meta: { resolution: string; point_count: number; unit: string } }>
}

type Props = {
  mode: 'profile' | 'overrides'
  text: string
  onChange: (text: string) => void
  disabled?: boolean
  profileId?: string | null
  profilePrices?: Record<string, Price>
}

const DAY_STEPS: Record<string, number> = { '15min': 96, '30min': 48, '1h': 24 }
const MONTHS = Array.from({ length: 12 }, (_, n) => String(n + 1))

function parseDoc(text: string): Json | null {
  if (!text.trim()) return null
  try {
    const raw: unknown = JSON.parse(text)
    return raw && typeof raw === 'object' && !Array.isArray(raw) ? raw as Json : null
  } catch {
    return null
  }
}

function priceCatalog(doc: Json | null): Record<string, Price> {
  const raw = doc?.energy_prices
  return raw && typeof raw === 'object' && !Array.isArray(raw)
    ? raw as Record<string, Price> : {}
}

function priceUnit(item: Price): string {
  if (item.kind === 'constant') return item.value?.unit ?? 'CNY/kWh'
  if (item.kind === 'monthly_daily') return item.months?.['1']?.series_meta?.unit ?? 'CNY/kWh'
  return item.series_meta?.unit ?? 'CNY/kWh'
}

function monthly(unit: string, resolution: string, prev?: Price): Price {
  const steps = DAY_STEPS[resolution]
  return {
    kind: 'monthly_daily',
    months: Object.fromEntries(MONTHS.map(month => [
      month, {
        ref: prev?.months?.[month]?.ref ?? '',
        series_meta: { resolution, point_count: steps, unit },
      },
    ])),
  }
}

export default function MonthlyDailyPriceEditor({
  mode, text, onChange, disabled = false, profileId, profilePrices = {},
}: Props) {
  const doc = parseDoc(text)
  const valid = !text.trim() || doc !== null
  const specified = priceCatalog(doc)
  const candidates = mode === 'profile' ? specified : profilePrices

  function replace(id: string, next: Price | null) {
    if (disabled || !valid) return
    const base = doc ?? (mode === 'overrides' && profileId ? {
      schema: 'ies.finance-overrides',
      schema_version: '5.0.0',
      profile_ref: { id: profileId },
    } : null)
    if (!base) return
    const catalog: Record<string, Price> = { ...priceCatalog(base) }
    if (next === null) delete catalog[id]
    else if (mode === 'profile') {
      const original = specified[id]
      catalog[id] = { carrier: original.carrier, direction: original.direction, ...next }
    } else {
      catalog[id] = next
    }
    onChange(JSON.stringify({ ...base, energy_prices: catalog }, null, 2))
  }

  function changeKind(id: string, kind: string, current: Price) {
    const baseline = mode === 'profile' ? current : profilePrices[id]
    const unit = priceUnit(baseline ?? current)
    if (kind === 'inherit' && mode === 'overrides') {
      replace(id, null)
    } else if (kind === 'monthly_daily') {
      const source = current.kind === kind ? current : undefined
      replace(id, monthly(unit, source?.months?.['1']?.series_meta?.resolution ?? '1h', source))
    } else if (kind === 'constant') {
      replace(id, { kind, value: current.value ?? baseline?.value ?? { value: '0', unit } })
    } else if (kind === 'time_series') {
      replace(id, {
        kind, ref: current.ref ?? '',
        series_meta: current.series_meta ?? { resolution: '1h', point_count: 1, unit },
      })
    }
  }

  return (
    <section aria-label={mode === 'profile' ? '画像月份日价格配置' : '项目月份日价格配置'}
      style={{ marginTop: 'var(--ies-space-3)', padding: 'var(--ies-space-3)', border: '1px solid var(--ies-color-border)', borderRadius: 8 }}>
      <h3>能源价格来源：常数／全期序列／月份日曲线</h3>
      <p style={{ fontSize: 'var(--ies-fs-sm)' }}>月份日曲线使用已上传的价格对象 ID；完整配置 1—12 月，每日重用且闰日使用二月曲线。覆盖时整项替换，不按月继承。</p>
      {!valid && <Alert variant="error">请先修正 JSON 格式，再使用价格编辑器。</Alert>}
      {!Object.keys(candidates).length && <p>请先填写画像的能源价格条目，或选择项目画像。</p>}
      {Object.entries(candidates).sort(([a], [b]) => a.localeCompare(b)).map(([id, baseline]) => {
        const isOverridden = mode === 'overrides' && Object.hasOwn(specified, id)
        const current = isOverridden ? specified[id] : baseline
        const kindValue = mode === 'overrides' && !isOverridden ? 'inherit' : current.kind
        const own = mode === 'profile' || isOverridden
        const monthlyEntry = own && current.kind === 'monthly_daily' ? current : null
        const source = monthlyEntry?.months
        const resolution = source?.['1']?.series_meta?.resolution ?? '1h'
        const unit = source?.['1']?.series_meta?.unit ?? priceUnit(baseline)
        const prefix = `${mode}-price-${id}`
        return (
          <div key={id} style={{ marginTop: 16, paddingTop: 12, borderTop: '1px solid var(--ies-color-border)' }}>
            <h4>{id} {baseline.direction ? `(${baseline.direction})` : ''}</h4>
            <FormField label={`${id} 价格来源`} htmlFor={`${prefix}-kind`}>
              <Select id={`${prefix}-kind`} disabled={disabled || !valid}
                value={kindValue} onChange={e => changeKind(id, e.target.value, current)}>
                {mode === 'overrides' && <option value="inherit">沿用画像</option>}
                <option value="constant">常数价格</option>
                <option value="time_series">全期价格序列</option>
                <option value="monthly_daily">按月份选择日曲线</option>
              </Select>
            </FormField>
            {monthlyEntry && source && (
              <>
                <div className="ies-flex" style={{ gap: 'var(--ies-space-2)', flexWrap: 'wrap', marginTop: 8 }}>
                  <FormField label="日曲线分辨率" htmlFor={`${prefix}-resolution`}>
                    <Select id={`${prefix}-resolution`} value={resolution} disabled={disabled || !valid}
                      onChange={e => replace(id, monthly(unit, e.target.value, monthlyEntry))}>
                      <option value="1h">1h（24 点）</option>
                      <option value="30min">30min（48 点）</option>
                      <option value="15min">15min（96 点）</option>
                    </Select>
                  </FormField>
                  <FormField label="价格单位" htmlFor={`${prefix}-unit`}>
                    <Input id={`${prefix}-unit`} value={unit} disabled={disabled || !valid}
                      onChange={e => replace(id, monthly(e.target.value, resolution, monthlyEntry))} />
                  </FormField>
                </div>
                <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(150px, 1fr))', gap: 8, marginTop: 12 }}>
                  {MONTHS.map(month => (
                    <FormField key={month} label={`${month} 月日曲线对象 ID`} htmlFor={`${prefix}-month-${month}`}>
                      <Input id={`${prefix}-month-${month}`}
                        value={source[month]?.ref ?? ''} disabled={disabled || !valid}
                        onChange={e => replace(id, monthlyEntry && {
                          kind: 'monthly_daily',
                          months: {
                            ...source,
                            [month]: {
                              ref: e.target.value,
                              series_meta: { resolution, point_count: DAY_STEPS[resolution], unit },
                            },
                          },
                        })} />
                    </FormField>
                  ))}
                </div>
                <Button variant="secondary" size="sm" disabled={disabled || !valid}
                  onClick={() => replace(id, {
                    kind: 'monthly_daily',
                    months: Object.fromEntries(MONTHS.map(m => [
                      m, { ref: source['1']?.ref ?? '', series_meta: { resolution, point_count: DAY_STEPS[resolution], unit } },
                    ])),
                  })}>全部月份引用一月曲线</Button>
              </>
            )}
          </div>
        )
      })}
    </section>
  )
}
