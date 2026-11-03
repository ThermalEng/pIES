import { useEffect, useState } from 'react'
import { parseDocument, stringify } from 'yaml'
import { getProject, putDraftCommands } from '../features/projects'
import { Alert, Button, Card, FormField, Textarea } from '../components/ui'
import { errorMessage, useI18n } from '../i18n'

/** Complete document view, not an allocation-policy or device-specific form. */
export default function SystemEquationsEditor({ projectId }: { projectId: number }) {
  const { t } = useI18n()
  const [text, setText] = useState('')
  const [revision, setRevision] = useState<number | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)

  async function load() {
    setBusy(true)
    setError(null)
    try {
      const project = await getProject(projectId)
      setText(stringify({ system_equations: project.draft!.system_equations ?? {},
        outputs: project.draft!.outputs ?? { series: [], metrics: [], system_series: [] } }, { lineWidth: 0 }))
      setRevision(project.draft!.revision)
      setSaved(false)
    } catch (err) {
      setError(errorMessage(err))
    } finally { setBusy(false) }
  }

  useEffect(() => { void load() }, [projectId])

  async function save() {
    if (revision === null) return
    setBusy(true)
    setError(null)
    setSaved(false)
    try {
      const document = parseDocument(text, { uniqueKeys: true })
      if (document.errors.length) throw document.errors[0]
      const payload = document.toJS() as Record<string, unknown>
      const result = await putDraftCommands(projectId, [{ type: 'assembly.set_system_equations', payload }], revision)
      setRevision(result.revision)
      setSaved(true)
    } catch (err) { setError(errorMessage(err)) }
    finally { setBusy(false) }
  }

  return <Card title={t('ies.system.title')}>
    <p>{t('ies.system.hint')}</p>
    {error && <Alert variant="error">{error}</Alert>}
    {saved && <Alert variant="success">{t('ies.system.saved')}</Alert>}
    <FormField label={t('ies.system.document')} htmlFor="system-equations-yaml">
      <Textarea id="system-equations-yaml" value={text} rows={18} spellCheck={false}
        disabled={busy || revision === null} onChange={(event) => { setText(event.target.value); setSaved(false) }} />
    </FormField>
    <Button disabled={busy || revision === null} onClick={() => void save()}>{t('ies.system.save')}</Button>
    <Button variant="secondary" disabled={busy} onClick={() => void load()}>{t('ies.system.reload')}</Button>
  </Card>
}
