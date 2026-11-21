/**
 * 用户操作续期。只监听真实输入，不把 HTTP 请求或后台定时器视为用户活跃。
 * 密集操作按 30 秒合并，尾次操作仍会发送；没有新操作就不再请求。
 */
const ACTIVITY_EVENTS = ['pointerdown', 'pointermove', 'keydown', 'input', 'wheel'] as const
const REFRESH_INTERVAL_MS = 30_000

export function startSessionActivity(
  refresh: (signal: AbortSignal) => Promise<void>,
  onError: (error: unknown | null) => void,
): () => void {
  const controller = new AbortController()
  let timer: ReturnType<typeof setTimeout> | undefined
  let lastRefresh = -Infinity
  let lastActivity = -Infinity
  let pending = false
  let refreshing = false

  function schedule(): void {
    if (controller.signal.aborted || refreshing || !pending || timer !== undefined) return
    // 标签页隐藏或休眠恢复后，不把早先积压的事件当作此刻仍在操作。
    if (document.visibilityState !== 'visible' || Date.now() - lastActivity >= 2 * REFRESH_INTERVAL_MS) {
      pending = false
      return
    }
    const delay = Math.max(0, lastRefresh + REFRESH_INTERVAL_MS - Date.now())
    if (delay > 0) {
      timer = setTimeout(() => {
        timer = undefined
        schedule()
      }, delay)
      return
    }
    pending = false
    refreshing = true
    lastRefresh = Date.now()
    void refresh(controller.signal)
      .then(() => { if (!controller.signal.aborted) onError(null) })
      .catch((error: unknown) => { if (!controller.signal.aborted) onError(error) })
      .finally(() => {
        refreshing = false
        schedule()
      })
  }

  function activity(event: Event): void {
    if (!event.isTrusted || document.visibilityState !== 'visible') return
    lastActivity = Date.now()
    pending = true
    schedule()
  }

  for (const type of ACTIVITY_EVENTS) {
    window.addEventListener(type, activity, { capture: true, passive: true })
  }
  return () => {
    controller.abort()
    if (timer !== undefined) clearTimeout(timer)
    for (const type of ACTIVITY_EVENTS) window.removeEventListener(type, activity, true)
  }
}
