/* Reusable chart components — pure SVG, no external dependencies */

/* ── DonutChart ── */

interface DonutChartProps {
  value: number;
  max?: number;
  size?: number;
  strokeWidth?: number;
  color?: string;
  label?: string;
  sublabel?: string;
}

export function DonutChart({ value, max = 100, size = 140, strokeWidth = 10, color = '#4ade80', label, sublabel }: DonutChartProps) {
  const radius = (size - strokeWidth) / 2;
  const circumference = 2 * Math.PI * radius;
  const progress = Math.min(value / max, 1);
  const dashoffset = circumference * (1 - progress);

  return (
    <div style={{ position: 'relative', width: size, height: size }}>
      <svg width={size} height={size} style={{ transform: 'rotate(-90deg)' }}>
        <circle cx={size / 2} cy={size / 2} r={radius} fill="none" stroke="#1a1a2e" strokeWidth={strokeWidth} />
        <circle
          cx={size / 2} cy={size / 2} r={radius} fill="none" stroke={color} strokeWidth={strokeWidth}
          strokeDasharray={circumference} strokeDashoffset={dashoffset}
          strokeLinecap="round" style={{ transition: 'stroke-dashoffset 0.8s ease' }}
        />
      </svg>
      <div style={{
        position: 'absolute', top: 0, left: 0, right: 0, bottom: 0,
        display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center',
      }}>
        <span style={{ fontSize: size * 0.22, fontWeight: 700, color: '#fff' }}>{value}</span>
        {label && <span style={{ fontSize: size * 0.09, color: '#888', marginTop: 2 }}>{label}</span>}
        {sublabel && <span style={{ fontSize: size * 0.075, color }}>{sublabel}</span>}
      </div>
    </div>
  );
}

/* ── HorizontalBarChart ── */

interface BarSegment { value: number; color: string; label?: string }
interface BarData { label: string; segments: BarSegment[] }

interface HorizontalBarChartProps {
  data: BarData[];
  height?: number;
  showValues?: boolean;
  maxValue?: number;
}

export function HorizontalBarChart({ data, height = 24, showValues = true, maxValue }: HorizontalBarChartProps) {
  const max = maxValue || Math.max(...data.map(d => d.segments.reduce((s, seg) => s + seg.value, 0)), 1);

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '0.5rem' }}>
      {data.map((bar, i) => {
        const total = bar.segments.reduce((s, seg) => s + seg.value, 0);
        return (
          <div key={i} style={{ display: 'flex', alignItems: 'center', gap: '0.75rem' }}>
            <span style={{ width: 130, fontSize: '0.78rem', color: '#999', textAlign: 'right', flexShrink: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{bar.label}</span>
            <div style={{ flex: 1, position: 'relative', height, background: '#1a1a1a', borderRadius: 4, overflow: 'hidden', display: 'flex' }}>
              {bar.segments.map((seg, j) => (
                <div key={j} style={{
                  width: `${(seg.value / max) * 100}%`,
                  height: '100%', background: seg.color,
                  transition: 'width 0.6s ease',
                }} title={seg.label ? `${seg.label}: ${seg.value}` : String(seg.value)} />
              ))}
            </div>
            {showValues && <span style={{ width: 40, fontSize: '0.75rem', color: '#888', textAlign: 'right', flexShrink: 0 }}>{total}</span>}
          </div>
        );
      })}
    </div>
  );
}

/* ── KPICard ── */

interface KPICardProps {
  icon: string;
  value: string;
  label: string;
  change?: number;
  accentColor?: string;
}

export function KPICard({ icon, value, label, change, accentColor = '#f97316' }: KPICardProps) {
  return (
    <div style={{
      background: '#111', border: '1px solid #1a1a1a', borderRadius: '12px', padding: '1.1rem 1.25rem',
      borderLeft: `3px solid ${accentColor}`, position: 'relative', overflow: 'hidden',
    }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start' }}>
        <div>
          <div style={{ fontSize: '0.72rem', color: '#666', marginBottom: '0.4rem', textTransform: 'uppercase', letterSpacing: '0.04em' }}>{label}</div>
          <div style={{ fontSize: '1.65rem', fontWeight: 700, color: '#fff', letterSpacing: '-0.02em' }}>{value}</div>
        </div>
        <div style={{
          width: 36, height: 36, borderRadius: '10px', background: `${accentColor}15`,
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          fontSize: '1rem', color: accentColor,
        }}>{icon}</div>
      </div>
      {change !== undefined && change !== 0 && (
        <div style={{
          marginTop: '0.5rem', fontSize: '0.72rem', fontWeight: 500,
          color: change > 0 ? '#4ade80' : '#ef4444',
          display: 'flex', alignItems: 'center', gap: '0.2rem',
        }}>
          {change > 0 ? '\u2191' : '\u2193'} {Math.abs(change)}%
        </div>
      )}
    </div>
  );
}

/* ── StatusBadge ── */

interface StatusBadgeProps {
  status: 'running' | 'stopped' | 'error' | 'warning';
  label?: string;
}

export function StatusBadge({ status, label }: StatusBadgeProps) {
  const colors: Record<string, { bg: string; fg: string; border: string }> = {
    running: { bg: '#052e16', fg: '#4ade80', border: '#166534' },
    stopped: { bg: '#1a1a1a', fg: '#666', border: '#333' },
    error: { bg: '#450a0a', fg: '#fca5a5', border: '#991b1b' },
    warning: { bg: '#78350f', fg: '#fcd34d', border: '#92400e' },
  };
  const c = colors[status] || colors.stopped;
  return (
    <span style={{
      padding: '0.15rem 0.6rem', borderRadius: '4px', fontSize: '0.7rem', fontWeight: 600,
      background: c.bg, color: c.fg, border: `1px solid ${c.border}`,
      textTransform: 'capitalize',
    }}>{label || status}</span>
  );
}

/* ── SeverityBadge ── */

interface SeverityBadgeProps {
  severity: 'CRITICAL' | 'WARNING' | 'INFO';
}

export function SeverityBadge({ severity }: SeverityBadgeProps) {
  const colors: Record<string, { bg: string; fg: string; border: string }> = {
    CRITICAL: { bg: '#7f1d1d', fg: '#fca5a5', border: '#991b1b' },
    WARNING: { bg: '#78350f', fg: '#fcd34d', border: '#92400e' },
    INFO: { bg: '#1e3a5f', fg: '#93c5fd', border: '#1e40af' },
  };
  const c = colors[severity] || colors.INFO;
  return (
    <span style={{
      padding: '0.15rem 0.6rem', borderRadius: '4px', fontSize: '0.65rem', fontWeight: 700,
      background: c.bg, color: c.fg, border: `1px solid ${c.border}`,
      letterSpacing: '0.05em',
    }}>{severity}</span>
  );
}

/* ── EventTypeBadge ── */

interface EventTypeBadgeProps {
  type: string;
}

export function EventTypeBadge({ type }: EventTypeBadgeProps) {
  const colors: Record<string, string> = {
    DECISION: '#2563eb', WRITE: '#f97316', SEARCH: '#8b5cf6',
    ERROR: '#ef4444', CRASH: '#ef4444', RECOVERY: '#4ade80',
  };
  const color = colors[type] || '#666';
  return (
    <span style={{
      padding: '0.15rem 0.6rem', borderRadius: '4px', fontSize: '0.65rem', fontWeight: 700,
      background: `${color}22`, color, border: `1px solid ${color}44`,
      letterSpacing: '0.05em',
    }}>{type}</span>
  );
}

/* ── SectionHeader ── */

export function SectionHeader({ title, action }: { title: string; action?: React.ReactNode }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '0.75rem' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
        <div style={{ width: 3, height: 18, background: '#f97316', borderRadius: 2 }} />
        <h2 style={{ fontSize: '0.82rem', fontWeight: 600, color: '#fff', textTransform: 'uppercase', letterSpacing: '0.05em' }}>{title}</h2>
      </div>
      {action}
    </div>
  );
}

/* ── LineChart (simple SVG polyline) ── */

interface LineChartProps {
  data: { label: string; value: number }[];
  height?: number;
  color?: string;
  showDots?: boolean;
}

export function LineChart({ data, height = 160, color = '#f97316', showDots = true }: LineChartProps) {
  if (data.length === 0) return <div style={{ height, display: 'flex', alignItems: 'center', justifyContent: 'center', color: '#555' }}>No data</div>;

  const padding = { top: 10, right: 10, bottom: 30, left: 50 };
  const width = 600;
  const chartW = width - padding.left - padding.right;
  const chartH = height - padding.top - padding.bottom;

  const maxVal = Math.max(...data.map(d => d.value), 0.001);
  const minVal = 0;

  const points = data.map((d, i) => ({
    x: padding.left + (i / Math.max(data.length - 1, 1)) * chartW,
    y: padding.top + chartH - ((d.value - minVal) / (maxVal - minVal)) * chartH,
  }));

  const polyline = points.map(p => `${p.x},${p.y}`).join(' ');

  return (
    <svg width="100%" viewBox={`0 0 ${width} ${height}`} style={{ display: 'block' }}>
      {/* Grid lines */}
      {[0, 0.25, 0.5, 0.75, 1].map(pct => {
        const y = padding.top + chartH * (1 - pct);
        const val = (minVal + (maxVal - minVal) * pct).toFixed(maxVal < 1 ? 4 : 0);
        return (
          <g key={pct}>
            <line x1={padding.left} y1={y} x2={width - padding.right} y2={y} stroke="#1a1a1a" strokeWidth={1} />
            <text x={padding.left - 6} y={y + 4} textAnchor="end" fill="#555" fontSize={9}>{val}</text>
          </g>
        );
      })}
      {/* Area fill */}
      <polygon
        points={`${points[0].x},${padding.top + chartH} ${polyline} ${points[points.length - 1].x},${padding.top + chartH}`}
        fill={`${color}15`}
      />
      {/* Line */}
      <polyline points={polyline} fill="none" stroke={color} strokeWidth={2} strokeLinejoin="round" />
      {/* Dots */}
      {showDots && points.map((p, i) => (
        <circle key={i} cx={p.x} cy={p.y} r={3} fill={color} stroke="#111" strokeWidth={1.5} />
      ))}
      {/* X labels */}
      {data.map((d, i) => {
        if (data.length > 10 && i % Math.ceil(data.length / 7) !== 0) return null;
        return (
          <text key={i} x={points[i].x} y={height - 5} textAnchor="middle" fill="#555" fontSize={9}>
            {d.label}
          </text>
        );
      })}
    </svg>
  );
}
