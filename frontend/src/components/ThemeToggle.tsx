import { useI18n } from '../i18n'
import { useTheme } from '../shared/theme'
import { Icon, IconButton } from './ui'

/** One-click toggle; pressed means dark mode is active in either language. */
export function ThemeToggle() {
  const { t } = useI18n()
  const { theme, toggleTheme } = useTheme()
  return (
    <IconButton
      size="sm"
      className="ies-theme-toggle"
      aria-label={t('ies.theme.dark_mode')}
      aria-pressed={theme === 'dark'}
      title={t(theme === 'dark' ? 'ies.theme.to_light' : 'ies.theme.to_dark')}
      onClick={toggleTheme}
    >
      <Icon name={theme === 'dark' ? 'sun' : 'moon'} size={16} />
    </IconButton>
  )
}
