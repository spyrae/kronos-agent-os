import { useEffect, useState } from 'react';
import { api } from '../api/client';
import { StatusBadge } from '../components/Charts';

interface Agent {
  name: string; enabled: boolean; tier: string;
  description: string; tool_prefixes: string[];
  module: string; factory: string;
}

interface PerfAgent {
  name: string; score: number; ops: number;
  write_latency_ms: number; read_latency_ms: number; status: string;
}

export default function AgentsPage() {
  const [agents, setAgents] = useState<Agent[]>([]);
  const [perf, setPerf] = useState<Record<string, PerfAgent>>({});
  const [toast, setToast] = useState('');

  const showToast = (msg: string) => { setToast(msg); setTimeout(() => setToast(''), 3000); };

  const load = () => {
    api<{ agents: Agent[] }>('/api/agents/').then(r => setAgents(r.agents)).catch(() => {});
    api<{ agents: PerfAgent[] }>('/api/performance/agents').then(r => {
      const map: Record<string, PerfAgent> = {};
      r.agents.forEach(a => { map[a.name] = a; });
      setPerf(map);
    }).catch(() => {});
  };

  useEffect(() => { load(); }, []);

  const toggle = async (name: string) => {
    const r = await api<{ enabled: boolean }>(`/api/agents/${name}/toggle`, { method: 'POST' });
    showToast(`${name.replace(/_/g, '-')} ${r.enabled ? 'enabled' : 'disabled'}`);
    load();
  };

  const scoreColor = (s: number) => s >= 80 ? '#4ade80' : s >= 60 ? '#f59e0b' : '#ef4444';

  return (
    <div>
      <h1 style={{ fontSize: '1.5rem', fontWeight: 600, marginBottom: '1.25rem' }}>Agents ({agents.length})</h1>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(420px, 1fr))', gap: '1rem' }}>
        {agents.map(a => {
          const p = perf[a.name];
          return (
            <div key={a.name} style={{
              background: '#111', border: `1px solid ${a.enabled ? '#1a1a2e' : '#1a1a1a'}`,
              borderRadius: '12px', padding: '1.25rem',
              opacity: a.enabled ? 1 : 0.55, transition: 'opacity 0.2s',
              borderLeft: `3px solid ${a.enabled ? '#4ade80' : '#333'}`,
            }}>
              {/* Header */}
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '0.6rem' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
                  <span style={{
                    width: 8, height: 8, borderRadius: '50%',
                    background: a.enabled ? '#4ade80' : '#ef4444',
                  }} />
                  <span style={{ fontWeight: 600, fontSize: '1rem', color: '#fff' }}>{a.name.replace(/_/g, '-')}</span>
                </div>
                <div style={{ display: 'flex', gap: '0.4rem', alignItems: 'center' }}>
                  <StatusBadge status={a.enabled ? 'running' : 'stopped'} label={a.enabled ? 'Running' : 'Stopped'} />
                  <button onClick={() => toggle(a.name)} style={{
                    padding: '0.25rem 0.65rem', borderRadius: 4, border: 'none', cursor: 'pointer',
                    fontSize: '0.72rem', fontWeight: 600,
                    background: a.enabled ? '#166534' : '#333', color: '#fff',
                  }}>{a.enabled ? 'ON' : 'OFF'}</button>
                </div>
              </div>

              {/* Description */}
              <p style={{ fontSize: '0.8rem', color: '#888', marginBottom: '0.85rem', lineHeight: 1.45 }}>{a.description}</p>

              {/* Metrics */}
              <div style={{
                display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: '0.5rem',
                padding: '0.65rem 0', borderTop: '1px solid #1a1a1a', borderBottom: '1px solid #1a1a1a',
                marginBottom: '0.75rem',
              }}>
                <div style={{ textAlign: 'center' }}>
                  <div style={{ fontSize: '0.62rem', color: '#555', textTransform: 'uppercase', marginBottom: '0.2rem' }}>Score</div>
                  <div style={{ fontSize: '1.1rem', fontWeight: 700, color: p ? scoreColor(p.score) : '#555' }}>{p ? p.score : '—'}</div>
                </div>
                <div style={{ textAlign: 'center' }}>
                  <div style={{ fontSize: '0.62rem', color: '#555', textTransform: 'uppercase', marginBottom: '0.2rem' }}>OPS</div>
                  <div style={{ fontSize: '1.1rem', fontWeight: 700, color: '#fff' }}>{p ? p.ops : '—'}</div>
                </div>
                <div style={{ textAlign: 'center' }}>
                  <div style={{ fontSize: '0.62rem', color: '#555', textTransform: 'uppercase', marginBottom: '0.2rem' }}>Latency</div>
                  <div style={{ fontSize: '1.1rem', fontWeight: 700, color: '#fff' }}>{p ? `${p.write_latency_ms}ms` : '—'}</div>
                </div>
              </div>

              {/* Tags */}
              <div style={{ display: 'flex', gap: '0.35rem', flexWrap: 'wrap' }}>
                <span style={{
                  padding: '0.15rem 0.5rem', borderRadius: 4, fontSize: '0.65rem', fontWeight: 600,
                  background: a.tier === 'standard' ? '#1e3a5f' : '#1a2e1a',
                  color: a.tier === 'standard' ? '#93c5fd' : '#86efac',
                  border: `1px solid ${a.tier === 'standard' ? '#1e40af33' : '#16653433'}`,
                }}>{a.tier}</span>
                {a.tool_prefixes.slice(0, 4).map(t => (
                  <span key={t} style={{
                    padding: '0.15rem 0.4rem', borderRadius: 3, fontSize: '0.62rem',
                    background: '#1a1a1a', color: '#666', border: '1px solid #222',
                  }}>{t}</span>
                ))}
                {a.tool_prefixes.length > 4 && (
                  <span style={{ fontSize: '0.62rem', color: '#444', padding: '0.15rem 0.2rem' }}>
                    +{a.tool_prefixes.length - 4}
                  </span>
                )}
              </div>
            </div>
          );
        })}
      </div>

      {/* Toast */}
      {toast && (
        <div style={{
          position: 'fixed', top: '1rem', right: '1rem', padding: '0.75rem 1.5rem',
          borderRadius: '8px', background: '#166534', color: '#fff', fontSize: '0.85rem',
          zIndex: 100, boxShadow: '0 4px 20px rgba(0,0,0,0.4)', border: '1px solid #4ade8033',
        }}>{toast}</div>
      )}
    </div>
  );
}
