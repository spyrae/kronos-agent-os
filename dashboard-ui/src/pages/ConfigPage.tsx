import { useEffect, useState } from 'react';
import { api } from '../api/client';
import { SectionHeader } from '../components/Charts';

interface EnvVar { key: string; value: string; is_secret: boolean; }
interface LlmConfig { tiers: Record<string, { model: string; temperature?: number; max_tokens?: number }>; routing: { complex_patterns_count: number; simple_patterns_count: number } }

const TIER_COLORS: Record<string, string> = {
  standard: '#3b82f6', lite: '#4ade80', fallback: '#f59e0b', vision: '#8b5cf6',
};

export default function ConfigPage() {
  const [vars, setVars] = useState<EnvVar[]>([]);
  const [llm, setLlm] = useState<LlmConfig | null>(null);
  const [editing, setEditing] = useState<string | null>(null);
  const [editValue, setEditValue] = useState('');
  const [toast, setToast] = useState('');
  const [error, setError] = useState('');

  const showToast = (msg: string) => { setToast(msg); setTimeout(() => setToast(''), 3000); };
  const showError = (msg: string) => { setError(msg); setTimeout(() => setError(''), 5000); };

  const load = async () => {
    try {
      const [envR, llmR] = await Promise.all([
        api<{ vars: EnvVar[] }>('/api/config/env'),
        api<LlmConfig>('/api/config/llm'),
      ]);
      setVars(envR.vars);
      setLlm(llmR);
    } catch (e: any) { showError(e.message); }
  };
  useEffect(() => { load(); }, []);

  const startEdit = (key: string) => { setEditing(key); setEditValue(''); };

  const saveVar = async () => {
    if (!editing || !editValue) return;
    try {
      await api(`/api/config/env/${editing}`, { method: 'PUT', body: JSON.stringify({ value: editValue }) });
      showToast(`${editing} updated. Restart required.`);
      setEditing(null);
      setEditValue('');
      load();
    } catch (e: any) { showError(e.message); }
  };

  const card: React.CSSProperties = {
    background: '#111', border: '1px solid #1a1a1a', borderRadius: '12px', padding: '1.25rem',
  };

  return (
    <div>
      {/* Toast / Error */}
      {toast && <div style={{ position: 'fixed', top: '1rem', right: '1rem', padding: '0.75rem 1.5rem', borderRadius: 8, background: '#166534', color: '#fff', fontSize: '0.85rem', zIndex: 100, boxShadow: '0 4px 20px rgba(0,0,0,0.4)', border: '1px solid #4ade8033' }}>{toast}</div>}
      {error && <div style={{ position: 'fixed', top: '1rem', right: '1rem', padding: '0.75rem 1.5rem', borderRadius: 8, background: '#991b1b', color: '#fff', fontSize: '0.85rem', zIndex: 100, boxShadow: '0 4px 20px rgba(0,0,0,0.4)', border: '1px solid #ef444433' }}>{error}</div>}

      <h1 style={{ fontSize: '1.5rem', fontWeight: 600, marginBottom: '1.5rem' }}>Settings</h1>

      {/* LLM Models */}
      {llm && (
        <div style={{ marginBottom: '1.5rem' }}>
          <SectionHeader title="LLM Models" />
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(260px, 1fr))', gap: '1rem' }}>
            {Object.entries(llm.tiers).map(([tier, config]) => (
              <div key={tier} style={{
                ...card,
                borderLeft: `3px solid ${TIER_COLORS[tier] || '#666'}`,
              }}>
                <div style={{
                  fontSize: '0.65rem', fontWeight: 600, color: '#555', textTransform: 'uppercase',
                  letterSpacing: '0.06em', marginBottom: '0.4rem',
                }}>{tier}</div>
                <div style={{ fontSize: '1rem', fontWeight: 600, color: TIER_COLORS[tier] || '#fff', marginBottom: '0.5rem' }}>{config.model}</div>
                <div style={{ display: 'flex', gap: '1rem', fontSize: '0.75rem', color: '#666' }}>
                  {config.temperature !== null && config.temperature !== undefined && (
                    <span>Temp: <span style={{ color: '#999' }}>{config.temperature}</span></span>
                  )}
                  {config.max_tokens !== null && config.max_tokens !== undefined && (
                    <span>Max: <span style={{ color: '#999' }}>{config.max_tokens?.toLocaleString()}</span></span>
                  )}
                </div>
              </div>
            ))}
          </div>
          <div style={{
            marginTop: '0.75rem', fontSize: '0.72rem', color: '#444',
            display: 'flex', gap: '1rem',
          }}>
            <span>Routing: {llm.routing.complex_patterns_count} complex patterns</span>
            <span>{llm.routing.simple_patterns_count} simple patterns</span>
          </div>
        </div>
      )}

      {/* Env Vars */}
      <SectionHeader title="Environment Variables" />
      <div style={{ ...card, padding: 0, overflow: 'hidden' }}>
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.82rem' }}>
          <thead>
            <tr style={{ borderBottom: '1px solid #1a1a1a' }}>
              {['Key', 'Value', ''].map(h => (
                <th key={h} style={{
                  padding: '0.65rem 1rem', textAlign: 'left', color: '#555',
                  fontWeight: 500, fontSize: '0.72rem', textTransform: 'uppercase', letterSpacing: '0.04em',
                }}>{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {vars.map(v => (
              <tr key={v.key} style={{ borderBottom: '1px solid #111' }}>
                <td style={{ padding: '0.55rem 1rem', fontFamily: 'monospace', color: '#e0e0e0', fontSize: '0.8rem' }}>{v.key}</td>
                <td style={{ padding: '0.55rem 1rem', fontFamily: 'monospace', color: v.is_secret ? '#444' : '#4ade80', fontSize: '0.8rem' }}>
                  {editing === v.key ? (
                    <div style={{ display: 'flex', gap: '0.3rem' }}>
                      <input
                        value={editValue} onChange={e => setEditValue(e.target.value)}
                        onKeyDown={e => { if (e.key === 'Enter') saveVar(); if (e.key === 'Escape') setEditing(null); }}
                        placeholder="New value" autoFocus
                        style={{
                          padding: '0.35rem 0.6rem', borderRadius: 6, border: '1px solid #333',
                          background: '#0a0a0a', color: '#fff', flex: 1, fontFamily: 'monospace', fontSize: '0.8rem', outline: 'none',
                        }}
                      />
                      <button onClick={saveVar} style={{
                        padding: '0.35rem 0.75rem', borderRadius: 6, border: 'none',
                        background: '#f97316', color: '#fff', cursor: 'pointer', fontSize: '0.75rem', fontWeight: 600,
                      }}>Save</button>
                      <button onClick={() => setEditing(null)} style={{
                        padding: '0.35rem 0.5rem', borderRadius: 6, border: '1px solid #222',
                        background: 'transparent', color: '#888', cursor: 'pointer', fontSize: '0.75rem',
                      }}>{'\u2715'}</button>
                    </div>
                  ) : v.value}
                </td>
                <td style={{ padding: '0.55rem 1rem', textAlign: 'right' }}>
                  {editing !== v.key && (
                    <button onClick={() => startEdit(v.key)} style={{
                      padding: '0.2rem 0.6rem', borderRadius: 4, border: '1px solid #222',
                      cursor: 'pointer', fontSize: '0.72rem', background: 'transparent', color: '#888',
                    }}>Edit</button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div style={{ marginTop: '0.5rem', fontSize: '0.72rem', color: '#444' }}>
        Changes require service restart to take effect.
      </div>
    </div>
  );
}
