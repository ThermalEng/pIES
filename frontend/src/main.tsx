/**
 * React 应用入口:StrictMode + ThemeProvider + I18nProvider + BrowserRouter。
 */

import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'

import App from './App'
import { I18nProvider } from './i18n'
import { initializeTheme, ThemeProvider } from './shared/theme'
import './styles.css'

const initialTheme = initializeTheme()
const rootElement = document.getElementById('root')
if (!rootElement) {
  throw new Error('缺少 #root 挂载节点(index.html)')
}

createRoot(rootElement).render(
  <StrictMode>
    <ThemeProvider initialTheme={initialTheme}>
      <I18nProvider>
        <BrowserRouter>
          <App />
        </BrowserRouter>
      </I18nProvider>
    </ThemeProvider>
  </StrictMode>,
)
