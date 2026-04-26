import { useEffect, useState } from 'react';
import { api } from '../api/client';
import { SeverityBadge } from '../components/Charts';

interface Anomaly {
  id: string; severity: 'CRITICAL' | 'WARNING' | 'INFO';
  type: string; agent: string; description: string; timestamp: string;
}

const FILTERS = ['All', 'Loops', 'Latency', 'Errors', 'Crashes'] as const;

function timeAgo(ts: string): string {
  if (!ts) return '';
  const diff = Date.now() - new Date(ts).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins} min ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}

export default function AnomaliesPage() {
  const [anomalies, setAnomalies] = useState<Anomaly[]>([]);
  const [summary, setSummary] = useState<Record<string, number>>({});
  const [filter, setFilter] = useState('All');
  const [loading, setLoading] = useState(false);

  const load = () => {
    setLoading(true);
    api<{ anomalies: Anomaly[]; summary: Record<string, number> }>('/api/anomalies/list')
      .then(r => { setAnomalies(r.anomalies); setSummary(r.summary); })
      .catch(() => {})
      .finally(() => setLoading(false));
  };

  useEffect(() => { load(); }, []);

  const filtered = anomalies.filter(a => {
    if (filter === 'All') return true;
    if (filter === 'Loops') return a.type.includes('LOOP');
    if (filter === 'Latency') return a.type === 'LATENCY_SPIKE';
    if (filter === 'Errors') return a.type === 'CRASH_LOOP' || a.severity === 'CRITICAL';
    if (filter === 'Crashes') return a.type === 'CRASH_LOOP';
    return true;
  });

  const card: React.CSSProperties = {
    background: '#111', border: '1px solid #1a1a1a', borderRadius: '12px', padding: '1.25rem',
  };

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '1.25rem' }}>
        <div>
          <h1 style={{ fontSize: '1.5rem', fontWeight: 600, marginBottom: '0.2rem' }}>Anomaly Detection</h1>
          <p style={{ fontSize: '0.82rem', color: '#666' }}>Real-time anomaly detection across all agents</p>
        </div>
        <button onClick={load} disabled={loading} style={{
          padding: '0.5rem 1.25rem', borderRadius: 8, border: 'none', cursor: loading ? 'wait' : 'pointer',
          background: '#f97316', color: '#fff', fontSize: '0.85rem', fontWeight: 600,
        }}>{loading ? 'Checking...' : 'Check Now'}</button>
      </div>

      {/* Filter pills */}
      <div style={{ display: 'flex', gap: '0.3rem', marginBottom: '1rem' }}>
        {FILTERS.map(f => (
          <button key={f} onClick={() => setFilter(f)} style={{
            padding: '0.4rem 0.85rem', borderRadius: 20, border: `1px solid ${filter === f ? '#f97316' : '#1a1a1a'}`,
            cursor: 'pointer', background: filter === f ? '#f9731622' : '#111',
            color: filter === f ? '#f97316' : '#777', fontSize: '0.78rem', fontWeight: filter === f ? 600 : 400,
          }}>{f}</button>
        ))}
      </div>

      {/* Summary */}
      <div style={{ ...card, marginBottom: '1rem', display: 'flex', gap: '1.5rem', alignItems: 'center' }}>
        {(['CRITICAL', 'WARNING', 'INFO'] as const).map(s => (
          <div key={s} style={{ display: 'flex', alignItems: 'center', gap: '0.4rem' }}>
            <span style={{
              fontSize: '1.1rem', fontWeight: 700,
              color: s === 'CRITICAL' ? '#ef4444' : s === 'WARNING' ? '#f59e0b' : '#3b82f6',
            }}>{summary[s] || 0}</span>
            <SeverityBadge severity={s} />
          </div>
        ))}
        <span style={{ marginLeft: 'auto', fontSize: '0.72rem', color: '#555' }}>
          {anomalies.length} total anomalies
        </span>
      </div>

      {/* Anomaly list */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: '0.5rem' }}>
        {filtered.map(a => (
          <div key={a.id} style={{
            ...card, padding: '0.85rem 1rem',
            borderLeft: `3px solid ${a.severity === 'CRITICAL' ? '#ef4444' : a.severity === 'WARNING' ? '#f59e0b' : '#3b82f6'}`,
            background: a.severity === 'CRITICAL' ? '#110505' : a.severity === 'WARNING' ? '#111005' : '#111',
          }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', marginBottom: '0.35rem' }}>
              <span style={{
                width: 7, height: 7, borderRadius: '50%',
                background: a.severity === 'CRITICAL' ? '#ef4444' : a.severity === 'WARNING' ? '#f59e0b' : '#3b82f6',
              }} />
              <span style={{ fontSize: '0.85rem', fontWeight: 600, color: '#e0e0e0' }}>{a.agent.replace(/_/g, '-')}</span>
              <SeverityBadge severity={a.severity} />
              <span style={{
                padding: '0.1rem 0.5rem', borderRadius: 3, fontSize: '0.65rem',
                background: '#1a1a1a', color: '#888', fontFamily: 'monospace',
              }}>{a.type.replace(/_/g, ' ')}</span>
              <span style={{ marginLeft: 'auto', fontSize: '0.7rem', color: '#444' }}>{timeAgo(a.timestamp)}</span>
            </div>
            <div style={{ fontSize: '0.8rem', color: '#999', paddingLeft: '1.15rem' }}>{a.description}</div>
          </div>
        ))}
        {filtered.length === 0 && (
          <div style={{ ...card, textAlign: 'center', color: '#4ade80', padding: '3rem' }}>
            <div style={{ fontSize: '1.5rem', marginBottom: '0.5rem' }}>{'\u2713'}</div>
            <div style={{ fontSize: '0.95rem', fontWeight: 500 }}>No anomalies detected</div>
            <div style={{ fontSize: '0.8rem', color: '#666', marginTop: '0.3rem' }}>All agents are operating normally</div>
          </div>
        )}
      </div>
    </div>
  );
}
