import { useEffect, useState } from 'react';
import type { CSSProperties } from 'react';
import { api } from '../api/client';
import { SectionHeader, StatusBadge } from '../components/Charts';

interface SwarmRole {
  agent: string;
  role: string;
  tier: number;
  status: string;
  task: string;
}

interface SwarmStep {
  agent: string;
  kind: string;
  text: string;
  status: string;
}

interface SwarmRun {
  id: string;
  title: string;
  status: string;
  created_at: string;
  updated_at: string;
  summary: string;
  roles: SwarmRole[];
  steps: SwarmStep[];
  final: string;
  metrics: Record<string, number>;
  demo: boolean;
}

function statusFor(status: string): 'running' | 'stopped' | 'error' | 'warning' {
  if (status === 'active' || status === 'winner' || status === 'sent' || status === 'completed') return 'running';
  if (status === 'claimed' || status === 'demo' || status === 'observed') return 'warning';
  if (status === 'cancelled' || status === 'expired') return 'error';
  return 'stopped';
}

function formatDate(value: string) {
  if (!value) return '-';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString([], { month: 'short', day: '2-digit', hour: '2-digit', minute: '2-digit' });
}

export default function SwarmPage() {
  const [runs, setRuns] = useState<SwarmRun[]>([]);
  const [selectedId, setSelectedId] = useState('');

  useEffect(() => {
    api<{ runs: SwarmRun[] }>('/api/swarm/runs').then(r => {
      setRuns(r.runs);
      if (!selectedId && r.runs.length > 0) setSelectedId(r.runs[0].id);
    }).catch(() => {});
  }, []);

  const selected = runs.find(run => run.id === selectedId) || runs[0];
  const card: CSSProperties = {
    background: '#111', border: '1px solid #1a1a1a', borderRadius: 8, padding: '1rem',
  };

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: '1rem', marginBottom: '1.25rem' }}>
        <h1 style={{ fontSize: '1.5rem', fontWeight: 600 }}>Swarm Visualizer</h1>
        {selected && <StatusBadge status={statusFor(selected.status)} label={selected.status} />}
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: '290px minmax(0, 1fr)', gap: '1rem' }}>
        <div style={card}>
          <SectionHeader title="Runs" />
          <div style={{ display: 'flex', flexDirection: 'column', gap: '0.45rem' }}>
            {runs.map(run => (
              <button
                key={run.id}
                onClick={() => setSelectedId(run.id)}
                style={{
                  textAlign: 'left', padding: '0.7rem 0.75rem', borderRadius: 8,
                  border: `1px solid ${selected?.id === run.id ? '#f9731655' : '#171717'}`,
                  background: selected?.id === run.id ? '#17120c' : '#0a0a0a',
                  color: '#ddd', cursor: 'pointer',
                }}
              >
                <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '0.5rem', marginBottom: '0.3rem' }}>
                  <span style={{ fontWeight: 650, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{run.title}</span>
                  {run.demo && <span style={{ color: '#06b6d4', fontSize: '0.65rem', fontWeight: 700 }}>DEMO</span>}
                </div>
                <div style={{ color: '#666', fontSize: '0.7rem' }}>{run.roles.length} roles · {formatDate(run.updated_at)}</div>
              </button>
            ))}
          </div>
        </div>

        {selected && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '1rem' }}>
            <div style={card}>
              <SectionHeader title="Arbitration Map" action={<span style={{ color: '#888', fontSize: '0.75rem' }}>{selected.summary}</span>} />
              <div style={{ overflowX: 'auto' }}>
                <svg width={920} height={290} style={{ display: 'block', margin: '0 auto' }}>
                  <defs>
                    <marker id="swarm-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
                      <path d="M 0 0 L 10 5 L 0 10 z" fill="#444" />
                    </marker>
                    <marker id="swarm-winner-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
                      <path d="M 0 0 L 10 5 L 0 10 z" fill="#4ade80" />
                    </marker>
                  </defs>
                  <rect x={20} y={118} width={150} height={54} rx={8} fill="#0a0a0a" stroke="#333" />
                  <text x={95} y={140} textAnchor="middle" fill="#fff" fontSize={12} fontWeight={650}>Trigger</text>
                  <text x={95} y={158} textAnchor="middle" fill="#777" fontSize={10}>{selected.id.slice(0, 24)}</text>

                  {selected.roles.map((role, index) => {
                    const x = 240 + index * 150;
                    const y = 48 + (index % 2) * 112;
                    const winner = role.status === 'winner' || role.status === 'sent';
                    return (
                      <g key={role.agent}>
                        <path
                          d={`M 170 145 C ${x - 55} 145 ${x - 50} ${y + 25} ${x} ${y + 25}`}
                          fill="none"
                          stroke={winner ? '#4ade80' : '#444'}
                          strokeWidth={winner ? 2.5 : 1.5}
                          markerEnd={winner ? 'url(#swarm-winner-arrow)' : 'url(#swarm-arrow)'}
                        />
                        <rect x={x} y={y} width={128} height={58} rx={8} fill={winner ? '#052e16' : '#101010'} stroke={winner ? '#4ade80' : '#2a2a2a'} />
                        <text x={x + 64} y={y + 20} textAnchor="middle" fill="#fff" fontSize={11} fontWeight={650}>{role.agent}</text>
                        <text x={x + 64} y={y + 37} textAnchor="middle" fill="#888" fontSize={10}>{role.role}</text>
                        <text x={x + 64} y={y + 51} textAnchor="middle" fill={winner ? '#4ade80' : '#777'} fontSize={10}>{role.status}</text>
                      </g>
                    );
                  })}

                  <rect x={760} y={118} width={140} height={54} rx={8} fill="#111827" stroke="#3b82f6" />
                  <text x={830} y={140} textAnchor="middle" fill="#fff" fontSize={12} fontWeight={650}>Synthesis</text>
                  <text x={830} y={158} textAnchor="middle" fill="#93c5fd" fontSize={10}>single answer</text>
                </svg>
              </div>
            </div>

            <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1fr) 320px', gap: '1rem' }}>
              <div style={card}>
                <SectionHeader title="Steps" />
                <div style={{ display: 'flex', flexDirection: 'column', gap: '0.55rem', maxHeight: 360, overflow: 'auto' }}>
                  {selected.steps.map((step, index) => (
                    <div key={`${step.agent}-${index}`} style={{ background: '#0a0a0a', border: '1px solid #171717', borderRadius: 8, padding: '0.7rem 0.8rem' }}>
                      <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', marginBottom: '0.35rem' }}>
                        <span style={{ color: '#ddd', fontWeight: 650, fontSize: '0.82rem' }}>{step.agent}</span>
                        <StatusBadge status={statusFor(step.status)} label={step.kind} />
                        <span style={{ color: '#555', fontSize: '0.7rem' }}>{step.status}</span>
                      </div>
                      <div style={{ color: '#999', fontSize: '0.78rem', lineHeight: 1.45 }}>{step.text}</div>
                    </div>
                  ))}
                </div>
              </div>

              <div style={card}>
                <SectionHeader title="Decision" />
                <div style={{ color: '#ddd', fontSize: '0.86rem', lineHeight: 1.55, marginBottom: '1rem' }}>{selected.final || selected.summary}</div>
                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '0.5rem' }}>
                  {Object.entries(selected.metrics).map(([key, value]) => (
                    <div key={key} style={{ background: '#0a0a0a', border: '1px solid #171717', borderRadius: 6, padding: '0.55rem 0.65rem' }}>
                      <div style={{ color: '#666', fontSize: '0.64rem', textTransform: 'uppercase', letterSpacing: '0.04em', marginBottom: '0.2rem' }}>{key.replace(/_/g, ' ')}</div>
                      <div style={{ color: '#fff', fontSize: '1.05rem', fontWeight: 750 }}>{value}</div>
                    </div>
                  ))}
                </div>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
