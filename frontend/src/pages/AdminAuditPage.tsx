/**
 * 审计日志页(/admin/audit, 仅管理员, 只读)。
 *
 * 页面只组合 feature(hook + 面板); 权限由路由 RequireAdmin 与后端
 * CurrentAdmin 双重把关, 普通工程师无入口、直接访问被重定向。
 */

import { useI18n } from '../i18n'
import { AdminAuditPanel } from '../features/admin-audit/components/AdminAuditPanel'

export default function AdminAuditPage() {
  const { t } = useI18n()
  return (
    <div className="ies-content--wide">
      <div className="ies-page-header">
        <h1 className="ies-page-title">{t('ies.admin.audit')}</h1>
      </div>
      <AdminAuditPanel />
    </div>
  )
}
