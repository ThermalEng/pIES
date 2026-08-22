interface BrandMarkProps {
  className?: string
  title?: string
  variant?: 'color' | 'mono' | 'reverse'
}

/** pIES A2 标志。颜色只由 variant 决定，不承载业务状态。 */
export function BrandMark({ className, title, variant = 'color' }: BrandMarkProps) {
  const outer = variant === 'reverse' ? '#ffffff' : '#0e5cad'
  const inner = variant === 'color' ? '#0891b2' : outer

  return (
    <svg
      className={className}
      viewBox="8 7 50 52"
      role={title ? 'img' : undefined}
      aria-hidden={title ? undefined : true}
      aria-label={title}
      focusable="false"
    >
      <path
        d="M15 52V14"
        fill="none"
        stroke={outer}
        strokeWidth="7.5"
        strokeLinecap="round"
      />
      <path
        d="M24 14h12c10 0 16 5 16 14S46 42 36 42H24"
        fill="none"
        stroke={outer}
        strokeWidth="7.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <path d="M27 22h7l7 6-7 6h-7l6-6Z" fill={inner} />
    </svg>
  )
}
