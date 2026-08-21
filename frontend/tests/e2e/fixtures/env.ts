// QA-E2E-01 环境常量与共享前缀(13.2 独立用户/项目/幂等键)。

/** 会话建立基础地址。 */
export const APP_URL = process.env.E2E_APP_URL ?? 'http://web:80'

/** 每场景独立幂等前缀: 由运行时注入, 防止并行重跑冲突。 */
export function uniq(prefix: string): string {
  const run = process.env.E2E_RUN_ID ?? ''
  return run ? `${prefix}-${run}` : prefix
}

/** 全局唯一用户名(随机后缀; 后端约束 ^[a-z0-9_]{3,32}$)。 */
export function uniqueName(prefix: string): string {
  const rand = Math.random().toString(36).slice(2, 8)
  // 前缀中的连字符/空格替换为下划线, 并整体转小写
  const safe = prefix.toLowerCase().replace(/[^a-z0-9_]/g, '_').slice(0, 24)
  return `${safe}_${rand}`
}

/** 强密码(后端强度校验要求)。 */
export function strongPassword(tag: string): string {
  return `${tag}-Passw0rd!${Math.floor(Math.random() * 1e6)}`
}
