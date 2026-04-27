import { useEffect, useState } from 'react';
import type { CSSProperties, ReactNode } from 'react';
import { api } from '../api/client';
import { DonutChart, EventTypeBadge, HorizontalBarChart, KPICard, SectionHeader, SeverityBadge, StatusBadge } from '../components/Charts';

interface KPI {
  active_agents: number; active_agents_total: number; active_agents_change: number;
  total_ops: number; total_ops_change: number;
  avg_score: number; avg_score_change: number;
  uptime_seconds: number; memories_count: number; storage_kb: number;
}

interface OpsAgent { name: string; writes: number; reads: number; searches: number }
interface PerfAgent { name: string; score: number; ops: number; write_latency_ms: number; read_latency_ms: number; status: string }
interface Anomaly { id: string; severity: 'CRITICAL' | 'WARNING' | 'INFO'; type: string; agent: string; description: string; timestamp: string }

interface ControlRoom {
  runtime: {
    agent: string;
    status: string;
    uptime_seconds: number;
    workspace: string;
    db_dir: string;
    audit_entries: number;
    tool_events?: number;
  };
  agents: { enabled: number; total: number; primary: string };
  safety: {
    posture: string;
    enabled: number;
    blocked: number;
    warnings: number;
    items: { key: string; label: string; status: string; risk: string }[];
  };
  approvals: { pending: number; recent: unknown[]; policy: string };
  jobs: {
    enabled: number;
    running: number;
    total: number;
    items: { name: string; enabled: boolean; running: boolean; status: string; schedule: string; last_run: string | null }[];
  };
  memory: {
    status: string;
    db_dir: string;
    fts_facts: number;
    kg_entities: number;
    kg_relations: number;
    qdrant_present: boolean;
  };
  coordination: {
    status: string;
    db_path: string;
    messages: number;
    active_claims: number;
    sent_claims: number;
    shared_facts: number;
    duplicate_replies_avoided: number;
  };
  sessions: { id: string; agent: string; requests: number; last_seen: string; summary: string }[];
  recent_activity: {
    id: string;
    type: string;
    agent: string;
    description: string;
    timestamp: string;
    duration_ms?: number;
    cost_usd?: number;
  }[];
}

type BadgeStatus = 'running' | 'stopped' | 'error' | 'warning';

const panel: CSSProperties = {
  background: '#111',
  border: '1px solid #1a1a1a',
  borderRadius: 8,
  padding: '1rem',
};

const mutedText: CSSProperties = {
  color: '#777',
  fontSize: '0.72rem',
  textTransform: 'uppercase',
  letterSpacing: '0.04em',
};

function formatUptime(s: number) {
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
  return `${Math.floor(s / 86400)}d ${Math.floor((s % 86400) / 3600)}h`;
}

function formatDateTime(value: string | null) {
  if (!value) return 'Never';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString([], { month: 'short', day: '2-digit', hour: '2-digit', minute: '2-digit' });
}

function compactPath(value: string) {
  if (!value) return '-';
  if (value.length <= 42) return value;
  return `...${value.slice(-39)}`;
}

function compactId(value: string) {
  if (value.length <= 14) return value;
  return `${value.slice(0, 6)}...${value.slice(-5)}`;
}

function scoreColor(score: number) {
  return score >= 80 ? '#4ade80' : score >= 60 ? '#f59e0b' : '#ef4444';
}

function statusFor(value: string): BadgeStatus {
  if (value === 'running' || value === 'active' || value === 'ready' || value === 'strict') return 'running';
  if (value === 'open' || value === 'enabled') return 'warning';
  if (value === 'error' || value === 'failed') return 'error';
  return 'stopped';
}

function jobStatus(value: string): BadgeStatus {
  if (value === 'running') return 'running';
  if (value === 'enabled') return 'warning';
  return 'stopped';
}

function MiniStat({ label, value, accent }: { label: string; value: ReactNode; accent?: string }) {
  return (
    <div style={{ background: '#0a0a0a', border: '1px solid #181818', borderRadius: 6, padding: '0.55rem 0.65rem', minWidth: 0 }}>
      <div style={{ color: '#666', fontSize: '0.68rem', marginBottom: '0.25rem', textTransform: 'uppercase', letterSpacing: '0.04em' }}>{label}</div>
      <div style={{ color: accent || '#f3f3f3', fontSize: '0.9rem', fontWeight: 650, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{value}</div>
    </div>
  );
}

function EmptyState({ children }: { children: ReactNode }) {
  return <div style={{ padding: '1.25rem', textAlign: 'center', color: '#555', fontSize: '0.82rem' }}>{children}</div>;
}

export default function OverviewPage() {
  const [kpi, setKpi] = useState<KPI | null>(null);
  const [control, setControl] = useState<ControlRoom | null>(null);
  const [ops, setOps] = useState<OpsAgent[]>([]);
  const [agents, setAgents] = useState<PerfAgent[]>([]);
  const [anomalies, setAnomalies] = useState<Anomaly[]>([]);

  const load = () => {
    api<KPI>('/api/overview/kpi').then(setKpi).catch(() => {});
    api<ControlRoom>('/api/overview/control-room').then(setControl).catch(() => {});
    api<{ agents: OpsAgent[] }>('/api/overview/operations').then(r => setOps(r.agents)).catch(() => {});
    api<{ agents: PerfAgent[] }>('/api/performance/agents').then(r => setAgents(r.agents)).catch(() => {});
    api<{ anomalies: Anomaly[] }>('/api/anomalies/list').then(r => setAnomalies(r.anomalies)).catch(() => {});
  };

  useEffect(() => {
    load();
    const interval = setInterval(load, 30000);
    return () => clearInterval(interval);
  }, []);

  const barData = ops.map(a => ({
    label: a.name.replace(/_/g, '-'),
    segments: [
      { value: a.writes, color: '#f97316', label: 'Writes' },
      { value: a.reads, color: '#3b82f6', label: 'Reads' },
      { value: a.searches, color: '#4ade80', label: 'Searches' },
    ],
  }));

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: '1rem', marginBottom: '1.1rem' }}>
        <h1 style={{ fontSize: '1.5rem', fontWeight: 600 }}>KAOS Control Room</h1>
        {control && <StatusBadge status={statusFor(control.runtime.status)} label={control.runtime.status} />}
      </div>

      {control && (
        <>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(190px, 1fr))', gap: '0.85rem', marginBottom: '1rem' }}>
            <div style={panel}>
              <div style={mutedText}>Runtime</div>
              <div style={{ marginTop: '0.45rem', fontSize: '1.25rem', fontWeight: 700, color: '#fff' }}>{control.runtime.agent}</div>
              <div style={{ marginTop: '0.65rem', display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '0.45rem' }}>
                <MiniStat label="Uptime" value={formatUptime(control.runtime.uptime_seconds)} accent="#06b6d4" />
                <MiniStat label="Tools" value={control.runtime.tool_events ?? 0} />
              </div>
            </div>

            <div style={panel}>
              <div style={mutedText}>Safety</div>
              <div style={{ marginTop: '0.55rem', display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '0.7rem' }}>
                <StatusBadge status={statusFor(control.safety.posture)} label={control.safety.posture} />
                <span style={{ color: control.safety.warnings ? '#f59e0b' : '#4ade80', fontSize: '1.25rem', fontWeight: 700 }}>{control.safety.warnings}</span>
              </div>
              <div style={{ marginTop: '0.65rem', display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '0.45rem' }}>
                <MiniStat label="Enabled" value={control.safety.enabled} />
                <MiniStat label="Blocked" value={control.safety.blocked} />
              </div>
            </div>

            <div style={panel}>
              <div style={mutedText}>Approvals</div>
              <div style={{ marginTop: '0.45rem', fontSize: '1.9rem', lineHeight: 1.05, fontWeight: 750, color: control.approvals.pending ? '#f59e0b' : '#4ade80' }}>
                {control.approvals.pending}
              </div>
              <div style={{ marginTop: '0.65rem' }}>
                <MiniStat label="Policy" value={control.approvals.policy} />
              </div>
            </div>

            <div style={panel}>
              <div style={mutedText}>Coordination</div>
              <div style={{ marginTop: '0.55rem', display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: '0.7rem' }}>
                <StatusBadge status={statusFor(control.coordination.status)} label={control.coordination.status.replace(/_/g, ' ')} />
                <span style={{ color: '#f3f3f3', fontSize: '1.25rem', fontWeight: 700 }}>{control.coordination.active_claims}</span>
              </div>
              <div style={{ marginTop: '0.65rem', display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '0.45rem' }}>
                <MiniStat label="Messages" value={control.coordination.messages} />
                <MiniStat label="Avoided" value={control.coordination.duplicate_replies_avoided} />
              </div>
            </div>
          </div>

          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(270px, 1fr))', gap: '1rem', marginBottom: '1.5rem' }}>
            <div style={panel}>
              <SectionHeader title="Recent Sessions" />
              <div style={{ display: 'flex', flexDirection: 'column', gap: '0.45rem' }}>
                {control.sessions.length === 0 && <EmptyState>No sessions recorded</EmptyState>}
                {control.sessions.map(session => (
                  <div key={session.id} style={{ display: 'grid', gridTemplateColumns: '88px 1fr auto', gap: '0.65rem', alignItems: 'center', padding: '0.55rem 0', borderBottom: '1px solid #171717' }}>
                    <span style={{ color: '#ccc', fontSize: '0.78rem', fontWeight: 650 }}>{compactId(session.id)}</span>
                    <span style={{ color: '#777', fontSize: '0.76rem', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{session.summary || session.agent}</span>
                    <span style={{ color: '#f97316', fontSize: '0.75rem', fontWeight: 650 }}>{session.requests}</span>
                  </div>
                ))}
              </div>
            </div>

            <div style={panel}>
              <SectionHeader title="Tool And Audit Activity" />
              <div style={{ display: 'flex', flexDirection: 'column', gap: '0.55rem' }}>
                {control.recent_activity.length === 0 && <EmptyState>No activity recorded</EmptyState>}
                {control.recent_activity.map(event => (
                  <div key={event.id} style={{ display: 'grid', gridTemplateColumns: '86px 1fr auto', gap: '0.65rem', alignItems: 'center' }}>
                    <EventTypeBadge type={event.type} />
                    <div style={{ minWidth: 0 }}>
                      <div style={{ color: '#ddd', fontSize: '0.78rem', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{event.description || event.agent}</div>
                      <div style={{ color: '#555', fontSize: '0.68rem', marginTop: '0.15rem' }}>{formatDateTime(event.timestamp)}</div>
                    </div>
                    <span style={{ color: '#777', fontSize: '0.72rem' }}>{event.duration_ms ? `${event.duration_ms}ms` : '-'}</span>
                  </div>
                ))}
              </div>
            </div>

            <div style={panel}>
              <SectionHeader title="Memory Plane" action={<StatusBadge status={statusFor(control.memory.status)} label={control.memory.status.replace(/_/g, ' ')} />} />
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2, 1fr)', gap: '0.5rem' }}>
                <MiniStat label="Facts" value={control.memory.fts_facts} />
                <MiniStat label="Entities" value={control.memory.kg_entities} />
                <MiniStat label="Relations" value={control.memory.kg_relations} />
                <MiniStat label="Vector" value={control.memory.qdrant_present ? 'Ready' : 'Empty'} accent={control.memory.qdrant_present ? '#4ade80' : '#777'} />
              </div>
              <div style={{ marginTop: '0.65rem', color: '#555', fontSize: '0.72rem', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{compactPath(control.memory.db_dir)}</div>
            </div>

            <div style={panel}>
              <SectionHeader title="Scheduled Jobs" action={<span style={{ color: '#888', fontSize: '0.75rem' }}>{control.jobs.running}/{control.jobs.enabled}</span>} />
              <div style={{ display: 'flex', flexDirection: 'column', gap: '0.5rem' }}>
                {control.jobs.items.length === 0 && <EmptyState>No scheduler attached</EmptyState>}
                {control.jobs.items.slice(0, 5).map(job => (
                  <div key={job.name} style={{ display: 'grid', gridTemplateColumns: '1fr auto', gap: '0.65rem', alignItems: 'center', paddingBottom: '0.5rem', borderBottom: '1px solid #171717' }}>
                    <div style={{ minWidth: 0 }}>
                      <div style={{ color: '#ddd', fontSize: '0.78rem', fontWeight: 600, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{job.name}</div>
                      <div style={{ color: '#555', fontSize: '0.68rem', marginTop: '0.15rem' }}>{job.schedule} · {formatDateTime(job.last_run)}</div>
                    </div>
                    <StatusBadge status={jobStatus(job.status)} label={job.status} />
                  </div>
                ))}
              </div>
            </div>
          </div>
        </>
      )}

      {kpi && (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))', gap: '1rem', marginBottom: '1.5rem' }}>
          <KPICard icon="A" value={String(kpi.active_agents)} label="Active Agents" change={kpi.active_agents_change} accentColor="#f97316" />
          <KPICard icon="O" value={String(kpi.total_ops)} label="Total Ops" change={kpi.total_ops_change} accentColor="#ef4444" />
          <KPICard icon="S" value={String(kpi.avg_score)} label="Avg Score" change={kpi.avg_score_change} accentColor="#4ade80" />
          <KPICard icon="T" value={formatUptime(kpi.uptime_seconds)} label="Uptime" accentColor="#06b6d4" />
        </div>
      )}

      <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1fr) minmax(280px, 320px)', gap: '1rem', marginBottom: '1.5rem' }}>
        <div style={panel}>
          <SectionHeader title="Operations Overview" action={
            <div style={{ display: 'flex', gap: '0.75rem', fontSize: '0.7rem', flexWrap: 'wrap' }}>
              <span style={{ display: 'flex', alignItems: 'center', gap: '0.3rem' }}><span style={{ width: 8, height: 8, borderRadius: 2, background: '#f97316', display: 'inline-block' }} /> Writes</span>
              <span style={{ display: 'flex', alignItems: 'center', gap: '0.3rem' }}><span style={{ width: 8, height: 8, borderRadius: 2, background: '#3b82f6', display: 'inline-block' }} /> Reads</span>
              <span style={{ display: 'flex', alignItems: 'center', gap: '0.3rem' }}><span style={{ width: 8, height: 8, borderRadius: 2, background: '#4ade80', display: 'inline-block' }} /> Searches</span>
            </div>
          } />
          {barData.length > 0 ? (
            <HorizontalBarChart data={barData} height={28} />
          ) : (
            <EmptyState>No operations recorded yet</EmptyState>
          )}
        </div>

        <div style={panel}>
          <SectionHeader title="Agent Health" />
          <div style={{ display: 'flex', justifyContent: 'center', marginBottom: '1rem' }}>
            <DonutChart
              value={kpi?.active_agents || 0}
              max={kpi?.active_agents_total || 1}
              size={130}
              color="#4ade80"
              label="Agents"
              sublabel={`Healthy ${kpi?.active_agents || 0}`}
            />
          </div>
          {kpi && (
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2, 1fr)', gap: '0.5rem' }}>
              <MiniStat label="Memories" value={kpi.memories_count} />
              <MiniStat label="Storage" value={`${kpi.storage_kb} KB`} />
            </div>
          )}
        </div>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1fr) minmax(280px, 320px)', gap: '1rem' }}>
        <div style={{ ...panel, overflowX: 'auto' }}>
          <SectionHeader title="Agents" />
          <table style={{ width: '100%', minWidth: 640, borderCollapse: 'collapse', fontSize: '0.82rem' }}>
            <thead>
              <tr style={{ borderBottom: '1px solid #1a1a1a' }}>
                {['Agent', 'Status', 'Score', 'OPS', 'W Latency', 'R Latency'].map(h => (
                  <th key={h} style={{ padding: '0.5rem 0.6rem', textAlign: 'left', color: '#555', fontWeight: 500, fontSize: '0.72rem', textTransform: 'uppercase', letterSpacing: '0.04em' }}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {agents.map(a => (
                <tr key={a.name} style={{ borderBottom: '1px solid #111' }}>
                  <td style={{ padding: '0.55rem 0.6rem' }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '0.4rem' }}>
                      <span style={{ width: 6, height: 6, borderRadius: '50%', background: a.status === 'running' ? '#4ade80' : '#ef4444' }} />
                      <span style={{ color: '#e0e0e0', fontWeight: 500 }}>{a.name.replace(/_/g, '-')}</span>
                    </div>
                  </td>
                  <td style={{ padding: '0.55rem 0.6rem' }}><StatusBadge status={statusFor(a.status)} label={a.status === 'running' ? 'Running' : 'Stopped'} /></td>
                  <td style={{ padding: '0.55rem 0.6rem' }}><span style={{ color: scoreColor(a.score), fontWeight: 600 }}>{a.score}</span></td>
                  <td style={{ padding: '0.55rem 0.6rem', color: '#888' }}>{a.ops}</td>
                  <td style={{ padding: '0.55rem 0.6rem', color: '#888' }}>{a.write_latency_ms}ms</td>
                  <td style={{ padding: '0.55rem 0.6rem', color: '#888' }}>{a.read_latency_ms}ms</td>
                </tr>
              ))}
              {agents.length === 0 && (
                <tr><td colSpan={6}><EmptyState>No agent data</EmptyState></td></tr>
              )}
            </tbody>
          </table>
        </div>

        <div style={panel}>
          <SectionHeader title="Anomalies" action={
            anomalies.length > 0 ? <span style={{
              width: 20, height: 20, borderRadius: '50%', background: '#ef4444',
              display: 'flex', alignItems: 'center', justifyContent: 'center',
              fontSize: '0.65rem', fontWeight: 700, color: '#fff',
            }}>{anomalies.length}</span> : undefined
          } />
          <div style={{ display: 'flex', flexDirection: 'column', gap: '0.5rem', maxHeight: 320, overflow: 'auto' }}>
            {anomalies.length === 0 && <EmptyState>No anomalies detected</EmptyState>}
            {anomalies.slice(0, 10).map(a => (
              <div key={a.id} style={{
                padding: '0.65rem 0.75rem', borderRadius: 8,
                background: a.severity === 'CRITICAL' ? '#1a0505' : a.severity === 'WARNING' ? '#1a1205' : '#0a0a1a',
                border: `1px solid ${a.severity === 'CRITICAL' ? '#331111' : a.severity === 'WARNING' ? '#332211' : '#111133'}`,
              }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '0.4rem', marginBottom: '0.3rem', flexWrap: 'wrap' }}>
                  <span style={{ width: 6, height: 6, borderRadius: '50%', background: a.severity === 'CRITICAL' ? '#ef4444' : a.severity === 'WARNING' ? '#f59e0b' : '#3b82f6' }} />
                  <span style={{ fontSize: '0.78rem', color: '#ccc', fontWeight: 500 }}>{a.agent.replace(/_/g, '-')}</span>
                  <SeverityBadge severity={a.severity} />
                </div>
                <div style={{ fontSize: '0.75rem', color: '#888', marginBottom: '0.2rem' }}>{a.description}</div>
                <div style={{ fontSize: '0.65rem', color: '#444' }}>{a.type.replace(/_/g, ' ')}</div>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
