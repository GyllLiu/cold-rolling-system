export function PageHeader({
  eyebrow,
  title,
  description,
  actions,
}: {
  eyebrow: string;
  title: string;
  description: string;
  actions?: React.ReactNode;
}) {
  return (
    <header className="page-header">
      <div>
        <span className="kicker">{eyebrow}</span>
        <h1>{title}</h1>
        <p>{description}</p>
      </div>
      {actions ? <div className="page-actions">{actions}</div> : null}
    </header>
  );
}

export function StatusPill({
  children,
  tone = "neutral",
}: {
  children: React.ReactNode;
  tone?: "success" | "warning" | "danger" | "neutral" | "info";
}) {
  return <span className={`status-pill status-${tone}`}>{children}</span>;
}

export function MetricCard({
  label,
  value,
  note,
  hint,
}: {
  label: string;
  value: React.ReactNode;
  note: string;
  hint?: string;
}) {
  return (
    <article className="metric-card">
      <div className="metric-card-top">
        <span className="metric-label">{label}</span>
        {hint ? <span className="metric-hint">{hint}</span> : <span className="metric-spark" aria-hidden />}
      </div>
      <strong className="metric-value">{value}</strong>
      <small className="metric-note">{note}</small>
    </article>
  );
}

export function ConfidenceGauge({
  value,
  size = 112,
  label = "置信度",
}: {
  value: number | null;
  size?: number;
  label?: string;
}) {
  const score = value == null ? null : Math.max(0, Math.min(100, Math.round(value)));
  const radius = 40;
  const circumference = 2 * Math.PI * radius;
  const progress = score == null ? 0 : score / 100;
  const offset = circumference * (1 - progress);
  const tone = score == null ? "empty" : score >= 70 ? "high" : score >= 40 ? "mid" : "low";

  return (
    <div className={`confidence-gauge tone-${tone}`} style={{ width: size, height: size }} aria-label={`${label} ${score ?? "—"}`}>
      <svg viewBox="0 0 100 100" className="confidence-gauge-svg" aria-hidden>
        <circle className="gauge-track" cx="50" cy="50" r={radius} />
        <circle
          className="gauge-progress"
          cx="50"
          cy="50"
          r={radius}
          strokeDasharray={circumference}
          strokeDashoffset={offset}
          transform="rotate(-90 50 50)"
        />
        <circle className="gauge-ticks" cx="50" cy="50" r="46" />
      </svg>
      <div className="confidence-gauge-center">
        <span>{score == null ? "—" : score}</span>
        <small>{score == null ? label : "/ 100"}</small>
      </div>
    </div>
  );
}

export function Skeleton({ className = "" }: { className?: string }) {
  return <div className={`skeleton ${className}`} aria-hidden />;
}

export function MetricSkeleton() {
  return (
    <article className="metric-card metric-skeleton" aria-hidden>
      <div className="metric-card-top">
        <Skeleton className="skeleton-line short" />
        <Skeleton className="skeleton-dot" />
      </div>
      <Skeleton className="skeleton-value" />
      <Skeleton className="skeleton-line" />
    </article>
  );
}

export function PanelSkeleton({ rows = 4 }: { rows?: number }) {
  return (
    <div className="panel-skeleton" aria-busy="true" aria-label="加载中">
      <div className="panel-skeleton-head">
        <Skeleton className="skeleton-line short" />
        <Skeleton className="skeleton-line medium" />
      </div>
      {Array.from({ length: rows }, (_, index) => (
        <Skeleton key={index} className="skeleton-row" />
      ))}
    </div>
  );
}

export function EmptyState({ title, description }: { title: string; description: string }) {
  return (
    <div className="empty-state">
      <b>{title}</b>
      <span>{description}</span>
    </div>
  );
}

export function Field({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <label className="field">
      <span>
        {label}
        {hint ? <small>{hint}</small> : null}
      </span>
      {children}
    </label>
  );
}

export function LoadingBlock({ label = "正在读取本地数据…" }: { label?: string }) {
  return (
    <div className="loading-block">
      <i />
      <span>{label}</span>
    </div>
  );
}
