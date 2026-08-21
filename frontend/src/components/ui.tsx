/**
 * 基础 UI 组件库(设计系统)。
 *
 * 无障碍要求(WCAG 2.2 AA):
 * - 所有交互控件具备语义化元素 + aria 属性(aria-label/aria-describedby/aria-busy 等)。
 * - 焦点可见(:focus-visible 由 styles.css 统一提供)。
 * - 状态徽章不只靠颜色:文字 + 图标 + 语义色(统一圆角矩形, 风格一致)。
 * - 对话框支持 Esc 关闭、焦点圈定与 aria-modal。
 * - 表单字段 label 与控件 id 关联(useId)。
 */

import { useEffect, useId, useRef } from 'react'
import type {
  ButtonHTMLAttributes,
  InputHTMLAttributes,
  ReactNode,
  RefObject,
  SelectHTMLAttributes,
  TableHTMLAttributes,
  TdHTMLAttributes,
  TextareaHTMLAttributes,
  ThHTMLAttributes,
} from 'react'
import { createPortal } from 'react-dom'

import { useI18n } from '../i18n'
import type { ProjectStatus, Severity, TaskStatus, TaskOutcome } from '../types'

// ---------------------------------------------------------------------------
// 图标(内联 SVG,stroke 随当前文字颜色)
// ---------------------------------------------------------------------------

export type IconName =
  | 'check'
  | 'cross'
  | 'warning'
  | 'info'
  | 'clock'
  | 'stop'
  | 'question'
  | 'spinner'
  | 'search'
  | 'download'
  | 'upload'
  | 'plus'
  | 'trash'

const ICON_PATHS: Record<IconName, ReactNode> = {
  check: <path d="M20 6 9 17l-5-5" />,
  cross: <path d="M18 6 6 18M6 6l12 12" />,
  warning: (
    <>
      <path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" />
      <path d="M12 9v4M12 17h.01" />
    </>
  ),
  info: (
    <>
      <circle cx="12" cy="12" r="10" />
      <path d="M12 16v-4M12 8h.01" />
    </>
  ),
  clock: (
    <>
      <circle cx="12" cy="12" r="10" />
      <path d="M12 6v6l4 2" />
    </>
  ),
  stop: <rect x="5" y="5" width="14" height="14" rx="1" />,
  question: (
    <>
      <circle cx="12" cy="12" r="10" />
      <path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3M12 17h.01" />
    </>
  ),
  spinner: <path d="M12 3a9 9 0 1 0 9 9" />,
  search: (
    <>
      <circle cx="11" cy="11" r="7" />
      <path d="m21 21-4.35-4.35" />
    </>
  ),
  download: <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3" />,
  upload: <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M17 8l-5-5-5 5M12 3v12" />,
  plus: <path d="M12 5v14M5 12h14" />,
  trash: (
    <>
      <path d="M3 6h18M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2m3 0v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6" />
      <path d="M10 11v6M14 11v6" />
    </>
  ),
}

/** 内联 SVG 图标。 */
export function Icon({ name, size = 16, label }: { name: IconName; size?: number; label?: string }) {
  return (
    <svg
      className="ies-icon"
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden={label ? undefined : 'true'}
      role={label ? 'img' : undefined}
    >
      {label ? <title>{label}</title> : null}
      {ICON_PATHS[name]}
    </svg>
  )
}

// ---------------------------------------------------------------------------
// 按钮
// ---------------------------------------------------------------------------

export type ButtonVariant = 'primary' | 'secondary' | 'danger' | 'ghost'
export type ButtonSize = 'sm' | 'md' | 'lg'

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant
  size?: ButtonSize
  /** 加载态(禁用 + 内置 spinner + aria-busy)。 */
  loading?: boolean
  /** 撑满父容器宽度。 */
  fullWidth?: boolean
  icon?: IconName
}

/** 按钮:支持变体/尺寸/加载态/图标,loading 时 aria-busy 并禁用。 */
export function Button({
  variant = 'primary',
  size = 'md',
  loading = false,
  fullWidth = false,
  icon,
  children,
  disabled,
  className,
  type = 'button',
  ...rest
}: ButtonProps) {
  const cls = [
    'ies-btn',
    `ies-btn--${variant}`,
    `ies-btn--${size}`,
    fullWidth ? 'ies-btn--full' : '',
    loading ? 'ies-btn--loading' : '',
    className ?? '',
  ]
    .filter(Boolean)
    .join(' ')
  return (
    <button
      type={type}
      className={cls}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      aria-disabled={disabled || loading || undefined}
      {...rest}
    >
      {loading ? (
        <Spinner size="sm" className="ies-btn__spinner" />
      ) : icon ? (
        <Icon name={icon} size={size === 'sm' ? 14 : 16} />
      ) : null}
      {children}
    </button>
  )
}

/** 图标按钮(必须提供 aria-label,语义同 Button 但只有图标)。 */
export function IconButton({
  'aria-label': ariaLabel,
  size = 'md',
  variant = 'ghost',
  className,
  ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & { size?: ButtonSize; variant?: ButtonVariant }) {
  return (
    <button
      type="button"
      className={`ies-btn ies-btn--icon ies-btn--${variant} ies-btn--${size} ${className ?? ''}`.trim()}
      aria-label={ariaLabel}
      {...rest}
    />
  )
}

// ---------------------------------------------------------------------------
// 加载指示
// ---------------------------------------------------------------------------

/** 加载指示器(role=status + aria-label)。 */
export function Spinner({ size = 'md', label, className }: { size?: 'sm' | 'md' | 'lg'; label?: string; className?: string }) {
  const { t } = useI18n()
  return (
    <span
      className={`ies-spinner ies-spinner--${size} ${className ?? ''}`.trim()}
      role="status"
      aria-label={label ?? t('ies.common.loading')}
    >
      <Icon name="spinner" size={size === 'sm' ? 14 : size === 'lg' ? 26 : 18} />
    </span>
  )
}

// ---------------------------------------------------------------------------
// 表单控件
// ---------------------------------------------------------------------------

/** 文本输入框。 */
export function Input({ id, className, invalid, ...rest }: InputHTMLAttributes<HTMLInputElement> & { invalid?: boolean }) {
  return <input id={id} className={`ies-input ${invalid ? 'ies-input--invalid' : ''} ${className ?? ''}`.trim()} aria-invalid={invalid || undefined} {...rest} />
}

/** 多行文本输入。 */
export function Textarea({ id, className, invalid, ...rest }: TextareaHTMLAttributes<HTMLTextAreaElement> & { invalid?: boolean }) {
  return <textarea id={id} className={`ies-input ies-textarea ${invalid ? 'ies-input--invalid' : ''} ${className ?? ''}`.trim()} aria-invalid={invalid || undefined} {...rest} />
}

/** 原生选择器(原生 select 天然具备键盘与读屏支持)。 */
export function Select({ id, className, invalid, children, ...rest }: SelectHTMLAttributes<HTMLSelectElement> & { invalid?: boolean }) {
  return (
    <select id={id} className={`ies-select ${invalid ? 'ies-input--invalid' : ''} ${className ?? ''}`.trim()} aria-invalid={invalid || undefined} {...rest}>
      {children}
    </select>
  )
}

/** 复选框(含自定义焦点样式)。 */
export function Checkbox({
  id,
  label,
  className,
  ...rest
}: InputHTMLAttributes<HTMLInputElement> & { label?: ReactNode }) {
  return (
    <label className={`ies-checkbox ${className ?? ''}`.trim()}>
      <input id={id} type="checkbox" className="ies-checkbox__input" {...rest} />
      <span className="ies-checkbox__box" aria-hidden="true">
        <Icon name="check" size={12} />
      </span>
      {label ? <span className="ies-checkbox__label">{label}</span> : null}
    </label>
  )
}

// ---------------------------------------------------------------------------
// 表单字段(label + 控件 + 错误/帮助)
// ---------------------------------------------------------------------------

export interface FormFieldProps {
  label: string
  htmlFor?: string
  /** 是否必填(label 带星号 + aria-required 提示)。 */
  required?: boolean
  /** 错误信息(渲染在下方,与控件通过 aria-describedby 关联)。 */
  error?: string | null
  /** 帮助文本。 */
  hint?: string
  children: ReactNode
  className?: string
}

/** 表单字段包装:label 与控件关联,错误/帮助通过 aria-describedby 关联。 */
export function FormField({ label, htmlFor, required, error, hint, children, className }: FormFieldProps) {
  const { t } = useI18n()
  const generatedId = useId()
  const fieldId = htmlFor ?? generatedId
  const messageId = error || hint ? `${fieldId}-hint` : undefined
  return (
    <div className={`ies-form-field ${error ? 'ies-form-field--error' : ''} ${className ?? ''}`.trim()}>
      <label className="ies-form-label" htmlFor={fieldId}>
        {label}
        {required ? (
          <>
            <span className="ies-form-required" aria-hidden="true">
              *
            </span>
            <span className="sr-only">({t('ies.common.required_field')})</span>
          </>
        ) : null}
      </label>
      <div className="ies-form-control">{children}</div>
      {messageId ? (
        <div
          id={messageId}
          className={`ies-form-message ${error ? 'ies-form-message--error' : ''}`.trim()}
          role={error ? 'alert' : undefined}
        >
          {error ?? hint}
        </div>
      ) : null}
    </div>
  )
}

// ---------------------------------------------------------------------------
// 卡片
// ---------------------------------------------------------------------------

export interface CardProps {
  title?: ReactNode
  /** 标题右侧操作区。 */
  actions?: ReactNode
  children: ReactNode
  className?: string
  /** 无内边距(表格等场景)。 */
  flush?: boolean
}

/** 卡片容器(可带标题栏与操作区)。 */
export function Card({ title, actions, children, className, flush = false }: CardProps) {
  return (
    <section className={`ies-card ${flush ? 'ies-card--flush' : ''} ${className ?? ''}`.trim()}>
      {title !== undefined || actions !== undefined ? (
        <header className="ies-card__header">
          <h2 className="ies-card__title">{title}</h2>
          {actions ? <div className="ies-card__actions">{actions}</div> : null}
        </header>
      ) : null}
      <div className="ies-card__body">{children}</div>
    </section>
  )
}

// ---------------------------------------------------------------------------
// 表格
// ---------------------------------------------------------------------------

/** 表格容器(横向滚动 + 吸顶表头)。 */
export function Table({ className, children, ...rest }: TableHTMLAttributes<HTMLTableElement>) {
  return (
    <div className="ies-table-wrap">
      <table className={`ies-table ${className ?? ''}`.trim()} {...rest}>
        {children}
      </table>
    </div>
  )
}

export function THead({ children }: { children: ReactNode }) {
  return <thead className="ies-table__head">{children}</thead>
}

export function TBody({ children }: { children: ReactNode }) {
  return <tbody className="ies-table__body">{children}</tbody>
}

export interface TRProps extends TdHTMLAttributes<HTMLTableRowElement> {
  /** 行可点击(带 hover 反馈)。 */
  clickable?: boolean
}

export function TR({ clickable, className, children, ...rest }: TRProps) {
  return (
    <tr
      className={`ies-table__row${clickable ? ' ies-table__row--clickable' : ''} ${className ?? ''}`.trim()}
      {...rest}
    >
      {children}
    </tr>
  )
}

export function TH({ children, ...rest }: ThHTMLAttributes<HTMLTableCellElement>) {
  return (
    <th scope="col" {...rest}>
      {children}
    </th>
  )
}

export interface TDProps extends TdHTMLAttributes<HTMLTableCellElement> {
  align?: 'left' | 'center' | 'right'
}

export function TD({ align = 'left', className, children, ...rest }: TDProps) {
  return (
    <td className={`ies-table__td ies-table__td--${align} ${className ?? ''}`.trim()} {...rest}>
      {children}
    </td>
  )
}

// ---------------------------------------------------------------------------
// 标签 / 状态徽章
// ---------------------------------------------------------------------------

export type BadgeVariant = 'neutral' | 'primary' | 'success' | 'danger' | 'warning' | 'info'

export interface BadgeProps {
  label: string
  variant?: BadgeVariant
  /** 图标(图标 + 文字双重编码)。 */
  icon?: IconName
  /** 运行中动画脉冲。 */
  pulse?: boolean
  size?: 'sm' | 'md'
}

/** 通用徽章:文字 + 图标 + 形状三重编码,状态不只靠颜色。 */
export function Badge({ label, variant = 'neutral', icon, pulse = false, size = 'md' }: BadgeProps) {
  // 统一圆角矩形: 状态由颜色+图标+文字区分(满足 WCAG 非颜色信息),
  // 不再用形状区分(避免视觉风格不统一)
  return (
    <span className={`ies-badge ies-badge--${variant} ies-badge--shape-square ies-badge--${size}`.trim()}>
      {icon ? (
        <span className="ies-badge__icon" aria-hidden="true">
          <Icon name={icon} size={size === 'sm' ? 11 : 13} />
        </span>
      ) : null}
      <span className="ies-badge__label">{label}</span>
      {pulse ? <span className="ies-badge__pulse" aria-hidden="true" /> : null}
    </span>
  )
}

/** 诊断严重度徽章(统一圆角矩形, 颜色+图标+文字区分, 满足 WCAG 非颜色信息)。 */
export function SeverityBadge({ severity }: { severity: Severity }) {
  const { t } = useI18n()
  const map: Record<Severity, { variant: BadgeVariant; icon: IconName }> = {
    blocking: { variant: 'danger', icon: 'stop' },
    error: { variant: 'danger', icon: 'cross' },
    warning: { variant: 'warning', icon: 'warning' },
    info: { variant: 'info', icon: 'info' },
  }
  const cfg = map[severity]
  return <Badge label={t(`ies.severity.${severity}`)} variant={cfg.variant} icon={cfg.icon} />
}

/** 任务状态徽章(含运行中脉冲; 统一圆角矩形, 颜色+图标+文字区分状态)。 */
export function TaskStatusBadge({ status }: { status: TaskStatus }) {
  const { t } = useI18n()
  const map: Record<TaskStatus, { variant: BadgeVariant; icon: IconName; pulse?: boolean }> = {
    queued: { variant: 'neutral', icon: 'clock' },
    running: { variant: 'primary', icon: 'spinner', pulse: true },
    completed: { variant: 'success', icon: 'check' },
    cancelling: { variant: 'warning', icon: 'clock' },
    cancelled: { variant: 'neutral', icon: 'stop' },
    timed_out: { variant: 'warning', icon: 'warning' },
    failed: { variant: 'danger', icon: 'cross' },
  }
  const cfg = map[status]
  return (
    <Badge
      label={t(`ies.task.status_${status}`)}
      variant={cfg.variant}
      icon={cfg.icon}
      pulse={cfg.pulse}
    />
  )
}

/** 任务业务结局徽章。 */
export function TaskOutcomeBadge({ outcome }: { outcome: TaskOutcome | null }) {
  const { t } = useI18n()
  if (!outcome) return null
  const variant: BadgeVariant =
    outcome === 'normal_completion' ? 'success' : outcome === 'partial_batch' ? 'warning' : 'neutral'
  return <Badge label={t(`ies.task.outcome_${outcome}`)} variant={variant} icon={outcome === 'normal_completion' ? 'check' : 'warning'} />
}

/** 项目状态徽章。 */
export function ProjectStatusBadge({ status }: { status: ProjectStatus }) {
  const { t } = useI18n()
  const map: Record<ProjectStatus, { variant: BadgeVariant; icon: IconName }> = {
    active: { variant: 'success', icon: 'check' },
    archived: { variant: 'neutral', icon: 'clock' },
    deleted: { variant: 'danger', icon: 'cross' },
  }
  const cfg = map[status]
  return <Badge label={t(`ies.project.status_${status}`)} variant={cfg.variant} icon={cfg.icon} />
}

// ---------------------------------------------------------------------------
// 内联提示(Alert)
// ---------------------------------------------------------------------------

export interface AlertProps {
  variant?: 'error' | 'warning' | 'info' | 'success'
  title?: ReactNode
  children?: ReactNode
  /** 可关闭。 */
  closable?: boolean
  onClose?: () => void
}

/** 内联提示条:错误用 role=alert,其余用 role=status。 */
export function Alert({ variant = 'info', title, children, closable, onClose }: AlertProps) {
  const { t } = useI18n()
  const icon: IconName = variant === 'error' ? 'cross' : variant === 'warning' ? 'warning' : variant === 'success' ? 'check' : 'info'
  return (
    <div
      className={`ies-alert ies-alert--${variant}`}
      role={variant === 'error' ? 'alert' : 'status'}
      aria-live={variant === 'error' ? 'assertive' : 'polite'}
    >
      <span className="ies-alert__icon" aria-hidden="true">
        <Icon name={icon} size={16} />
      </span>
      <div className="ies-alert__content">
        {title ? <div className="ies-alert__title">{title}</div> : null}
        {children ? <div className="ies-alert__body">{children}</div> : null}
      </div>
      {closable ? (
        <IconButton aria-label={t('ies.common.close')} onClick={onClose} className="ies-alert__close">
          <Icon name="cross" size={14} />
        </IconButton>
      ) : null}
    </div>
  )
}

// ---------------------------------------------------------------------------
// 空状态
// ---------------------------------------------------------------------------

export interface EmptyStateProps {
  icon?: IconName
  title: string
  description?: string
  action?: ReactNode
}

/** 空状态占位。 */
export function EmptyState({ icon = 'info', title, description, action }: EmptyStateProps) {
  return (
    <div className="ies-empty">
      <span className="ies-empty__icon" aria-hidden="true">
        <Icon name={icon} size={32} />
      </span>
      <h3 className="ies-empty__title">{title}</h3>
      {description ? <p className="ies-empty__desc">{description}</p> : null}
      {action ? <div className="ies-empty__action">{action}</div> : null}
    </div>
  )
}

// ---------------------------------------------------------------------------
// 对话框
// ---------------------------------------------------------------------------

const FOCUSABLE_SELECTOR = 'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'

export interface DialogProps {
  open: boolean
  onClose: () => void
  title: ReactNode
  children: ReactNode
  /** 底部操作区。 */
  footer?: ReactNode
  size?: 'sm' | 'md' | 'lg'
  /** RR-P2-12: 打开时优先聚焦的元素; 缺省聚焦 body 内第一个表单控件。 */
  initialFocusRef?: RefObject<HTMLElement | null>
}

/** RR-P2-12: body 内表单控件优先于关闭按钮/链接等操作元素。 */
const FORM_CONTROL_SELECTOR =
  'input:not([type="hidden"]), select, textarea, [role="radio"], [role="checkbox"], button:not([aria-label])'

/** 模态对话框:Esc 关闭、焦点圈定、背景滚动锁定、aria-modal。 */
export function Dialog({ open, onClose, title, children, footer, size = 'md', initialFocusRef }: DialogProps) {
  const { t } = useI18n()
  const titleId = useId()
  const panelRef = useRef<HTMLDivElement | null>(null)
  // RR-P2-12: 记录触发元素, 关闭时恢复焦点(键盘用户的导航连续性)
  const triggerRef = useRef<HTMLElement | null>(null)

  useEffect(() => {
    if (!open) return
    const panel = panelRef.current

    // 打开时焦点移入对话框内目标元素:
    // 1) 显式 initialFocusRef(调用方声明);
    // 2) body 内第一个表单控件(header 先于 body 渲染, 直接 querySelector
    //    会选中关闭按钮 —— 输入项目名称等场景应聚焦表单, 而非操作按钮);
    // 3) 兜底第一个可聚焦元素。
    triggerRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null
    const focusTarget =
      initialFocusRef?.current ??
      panel?.querySelector<HTMLElement>(FORM_CONTROL_SELECTOR) ??
      panel?.querySelector<HTMLElement>(FOCUSABLE_SELECTOR)
    if (focusTarget) focusTarget.focus()

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault()
        onClose()
        return
      }
      // 焦点圈定:Tab 在对话框内循环
      if (event.key !== 'Tab' || !panel) return
      const focusables = Array.from(panel.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR)).filter(
        (el) => !el.hasAttribute('disabled') && el.offsetParent !== null,
      )
      if (focusables.length === 0) return
      const first = focusables[0]
      const last = focusables[focusables.length - 1]
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault()
        first.focus()
      }
    }

    const prevOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.body.style.overflow = prevOverflow
      document.removeEventListener('keydown', onKeyDown)
      // RR-P2-12: 关闭时恢复触发元素焦点(Esc/取消/确认均走此路径)
      const trigger = triggerRef.current
      triggerRef.current = null
      if (trigger && trigger.isConnected) trigger.focus()
    }
  }, [open, onClose, initialFocusRef])

  if (!open) return null

  return createPortal(
    <div className="ies-dialog-overlay" onClick={onClose}>
      <div
        ref={panelRef}
        className={`ies-dialog ies-dialog--${size}`}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        onClick={(event) => event.stopPropagation()}
      >
        <header className="ies-dialog__header">
          <h2 id={titleId} className="ies-dialog__title">
            {title}
          </h2>
          <IconButton aria-label={t('ies.common.close')} onClick={onClose} autoFocus={false}>
            <Icon name="cross" size={16} />
          </IconButton>
        </header>
        <div className="ies-dialog__body">{children}</div>
        {footer ? <footer className="ies-dialog__footer">{footer}</footer> : null}
      </div>
    </div>,
    document.body,
  )
}
