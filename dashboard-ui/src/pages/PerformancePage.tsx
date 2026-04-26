import { useEffect, useState } from 'react';
import { api } from '../api/client';
import { SectionHeader, StatusBadge } from '../components/Charts';

interface PerfAgent {
  name: string; score: number; ops: number;
  write_latency_ms: number; read_latency_ms: number; status: string;
}

export default function PerformancePage() {
  const [agents, setAgents] = useState<PerfAgent[]>([]);
  const [selected, setSelected] = useState('');
  const [range, setRange] = useState('1h');

  useEffect(() => {
    api<{ agents: PerfAgent[] }>('/api/performance/agents').then(r => {
      setAgents(r.agents);
      if (r.agents.length > 0 && !selected) setSelected(r.agents[0].name);
    }).catch(() => {});
  }, []);

  const card: React.CSSProperties = {
    background: '#111', border: '1px solid #1a1a1a', borderRadius: '12px', padding: '1.25rem',
  };

  const maxScore = 100;
  const maxLatency = Math.max(...agents.map(a => Math.max(a.write_latency_ms, a.read_latency_ms)), 1);
  const scoreColor = (s: number) => s >= 80 ? '#4ade80' : s >= 60 ? '#f59e0b' : '#ef4444';

  const timeRanges = ['5m', '15m', '1h', '6h', '24h'];
  const selectedAgent = agents.find(a => a.name === selected);

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '1.25rem' }}>
        <h1 style={{ fontSize: '1.5rem', fontWeight: 600 }}>Performance</h1>
        <div style={{ display: 'flex', gap: '0.25rem', background: '#111', borderRadius: 8, padding: '0.2rem', border: '1px solid #1a1a1a' }}>
          {timeRanges.map(r => (
            <button key={r} onClick={() => setRange(r)} style={{
              padding: '0.35rem 0.75rem', borderRadius: 6, border: 'none', cursor: 'pointer',
              background: range === r ? '#f97316' : 'transparent',
              color: range === r ? '#fff' : '#666', fontSize: '0.78rem', fontWeight: range === r ? 600 : 400,
            }}>{r}</button>
          ))}
        </div>
      </div>

      {/* Performance Score Comparison */}
      <div style={{ ...card, marginBottom: '1rem' }}>
        <SectionHeader title="Performance Score Comparison" />
        <div style={{ display: 'flex', flexDirection: 'column', gap: '0.65rem' }}>
          {agents.map(a => (
            <div key={a.name} style={{ display: 'flex', alignItems: 'center', gap: '0.75rem' }}>
              <span style={{ width: 140, fontSize: '0.78rem', color: '#999', textAlign: 'right', flexShrink: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {a.name.replace(/_/g, '-')}
              </span>
              <div style={{ flex: 1, position: 'relative', height: 26, background: '#1a1a1a', borderRadius: 4, overflow: 'hidden' }}>
                <div style={{
                  width: `${(a.score / maxScore) * 100}%`, height: '100%',
                  background: `linear-gradient(90deg, ${scoreColor(a.score)}88, ${scoreColor(a.score)})`,
                  borderRadius: 4, transition: 'width 0.6s ease',
                }} />
              </div>
              <span style={{ width: 42, fontSize: '0.82rem', fontWeight: 600, color: scoreColor(a.score), textAlign: 'right', flexShrink: 0 }}>
                {a.score}
              </span>
            </div>
          ))}
          {agents.length === 0 && <div style={{ padding: '2rem', textAlign: 'center', color: '#555' }}>No performance data</div>}
        </div>
      </div>

      {/* Latency Comparison */}
      <div style={{ ...card, marginBottom: '1rem' }}>
        <SectionHeader title="Latency Comparison (ms)" action={
          <div style={{ display: 'flex', gap: '0.75rem', fontSize: '0.7rem' }}>
            <span style={{ display: 'flex', alignItems: 'center', gap: '0.3rem' }}>
              <span style={{ width: 8, height: 8, borderRadius: 2, background: '#f97316', display: 'inline-block' }} /> Write Latency
            </span>
            <span style={{ display: 'flex', alignItems: 'center', gap: '0.3rem' }}>
              <span style={{ width: 8, height: 8, borderRadius: 2, background: '#3b82f6', display: 'inline-block' }} /> Read Latency
            </span>
          </div>
        } />
        <div style={{ display: 'flex', flexDirection: 'column', gap: '0.65rem' }}>
          {agents.map(a => (
            <div key={a.name} style={{ display: 'flex', alignItems: 'center', gap: '0.75rem' }}>
              <span style={{ width: 140, fontSize: '0.78rem', color: '#999', textAlign: 'right', flexShrink: 0 }}>
                {a.name.replace(/_/g, '-')}
              </span>
              <div style={{ flex: 1, display: 'flex', flexDirection: 'column', gap: 3 }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
                  <div style={{ flex: 1, height: 10, background: '#1a1a1a', borderRadius: 3, overflow: 'hidden' }}>
                    <div style={{ width: `${(a.write_latency_ms / maxLatency) * 100}%`, height: '100%', background: '#f97316', borderRadius: 3, transition: 'width 0.6s' }} />
                  </div>
                  <span style={{ fontSize: '0.7rem', color: '#888', width: 50, textAlign: 'right' }}>{a.write_latency_ms}ms</span>
                </div>
                <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
                  <div style={{ flex: 1, height: 10, background: '#1a1a1a', borderRadius: 3, overflow: 'hidden' }}>
                    <div style={{ width: `${(a.read_latency_ms / maxLatency) * 100}%`, height: '100%', background: '#3b82f6', borderRadius: 3, transition: 'width 0.6s' }} />
                  </div>
                  <span style={{ fontSize: '0.7rem', color: '#888', width: 50, textAlign: 'right' }}>{a.read_latency_ms}ms</span>
                </div>
              </div>
            </div>
          ))}
        </div>
      </div>

      {/* Agent detail */}
      <div style={card}>
        <SectionHeader title="Agent Detail" />
        <div style={{ marginBottom: '1rem' }}>
          <select
            value={selected}
            onChange={e => setSelected(e.target.value)}
            style={{
              padding: '0.45rem 0.75rem', borderRadius: 6, border: '1px solid #333',
              background: '#0a0a0a', color: '#fff', fontSize: '0.85rem',
            }}
          >
            {agents.map(a => <option key={a.name} value={a.name}>{a.name.replace(/_/g, '-')}</option>)}
          </select>
        </div>
        {selectedAgent && (
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(5, 1fr)', gap: '1rem' }}>
            {[
              { label: 'Status', value: selectedAgent.status, render: () => <StatusBadge status={selectedAgent.status as any} /> },
              { label: 'Score', value: selectedAgent.score, color: scoreColor(selectedAgent.score) },
              { label: 'Operations', value: selectedAgent.ops },
              { label: 'Write Latency', value: `${selectedAgent.write_latency_ms}ms` },
              { label: 'Read Latency', value: `${selectedAgent.read_latency_ms}ms` },
            ].map((m, i) => (
              <div key={i} style={{ background: '#0a0a0a', borderRadius: 8, padding: '0.75rem', textAlign: 'center' }}>
                <div style={{ fontSize: '0.7rem', color: '#666', marginBottom: '0.35rem', textTransform: 'uppercase' }}>{m.label}</div>
                {m.render ? m.render() : (
                  <div style={{ fontSize: '1.25rem', fontWeight: 700, color: m.color || '#fff' }}>{m.value}</div>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
