import { useEffect, useState } from 'react';
import type { CSSProperties } from 'react';
import { api } from '../api/client';
import { SectionHeader, StatusBadge } from '../components/Charts';

interface Stats {
  cost: { date: string; cost_usd: number; requests: number; input_tokens: number; output_tokens: number };
  cron_jobs: Record<string, { enabled: boolean; running: boolean; last_run: number }>;
}

interface Job {
  name: string;
  enabled: boolean;
  running: boolean;
  status: string;
  schedule: string;
  last_run: string | null;
  next_run: string | null;
  owner: string;
  capabilities: string[];
  safe_controls: { pause: boolean; resume: boolean; trigger_now: boolean };
}

interface JobRun {
  ts: string;
  job: string;
  status: string;
  duration_ms: number;
  error?: string;
  agent?: string;
}

interface RequestEntry {
  ts: string; tier: string; duration_ms: number; input_preview: string; output_preview: string; approx_cost_usd: number;
}

function statusFor(status: string): 'running' | 'stopped' | 'error' | 'warning' {
  if (status === 'running' || status === 'enabled' || status === 'ok') return 'running';
  if (status === 'error') return 'error';
  if (status === 'paused' || status === 'demo') return 'warning';
  return 'stopped';
}

function formatTime(value: string | null) {
  if (!value) return '-';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString([], { month: 'short', day: '2-digit', hour: '2-digit', minute: '2-digit' });
}

export default function MonitoringPage() {
  const [stats, setStats] = useState<Stats | null>(null);
  const [requests, setRequests] = useState<RequestEntry[]>([]);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [history, setHistory] = useState<JobRun[]>([]);
  const [selectedJob, setSelectedJob] = useState('all');
  const [toast, setToast] = useState('');

  const showToast = (msg: string) => { setToast(msg); setTimeout(() => setToast(''), 3000); };

  const load = () => {
    api<Stats>('/api/monitoring/stats').then(setStats).catch(() => {});
    api<{ requests: RequestEntry[] }>('/api/monitoring/requests?limit=20').then(r => setRequests(r.requests)).catch(() => {});
    api<{ jobs: Job[] }>('/api/monitoring/jobs').then(r => setJobs(r.jobs)).catch(() => {});
    api<{ runs: JobRun[] }>(`/api/monitoring/jobs/history?job=${selectedJob}&limit=80`).then(r => setHistory(r.runs)).catch(() => {});
  };

  useEffect(() => {
    load();
    const interval = setInterval(load, 30000);
    return () => clearInterval(interval);
  }, [selectedJob]);

  const controlJob = async (job: Job, action: 'pause' | 'resume' | 'trigger') => {
    if (action === 'trigger' && !window.confirm(`Trigger ${job.name} now?`)) return;
    try {
      await api(`/api/monitoring/jobs/${job.name}/${action}`, { method: 'POST' });
      showToast(`${job.name}: ${action}`);
      load();
    } catch (e: any) {
      showToast(e.message || 'Job action failed');
    }
  };

  const card: CSSProperties = {
    background: '#111', border: '1px solid #1a1a1a', borderRadius: 8, padding: '1rem',
  };

  return (
    <div>
      <h1 style={{ fontSize: '1.5rem', marginBottom: '1rem', fontWeight: 600 }}>Monitoring</h1>

      {stats && (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(160px, 1fr))', gap: '1rem', marginBottom: '1.25rem' }}>
          <Card title="Today's Cost" value={`$${stats.cost.cost_usd.toFixed(4)}`} />
          <Card title="Requests" value={String(stats.cost.requests)} />
          <Card title="Input Tokens" value={stats.cost.input_tokens.toLocaleString()} />
          <Card title="Output Tokens" value={stats.cost.output_tokens.toLocaleString()} />
        </div>
      )}

      <div style={{ ...card, marginBottom: '1rem' }}>
        <SectionHeader title="Scheduled Jobs" action={<span style={{ color: '#888', fontSize: '0.75rem' }}>{jobs.length} jobs</span>} />
        <div style={{ overflowX: 'auto' }}>
          <table style={{ width: '100%', minWidth: 900, borderCollapse: 'collapse', fontSize: '0.8rem' }}>
            <thead>
              <tr style={{ borderBottom: '1px solid #222' }}>
                {['Job', 'Status', 'Schedule', 'Next Run', 'Last Run', 'Owner', 'Capabilities', ''].map(header => (
                  <th key={header} style={{ padding: '0.55rem 0.6rem', textAlign: 'left', color: '#555', fontWeight: 600, fontSize: '0.68rem', textTransform: 'uppercase', letterSpacing: '0.04em' }}>{header}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {jobs.map(job => (
                <tr key={job.name} style={{ borderBottom: '1px solid #171717' }}>
                  <td style={{ padding: '0.65rem 0.6rem', color: '#eee', fontWeight: 650 }}>{job.name}</td>
                  <td style={{ padding: '0.65rem 0.6rem' }}><StatusBadge status={statusFor(job.status)} label={job.status} /></td>
                  <td style={{ padding: '0.65rem 0.6rem', color: '#888' }}>{job.schedule}</td>
                  <td style={{ padding: '0.65rem 0.6rem', color: '#888' }}>{formatTime(job.next_run)}</td>
                  <td style={{ padding: '0.65rem 0.6rem', color: '#888' }}>{formatTime(job.last_run)}</td>
                  <td style={{ padding: '0.65rem 0.6rem', color: '#888' }}>{job.owner}</td>
                  <td style={{ padding: '0.65rem 0.6rem', color: '#888' }}>{job.capabilities.join(', ')}</td>
                  <td style={{ padding: '0.65rem 0.6rem' }}>
                    <div style={{ display: 'flex', gap: '0.35rem', justifyContent: 'flex-end' }}>
                      {job.enabled ? (
                        <button disabled={!job.safe_controls.pause} onClick={() => controlJob(job, 'pause')} style={actionButton('#f59e0b')}>Pause</button>
                      ) : (
                        <button disabled={!job.safe_controls.resume} onClick={() => controlJob(job, 'resume')} style={actionButton('#4ade80')}>Resume</button>
                      )}
                      <button disabled={!job.safe_controls.trigger_now} onClick={() => controlJob(job, 'trigger')} style={actionButton('#06b6d4')}>Run</button>
                    </div>
                  </td>
                </tr>
              ))}
              {jobs.length === 0 && (
                <tr><td colSpan={8} style={{ padding: '2rem', textAlign: 'center', color: '#555' }}>No jobs registered</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1fr) 360px', gap: '1rem', marginBottom: '1rem' }}>
        <div style={card}>
          <SectionHeader title="Run History" action={
            <select value={selectedJob} onChange={e => setSelectedJob(e.target.value)} style={{
              background: '#0a0a0a', color: '#ddd', border: '1px solid #222', borderRadius: 6,
              padding: '0.35rem 0.55rem', fontSize: '0.76rem',
            }}>
              <option value="all">All jobs</option>
              {jobs.map(job => <option key={job.name} value={job.name}>{job.name}</option>)}
            </select>
          } />
          <div style={{ display: 'flex', flexDirection: 'column', gap: '0.45rem', maxHeight: 330, overflow: 'auto' }}>
            {history.length === 0 && <div style={{ padding: '2rem', textAlign: 'center', color: '#555' }}>No job runs recorded</div>}
            {history.map((run, index) => (
              <div key={`${run.job}-${run.ts}-${index}`} style={{ display: 'grid', gridTemplateColumns: '1fr auto', gap: '0.75rem', background: '#0a0a0a', border: '1px solid #171717', borderRadius: 8, padding: '0.65rem 0.75rem' }}>
                <div style={{ minWidth: 0 }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', marginBottom: '0.25rem' }}>
                    <span style={{ color: '#e5e5e5', fontWeight: 650, fontSize: '0.82rem' }}>{run.job}</span>
                    <StatusBadge status={statusFor(run.status)} label={run.status} />
                  </div>
                  <div style={{ color: run.error ? '#fca5a5' : '#666', fontSize: '0.72rem', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{run.error || run.agent || '-'}</div>
                </div>
                <div style={{ textAlign: 'right', color: '#777', fontSize: '0.72rem' }}>
                  <div>{formatTime(run.ts)}</div>
                  <div style={{ marginTop: '0.2rem' }}>{run.duration_ms ? `${run.duration_ms}ms` : '-'}</div>
                </div>
              </div>
            ))}
          </div>
        </div>

        <div style={card}>
          <SectionHeader title="Links" />
          <LinkStat label="Tool Events" value="Audit Trail" />
          <LinkStat label="Memory Updates" value="Memory Inspector" />
          <LinkStat label="Sessions" value="Requests" />
        </div>
      </div>

      <div style={card}>
        <SectionHeader title="Recent Requests" />
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.82rem' }}>
          <thead>
            <tr style={{ borderBottom: '1px solid #333', textAlign: 'left' }}>
              {['Time', 'Tier', 'Input', 'Duration', 'Cost'].map(header => (
                <th key={header} style={{ padding: '0.5rem', color: '#555', fontSize: '0.68rem', textTransform: 'uppercase', letterSpacing: '0.04em' }}>{header}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {requests.map((r, i) => (
              <tr key={i} style={{ borderBottom: '1px solid #1a1a1a' }}>
                <td style={{ padding: '0.5rem', color: '#888' }}>{r.ts?.slice(11, 19)}</td>
                <td style={{ padding: '0.5rem' }}>
                  <span style={{ padding: '0.1rem 0.5rem', borderRadius: 3, background: r.tier === 'standard' ? '#1e3a5f' : '#1a2e1a', fontSize: '0.75rem' }}>{r.tier}</span>
                </td>
                <td style={{ padding: '0.5rem', maxWidth: 520, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{r.input_preview}</td>
                <td style={{ padding: '0.5rem', color: '#888' }}>{(r.duration_ms / 1000).toFixed(1)}s</td>
                <td style={{ padding: '0.5rem', color: '#4ade80' }}>${r.approx_cost_usd?.toFixed(5)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {toast && (
        <div style={{
          position: 'fixed', bottom: '1.5rem', right: '1.5rem',
          background: '#1a1a2e', color: '#fff', padding: '0.75rem 1.5rem',
          borderRadius: 8, fontSize: '0.85rem', zIndex: 9999,
          boxShadow: '0 4px 20px rgba(0,0,0,0.4)', border: '1px solid #2563eb33',
        }}>{toast}</div>
      )}
    </div>
  );
}

function actionButton(color: string): CSSProperties {
  return {
    padding: '0.3rem 0.55rem', borderRadius: 6, border: `1px solid ${color}55`,
    background: `${color}18`, color, cursor: 'pointer', fontSize: '0.72rem', fontWeight: 650,
  };
}

function Card({ title, value }: { title: string; value: string }) {
  return (
    <div style={{ background: '#111', border: '1px solid #222', borderRadius: 8, padding: '1rem' }}>
      <div style={{ fontSize: '0.75rem', color: '#888', marginBottom: '0.25rem', textTransform: 'uppercase', letterSpacing: '0.04em' }}>{title}</div>
      <div style={{ fontSize: '1.45rem', fontWeight: 750 }}>{value}</div>
    </div>
  );
}

function LinkStat({ label, value }: { label: string; value: string }) {
  return (
    <div style={{ display: 'grid', gridTemplateColumns: '1fr auto', gap: '0.7rem', padding: '0.65rem 0', borderBottom: '1px solid #171717', fontSize: '0.8rem' }}>
      <span style={{ color: '#777' }}>{label}</span>
      <span style={{ color: '#ddd', fontWeight: 650 }}>{value}</span>
    </div>
  );
}
