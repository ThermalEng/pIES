/** Browser-local appearance preference, independent of users and projects. */
import { createContext, useContext, useState } from 'react'
import type { ReactNode } from 'react'

type Theme = 'light' | 'dark'
const STORAGE_KEY = 'iesplan.theme'

/** Apply before React mounts so the first application render has the saved theme. */
export function initializeTheme(): Theme {
  let theme: Theme = 'light'
  try {
    if (window.localStorage.getItem(STORAGE_KEY) === 'dark') theme = 'dark'
  } catch {
    // Browsers may disallow local preferences; the switch still works for this visit.
  }
  document.documentElement.dataset.theme = theme
  return theme
}

const ThemeContext = createContext<{ theme: Theme; toggleTheme: () => void } | null>(null)

export function ThemeProvider({ initialTheme, children }: { initialTheme: Theme; children: ReactNode }) {
  const [theme, setTheme] = useState(initialTheme)
  const toggleTheme = () => {
    const next = theme === 'light' ? 'dark' : 'light'
    document.documentElement.dataset.theme = next
    setTheme(next)
    try {
      window.localStorage.setItem(STORAGE_KEY, next)
    } catch {
      // Same browser restriction as reading the preference above.
    }
  }
  return <ThemeContext.Provider value={{ theme, toggleTheme }}>{children}</ThemeContext.Provider>
}

export function useTheme() {
  const context = useContext(ThemeContext)
  if (!context) throw new Error('useTheme requires ThemeProvider')
  return context
}
