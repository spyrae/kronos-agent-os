import { useEffect, useState } from 'react';
import { api } from '../api/client';
import { KPICard, DonutChart, HorizontalBarChart, SectionHeader, SeverityBadge, StatusBadge } from '../components/Charts';

interface KPI {
  active_agents: number; active_agents_total: number; active_agents_change: number;
  total_ops: number; total_ops_change: number;
  avg_score: number; avg_score_change: number;
  uptime_seconds: number; memories_count: number; storage_kb: number;
}

interface OpsAgent { name: string; writes: number; reads: number; searches: number }
interface PerfAgent { name: string; score: number; ops: number; write_latency_ms: number; read_latency_ms: number; status: string }
interface Anomaly { id: string; severity: 'CRITICAL' | 'WARNING' | 'INFO'; type: string; agent: string; description: string; timestamp: string }

export default function OverviewPage() {
  const [kpi, setKpi] = useState<KPI | null>(null);
  const [ops, setOps] = useState<OpsAgent[]>([]);
  const [agents, setAgents] = useState<PerfAgent[]>([]);
  const [anomalies, setAnomalies] = useState<Anomaly[]>([]);

  const load = () => {
    api<KPI>('/api/overview/kpi').then(setKpi).catch(() => {});
    api<{ agents: OpsAgent[] }>('/api/overview/operations').then(r => setOps(r.agents)).catch(() => {});
    api<{ agents: PerfAgent[] }>('/api/performance/agents').then(r => setAgents(r.agents)).catch(() => {});
    api<{ anomalies: Anomaly[] }>('/api/anomalies/list').then(r => setAnomalies(r.anomalies)).catch(() => {});
  };

  useEffect(() => {
    load();
    const interval = setInterval(load, 30000);
    return () => clearInterval(interval);
  }, []);

  const formatUptime = (s: number) => {
    if (s < 60) return `${s}s`;
    if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
    if (s < 86400) return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
    return `${Math.floor(s / 86400)}d ${Math.floor((s % 86400) / 3600)}h`;
  };

  const card: React.CSSProperties = {
    background: '#111', border: '1px solid #1a1a1a', borderRadius: '12px', padding: '1.25rem',
  };

  const barData = ops.map(a => ({
    label: a.name.replace(/_/g, '-'),
    segments: [
      { value: a.writes, color: '#f97316', label: 'Writes' },
      { value: a.reads, color: '#3b82f6', label: 'Reads' },
      { value: a.searches, color: '#4ade80', label: 'Searches' },
    ],
  }));

  const scoreColor = (s: number) => s >= 80 ? '#4ade80' : s >= 60 ? '#f59e0b' : '#ef4444';

  return (
    <div>
      <h1 style={{ fontSize: '1.5rem', fontWeight: 600, marginBottom: '1.25rem' }}>Overview</h1>

      {/* KPI Cards */}
      {kpi && (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: '1rem', marginBottom: '1.5rem' }}>
          <KPICard icon="\u2B22" value={String(kpi.active_agents)} label="Active Agents" change={kpi.active_agents_change} accentColor="#f97316" />
          <KPICard icon="\u26A1" value={String(kpi.total_ops)} label="Total Ops" change={kpi.total_ops_change} accentColor="#ef4444" />
          <KPICard icon="\u2197" value={String(kpi.avg_score)} label="Avg Score" change={kpi.avg_score_change} accentColor="#4ade80" />
          <KPICard icon="\u23F1" value={formatUptime(kpi.uptime_seconds)} label="Uptime" accentColor="#06b6d4" />
        </div>
      )}

      {/* Main grid: Operations + Agent Health */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 320px', gap: '1rem', marginBottom: '1.5rem' }}>
        {/* Operations Overview */}
        <div style={card}>
          <SectionHeader title="Operations Overview" action={
            <div style={{ display: 'flex', gap: '0.75rem', fontSize: '0.7rem' }}>
              <span style={{ display: 'flex', alignItems: 'center', gap: '0.3rem' }}><span style={{ width: 8, height: 8, borderRadius: 2, background: '#f97316', display: 'inline-block' }} /> Writes</span>
              <span style={{ display: 'flex', alignItems: 'center', gap: '0.3rem' }}><span style={{ width: 8, height: 8, borderRadius: 2, background: '#3b82f6', display: 'inline-block' }} /> Reads</span>
              <span style={{ display: 'flex', alignItems: 'center', gap: '0.3rem' }}><span style={{ width: 8, height: 8, borderRadius: 2, background: '#4ade80', display: 'inline-block' }} /> Searches</span>
            </div>
          } />
          {barData.length > 0 ? (
            <HorizontalBarChart data={barData} height={28} />
          ) : (
            <div style={{ padding: '2rem', textAlign: 'center', color: '#555' }}>No operations recorded yet</div>
          )}
        </div>

        {/* Agent Health */}
        <div style={card}>
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
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2, 1fr)', gap: '0.5rem', fontSize: '0.75rem' }}>
              <div style={{ background: '#0a0a0a', borderRadius: 6, padding: '0.5rem 0.75rem', textAlign: 'center' }}>
                <div style={{ color: '#888', marginBottom: '0.15rem' }}>Memories</div>
                <div style={{ fontWeight: 600, color: '#fff' }}>{kpi.memories_count}</div>
              </div>
              <div style={{ background: '#0a0a0a', borderRadius: 6, padding: '0.5rem 0.75rem', textAlign: 'center' }}>
                <div style={{ color: '#888', marginBottom: '0.15rem' }}>Storage</div>
                <div style={{ fontWeight: 600, color: '#fff' }}>{kpi.storage_kb} KB</div>
              </div>
            </div>
          )}
        </div>
      </div>

      {/* Bottom grid: Agents table + Anomalies */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 320px', gap: '1rem' }}>
        {/* Agents table */}
        <div style={card}>
          <SectionHeader title="Agents" action={
            <span style={{ fontSize: '0.72rem', color: '#f97316', cursor: 'pointer' }}>View All →</span>
          } />
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.82rem' }}>
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
                  <td style={{ padding: '0.55rem 0.6rem' }}><StatusBadge status={a.status as any} label={a.status === 'running' ? 'Running' : 'Stopped'} /></td>
                  <td style={{ padding: '0.55rem 0.6rem' }}>
                    <span style={{ color: scoreColor(a.score), fontWeight: 600 }}>{a.score}</span>
                  </td>
                  <td style={{ padding: '0.55rem 0.6rem', color: '#888' }}>{a.ops}</td>
                  <td style={{ padding: '0.55rem 0.6rem', color: '#888' }}>{a.write_latency_ms}ms</td>
                  <td style={{ padding: '0.55rem 0.6rem', color: '#888' }}>{a.read_latency_ms}ms</td>
                </tr>
              ))}
              {agents.length === 0 && (
                <tr><td colSpan={6} style={{ padding: '2rem', textAlign: 'center', color: '#555' }}>No agent data</td></tr>
              )}
            </tbody>
          </table>
        </div>

        {/* Anomalies */}
        <div style={card}>
          <SectionHeader title="Anomalies" action={
            anomalies.length > 0 ? <span style={{
              width: 20, height: 20, borderRadius: '50%', background: '#ef4444',
              display: 'flex', alignItems: 'center', justifyContent: 'center',
              fontSize: '0.65rem', fontWeight: 700, color: '#fff',
            }}>{anomalies.length}</span> : undefined
          } />
          <div style={{ display: 'flex', flexDirection: 'column', gap: '0.5rem', maxHeight: 320, overflow: 'auto' }}>
            {anomalies.length === 0 && (
              <div style={{ padding: '2rem', textAlign: 'center', color: '#555', fontSize: '0.85rem' }}>No anomalies detected</div>
            )}
            {anomalies.slice(0, 10).map(a => (
              <div key={a.id} style={{
                padding: '0.65rem 0.75rem', borderRadius: 8,
                background: a.severity === 'CRITICAL' ? '#1a0505' : a.severity === 'WARNING' ? '#1a1205' : '#0a0a1a',
                border: `1px solid ${a.severity === 'CRITICAL' ? '#331111' : a.severity === 'WARNING' ? '#332211' : '#111133'}`,
              }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '0.4rem', marginBottom: '0.3rem' }}>
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
