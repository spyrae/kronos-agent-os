import { useEffect, useState } from 'react';
import { api } from '../api/client';

interface Stats {
  cost: { date: string; cost_usd: number; requests: number; input_tokens: number; output_tokens: number };
  cron_jobs: Record<string, { enabled: boolean; running: boolean; last_run: number }>;
}

interface RequestEntry {
  ts: string; tier: string; duration_ms: number; input_preview: string; output_preview: string; approx_cost_usd: number;
}

export default function MonitoringPage() {
  const [stats, setStats] = useState<Stats | null>(null);
  const [requests, setRequests] = useState<RequestEntry[]>([]);

  useEffect(() => {
    api<Stats>('/api/monitoring/stats').then(setStats).catch(console.error);
    api<{ requests: RequestEntry[] }>('/api/monitoring/requests?limit=20').then(r => setRequests(r.requests)).catch(console.error);
    const interval = setInterval(() => {
      api<Stats>('/api/monitoring/stats').then(setStats).catch(() => {});
    }, 30000);
    return () => clearInterval(interval);
  }, []);

  return (
    <div>
      <h1 style={{ fontSize: '1.5rem', marginBottom: '1rem' }}>Monitoring</h1>

      {stats && (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: '1rem', marginBottom: '2rem' }}>
          <Card title="Today's Cost" value={`$${stats.cost.cost_usd.toFixed(4)}`} />
          <Card title="Requests" value={String(stats.cost.requests)} />
          <Card title="Input Tokens" value={stats.cost.input_tokens.toLocaleString()} />
          <Card title="Output Tokens" value={stats.cost.output_tokens.toLocaleString()} />
        </div>
      )}

      {stats && Object.keys(stats.cron_jobs).length > 0 && (
        <div style={{ marginBottom: '2rem' }}>
          <h2 style={{ fontSize: '1.1rem', marginBottom: '0.5rem' }}>Cron Jobs</h2>
          <div style={{ display: 'flex', gap: '0.5rem', flexWrap: 'wrap' }}>
            {Object.entries(stats.cron_jobs).map(([name, job]) => (
              <span key={name} style={{
                padding: '0.25rem 0.75rem', borderRadius: '4px', fontSize: '0.85rem',
                background: job.running ? '#1e3a5f' : '#1a1a1a', border: '1px solid #333',
                color: job.enabled ? '#4ade80' : '#666',
              }}>
                {job.running ? '...' : ''} {name}
              </span>
            ))}
          </div>
        </div>
      )}

      <h2 style={{ fontSize: '1.1rem', marginBottom: '0.5rem' }}>Recent Requests</h2>
      <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.85rem' }}>
        <thead>
          <tr style={{ borderBottom: '1px solid #333', textAlign: 'left' }}>
            <th style={{ padding: '0.5rem' }}>Time</th>
            <th style={{ padding: '0.5rem' }}>Tier</th>
            <th style={{ padding: '0.5rem' }}>Input</th>
            <th style={{ padding: '0.5rem' }}>Duration</th>
            <th style={{ padding: '0.5rem' }}>Cost</th>
          </tr>
        </thead>
        <tbody>
          {requests.map((r, i) => (
            <tr key={i} style={{ borderBottom: '1px solid #1a1a1a' }}>
              <td style={{ padding: '0.5rem', color: '#888' }}>{r.ts?.slice(11, 19)}</td>
              <td style={{ padding: '0.5rem' }}>
                <span style={{ padding: '0.1rem 0.5rem', borderRadius: '3px', background: r.tier === 'standard' ? '#1e3a5f' : '#1a2e1a', fontSize: '0.75rem' }}>{r.tier}</span>
              </td>
              <td style={{ padding: '0.5rem', maxWidth: '400px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{r.input_preview}</td>
              <td style={{ padding: '0.5rem', color: '#888' }}>{(r.duration_ms / 1000).toFixed(1)}s</td>
              <td style={{ padding: '0.5rem', color: '#4ade80' }}>${r.approx_cost_usd?.toFixed(5)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Card({ title, value }: { title: string; value: string }) {
  return (
    <div style={{ background: '#111', border: '1px solid #222', borderRadius: '8px', padding: '1rem' }}>
      <div style={{ fontSize: '0.8rem', color: '#888', marginBottom: '0.25rem' }}>{title}</div>
      <div style={{ fontSize: '1.5rem', fontWeight: 'bold' }}>{value}</div>
    </div>
  );
}
