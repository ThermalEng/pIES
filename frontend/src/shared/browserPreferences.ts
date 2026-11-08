/** Local display preferences, independent of account/project server state. */
export function readBrowserPreference(key: string): string | null {
  try {
    return window.localStorage.getItem(key)
  } catch {
    // Browser storage restrictions must not prevent the interface from opening.
    return null
  }
}

export function writeBrowserPreference(key: string, value: string): void {
  try {
    window.localStorage.setItem(key, value)
  } catch {
    // The current visit still uses the selected preference.
  }
}
