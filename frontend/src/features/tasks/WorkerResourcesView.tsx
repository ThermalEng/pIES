import { useI18n } from '../../i18n'
import type { WorkerResourceUsage } from '../../types'

/** Read-only deployment facts. Missing observation never blocks a task. */
export function WorkerResourcesView({ resources }: { resources?: WorkerResourceUsage }) {
  const { t } = useI18n()
  const amount = (bytes: number | null) => bytes === null
    ? t('ies.task.resources_unknown')
    : `${(bytes / 1024 ** 3).toLocaleString(undefined, { maximumFractionDigits: 2 })} GiB`
  const limit = (value: WorkerResourceUsage['memory_limit']) => value.status === 'limited'
    ? amount(value.bytes)
    : t(value.status === 'unlimited' ? 'ies.task.resources_unlimited' : 'ies.task.resources_unknown')
  return (
    <section aria-label={t('ies.task.resources_title')} style={{ margin: 'var(--ies-space-3) 0' }}>
      <h3 style={{ fontSize: 'var(--ies-fs-sm)', fontWeight: 700 }}>{t('ies.task.resources_title')}</h3>
      <p style={{ fontSize: 'var(--ies-fs-sm)', color: 'var(--ies-color-text-secondary)' }}>
        {t('ies.task.resources_scope')}
      </p>
      {!resources ? <p>{t('ies.task.resources_not_observed')}</p> : (
        <dl className="ies-kv">
          <dt>{t('ies.task.resources_memory_limit')}</dt><dd>{limit(resources.memory_limit)}</dd>
          <dt>{t('ies.task.resources_swap_limit')}</dt><dd>{limit(resources.swap_limit)}</dd>
          <dt>{t('ies.task.resources_memory_current')}</dt><dd>{amount(resources.memory_current_bytes)}</dd>
          <dt>{t('ies.task.resources_memory_peak')}</dt><dd>{amount(resources.memory_peak_bytes)}</dd>
          <dt>{t('ies.task.resources_swap_current')}</dt><dd>{amount(resources.swap_current_bytes)}</dd>
        </dl>
      )}
    </section>
  )
}
